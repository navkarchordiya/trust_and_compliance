"""Batch runs and evaluation.

A batch file is JSON (a list of requests) or CSV with columns name, website, text and,
for evaluation, expected. Each row becomes a job whose id comes from the file's content and
the row number, so running the same file again after a crash resumes the unfinished rows and
never creates a second job (or a false "duplicate") for a row.

  python -m vendorcheck batch cases.json          # run every row, write data/results/<batch>.jsonl
  python -m vendorcheck eval tests/cases.json     # same, then compare with each row's expected verdict
"""
import csv
import hashlib
import json
from pathlib import Path

from .pipeline import Pipeline, new_job
from .report import build_result, save_result
from .store import Store

# A wrong answer costs more than "cannot verify": these are safe misses, not failures.
SAFE_MISSES = {"CANNOT_VERIFY", "HOLD_FOR_REVIEW"}


def load_rows(path: str) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8")
    if path.lower().endswith(".csv"):
        rows = list(csv.DictReader(text.splitlines()))
    else:
        data = json.loads(text)
        if isinstance(data, dict):
            rows = data.get("cases") or data.get("requests") or []
        else:
            rows = data
    clean = []
    for row in rows:
        if isinstance(row, str):
            row = {"text": row}
        clean.append({k: (v.strip() if isinstance(v, str) else v) for k, v in row.items() if v not in (None, "")})
    return clean


def batch_id(path: str) -> str:
    return "b" + hashlib.sha256(Path(path).read_bytes()).hexdigest()[:6]


def run_batch(store: Store, cfg: dict, pipe: Pipeline, path: str, log=print) -> list[tuple[dict, dict]]:
    bid = batch_id(path)
    out = []
    for i, row in enumerate(load_rows(path)):
        request = {k: row[k] for k in ("name", "website", "text", "gstin") if row.get(k)}
        if not request:
            log(f"row {i}: no name, website or text; skipped")
            continue
        job_id = new_job(store, request, job_id=f"{bid}_{i:03d}")
        if store.job(job_id)["stage"] != "done":
            log(f"row {i}: {job_id} {request}")
            try:
                pipe.run(job_id)
            except Exception as e:             # one bad row must not stop the batch; it stays resumable
                store.log_event(job_id, "error", detail=f"{type(e).__name__}: {e}")
                log(f"row {i}: {job_id} failed ({type(e).__name__}: {e}); re-run the batch to resume it")
                out.append((row, {"job_id": job_id, "verdict": None, "error": f"{type(e).__name__}: {e}"}))
                continue
        save_result(store, cfg, job_id)
        out.append((row, build_result(store, cfg, job_id)))
    from .config import ROOT
    results = ROOT / "data" / "results"
    results.mkdir(parents=True, exist_ok=True)
    with open(results / f"{bid}.jsonl", "w", encoding="utf-8") as f:
        for row, result in out:
            f.write(json.dumps({"input": row, "result": result}, ensure_ascii=False) + "\n")
    log(f"results: {results / (bid + '.jsonl')}")
    return out


def grade(expected: str | None, got: str | None) -> str:
    """expected: one verdict, several separated by |, or !VERDICT for "anything but this one"
    (e.g. a front company must never come back REAL_AND_TRADING)."""
    if not expected:
        return "NO_EXPECTED"
    if expected.strip().startswith("!"):
        return "FAIL" if got == expected.strip()[1:].upper() else "PASS"
    allowed = {e.strip().upper() for e in expected.replace(",", "|").split("|") if e.strip()}
    if got in allowed:
        return "PASS"
    return "SAFE_MISS" if got in SAFE_MISSES else "FAIL"


def print_eval(results: list[tuple[dict, dict]]) -> int:
    counts = {"PASS": 0, "SAFE_MISS": 0, "FAIL": 0, "NO_EXPECTED": 0}
    print(f"{'#':<3} {'grade':<11} {'expected':<34} {'got':<17} {'cr':>5}  request")
    for i, (row, r) in enumerate(results):
        g = grade(row.get("expected"), r.get("verdict"))
        counts[g] += 1
        req = row.get("name") or row.get("website") or (row.get("text") or "")[:40]
        print(f"{i:<3} {g:<11} {row.get('expected', '-'):<34} {r.get('verdict') or '-':<17} "
              f"{(r.get('cost') or {}).get('crustdata_credits', 0):>5}  {req}")
    total = sum(counts.values())
    print(f"\n{counts['PASS']}/{total} pass, {counts['SAFE_MISS']} safe misses (cannot verify / hold), "
          f"{counts['FAIL']} wrong answers")
    return 1 if counts["FAIL"] else 0


def import_vendors(store: Store, path: str) -> str:
    """Load the client's existing vendor list so a request for a vendor it already has is caught as a
    duplicate. Each vendor is keyed by website, GSTIN and core name (columns: name, website, gstin)."""
    from . import names
    count = 0
    for row in load_rows(path):
        name = row.get("name") or row.get("vendor_name") or row.get("legal_name")
        domain = names.domain_of(row.get("website") or row.get("domain"))
        gstin = (row.get("gstin") or row.get("GSTIN") or "").strip().upper() or None
        keys = [domain, f"gstin:{gstin}" if gstin else None,
                f"name:{names.core_name(name)}" if names.core_name(name) else None]
        for key in [k for k in keys if k]:
            store.add_register_entry(key, name, domain, row.get("status") or "on the client's vendor list",
                                     "client-register")
        count += 1
    return f"imported {count} vendors from {path}"


def rescreen(store: Store, cfg: dict, pipe: Pipeline, log=print) -> list[dict]:
    """Re-screen every vendor approved through this service: news and GST registry again, the rest
    carried over. One job per vendor per day, so re-running after a crash resumes instead of repeating."""
    from datetime import date
    originals = [row["job_id"] for row in store.db.execute(
        "SELECT DISTINCT job_id FROM vendor_register WHERE decision='approved' AND job_id <> 'client-register'")]
    summary = []
    for original in originals:
        job = store.job(original)
        if not job:
            continue
        request = {**json.loads(job["request_json"]), "rescreen_of": original}
        job_id = new_job(store, request, job_id=f"rs_{original}_{date.today():%Y%m%d}")
        if store.job(job_id)["stage"] != "done":
            log(f"re-screening {original} as {job_id}")
            pipe.run(job_id)
        result = build_result(store, cfg, job_id)
        summary.append({"vendor": request.get("name") or request.get("website"), "original": original,
                        "was": job["verdict"], "now": result.get("verdict"), "job": job_id,
                        "credits": (result.get("cost") or {}).get("crustdata_credits")})
    return summary
