"""Regression tests for the defects found by an independent review on 22 Nov 2026."""
import json
import threading
import unittest

from vendorcheck import names
from vendorcheck.actions import decide_approval
from vendorcheck.pipeline import Pipeline, new_job
from vendorcheck.report import build_result

from test_offline import Base, Fake, Killed, company
from test_hard_cases import Fake2


class TestWrongVerdicts(Base):
    def test_rejecting_a_lookalike_does_not_reject_the_real_firm(self):
        fake = Fake2(company(), known_domains={"acmefreight.in"})
        job, r = self.run_job(fake, {"name": "Acme Freight Pvt Ltd", "website": "acmefreight-india.com"})
        decide_approval(self.store, job, "Asha", action="reject_vendor", reason="lookalike of a real carrier")
        keys = {row["company_key"] for row in self.store.db.execute("SELECT company_key FROM vendor_register")}
        self.assertNotIn("cd:101", keys)
        self.assertNotIn("acmefreight.in", keys)
        job2, r2 = self.run_job(Fake2(company(), known_domains={"acmefreight.in"}),
                                {"name": "Acme Freight Pvt Ltd", "website": "acmefreight.in"})
        self.assertEqual(r2["verdict"], "REAL_AND_TRADING", r2["looks_off"])

    def test_crime_words_do_not_clear_real_adverse_news(self):
        news = [{"subject": "Acme Freight", "title": "Acme Freight directors arrested in cyber fraud racket",
                 "url": "https://news.example/cf", "snippet": "Police said the firm ran the racket."}]
        job, r = self.run_job(Fake(company(), news=news))
        self.assertEqual(r["verdict"], "HOLD_FOR_REVIEW")
        self.assertTrue(r["looks_off"]["adverse_news"])

    def test_leader_article_naming_the_brand_is_linked_not_cleared(self):
        comp = company(name="Blue Dart Express Limited", domain="bluedart.com", founder="Anil Kumar")
        news = [{"subject": "Anil Kumar", "title": "Anil Kumar arrested in Rs 40 crore fraud",
                 "url": "https://news.example/bd", "snippet": "Anil Kumar arrested for fraud."}]
        pages = {"https://news.example/bd": "<p>Anil Kumar, managing director of Blue Dart, was arrested in a "
                                            "fraud case on 2 October 2026.</p>"}
        job, r = self.run_job(Fake(comp, news=news, pages=pages),
                              {"name": "Blue Dart Express Limited", "website": "bluedart.com"})
        self.assertEqual(r["verdict"], "HOLD_FOR_REVIEW", r)
        self.assertFalse(r["namesakes_cleared"])

    def test_confirmed_leader_alone_is_not_evidence_of_trading(self):
        comp = company(headcount=None, status=None)
        comp["hiring"] = {"openings_count": None}
        comp["news"] = []
        job, r = self.run_job(Fake(comp))
        self.assertNotEqual(r["verdict"], "REAL_AND_TRADING", r["is_trading"])

    def test_malformed_websites_are_no_website_not_a_lookalike(self):
        for bad in ("N/A", "-", "acmefreight"):
            self.assertIsNone(names.domain_of(bad))
        self.assertEqual(names.domain_of("acmefreight.in."), "acmefreight.in")
        job, r = self.run_job(Fake2(company(), known_domains={"acmefreight.in"}),
                              {"name": "Acme Freight Private Limited", "website": "N/A"})
        self.assertFalse(r["looks_off"]["strong"], r["looks_off"])

    def test_free_text_does_not_invent_names_or_websites(self):
        parsed = names.parse_free_text("Here's the vendor's details: Acme Freight Pvt Ltd, acmefreight.in")
        self.assertEqual(parsed["website"], "acmefreight.in")
        self.assertNotEqual(parsed["name"], "s the vendor")
        self.assertEqual(names.parse_free_text("Acme Freight Pvt.Ltd, website acmefreight.in")["website"],
                         "acmefreight.in")


