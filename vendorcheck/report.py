"""Show its work: the finished result, every fact with its source, and the cost of the run."""
import json
import time

from .store import Store


def build_result(store: Store, cfg: dict, job_id: str) -> dict:
    job = store.job(job_id)
    d = store.stage_output(job_id, "decide") or {}
    appr = store.db.execute("SELECT * FROM approvals WHERE job_id=?", (job_id,)).fetchone()
    ledger = store.ledger(job_id)
    by_endpoint: dict[str, float] = {}
    estimated = 0.0
    gst_calls = 0
    for c in ledger:
        by_endpoint[c["endpoint"]] = round(by_endpoint.get(c["endpoint"], 0) + c["credits"], 3)
        if c["credits_source"] == "estimated":
            estimated += c["credits"]
        if c["source"] == "gst" and c["status"] in ("FOUND", "NOT_FOUND"):
            gst_calls += 1
    credits = round(store.credits_spent(job_id), 3)
    ccfg = cfg.get("cost", {})
    inr = round(credits * ccfg.get("inr_per_credit", 0) + gst_calls * ccfg.get("gst_inr_per_call", 0), 2)
    full = {
        "job_id": job_id,
        "request": json.loads(job["request_json"]),
        "stage": job["stage"],
        "status": job["status"],
        "verdict": d.get("verdict"),
        "summary": d.get("summary"),
        "recommended_action": appr["recommended_action"] if appr else None,
        "approval": ({"status": appr["status"], "decided_by": appr["decided_by"],
                      "decided_action": appr["decided_action"], "decided_at": appr["decided_at"]}
                     if appr else None),
        "who_runs_it": d.get("who_runs_it"),
        "how_long_it_has_existed": d.get("how_long_it_has_existed"),
        "is_trading": d.get("is_trading"),
        "looks_off": d.get("looks_off"),
        "namesakes_cleared": d.get("namesakes_cleared"),
        "cannot_verify": d.get("cannot_verify"),
        "registry": d.get("registry"),
        "facts": [{"id": f["id"], "claim": f["claim"], "value": json.loads(f["value_json"]),
                   "source": f["source_url"], "field_or_passage": f["locator"], "quote": f["quote"],
                   "record_date": f["record_date"], "retrieved_at": f["retrieved_at"],
                   "raw_response": f"call #{f['call_id']}"}
                  for f in store.facts(job_id)],
        "cost": {"crustdata_credits": credits,
                 "credits_estimated_not_reported": round(estimated, 3),
                 "credit_limit": cfg["limits"]["max_credits_per_run"],
                 "inr": inr, "inr_per_credit_assumed": ccfg.get("inr_per_credit"),
                 "calls": len(ledger), "by_endpoint": by_endpoint,
                 "elapsed_s": round(job["updated_at"] - job["started_at"], 1)},
    }
    keep = set(cfg["output"]["fields"]) | {"summary", "namesakes_cleared", "stage", "status"}
    return {k: v for k, v in full.items() if k in keep}


def save_result(store: Store, cfg: dict, job_id: str, folder=None) -> str:
    """Write the finished result to data/results/JOB.json (the evidence file kept on record)."""
    from .config import ROOT
    folder = folder or ROOT / "data" / "results"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{job_id}.json"
    path.write_text(json.dumps(build_result(store, cfg, job_id), indent=2, ensure_ascii=False))
    return str(path)


