"""Is it active NOW (dated job postings), the company registration number (CIN), who the domain is
registered to, and directors named by the vendor. Mock data is for tests only."""
import json
import unittest

from vendorcheck import names
from test_offline import Base, Fake, company


class FakeJobs(Fake):
    """Adds Crustdata Job Search (API shape: job_details / metadata nested) and an RDAP registrant."""

    def __init__(self, comp, postings=None, registrant=None, **kw):
        super().__init__(comp, **kw)
        self.postings, self.registrant = postings, registrant
        self.queries = []

    def __call__(self, method, url, headers, body, timeout):
        if url.endswith("/job/search"):
            if "/job/search" in self.timeout_on:
                raise TimeoutError("simulated slow source")
            self.count["/job/search"] = self.count.get("/job/search", 0) + 1
            listings = [{"job_details": {"title": t, "url": f"https://jobs.example/{i}"},
                         "metadata": {"date_added": d}} for i, (t, d) in enumerate(self.postings or [])]
            return 200, {"x-credits-used": str(0.03 * len(listings))}, json.dumps(
                {"job_listings": listings, "total_count": len(listings)})
        if "rdap" in url and self.registrant:
            return 200, {}, json.dumps({"events": [{"eventAction": "registration", "eventDate": self.reg_date}],
                                        "entities": [{"roles": ["registrant"], "vcardArray": ["vcard", [
                                            ["version", {}, "text", "4.0"], ["org", {}, "text", self.registrant]]]}]})
        if url.endswith("/web/search/live"):
            self.queries.append(json.loads(body)["query"])
        return super().__call__(method, url, headers, body, timeout)


def stale_company():
    comp = company()
    comp["news"] = [{"article_title": "Acme Freight opens new warehouse", "article_url": "https://news.example/a",
                     "article_publish_date": "2023-01-10"}]
    return comp


class TestActiveNow(Base):
    def test_recent_job_posting_is_dated_proof_of_activity(self):
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")])
        job, r = self.run_job(fake)
        self.assertEqual(r["verdict"], "REAL_AND_TRADING", r)
        self.assertEqual(r["latest_activity"]["date"], "2026-11-15")
        self.assertTrue(any(x.startswith("Active now") for x in r["is_trading"]))
        fact = next(f for f in r["facts"] if f["claim"].startswith("Date a job posting"))
        self.assertEqual(fact["field_or_passage"], "job_listings[0].metadata.date_added")   # the date is cited
        self.assertEqual(fact["record_date"], "2026-11-15T10:00:00")
        self.assertIn("Fleet Manager", fact["claim"])

    def test_nothing_recent_is_never_approved(self):
        # Headcount, an "active" flag and a confirmed founder, but the newest dated record is 2023:
        # a dormant firm must not be called "real and trading".
        fake = FakeJobs(stale_company(), postings=[("Driver", "2023-02-01T00:00:00")])
        job, r = self.run_job(fake)
        self.assertNotEqual(r["verdict"], "REAL_AND_TRADING", r)
        self.assertTrue(any("Recent activity" in x for x in r["cannot_verify"]), r["cannot_verify"])
        self.assertFalse(any(x.startswith("Hiring") for x in r["is_trading"]))   # stale hiring is not a plus

    def test_news_about_another_company_is_not_activity(self):
        comp = stale_company()
        comp["news"] = [{"article_title": "Bigco posts record profit", "article_url": "https://news.example/b",
                         "article_publish_date": "2026-11-01"}]
        job, r = self.run_job(FakeJobs(comp, postings=[]))
        self.assertIsNone(r["latest_activity"])
        self.assertNotEqual(r["verdict"], "REAL_AND_TRADING", r)

    def test_job_search_down_is_cannot_verify_not_dormant(self):
        fake = FakeJobs(stale_company(), timeout_on={"/job/search"})
        job, r = self.run_job(fake)
        self.assertTrue(any("Job Search did not answer" in x for x in r["cannot_verify"]), r["cannot_verify"])
        self.assertNotEqual(r["verdict"], "REAL_AND_TRADING")


