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
    for c in ledger:
        by_endpoint[c["endpoint"]] = round(by_endpoint.get(c["endpoint"], 0) + c["credits"], 3)
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
        "facts": [{"id": f["id"], "claim": f["claim"], "value": json.loads(f["value_json"]),
                   "source": f["source_url"], "field_or_passage": f["locator"], "quote": f["quote"],
                   "retrieved_at": f["retrieved_at"], "raw_response": f"call #{f['call_id']}"}
                  for f in store.facts(job_id)],
        "cost": {"crustdata_credits": round(store.credits_spent(job_id), 3),
                 "credit_limit": cfg["limits"]["max_credits_per_run"],
                 "calls": len(ledger), "by_endpoint": by_endpoint,
                 "elapsed_s": round(job["updated_at"] - job["started_at"], 1)},
    }
    keep = set(cfg["output"]["fields"]) | {"summary", "namesakes_cleared", "stage", "status"}
    return {k: v for k, v in full.items() if k in keep}


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
        add(f"  - {l['name']} — {l.get('current_title') or l.get('title') or '?'} [{mark}]"
            + (f" ({l['why']})" if l.get("why") else ""))
    h = r.get("how_long_it_has_existed") or {}
    add(f"\nHow long it has existed: founded {h.get('year_founded') or '?'}; "
        f"domain registered {(h.get('domain_registered') or '?')[:10]}")
    add("\nTrading evidence:")
    for p in r.get("is_trading") or []:
        add(f"  + {p}")
    lo = r.get("looks_off") or {}
    if lo.get("strong") or lo.get("weak"):
        add("\nLooks off:")
        for s in lo.get("strong", []):
            add(f"  !! {s}")
        for s in lo.get("weak", []):
            add(f"  !  {s}")
    if r.get("namesakes_cleared"):
        add("\nNamesakes cleared:")
        for s in r["namesakes_cleared"]:
            add(f"  ~ {s}")
    if r.get("cannot_verify"):
        add("\nCannot verify:")
        for s in r["cannot_verify"]:
            add(f"  ? {s}")
    add("\nSources:")
    for f in r.get("facts") or []:
        add(f"  [{f['id']}] {f['claim']}: {json.dumps(f['value'])[:90]}")
        add(f"       {f['source']}  ·  {f['field_or_passage']}  ·  {f['retrieved_at']}  ·  {f['raw_response']}")
    c = r.get("cost") or {}
    add(f"\nCost: {c.get('crustdata_credits')} / {c.get('credit_limit')} Crustdata credits, "
        f"{c.get('calls')} calls, {c.get('elapsed_s')}s  {c.get('by_endpoint')}")
    return "\n".join(L)
