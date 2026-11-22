"""Offline tests: a fake transport stands in for Crustdata and RDAP (mock data is for tests only).

Run:  python -m unittest discover -s tests -v
"""
import json
import tempfile
import unittest
from pathlib import Path

from vendorcheck.actions import decide_approval
from vendorcheck.config import load_config
from vendorcheck.pipeline import Pipeline, new_job
from vendorcheck.report import build_result
from vendorcheck.store import Store


class Killed(Exception):
    """Stands in for the process being killed mid-call."""


FIXTURES = Path(__file__).parent / "fixtures"


def person(name, title, company_id, employer, website, url, start="2012-01-01T00:00:00.000Z", connections=900):
    """Same shape as people.founders[] / people.cxos[] in a real Company Enrich response."""
    return {"basic_profile": {"name": name, "current_title": title, "location": {"raw": "Pune, India"}},
            "professional_network": {"connections": connections},
            "social_handles": {"professional_network_identifier": {"profile_url": url}},
            "experience": {"employment_details": {"current": [
                {"employer_name": employer, "employer_company_id": [str(company_id)],
                 "employer_website": [website], "employee_title": title,
                 "employee_start_date": start, "employee_end_date": None}]}}}


def company(cid=101, name="Acme Freight Private Limited", domain="acmefreight.in", year=2012, headcount=140,
            founder="Ravi Menon", founder_url="https://www.linkedin.com/in/ravi-menon-acme", status="active",
            founder_employer_id=None):
    return {
        "crustdata_company_id": cid,
        "basic_info": {"name": name, "primary_domain": domain, "website": f"https://{domain}",
                       "year_founded": year, "employee_count_range": "51-200", "status": status},
        "headcount": {"total": headcount, "growth_percent": {"yoy": 12.5}},
        "locations": {"headquarters": "Pune, Maharashtra, India", "country": "IND"},
        "hiring": {"openings_count": 6},
        "people": {"founders": [person(founder, "Founder & CEO",
                                       founder_employer_id if founder_employer_id is not None else cid,
                                       name, domain, founder_url)],
                   "cxos": []},
        "news": [{"article_title": f"{name} opens new warehouse", "article_url": "https://news.example/acme",
                  "article_publish_date": "2026-10-02"}],
    }


class Fake:
    """Routes each request to a canned response. Counts calls per endpoint."""

    def __init__(self, comp, reg_date="2011-05-04T00:00:00Z", person_company_id=None, news=None, pages=None,
                 timeout_on=(), kill_on=None, identify_matches=None):
        self.comp, self.reg_date, self.news, self.pages = comp, reg_date, news or [], pages or {}
        self.person_company_id = person_company_id if person_company_id is not None else comp["crustdata_company_id"]
        self.timeout_on, self.kill_on = set(timeout_on), kill_on
        self.identify_matches = identify_matches
        self.count = {}

    def __call__(self, method, url, headers, body, timeout):
        ep = url.split("api.crustdata.com")[-1] if "crustdata" in url else "rdap"
        self.count[ep] = self.count.get(ep, 0) + 1
        if ep == self.kill_on:
            self.kill_on = None          # only the first time
            raise Killed(ep)
        if ep in self.timeout_on:
            raise TimeoutError("simulated slow source")
        payload = json.loads(body) if body else {}
        if ep == "/company/identify":
            matches = self.identify_matches or [{"confidence_score": 1.0, "company_data": {
                "crustdata_company_id": self.comp["crustdata_company_id"], "basic_info": self.comp["basic_info"]}}]
            return 200, {"x-credits-used": "0.2"}, json.dumps([{"matched_on": "x", "match_type": "domain",
                                                                 "matches": matches}])
        if ep == "/company/enrich":
            return 200, {"x-credits-used": "2"}, json.dumps([{"matches": [
                {"confidence_score": 1.0, "company_data": self.comp}]}])
        if ep == "/person/enrich":
            return 200, {"x-credits-used": "1"}, json.dumps([{"matches": [{"confidence_score": 1.0, "person_data": {
                "basic_profile": {"name": "Ravi Menon"},
                "experience": {"current": [{"name": self.comp["basic_info"]["name"], "title": "Founder & CEO",
                                            "crustdata_company_id": self.person_company_id}]}}}]}])
        if ep == "/web/search/live":
            res = [n for n in self.news if f'"{n["subject"]}"' in payload["query"]]
            return 200, {"x-credits-used": "1"}, json.dumps({"success": True, "results": res})
        if ep == "/web/enrich/live":
            u = payload["urls"][0]
            return 200, {"x-credits-used": "1"}, json.dumps([{"success": True, "url": u, "timestamp": 1,
                                                              "title": "t", "content": self.pages.get(u, "")}])
        if ep == "rdap":
            return 200, {}, json.dumps({"events": [{"eventAction": "registration", "eventDate": self.reg_date}]})
        return 404, {}, "{}"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.cfg = load_config()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def run_job(self, fake, request=None):
        job = new_job(self.store, request or {"name": "Acme Freight Pvt Ltd", "website": "https://acmefreight.in"})
        Pipeline(self.store, self.cfg, transport=fake, sleep=lambda s: None, log=lambda m: None).run(job)
        return job, build_result(self.store, self.cfg, job)