class TestRegistrationNumber(Base):
    def test_cin_on_homepage_is_decoded_and_cited(self):
        page = "<footer>Acme Freight Pvt Ltd | CIN: U63090MH2011PTC123456 | Mumbai</footer>"
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")],
                        pages={"https://www.acmefreight.in": page})
        job, r = self.run_job(fake)
        self.assertEqual(r["how_long_it_has_existed"]["year_incorporated"], 2011)
        self.assertTrue(any("CIN U63090MH2011PTC123456" in x and "Maharashtra" in x for x in r["is_trading"]))
        fact = next(f for f in r["facts"] if "CIN" in f["claim"])
        self.assertIn("U63090MH2011PTC123456", fact["quote"])

    def test_recently_incorporated_and_cin_mismatch(self):
        page = "CIN U63090MH2026PTC999999"
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")],
                        pages={"https://www.acmefreight.in": page})
        job, r = self.run_job(fake, {"name": "Acme Freight Pvt Ltd", "website": "acmefreight.in",
                                     "cin": "U63090MH2011PTC123456"})
        self.assertTrue(any("Recently incorporated" in x for x in r["looks_off"]["weak"]), r["looks_off"])
        self.assertTrue(any("differs from the CIN on its website" in x for x in r["looks_off"]["needs_review"]))
        self.assertEqual(r["verdict"], "HOLD_FOR_REVIEW")

    def test_decode_rejects_malformed(self):
        self.assertIsNone(names.decode_cin("U63090XX2011PTC123456"))     # no such state code
        self.assertIsNone(names.decode_cin("27AAPFU0939F1ZV"))            # a GSTIN is not a CIN
        self.assertEqual(names.decode_cin("l62200tg2008plc061781")["state"], "Telangana")


class TestDomainRegistrant(Base):
    def test_registered_to_the_vendor(self):
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")],
                        registrant="ACME FREIGHT PRIVATE LIMITED")
        job, r = self.run_job(fake)
        self.assertTrue(any("registered to ACME FREIGHT" in x for x in r["is_trading"]), r["is_trading"])

    def test_registered_to_someone_else(self):
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")], registrant="Bigco Limited")
        job, r = self.run_job(fake)
        self.assertTrue(any("registered to 'Bigco Limited'" in x for x in r["looks_off"]["weak"]), r["looks_off"])


class TestDeclaredDirectors(Base):
    def test_declared_directors_are_screened_and_reported(self):
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")])
        job, r = self.run_job(fake, {"name": "Acme Freight Pvt Ltd", "website": "acmefreight.in",
                                     "directors": "Mr. Suresh Patil; Ravi Menon"})
        self.assertIn('"Suresh Patil"', fake.queries[0])
        self.assertEqual(fake.queries[0].count('"Ravi Menon"'), 1)                 # no duplicate subject
        row = next(l for l in r["who_runs_it"] if l["name"] == "Suresh Patil")
        self.assertTrue(row["declared"])
        self.assertIsNone(row["confirmed"])
        self.assertTrue(any("Suresh Patil named on the vendor's form only" in x for x in r["cannot_verify"]))

    def test_adverse_news_on_a_declared_director_holds(self):
        news = [{"subject": "Suresh Patil", "title": "Suresh Patil of Acme Freight arrested in GST fraud",
                 "url": "https://news.example/p", "snippet": "Suresh Patil, director of Acme Freight, was arrested."}]
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")], news=news)
        job, r = self.run_job(fake, {"name": "Acme Freight Pvt Ltd", "website": "acmefreight.in",
                                     "directors": ["Suresh Patil"]})
        self.assertEqual(r["verdict"], "HOLD_FOR_REVIEW", r)
        self.assertTrue(any("Suresh Patil" in x for x in r["looks_off"]["adverse_news"]), r["looks_off"])

    def test_requests_without_extras_are_unchanged(self):
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")])
        job, r = self.run_job(fake)
        self.assertFalse(any(l.get("declared") for l in r["who_runs_it"]))


if __name__ == "__main__":
    unittest.main()


class TestDomainRegistryFallback(Base):
    def test_rdap_down_is_read_through_web_fetch(self):
        record = json.dumps({"events": [{"eventAction": "registration", "eventDate": "2011-05-04T00:00:00Z"}]})
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")], timeout_on={"rdap"},
                        pages={"https://rdap.org/domain/acmefreight.in": record})
        job, r = self.run_job(fake)
        self.assertEqual(r["how_long_it_has_existed"]["domain_registered"], "2011-05-04T00:00:00Z")
        fact = next(f for f in r["facts"] if "registration date" in f["claim"])
        self.assertIn("Crustdata Web Fetch", fact["claim"])
        # The stored response is the fetched page, so the fact is the verbatim passage that holds the date.
        self.assertEqual(fact["field_or_passage"], "passage")
        self.assertIn("2011-05-04T00:00:00Z", fact["quote"])
        self.assertEqual(r["verdict"], "REAL_AND_TRADING", r)

    def test_unregistered_domain_is_reported_once(self):
        class Unregistered(FakeJobs):
            def __call__(self, method, url, headers, body, timeout):
                if "rdap.org" in url:
                    return 404, {}, json.dumps({"errorCode": 404, "title": "Not Found",
                                                "description": ["Domain acme-x.in is available for registration"]})
                return super().__call__(method, url, headers, body, timeout)
        job, r = self.run_job(Unregistered(company()), {"name": "Acme X", "website": "acme-x.in"})
        hits = [x for x in r["looks_off"]["weak"] if "not a registered domain" in x]
        self.assertEqual(len(hits), 1, r["looks_off"])
        self.assertFalse(any("Domain age" in x for x in r["cannot_verify"]))


