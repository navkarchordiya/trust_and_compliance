"""Hard cases: lookalike names, duplicates, adverse-term false positives, the
GST registry, and resuming after a long pause. Mock data is for tests only."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vendorcheck import names
from vendorcheck.actions import decide_approval
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

    def test_name_only_exact_match_is_never_auto_approved(self):
        fake = Fake2(company(), known_domains={"acmefreight.in"})
        job, r = self.run_job(fake, {"name": "Acme Freight Private Limited"})
        self.assertEqual(r["verdict"], "HOLD_FOR_REVIEW", r)
        self.assertTrue(any("confirm the vendor's website" in x for x in r["looks_off"]["needs_review"]))


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
    def setUp(self):
        super().setUp()
        # These tests exercise the registry logic against a fake provider, so they switch the
        # provider on themselves; the shipped config keeps it off until there are production keys.
        self.cfg["registry"]["provider"] = "cashfree"

    def site(self, gstin):
        return {"https://www.acmefreight.in": f"<footer>Acme Freight Private Limited · GSTIN: {gstin}</footer>"}

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
        self.assertEqual(grade("!REAL_AND_TRADING", "CANNOT_VERIFY"), "PASS")
        self.assertEqual(grade("!REAL_AND_TRADING", "REAL_AND_TRADING"), "FAIL")


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


class TestClientRegisterAndEthics(Base):
    def test_vendor_already_on_client_list_is_duplicate_even_by_name_only(self):
        from vendorcheck.batch import import_vendors
        path = Path(self.tmp.name) / "vendors.csv"
        path.write_text("name,website,gstin\nAcme Freight Pvt Ltd,acmefreight.in,\n")
        import_vendors(self.store, str(path))
        fake = Fake(company(), timeout_on={"/company/identify"})      # even with the source down
        job, r = self.run_job(fake, {"name": "ACME FREIGHT PRIVATE LIMITED"})
        self.assertEqual(r["verdict"], "HOLD_FOR_REVIEW", r["looks_off"])
        self.assertTrue(any("client's vendor list" in w for w in r["looks_off"]["weak"]))

    def test_duplicate_by_gstin(self):
        from vendorcheck.batch import import_vendors
        path = Path(self.tmp.name) / "vendors.json"
        path.write_text(json.dumps([{"name": "Old Name Logistics", "gstin": "27AAPFU0939F1ZV"}]))
        import_vendors(self.store, str(path))
        job, r = self.run_job(Fake(company()), {"name": "Acme Freight", "website": "acmefreight.in",
                                                 "gstin": "27aapfu0939f1zv"})
        self.assertTrue(any("gstin:27AAPFU0939F1ZV" in w for w in r["looks_off"]["weak"]), r["looks_off"])

    def test_override_needs_a_reason_and_is_recorded(self):
        job, r = self.run_job(Fake(company()))
        refused = decide_approval(self.store, job, "tester", action="reject_vendor")
        self.assertIn("give a reason", refused)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) c FROM actions").fetchone()["c"], 0)
        done = decide_approval(self.store, job, "tester", action="reject_vendor", reason="site visit failed")
        self.assertIn("done", done)
        row = self.store.db.execute("SELECT override_reason FROM approvals WHERE job_id=?", (job,)).fetchone()
        self.assertEqual(row["override_reason"], "site visit failed")

    def test_event_log_and_purge(self):
        job, r = self.run_job(Fake(company()))
        events = [json.loads(l)["event"] for l in (self.store.log_dir / f"{job}.jsonl").read_text().splitlines()]
        self.assertIn("run_start", events)
        self.assertIn("call", events)
        decide_approval(self.store, job, "tester")
        self.store.db.execute("UPDATE jobs SET updated_at=? WHERE id=?", (time.time() - 40 * 86400, job))
        self.assertGreater(self.store.purge_raw_responses(30), 0)
        self.assertTrue(build_result(self.store, self.cfg, job)["facts"])      # evidence survives

    def test_cost_summary(self):
        from vendorcheck.report import cost_summary
        self.run_job(Fake(company()))
        self.assertIn("credits per job", cost_summary(self.store, self.cfg))


class TestRescreen(Base):
    def test_rescreen_is_cheap_and_alerts_once_on_new_adverse_news(self):
        from vendorcheck.batch import rescreen
        first = Fake(company())
        job, r = self.run_job(first)
        decide_approval(self.store, job, "tester")

        # A month later the founder is in the news, linked to the vendor.
        news = [{"subject": "Ravi Menon", "title": "Acme Freight director Ravi Menon arrested in GST fraud",
                 "url": "https://news.example/later", "snippet": "Ravi Menon of Acme Freight arrested."}]
        pages = {"https://news.example/later": "<p>Ravi Menon, director of Acme Freight Private Limited, "
                                               "was arrested for fraud.</p>"}
        later = Fake(company(), news=news, pages=pages)
        pipe = Pipeline(self.store, self.cfg, transport=later, sleep=lambda s: None, log=lambda m: None)
        rows = rescreen(self.store, self.cfg, pipe, log=lambda m: None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["now"], "HOLD_FOR_REVIEW")
        self.assertNotIn("/company/enrich", later.count)            # profile carried over, not paid again
        self.assertNotIn("/company/identify", later.count)
        self.assertLess(rows[0]["credits"], r["cost"]["crustdata_credits"])

        again = rescreen(self.store, self.cfg, pipe, log=lambda m: None)   # same day: resumes, no repeat
        self.assertEqual(again[0]["job"], rows[0]["job"])
        alerts = self.store.db.execute("SELECT COUNT(*) c FROM outbox WHERE body LIKE 'Monitoring alert%'").fetchone()
        self.assertEqual(alerts["c"], 1)
        facts = build_result(self.store, self.cfg, rows[0]["job"])["facts"]
        self.assertTrue(any("(from the original check)" in f["claim"] for f in facts))

    def test_rescreen_no_change_closes_quietly(self):
        from vendorcheck.batch import rescreen
        job, _ = self.run_job(Fake(company()))
        decide_approval(self.store, job, "tester")
        pipe = Pipeline(self.store, self.cfg, transport=Fake(company()), sleep=lambda s: None, log=lambda m: None)
        rows = rescreen(self.store, self.cfg, pipe, log=lambda m: None)
        self.assertEqual(rows[0]["now"], "REAL_AND_TRADING")
        self.assertEqual(self.store.job(rows[0]["job"])["status"], "closed")


class TestRequirementChangeReadiness(Base):
    def test_typo_in_request_gstin_is_a_question_not_an_accusation(self):
        bad = VALID_GSTIN[:-1] + "A"
        job, r = self.run_job(Fake(company()), {"name": "Acme Freight Pvt Ltd", "website": "acmefreight.in",
                                                 "gstin": bad})
        self.assertFalse(any("GSTIN" in s for s in r["looks_off"]["strong"]), r["looks_off"])
        self.assertTrue(any("ask the vendor to confirm" in s for s in r["looks_off"]["needs_review"]))

    def test_lower_cost_limit_keeps_the_news_check(self):
        self.cfg["limits"]["max_credits_per_run"] = 3.2       # identify 0.2 + enrich 2 + news 1
        news = [{"subject": "Acme Freight", "title": "Raid on Acme Freight over fake invoices",
                 "url": "https://news.example/r", "snippet": "Officers raid Acme Freight."}]
        fake = Fake(company(), news=news)
        job, r = self.run_job(fake)
        self.assertEqual(fake.count.get("/web/search/live"), 1)       # the news check still ran
        self.assertTrue(r["looks_off"]["adverse_news"])
        self.assertTrue(any("keep the run's credit budget" in c for c in r["cannot_verify"]))
        self.assertLessEqual(r["cost"]["crustdata_credits"], 3.2)

    def test_new_output_field_is_a_config_edit(self):
        self.cfg["output"]["fields"] = [f for f in self.cfg["output"]["fields"] if f != "registry"]
        job, r = self.run_job(Fake(company()))
        self.assertNotIn("registry", r)


class TestRealIdentifySharma(Base):
    """Real Identify-by-name answer for "Sharma Trading Co" (trimmed, fetched 22 Nov 2026): four different
    'Sharma ... Trading' firms. An unknown vendor with that name must not be called a lookalike of any of them."""

    def test_loose_name_matches_are_cannot_verify_not_lookalike(self):
        real = json.loads((Path(__file__).parent / "fixtures" / "identify_by_name_sharma.json").read_text())
        inner = Fake(company())

        def transport(method, url, headers, body, timeout):
            payload = json.loads(body) if body else {}
            if url.endswith("/company/identify"):
                if payload.get("domains"):
                    return 200, {}, json.dumps([{"matches": []}])
                return 200, {}, json.dumps(real)
            return inner(method, url, headers, body, timeout)
        job, r = self.run_job(transport, {"name": "Sharma Trading Co", "website": "sharma-trading-co-2026.in"})
        self.assertEqual(r["verdict"], "CANNOT_VERIFY", r["looks_off"])
        self.assertFalse(r["looks_off"]["strong"])
        self.assertNotIn("/company/enrich", inner.count)


class TestApproverList(Base):
    def test_only_listed_approvers_can_act(self):
        job, r = self.run_job(Fake(company()))
        refused = decide_approval(self.store, job, "Random Person", approvers=["Asha Rao"])
        self.assertIn("not on the approver list", refused)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) c FROM actions").fetchone()["c"], 0)
        self.assertIn("done", decide_approval(self.store, job, "asha rao", approvers=["Asha Rao"]))
