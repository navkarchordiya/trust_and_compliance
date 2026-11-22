"""Command line.

  python -m vendorcheck run --name "Acme Logistics" --website acme.in
  python -m vendorcheck run --text "Here is a new vendor: 'Acme Logistics', acme.in ..."
  python -m vendorcheck resume            # resume every unfinished run (after a crash or kill)
  python -m vendorcheck show JOB [--json]
  python -m vendorcheck approvals         # what is waiting for a person
  python -m vendorcheck approve JOB --by "Vedant" [--action add_to_approved_vendors|reject_vendor]
  python -m vendorcheck decline JOB --by "Vedant"
  python -m vendorcheck ledger JOB        # every call, its status and its cost
  python -m vendorcheck raw CALL_ID       # the stored raw response behind a fact
  python -m vendorcheck batch FILE        # JSON or CSV of requests; restart-safe
  python -m vendorcheck eval FILE         # batch + compare with each row's expected verdict
  python -m vendorcheck outbox            # internal notifications written so far
  python -m vendorcheck import-vendors F  # the client's existing vendor list (CSV/JSON): duplicates
  python -m vendorcheck costs             # measured cost per job, across all runs
  python -m vendorcheck purge --days 30   # delete stored raw responses of closed jobs (retention)
  python -m vendorcheck log JOB           # the run's event log
"""
import argparse
import json
import os
import sys

from .actions import decide_approval
from .config import load_config, load_env
from .pipeline import Pipeline, new_job
from .batch import print_eval, run_batch
from .report import build_result, render_text, save_result
from .store import Store


def main(argv=None):
    load_env()
    cfg = load_config()
    ap = argparse.ArgumentParser(prog="vendorcheck")
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--name")
    r.add_argument("--website")
    r.add_argument("--text")
    r.add_argument("--gstin", help="GSTIN from the vendor's onboarding form (optional)")
    r.add_argument("--json", action="store_true")
    sub.add_parser("resume")
    s = sub.add_parser("show")
    s.add_argument("job")
    s.add_argument("--json", action="store_true")
    sub.add_parser("approvals")
    a = sub.add_parser("approve")
    a.add_argument("job")
    a.add_argument("--by", required=True)
    a.add_argument("--action")
    a.add_argument("--reason", help="required when choosing a different action than recommended")
    d = sub.add_parser("decline")
    d.add_argument("job")
    d.add_argument("--by", required=True)
    d.add_argument("--reason")
    l = sub.add_parser("ledger")
    l.add_argument("job")
    w = sub.add_parser("raw")
    w.add_argument("call_id", type=int)
    b = sub.add_parser("batch")
    b.add_argument("file")
    e = sub.add_parser("eval")
    e.add_argument("file")
    sub.add_parser("outbox")
    iv = sub.add_parser("import-vendors")
    iv.add_argument("file")
    sub.add_parser("costs")
    pg = sub.add_parser("purge")
    pg.add_argument("--days", type=float, default=cfg.get("privacy", {}).get("retention_days", 30))
    lg = sub.add_parser("log")
    lg.add_argument("job")
    args = ap.parse_args(argv)

    store = Store(args.db)
    pipe = Pipeline(store, cfg, log=lambda m: print(m, file=sys.stderr))

    def show(job_id, as_json=False):
        save_result(store, cfg, job_id)
        res = build_result(store, cfg, job_id)
        print(json.dumps(res, indent=2, ensure_ascii=False) if as_json else render_text(res))

    if args.cmd in ("run", "resume", "batch", "eval") and not os.environ.get("CRUSTDATA_API_KEY"):
        print("WARNING: CRUSTDATA_API_KEY is not set (copy .env.example to .env). "
              "Crustdata checks will come back as 'cannot verify'.", file=sys.stderr)

    if args.cmd == "run":
        if not (args.name or args.website or args.text):
            ap.error("give --name/--website or --text")
        job_id = new_job(store, {k: v for k, v in
                                 {"name": args.name, "website": args.website, "text": args.text,
                                  "gstin": args.gstin}.items() if v})
        print(f"started {job_id}", file=sys.stderr)
        pipe.run(job_id)
        show(job_id, args.json)
    elif args.cmd == "resume":
        jobs = [j for j in store.jobs("running") if j["stage"] != "done"]
        if not jobs:
            print("nothing to resume", file=sys.stderr)
        for j in jobs:
            print(f"resuming {j['id']} at stage {j['stage']}", file=sys.stderr)
            pipe.run(j["id"])
            show(j["id"])
    elif args.cmd == "show":
        show(args.job, args.json)
    elif args.cmd == "approvals":
        for row in store.db.execute("SELECT a.*, j.verdict FROM approvals a JOIN jobs j ON j.id=a.job_id "
                                    "WHERE a.status='pending'"):
            print(f"{row['job_id']}  {row['verdict']:<17} recommend {row['recommended_action']:<24} {row['reason']}")
    elif args.cmd == "approve":
        print(decide_approval(store, args.job, args.by, args.action, reason=args.reason))
    elif args.cmd == "decline":
        print(decide_approval(store, args.job, args.by, decline=True, reason=args.reason))
    elif args.cmd == "import-vendors":
        from .batch import import_vendors
        print(import_vendors(store, args.file))
    elif args.cmd == "costs":
        from .report import cost_summary
        print(cost_summary(store, cfg))
    elif args.cmd == "purge":
        n = store.purge_raw_responses(args.days)
        print(f"deleted the raw responses of {n} calls from closed jobs older than {args.days:g} days "
              "(facts, quotes and sources are kept)")
    elif args.cmd == "log":
        path = store.log_dir / f"{args.job}.jsonl"
        print(path.read_text() if path.exists() else "no log for that job")
    elif args.cmd == "ledger":
        total = 0.0
        for c in store.ledger(args.job):
            total += c["credits"]
            print(f"#{c['id']:<4} {c['source']:<10} {c['endpoint']:<20} {c['status']:<15} "
                  f"{c['credits']:>5} cr ({c['credits_source'] or '-':<9})  {c['attempts']} try  "
                  f"{c['latency_ms'] or 0:>6} ms  {c['retrieved_at']}")
        inr = round(total * cfg.get("cost", {}).get("inr_per_credit", 0), 2)
        print(f"total {round(total, 3)} credits  ≈ ₹{inr} (at ₹{cfg.get('cost', {}).get('inr_per_credit')}/credit)")
    elif args.cmd == "batch":
        run_batch(store, cfg, pipe, args.file, log=lambda m: print(m, file=sys.stderr))
    elif args.cmd == "eval":
        sys.exit(print_eval(run_batch(store, cfg, pipe, args.file, log=lambda m: print(m, file=sys.stderr))))
    elif args.cmd == "outbox":
        for row in store.db.execute("SELECT * FROM outbox ORDER BY created_at"):
            print(f"{row['created_at']}  to {row['to_team']:<20} {row['body']}")
    elif args.cmd == "raw":
        c = store.call(args.call_id)
        if not c:
            sys.exit("no such call")
        print(json.dumps({k: c[k] for k in ("id", "source", "endpoint", "status", "retrieved_at", "request_json")},
                         indent=2))
        print(c["response_text"] or c["error"])


if __name__ == "__main__":
    main()