class TestVerdicts(Base):
    def test_honest_firm_is_real_and_trading(self):
        job, r = self.run_job(Fake(company()))
        self.assertEqual(r["verdict"], "REAL_AND_TRADING", r)
        self.assertTrue(r["facts"])
        for f in r["facts"]:
            self.assertTrue(f["source"] and f["retrieved_at"] and f["quote"])
        self.assertLessEqual(r["cost"]["crustdata_credits"], self.cfg["limits"]["max_credits_per_run"])

    def test_timeout_is_cannot_verify_not_front(self):
        job, r = self.run_job(Fake(company(), timeout_on={"/company/identify"}))
        self.assertEqual(r["verdict"], "CANNOT_VERIFY")
        self.assertTrue(any("did not answer" in c for c in r["cannot_verify"]))
        calls = self.store.ledger(job)
        self.assertEqual(calls[0]["status"], "UNAVAILABLE")
        self.assertEqual(calls[0]["attempts"], 1 + self.cfg["limits"]["max_retries"])

    def test_front_name_and_age_mismatch(self):
        comp = company(name="Bluedart Express Logistics", domain="acmefreight.in", year=1998, headcount=1,
                       founder_employer_id=999)
        fake = Fake(comp, reg_date="2026-09-20T00:00:00Z", person_company_id=999)
        job, r = self.run_job(fake)
        self.assertEqual(r["verdict"], "FRONT_SUSPECTED", r["looks_off"])
        self.assertEqual(r["recommended_action"], "reject_vendor")
        self.assertEqual(r["approval"]["status"], "pending")      # rejecting waits for a person too

    def test_namesake_is_cleared(self):
        news = [{"subject": "Ravi Menon", "title": "Ravi Menon arrested in jewellery fraud",
                 "url": "https://news.example/jeweller", "snippet": "Ravi Menon, a Jaipur jeweller, arrested for fraud"}]
        pages = {"https://news.example/jeweller":
                 "<html><body><p>Police said Ravi Menon, who runs a jewellery shop in Jaipur, was arrested "
                 "for fraud on 3 October 2026.</p></body></html>"}
        job, r = self.run_job(Fake(company(), news=news, pages=pages))
        self.assertEqual(r["verdict"], "REAL_AND_TRADING", r)
        self.assertTrue(r["namesakes_cleared"])

    def test_adverse_news_linked_holds(self):
        news = [{"subject": "Ravi Menon", "title": "Acme Freight director Ravi Menon arrested in GST fraud",
                 "url": "https://news.example/acme-raid", "snippet": "Ravi Menon of Acme Freight arrested"}]
        pages = {"https://news.example/acme-raid":
                 "<p>Ravi Menon, director of Acme Freight Private Limited, was arrested in a fake invoice "
                 "fraud on 12 October 2026.</p>"}
        job, r = self.run_job(Fake(company(), news=news, pages=pages))
        self.assertEqual(r["verdict"], "HOLD_FOR_REVIEW")
        self.assertTrue(any(f["field_or_passage"] == "passage" for f in r["facts"]))

    def test_unknown_company_cannot_verify(self):
        fake = Fake(company(), identify_matches=[])
        fake.identify_matches = None

        def no_match(method, url, headers, body, timeout):
            if url.endswith("/company/identify"):
                return 200, {}, json.dumps([{"matched_on": "x", "matches": []}])
            return fake(method, url, headers, body, timeout)
        job, r = self.run_job(no_match)
        self.assertEqual(r["verdict"], "CANNOT_VERIFY")


