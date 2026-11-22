"""Run tests/cases.json through the real pipeline using REAL Crustdata answers recorded on 22 Nov 2026
(tests/fixtures/live_2026-11-22). A request that was not recorded is answered as a timeout, so a change to
a query or field list shows up as "cannot verify", never as a wrong answer.

  python scripts/replay_live.py            # prints pass / safe miss / wrong for each case

RDAP (domain age) was not reachable when these were recorded, so domain age is "cannot verify" here.
"""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vendorcheck import config as config_module                       # noqa: E402
from vendorcheck.batch import print_eval, run_batch                    # noqa: E402
from vendorcheck.pipeline import Pipeline                              # noqa: E402
from vendorcheck.store import Store                                    # noqa: E402

RECORDINGS = ROOT / "tests" / "fixtures" / "live_2026-11-22"
MISSING: list = []                                                     # requests with no recording (debugging)


def replay_transport(method, url, headers, body, timeout):
    key = hashlib.sha256(f"{method} {url} {body or b''!r}".encode()).hexdigest()[:16]
    path = RECORDINGS / f"{key}.json"
    if not path.exists():
        MISSING.append((url, body))
        raise TimeoutError("no recorded answer for this request")
    record = json.loads(path.read_text())
    return record.get("http_status", 200), {"x-credits-used": str(record.get("credits", 0))}, record["text"]


def replay(cases: str = str(ROOT / "tests" / "cases.json"), quiet: bool = False):
    cfg = config_module.load_config()
    cfg["limits"]["max_retries"] = 0
    cfg.setdefault("activity", {})["as_of"] = "2026-11-22"             # the day the answers were recorded
    work = Path(tempfile.mkdtemp())
    config_module.ROOT = work                                           # results and logs go to a temp folder
    store = Store(work / "replay.db")
    pipe = Pipeline(store, cfg, transport=replay_transport, sleep=lambda s: None, log=lambda m: None)
    return run_batch(store, cfg, pipe, cases, log=(lambda m: None) if quiet else print)


if __name__ == "__main__":
    sys.exit(print_eval(replay(quiet=True)))
