"""The review app end to end over HTTP, with the fake sources (mock data is for tests only)."""
import functools
import json
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from vendorcheck import web
from vendorcheck.config import load_config
from vendorcheck.pipeline import Pipeline
from vendorcheck.store import Store

from test_offline import Fake, company


class TestReviewApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "web.db")
        Store(self.db).db.close()
        self.cfg = load_config()
        news = [{"subject": "Ravi Menon", "title": "<script>alert(1)</script> Ravi Menon arrested in fraud",
                 "url": "https://news.example/x", "snippet": "Ravi Menon arrested for fraud in Jaipur."}]
        pages = {"https://news.example/x": "<p>Ravi Menon, a Jaipur jeweller, was arrested for fraud.</p>"}
        fake = Fake(company(), news=news, pages=pages)
        self.patch = mock.patch.object(web, "Pipeline",
                                       functools.partial(Pipeline, transport=fake, sleep=lambda s: None))
        self.patch.start()
        self.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.db, self.cfg))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.patch.stop()
        self.tmp.cleanup()

    def token(self):
        return re.search(r"name='csrf' value='([^']+)'", self.get("/")).group(1)

    def post(self, path, fields, with_token=True):
        if with_token:
            fields = {**fields, "csrf": self.token()}
        data = urllib.parse.urlencode(fields).encode()
        with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data)) as response:
            return response.geturl(), response.read().decode()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return response.read().decode()

    def test_post_from_another_page_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.post("/check", {"name": "Acme Freight", "website": "acmefreight.in"}, with_token=False)
        self.assertEqual(refused.exception.code, 403)
        self.assertEqual(Store(self.db).db.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"], 0)

    def test_check_review_and_approve_once(self):
        final_url, _ = self.post("/check", {"name": "Acme Freight Pvt Ltd", "website": "acmefreight.in"})
        job_id = final_url.rsplit("/", 1)[-1]
        for _ in range(100):
            result = json.loads(self.get(f"/job/{job_id}.json"))
            if result["stage"] == "done":
                break
            time.sleep(0.05)
        self.assertEqual(result["verdict"], "REAL_AND_TRADING", result)

        html = self.get(f"/job/{job_id}")
        self.assertIn("Real and trading", html)
        self.assertIn("Every fact and its source", html)
        self.assertNotIn("<script>alert(1)</script>", html)          # outside text is escaped

        _, first = self.post(f"/job/{job_id}/decide", {"by": "Asha", "action": "add_to_approved_vendors",
                                                        "decision": "approve"})
        _, second = self.post(f"/job/{job_id}/decide", {"by": "Asha", "action": "add_to_approved_vendors",
                                                         "decision": "approve"})
        self.assertIn("done, approved by Asha", first)
        self.assertIn("nothing done again", second)
        store = Store(self.db)
        self.assertEqual(store.db.execute("SELECT COUNT(*) c FROM actions").fetchone()["c"], 1)

        first_fact = next(f for f in result["facts"] if f["field_or_passage"].endswith("basic_info.name"))
        call_page = self.get(f"/call/{first_fact['call_id']}?path=" + urllib.parse.quote(first_fact["field_or_passage"]))
        self.assertIn("Acme Freight Private Limited", call_page)
        self.assertIn("Cost ledger", self.get(f"/ledger/{job_id}"))
        self.assertIn("Measured cost", self.get("/"))
