"""The 8 test cases on REAL Crustdata answers recorded on 22 Nov 2026 (see scripts/replay_live.py).
The bar is the brief's: no wrong answers. Unrecorded requests become "cannot verify", never wrong."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from vendorcheck import config as config_module
from vendorcheck.batch import grade


class TestLiveReplay(unittest.TestCase):
    def test_no_wrong_answers_on_real_data(self):
        import replay_live
        saved_root = config_module.ROOT
        try:
            results = replay_live.replay(quiet=True)
        finally:
            config_module.ROOT = saved_root
        grades = [grade(row.get("expected"), r.get("verdict")) for row, r in results]
        import json
        from pathlib import Path
        cases = json.loads((Path(__file__).parent / "cases.json").read_text())
        self.assertEqual(len(grades), len(cases))
        self.assertNotIn("FAIL", grades, [(row.get("case"), r.get("verdict")) for row, r in results])
        verdicts = {row["case"]: r["verdict"] for row, r in results}
        self.assertEqual(verdicts["honest established firm"], "REAL_AND_TRADING")
        self.assertEqual(verdicts["real firm, leader in adverse news (Vedant's live case)"], "HOLD_FOR_REVIEW")
        self.assertEqual(verdicts["no record anywhere"], "CANNOT_VERIFY")