def render_text(r: dict) -> str:
    L = []
    add = L.append
    add(f"\n=== {r['job_id']} — {r.get('verdict')} ===")
    add(r.get("summary") or "")
    if r.get("approval"):
        a = r["approval"]
        add(f"Recommended action: {r.get('recommended_action')}  |  approval: {a['status']}"
            + (f" by {a['decided_by']} ({a['decided_action']})" if a.get("decided_by") else ""))
    add("\nWho runs it:")
    for l in r.get("who_runs_it") or []:
        mark = {True: "confirmed", False: "NOT confirmed", None: "cannot verify"}[l.get("confirmed")]
        add(f"  - {l['name']} — {l.get('current_title') or l.get('title') or '?'}"
            + (f", since {l['since']}" if l.get("since") else "") + f" [{mark}]"
            + (f" ({l['why']})" if l.get("why") else ""))
    h = r.get("how_long_it_has_existed") or {}
    add(f"\nHow long it has existed: founded {h.get('year_founded') or '?'}; "
        f"domain registered {(h.get('domain_registered') or '?')[:10]}; "
        f"earliest founder start {h.get('earliest_founder_start') or '?'}")
    add("\nTrading evidence:")
    for p in r.get("is_trading") or []:
        add(f"  + {p}")
    lo = r.get("looks_off") or {}
    if any(lo.get(k) for k in ("strong", "adverse_news", "needs_review", "weak")):
        add("\nLooks off:")
        for s in lo.get("strong", []):
            add(f"  !! front signal: {s}")
        for s in lo.get("adverse_news", []):
            add(f"  !! {s}")
        for s in lo.get("needs_review", []):
            add(f"  !  {s}")
        for s in lo.get("weak", []):
            add(f"  !  {s}")
    if r.get("namesakes_cleared"):
        add("\nNamesakes cleared:")
        for s in r["namesakes_cleared"]:
            add(f"  ~ {s}")
    if r.get("registry"):
        add("\nGST registration:")
        for g in r["registry"]:
            add(f"  # {g['gstin']}  checksum {'ok' if g['checksum_ok'] else 'FAILED'}  PAN type {g.get('pan_holder_type')}"
                f"  registry {g.get('registry')}"
                + (f": {g.get('status')}, {g.get('legal_name')}, registered {g.get('registered')}"
                   if g.get("registry") == "FOUND" else ""))
    if r.get("cannot_verify"):
        add("\nCannot verify:")
        for s in r["cannot_verify"]:
            add(f"  ? {s}")
    add("\nSources:")
    for f in r.get("facts") or []:
        add(f"  [{f['id']}] {f['claim']}: {json.dumps(f['value'])[:90]}")
        dated = f"record dated {f['record_date']}" if f.get("record_date") else "record date not stated by source"
        add(f"       {f['source']}  ·  {f['field_or_passage']}  ·  {dated}  ·  retrieved {f['retrieved_at']}"
            f"  ·  {f['raw_response']}")
    c = r.get("cost") or {}
    add(f"\nCost: {c.get('crustdata_credits')} / {c.get('credit_limit')} Crustdata credits "
        f"(≈ ₹{c.get('inr')} at ₹{c.get('inr_per_credit_assumed')}/credit), "
        f"{c.get('calls')} calls, {c.get('elapsed_s')}s  {c.get('by_endpoint')}")
    return "\n".join(L)


def cost_summary(store: Store, cfg: dict) -> str:
    """Measured cost per job across every run in this database: what the finance judge asks for."""
    rate = cfg.get("cost", {}).get("inr_per_credit", 0)
    rows = store.db.execute(
        "SELECT j.id, j.verdict, j.started_at, j.updated_at, COALESCE(SUM(c.credits), 0) AS credits, "
        "COUNT(c.id) AS calls FROM jobs j LEFT JOIN calls c ON c.job_id = j.id "
        "WHERE j.stage = 'done' GROUP BY j.id").fetchall()
    if not rows:
        return "no finished jobs yet"
    credits = sorted(r["credits"] for r in rows)
    seconds = sorted(r["updated_at"] - r["started_at"] for r in rows)
    mean = sum(credits) / len(credits)
    p90 = credits[min(len(credits) - 1, int(0.9 * len(credits)))]
    lines = [f"{len(rows)} finished jobs",
             f"credits per job: mean {mean:.2f}, p90 {p90:.2f}, max {credits[-1]:.2f}",
             f"rupees per job (at Rs {rate}/credit): mean Rs {mean * rate:.2f}, p90 Rs {p90 * rate:.2f}",
             f"seconds per job: median {seconds[len(seconds) // 2]:.1f}, max {seconds[-1]:.1f}",
             "by verdict:"]
    by_verdict: dict[str, list] = {}
    for r in rows:
        by_verdict.setdefault(r["verdict"] or "-", []).append(r["credits"])
    for verdict, values in sorted(by_verdict.items()):
        lines.append(f"  {verdict:<17} {len(values):>3} jobs, mean {sum(values) / len(values):.2f} credits")
    return "\n".join(lines)