class TestRobustness(Base):
    def test_kill_and_resume_does_not_repeat_calls(self):
        fake = Fake(company(), kill_on="/web/search/live")
        job = new_job(self.store, {"name": "Acme Freight", "website": "acmefreight.in"})
        pipe = Pipeline(self.store, self.cfg, transport=fake, sleep=lambda s: None, log=lambda m: None)
        with self.assertRaises(Killed):
            pipe.run(job)
        self.assertEqual(self.store.job(job)["stage"], "media")
        Pipeline(self.store, self.cfg, transport=fake, sleep=lambda s: None, log=lambda m: None).run(job)
        self.assertEqual(fake.count["/company/identify"], 1)      # not paid twice
        self.assertEqual(fake.count["/company/enrich"], 1)
        self.assertEqual(build_result(self.store, self.cfg, job)["verdict"], "REAL_AND_TRADING")

    def test_approval_happens_exactly_once(self):
        job, r = self.run_job(Fake(company()))
        first = decide_approval(self.store, job, "tester")
        second = decide_approval(self.store, job, "tester")
        self.assertIn("done", first)
        self.assertIn("already", second)
        n = self.store.db.execute("SELECT COUNT(*) c FROM actions WHERE job_id=?", (job,)).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_duplicate_vendor_is_held(self):
        job1, _ = self.run_job(Fake(company()))
        decide_approval(self.store, job1, "tester")
        job2, r2 = self.run_job(Fake(company()), {"name": "ACME FREIGHT PVT. LTD.", "website": "www.acmefreight.in"})
        self.assertEqual(r2["verdict"], "HOLD_FOR_REVIEW")
        self.assertTrue(any("Duplicate" in w for w in r2["looks_off"]["weak"]))


class TestRealShape(Base):
    """Real Company Enrich response for delhivery.com (slimmed), fetched 22 Nov 2026."""

    def test_delhivery_fixture(self):
        enrich = json.loads((FIXTURES / "delhivery_enrich.json").read_text())
        comp = enrich[0]["matches"][0]["company_data"]
        fake = Fake(comp, reg_date="2011-03-16T07:47:18Z")
        job, r = self.run_job(fake, {"name": "Delhivery Limited", "website": "https://www.delhivery.com/"})
        names_ = [l["name"] for l in r["who_runs_it"]]
        self.assertEqual(names_[:3], ["Mohit Tandon", "Suraj Saharan", "Bhavesh Manglani"])
        self.assertTrue(all(l["confirmed"] for l in r["who_runs_it"]))
        self.assertNotIn("/person/enrich", fake.count)          # history came with Company Enrich
        self.assertEqual(r["how_long_it_has_existed"]["earliest_founder_start"], "2011-04-01")
        self.assertEqual(r["verdict"], "REAL_AND_TRADING", r)

    def test_junk_cxo_filtered(self):
        enrich = json.loads((FIXTURES / "delhivery_enrich.json").read_text())
        comp = enrich[0]["matches"][0]["company_data"]
        comp["people"]["founders"] = []
        job, r = self.run_job(Fake(comp, reg_date="2011-03-16T07:47:18Z"),
                              {"name": "Delhivery", "website": "delhivery.com"})
        names_ = [l["name"] for l in r["who_runs_it"]]
        self.assertIn("Sahil Barua", names_)
        self.assertNotIn("reus zxz", names_)


if __name__ == "__main__":
    unittest.main()
