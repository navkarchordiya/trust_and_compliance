#!/usr/bin/env bash
# Kill-on-stage demo: start a vendor check, kill -9 it mid-run, resume, then approve twice.
# Shows: the run resumes where it stopped, no call is paid twice, and the action happens once.
#
#   ./scripts/kill_demo.sh delhivery.com "Delhivery Limited" 6
set -euo pipefail
PY="${PYTHON:-python3}"
WEBSITE="${1:-delhivery.com}"
NAME="${2:-Delhivery Limited}"
KILL_AFTER="${3:-6}"                       # seconds before the kill
DB="data/kill_demo.db"
rm -f "$DB" "$DB"-*

echo "== start: $NAME ($WEBSITE); killing it after ${KILL_AFTER}s"
"$PY" -m vendorcheck --db "$DB" run --name "$NAME" --website "$WEBSITE" > /dev/null 2>&1 &
PID=$!
sleep "$KILL_AFTER"
kill -9 "$PID" 2>/dev/null && echo "== killed (kill -9) pid $PID" || echo "== run had already finished"

JOB=$("$PY" - "$DB" <<'PY'
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute("SELECT id FROM jobs ORDER BY started_at DESC LIMIT 1").fetchone()[0])
PY
)
echo "== job $JOB stopped at stage: $(sqlite3 "$DB" "SELECT stage FROM jobs WHERE id='$JOB'" 2>/dev/null || echo '?')"
echo "== resume"
"$PY" -m vendorcheck --db "$DB" resume | tail -5
echo "== ledger: each call once, completed calls not paid again"
"$PY" -m vendorcheck --db "$DB" ledger "$JOB"
echo "== approve twice: the action happens exactly once"
"$PY" -m vendorcheck --db "$DB" approve "$JOB" --by "Demo Approver" --action "$(sqlite3 "$DB" "SELECT CASE recommended_action WHEN 'manual_review' THEN 'add_to_approved_vendors' ELSE recommended_action END FROM approvals WHERE job_id='$JOB'" 2>/dev/null || echo add_to_approved_vendors)" --reason "demo" || true
"$PY" -m vendorcheck --db "$DB" approve "$JOB" --by "Demo Approver" || true
"$PY" -m vendorcheck --db "$DB" outbox
