"""Hard cases from the brief: lookalike names, duplicates, adverse-term false positives, the
GST registry, and resuming after a long pause. Mock data is for tests only."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vendorcheck import names
from vendorcheck.batch import grade, run_batch
from vendorcheck.pipeline import Pipeline, new_job
from vendorcheck.report import build_result

from test_offline import Base, Fake, company

VALID_GSTIN = "27AAPFU0939F1ZV"       # a well-known valid sample GSTIN (checksum ok)
COMPANY_GSTIN = "27AAACA1234A1Z" + "?"  # filled in setUpModule with the right check character


def with_check_char(first14: str) -> str:
    for c in names._GST_CHARS:
        if names.gstin_checksum_ok(first14 + c):
            return first14 + c
    raise ValueError(first14)


def setUpModule():
    global COMPANY_GSTIN
    COMPANY_GSTIN = with_check_char("27AAACA1234A1Z")


class Fake2(Fake):
    """Adds: Identify that only knows the real domain, website pages, and a GST registry."""

    def __init__(self, comp, known_domains=None, gst=None, **kw):
        super().__init__(comp, **kw)
        self.known_domains = known_domains
        self.gst = gst or {}

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body) if body else {}
        if "cashfree" in url:
            self.count["/gstin"] = self.count.get("/gstin", 0) + 1
            rec = self.gst.get(payload["GSTIN"])
            if rec is None:
                return 200, {}, json.dumps({"GSTIN": payload["GSTIN"], "valid": False, "message": "not found"})
            return 200, {}, json.dumps({"GSTIN": payload["GSTIN"], "valid": True, **rec})
        if url.endswith("/company/identify") and self.known_domains is not None:
            self.count["/company/identify"] = self.count.get("/company/identify", 0) + 1
            asked = (payload.get("domains") or [None])[0]
            if asked is not None and asked not in self.known_domains:
                return 200, {"x-credits-used": "0"}, json.dumps([{"matches": []}])
            m = {"confidence_score": 0.95, "company_data": {
                "crustdata_company_id": self.comp["crustdata_company_id"], "basic_info": self.comp["basic_info"]}}
            return 200, {"x-credits-used": "0"}, json.dumps([{"matches": [m]}])
        return super().__call__(method, url, headers, body, timeout)


class TestLookalike(Base):
    def test_lookalike_website_is_not_cleared_with_real_firms_records(self):
        fake = Fake2(company(), known_domains={"acmefreight.in"}, reg_date="2026-10-01T00:00:00Z")
        job, r = self.run_job(fake, {"name": "Acme Freight Pvt Ltd", "website": "acmefreight-india.com"})
        self.assertIn(r["verdict"], ("HOLD_FOR_REVIEW", "FRONT_SUSPECTED"), r)
        self.assertTrue(any("Lookalike" in s for s in r["looks_off"]["strong"]))
        self.assertNotIn("/company/enrich", fake.count)      # the real firm's evidence is never borrowed
        self.assertFalse(r["who_runs_it"])

    def test_name_only_partial_match_cannot_verify(self):
        fake = Fake2(company(), known_domains={"acmefreight.in"})
        job, r = self.run_job(fake, {"name": "Acme Global Freight Logistics Solutions"})
        self.assertEqual(r["verdict"], "CANNOT_VERIFY", r)

    def test_name_only_exact_match_resolves(self):
        fake = Fake2(company(), known_domains={"acmefreight.in"})
        job, r = self.run_job(fake, {"name": "Acme Freight Private Limited"})
        self.assertEqual(r["verdict"], "REAL_AND_TRADING", r)


class TestAdverseMatching(Base):
    def test_default_and_far_away_terms_are_not_adverse(self):
        news = [{"subject": "Acme Freight", "title": "Acme Freight Private Limited adds 40 trucks",
                 "url": "https://news.example/a", "snippet": "Tracking is on by default. Separately, police "
                                                             "arrested a man in Delhi."}]
        fake = Fake(company(), news=news)
        job, r = self.run_job(fake)
        self.assertEqual(r["verdict"], "REAL_AND_TRADING", r)
        self.assertFalse(r["looks_off"]["strong"])

    def test_vendor_as_victim_is_not_adverse(self):
        news = [{"subject": "Acme Freight",
                 "title": "Job fraud: gang posing as Acme Freight Private Limited arrested",
                 "url": "https://news.example/b", "snippet": "Police arrested two men for fraud."}]
        job, r = self.run_job(Fake(company(), news=news))
        self.assertEqual(r["verdict"], "REAL_AND_TRADING", r)
        self.assertTrue(r["namesakes_cleared"])

    def test_company_accused_is_held(self):
        news = [{"subject": "Acme Freight",
                 "title": "GST officers raid Acme Freight Private Limited over fake invoice racket",
                 "url": "https://news.example/c", "snippet": "Acme Freight Private Limited accused of fraud."}]
        pages = {"https://news.example/c": "<p>GST officers raid Acme Freight Private Limited over a fake "
                                           "invoice racket worth 40 crore, on 14 October 2026.</p>"}
        job, r = self.run_job(Fake(company(), news=news, pages=pages))
        self.assertEqual(r["verdict"], "HOLD_FOR_REVIEW", r)


class TestRegistry(Base):
    def site(self, gstin):
        return {"https://acmefreight.in": f"<footer>Acme Freight Private Limited · GSTIN: {gstin}</footer>"}

    def test_gstin_checksum(self):
        self.assertTrue(names.gstin_checksum_ok(VALID_GSTIN))
        self.assertFalse(names.gstin_checksum_ok(VALID_GSTIN[:-1] + "A"))
        self.assertEqual(names.find_gstins(f"GST No. {VALID_GSTIN}, again {VALID_GSTIN}"), [VALID_GSTIN])

    def test_fake_gstin_on_website_is_strong_signal(self):
        bad = COMPANY_GSTIN[:-1] + ("A" if COMPANY_GSTIN[-1] != "A" else "B")
        job, r = self.run_job(Fake(company(), pages=self.site(bad)))
        self.assertTrue(any("Invalid GSTIN" in s for s in r["looks_off"]["strong"]), r["looks_off"])
        self.assertEqual(r["verdict"], "HOLD_FOR_REVIEW")
        self.assertTrue(any(f["claim"].startswith("GSTIN shown") for f in r["facts"]))

    def test_registry_not_configured_is_cannot_verify_not_pass(self):
        with mock.patch.dict(os.environ, {"CASHFREE_CLIENT_ID": "", "CASHFREE_CLIENT_SECRET": ""}):
            job, r = self.run_job(Fake(company(), pages=self.site(COMPANY_GSTIN)))
        self.assertTrue(any("registry not checked" in c for c in r["cannot_verify"]))
        self.assertEqual(r["verdict"], "REAL_AND_TRADING")

    def test_cancelled_gst_registration(self):
        gst = {COMPANY_GSTIN: {"gst_in_status": "Cancelled", "legal_name_of_business": "ACME FREIGHT PRIVATE LIMITED",
                               "trade_name_of_business": "ACME FREIGHT", "date_of_registration": "01/07/2017",
                               "constitution_of_business": "Private Limited Company"}}
        with mock.patch.dict(os.environ, {"CASHFREE_CLIENT_ID": "x", "CASHFREE_CLIENT_SECRET": "y"}):
            job, r = self.run_job(Fake2(company(), gst=gst, pages=self.site(COMPANY_GSTIN)))
        self.assertTrue(any("Cancelled" in s for s in r["looks_off"]["strong"]), r["looks_off"])
        self.assertNotEqual(r["verdict"], "REAL_AND_TRADING")
        dated = [f for f in r["facts"] if f["claim"].startswith("GST registration status")]
        self.assertEqual(dated[0]["record_date"], "01/07/2017")

    def test_active_gst_registration_is_positive(self):
        gst = {COMPANY_GSTIN: {"gst_in_status": "Active", "legal_name_of_business": "ACME FREIGHT PRIVATE LIMITED",
                               "trade_name_of_business": "ACME FREIGHT", "date_of_registration": "01/07/2017"}}
        with mock.patch.dict(os.environ, {"CASHFREE_CLIENT_ID": "x", "CASHFREE_CLIENT_SECRET": "y"}):
            job, r = self.run_job(Fake2(company(), gst=gst, pages=self.site(COMPANY_GSTIN)))
        self.assertEqual(r["verdict"], "REAL_AND_TRADING", r)
        self.assertTrue(any("GST registration" in p and "active" in p for p in r["is_trading"]))


class TestRestartAndBatch(Base):
    def test_resume_after_long_pause_still_calls_sources(self):
        fake = Fake(company(), kill_on="/company/enrich")
        job = new_job(self.store, {"name": "Acme Freight", "website": "acmefreight.in"})
        with self.assertRaises(Exception):
            Pipeline(self.store, self.cfg, transport=fake, sleep=lambda s: None, log=lambda m: None).run(job)
        self.store.update_job(job, started_at=time.time() - 3600)      # an hour later
        Pipeline(self.store, self.cfg, transport=fake, sleep=lambda s: None, log=lambda m: None).run(job)
        r = build_result(self.store, self.cfg, job)
        self.assertEqual(r["verdict"], "REAL_AND_TRADING", r["cannot_verify"])

    def test_batch_rerun_is_idempotent_and_flags_in_batch_duplicate(self):
        rows = [{"name": "Acme Freight Pvt Ltd", "website": "acmefreight.in", "expected": "REAL_AND_TRADING"},
                {"name": "ACME FREIGHT", "website": "https://www.acmefreight.in/", "expected": "HOLD_FOR_REVIEW"}]
        path = Path(self.tmp.name) / "cases.json"
        path.write_text(json.dumps(rows))
        fake = Fake(company())
        pipe = Pipeline(self.store, self.cfg, transport=fake, sleep=lambda s: None, log=lambda m: None)
        with mock.patch("vendorcheck.config.ROOT", Path(self.tmp.name)):
            first = run_batch(self.store, self.cfg, pipe, str(path), log=lambda m: None)
            calls = dict(fake.count)
            second = run_batch(self.store, self.cfg, pipe, str(path), log=lambda m: None)
        self.assertEqual([r["verdict"] for _, r in first], ["REAL_AND_TRADING", "HOLD_FOR_REVIEW"])
        self.assertEqual([r["verdict"] for _, r in second], ["REAL_AND_TRADING", "HOLD_FOR_REVIEW"])
        self.assertEqual(calls, fake.count)                     # the re-run paid for nothing
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"], 2)
        self.assertEqual([grade(row["expected"], r["verdict"]) for row, r in first], ["PASS", "PASS"])

    def test_grade_prefers_cannot_verify_to_wrong(self):
        self.assertEqual(grade("FRONT_SUSPECTED", "CANNOT_VERIFY"), "SAFE_MISS")
        self.assertEqual(grade("FRONT_SUSPECTED", "REAL_AND_TRADING"), "FAIL")
        self.assertEqual(grade("REAL_AND_TRADING|CANNOT_VERIFY", "CANNOT_VERIFY"), "PASS")


if __name__ == "__main__":
    unittest.main()


class TestRealIdentifyByName(Base):
    """Real Identify-by-name answer for "Delhivery Limited" (slimmed, fetched 22 Nov 2026): every match,
    including Reliance and ITC, comes back at confidence 0.6."""

    def padded(self, comp):
        real = json.loads((Path(__file__).parent / "fixtures" / "identify_by_name_delhivery.json").read_text())
        inner = Fake(comp)

        def transport(method, url, headers, body, timeout):
            payload = json.loads(body) if body else {}
            if url.endswith("/company/identify"):
                if payload.get("domains"):
                    return 200, {"x-credits-used": "0"}, json.dumps([{"matches": []}])
                return 200, {"x-credits-used": "0"}, json.dumps(real)
            return inner(method, url, headers, body, timeout)
        return transport, inner

    def delhivery(self):
        enrich = json.loads((Path(__file__).parent / "fixtures" / "delhivery_enrich.json").read_text())
        return enrich[0]["matches"][0]["company_data"]

    def test_lookalike_domain_with_real_padded_answer(self):
        transport, inner = self.padded(self.delhivery())
        job, r = self.run_job(transport, {"name": "Delhivery Limited", "website": "delhivery-logistics-india.com"})
        self.assertTrue(any("Lookalike" in s and "delhivery.com" in s for s in r["looks_off"]["strong"]), r)
        self.assertNotIn("/company/enrich", inner.count)

    def test_unknown_vendor_is_not_accused(self):
        transport, inner = self.padded(self.delhivery())
        job, r = self.run_job(transport, {"name": "Sharma Trading Co", "website": "sharma-trading-co-2026.in"})
        self.assertEqual(r["verdict"], "CANNOT_VERIFY", r["looks_off"])
        self.assertFalse(r["looks_off"]["strong"])

    def test_name_only_picks_the_exact_name_not_the_first_row(self):
        transport, inner = self.padded(self.delhivery())
        job, r = self.run_job(transport, {"name": "Delhivery courier service"})
        ident = self.store.stage_output(job, "identify")
        self.assertEqual(ident["top"]["name"], "Delhivery courier service")
        self.assertEqual(ident["top"]["id"], 1236803)          # not Delhivery (5798), the first row