class TestPlainWordsExtras(Base):
    def test_directors_and_gstin_read_from_plain_words(self):
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")])
        job, r = self.run_job(fake, {"text": "New vendor 'Acme Freight Pvt Ltd', website acmefreight.in. "
                                             "Directors: Suresh Patil and Mr. Anil Rao. Our logistics partner Blue Dart."})
        declared = [l["name"] for l in r["who_runs_it"] if l.get("declared")]
        self.assertEqual(declared, ["Suresh Patil", "Anil Rao"])
        self.assertIn('"Anil Rao"', fake.queries[0])

    def test_free_text_parsers(self):
        self.assertEqual(names.directors_in_text("Directors are not known yet"), [])
        self.assertEqual(names.directors_in_text("promoter is Rakesh Kumar Gupta; website x.in"), ["Rakesh Kumar Gupta"])



class TestReviewFixes2(Base):
    """Defects found by the second independent review (all reproduced first)."""

    def test_odd_posting_dates_are_skipped_not_fatal(self):
        for bad in ("15/11/2026", 1763164800, "Nov 15, 2026", None):
            fake = FakeJobs(company(), postings=[("Driver", bad)])
            job, r = self.run_job(fake)
            self.assertEqual(r["status"], "pending_approval", (bad, r["status"]))     # decided, not stuck
            self.assertFalse((r["latest_activity"] or {}).get("what", "").startswith("job"), bad)

    def test_both_rdap_routes_down_is_only_cannot_verify(self):
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")],
                        timeout_on={"rdap", "/web/enrich/live"})
        job, r = self.run_job(fake)
        self.assertEqual(r["verdict"], "REAL_AND_TRADING", r["cannot_verify"])

    def test_odd_vcard_keeps_the_domain_age(self):
        class OddCard(FakeJobs):
            def __call__(self, method, url, headers, body, timeout):
                if "rdap" in url:
                    return 200, {}, json.dumps({"events": [{"eventAction": "registration", "eventDate": self.reg_date}],
                                                "entities": [{"roles": ["registrant"], "vcardArray": ["vcard"]}]})
                return super().__call__(method, url, headers, body, timeout)
        job, r = self.run_job(OddCard(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")]))
        self.assertEqual(r["how_long_it_has_existed"]["domain_registered"], "2011-05-04T00:00:00Z")

    def test_privacy_registrant_is_ignored(self):
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")],
                        registrant="REDACTED FOR PRIVACY")
        job, r = self.run_job(fake)
        self.assertFalse(any("registered to" in x for x in r["looks_off"]["weak"]), r["looks_off"])

    def test_form_cin_shown_on_website_is_not_a_mismatch(self):
        page = "CIN U63090MH2011PTC123456 · Parent company CIN L63090MH1990PLC654321"
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")],
                        pages={"https://www.acmefreight.in": page})
        job, r = self.run_job(fake, {"name": "Acme Freight Pvt Ltd", "website": "acmefreight.in",
                                     "cin": "U63090MH2011PTC123456"})
        self.assertFalse(r["looks_off"]["needs_review"], r["looks_off"])

    def test_future_cin_year_is_malformed(self):
        self.assertIsNone(names.decode_cin("U63090MH2099PTC123456"))

    def test_directors_not_claimed_screened_when_news_down(self):
        fake = FakeJobs(company(), postings=[("Fleet Manager", "2026-11-15T10:00:00")], timeout_on={"/web/search/live"})
        job, r = self.run_job(fake, {"name": "Acme Freight Pvt Ltd", "website": "acmefreight.in",
                                     "directors": "Suresh Patil"})
        line = next(x for x in r["is_trading"] if x.startswith("Directors named"))
        self.assertNotIn("each was screened", line)
        self.assertTrue(any("were not screened in the news" in x for x in r["cannot_verify"]), r["cannot_verify"])