class TestRobustness(Base):
    def odd(self, endpoint, body):
        inner = Fake(company())

        def transport(method, url, headers, payload, timeout):
            if url.endswith(endpoint):
                return 200, {}, body
            return inner(method, url, headers, payload, timeout)
        return transport

    def test_unexpected_response_shapes_never_crash_or_approve(self):
        for endpoint, body in (("/company/identify", '["x"]'), ("/company/identify", '{"error": "bad"}'),
                               ("/company/enrich", '{"oops": 1}'), ("/web/search/live", '{"results": [null]}')):
            job, r = self.run_job(self.odd(endpoint, body))
            self.assertEqual(self.store.job(job)["stage"], "done", (endpoint, body))
            if endpoint != "/web/search/live":
                self.assertNotEqual(r["verdict"], "REAL_AND_TRADING", (endpoint, body))

    def test_interrupted_call_stays_in_the_ledger(self):
        fake = Fake(company(), kill_on="/web/search/live")
        job = new_job(self.store, {"name": "Acme Freight", "website": "acmefreight.in"})
        with self.assertRaises(Killed):
            Pipeline(self.store, self.cfg, transport=fake, sleep=lambda s: None, log=lambda m: None).run(job)
        Pipeline(self.store, self.cfg, transport=fake, sleep=lambda s: None, log=lambda m: None).run(job)
        statuses = [row["status"] for row in self.store.ledger(job) if row["endpoint"] == "/web/search/live"]
        self.assertEqual(len(statuses), 2)
        self.assertIn("INTERRUPTED", statuses)
        self.assertTrue(set(statuses) & {"FOUND", "NOT_FOUND"})

    def test_two_racing_decisions_do_only_one_action(self):
        job, r = self.run_job(Fake(company()))
        db_path = self.store.path
        results = []

        def decide(action, reason):
            from vendorcheck.store import Store
            store = Store(db_path)
            results.append(decide_approval(store, job, "Asha", action=action, reason=reason))
            store.db.close()
        threads = [threading.Thread(target=decide, args=("add_to_approved_vendors", None)),
                   threading.Thread(target=decide, args=("reject_vendor", "second reviewer"))]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) c FROM actions").fetchone()["c"], 1)
        self.assertEqual(self.store.db.execute(
            "SELECT COUNT(*) c FROM outbox WHERE to_team='procurement-team'").fetchone()["c"], 1)

    def test_hand_over_marks_done_in_the_same_transaction(self):
        job, r = self.run_job(Fake(company()))
        self.assertEqual(self.store.job(job)["stage"], "done")
        decide_approval(self.store, job, "Asha")
        # Re-running a finished job (a restart) must never reopen it.
        Pipeline(self.store, self.cfg, transport=Fake(company()), sleep=lambda s: None, log=lambda m: None).run(job)
        self.assertEqual(self.store.job(job)["status"], "closed")


if __name__ == "__main__":
    unittest.main()


class TestRealDataHelpers(unittest.TestCase):
    """Helpers added after the second batch of real Crustdata answers (22 Nov)."""

    def test_storefront_pages_are_not_company_websites(self):
        from vendorcheck import names
        self.assertTrue(names.is_hosting("acme-freight.business.site"))
        self.assertTrue(names.is_hosting("indiamart.com"))
        self.assertFalse(names.is_hosting("delhivery.com"))
        self.assertFalse(names.is_hosting(None))

    def test_only_leadership_titles_run_the_company(self):
        from vendorcheck import names
        self.assertTrue(names.is_leadership_title("Executive Director & CFO"))
        self.assertTrue(names.is_leadership_title("Co-Founder"))
        self.assertFalse(names.is_leadership_title("Senior Territory Manager"))
        self.assertFalse(names.is_leadership_title(None))

    def test_requested_name_carrying_a_known_brand(self):
        from vendorcheck import names
        self.assertTrue(names.carries_brand("Xpressbees Logistics",
                                            "Xpressbees (BusyBees Logistics Solutions Pvt. Ltd.)"))
        self.assertFalse(names.carries_brand("Sharma Trading Co", "Xpressbees (BusyBees Logistics Solutions Pvt. Ltd.)"))
