"""Irreversible actions, run only after a person approves. Each runs exactly once.

The approval decision, the action row (keyed by its idempotency key), the vendor register
entry and the team notification are written in ONE transaction. If the process is killed
mid-way, nothing is written and the approval can simply be repeated; if it already
committed, repeating it is a no-op.
"""
import json

from .store import Store, now_iso

ALLOWED = {"add_to_approved_vendors", "reject_vendor"}


def decide_approval(store: Store, job_id: str, by: str, action: str | None = None, decline: bool = False) -> str:
    appr = store.db.execute("SELECT * FROM approvals WHERE job_id=?", (job_id,)).fetchone()
    if not appr:
        return f"{job_id}: no approval waiting (has the run reached hand_over?)"
    if appr["status"] != "pending":
        return f"{job_id}: already {appr['status']} by {appr['decided_by']} ({appr['decided_action']}); nothing done again."

    if decline:
        store.db.execute("UPDATE approvals SET status='declined', decided_by=?, decided_at=? WHERE job_id=?",
                         (by, now_iso(), job_id))
        store.update_job(job_id, status="closed")
        return f"{job_id}: declined by {by}. No action taken."

    action = action or appr["recommended_action"]
    if action not in ALLOWED:
        return (f"{job_id}: recommendation is '{appr['recommended_action']}'. Choose an action explicitly: "
                f"--action add_to_approved_vendors or --action reject_vendor")

    job = store.job(job_id)
    ident = store.stage_output(job_id, "identify") or {}
    req = store.stage_output(job_id, "take_request") or {}
    top = ident.get("top") or {}
    key = f"{job_id}:{action}"
    company_key = f"cd:{top['id']}" if top.get("id") else (req.get("domain") or req.get("name"))

    db = store.db
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute("SELECT 1 FROM actions WHERE idempotency_key=?", (key,)).fetchone():
            db.execute("ROLLBACK")
            return f"{job_id}: {action} already done; skipped."
        payload = {"company_key": company_key, "name": top.get("name") or req.get("name"),
                   "domain": req.get("domain") or top.get("domain"), "verdict": job["verdict"], "approved_by": by}
        db.execute("INSERT INTO actions (idempotency_key, job_id, action, payload_json, done_at) VALUES (?,?,?,?,?)",
                   (key, job_id, action, json.dumps(payload), now_iso()))
        decision = "approved" if action == "add_to_approved_vendors" else "rejected"
        for register_key in {company_key, payload["domain"]} - {None, ""}:
            db.execute("INSERT OR REPLACE INTO vendor_register (company_key, name, domain, decision, job_id, "
                       "decided_at) VALUES (?,?,?,?,?,?)",
                       (register_key, payload["name"], payload["domain"], decision, job_id, now_iso()))
        db.execute("INSERT OR IGNORE INTO outbox (idempotency_key, to_team, body, created_at) VALUES (?,?,?,?)",
                   (key + ":notify", "procurement-team",
                    f"{payload['name']} ({payload['domain']}): {action} approved by {by}.", now_iso()))
        db.execute("UPDATE approvals SET status='approved', decided_by=?, decided_action=?, decided_at=? "
                   "WHERE job_id=?", (by, action, now_iso(), job_id))
        db.execute("UPDATE jobs SET status='closed' WHERE id=?", (job_id,))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return f"{job_id}: {action} done, approved by {by}."
