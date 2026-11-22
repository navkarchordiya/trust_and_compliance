"""SQLite job store. Everything a run needs to resume after a kill lives here."""
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    request_json TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,              -- running | pending_approval | closed
    verdict TEXT,
    result_json TEXT,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stage_outputs (
    job_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    output_json TEXT NOT NULL,
    PRIMARY KEY (job_id, stage)
);
-- One row per source call: the raw response is the evidence, and this is the cost ledger.
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL,              -- FOUND | NOT_FOUND | UNAVAILABLE | ERROR | SKIPPED_BUDGET
    http_status INTEGER,
    response_text TEXT,
    error TEXT,
    credits REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    retrieved_at TEXT NOT NULL,
    UNIQUE (job_id, request_key)
);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    claim TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_url TEXT NOT NULL,
    locator TEXT NOT NULL,             -- JSON field path, or "passage"
    quote TEXT NOT NULL,               -- exact field value or passage from the stored response
    retrieved_at TEXT NOT NULL,
    call_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    recommended_action TEXT NOT NULL,  -- add_to_approved_vendors | reject_vendor | manual_review
    reason TEXT NOT NULL,
    status TEXT NOT NULL,              -- pending | approved | declined
    decided_by TEXT,
    decided_action TEXT,
    decided_at TEXT
);
-- Irreversible actions. The primary key is the idempotency key, so each happens once.
CREATE TABLE IF NOT EXISTS actions (
    idempotency_key TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    done_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vendor_register (
    company_key TEXT PRIMARY KEY,      -- crustdata id or domain
    name TEXT,
    domain TEXT,
    decision TEXT NOT NULL,            -- approved | rejected
    job_id TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
-- Internal notifications only: agents stay in the room.
CREATE TABLE IF NOT EXISTS outbox (
    idempotency_key TEXT PRIMARY KEY,
    to_team TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str | None = None):
        path = Path(path) if path else ROOT / "data" / "vendorcheck.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)  # autocommit; explicit BEGIN for transactions
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first schema, so an existing database keeps working."""
        wanted = {
            "calls": {"credits_source": "TEXT"},       # reported (X-Credits-Used header) | estimated | none
            "facts": {"record_date": "TEXT"},          # the date the source gives for the record itself
        }
        for table, columns in wanted.items():
            have = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for column, kind in columns.items():
                if column not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    # ---- jobs ----
    def create_job(self, job_id: str, request: dict, first_stage: str) -> None:
        t = time.time()
        self.db.execute(
            "INSERT INTO jobs (id, request_json, stage, status, started_at, updated_at) VALUES (?,?,?,?,?,?)",
            (job_id, json.dumps(request), first_stage, "running", t, t),
        )

    def job(self, job_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def jobs(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.db.execute("SELECT * FROM jobs WHERE status=? ORDER BY started_at", (status,)).fetchall()
        return self.db.execute("SELECT * FROM jobs ORDER BY started_at").fetchall()

    def update_job(self, job_id: str, **fields) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    # ---- stage checkpoints ----
    def save_stage(self, job_id: str, stage: str, output: dict, next_stage: str) -> None:
        """Save a stage's output and advance the job in one transaction."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT OR REPLACE INTO stage_outputs (job_id, stage, output_json) VALUES (?,?,?)",
                (job_id, stage, json.dumps(output)),
            )
            self.db.execute("UPDATE jobs SET stage=?, updated_at=? WHERE id=?", (next_stage, time.time(), job_id))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def stage_output(self, job_id: str, stage: str) -> dict | None:
        row = self.db.execute(
            "SELECT output_json FROM stage_outputs WHERE job_id=? AND stage=?", (job_id, stage)
        ).fetchone()
        return json.loads(row["output_json"]) if row else None

    # ---- calls / ledger ----
    def cached_call(self, job_id: str, request_key: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM calls WHERE job_id=? AND request_key=?", (job_id, request_key)
        ).fetchone()

    def record_call(self, **row) -> int:
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        cur = self.db.execute(f"INSERT OR REPLACE INTO calls ({cols}) VALUES ({qs})", tuple(row.values()))
        return cur.lastrowid

    def call(self, call_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()

    def ledger(self, job_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT id, source, endpoint, status, credits, credits_source, attempts, latency_ms, retrieved_at "
            "FROM calls WHERE job_id=? ORDER BY id", (job_id,)
        ).fetchall()

    def credits_spent(self, job_id: str) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(credits),0) AS c FROM calls WHERE job_id=?", (job_id,)).fetchone()
        return float(row["c"])

    # ---- facts ----
    def clear_facts(self, job_id: str, stage: str) -> None:
        self.db.execute("DELETE FROM facts WHERE job_id=? AND stage=?", (job_id, stage))

    def add_fact(self, **row) -> int:
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        return self.db.execute(f"INSERT INTO facts ({cols}) VALUES ({qs})", tuple(row.values())).lastrowid

    def facts(self, job_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM facts WHERE job_id=? ORDER BY id", (job_id,)).fetchall()

    # ---- vendor register (duplicates) ----
    def register_lookup(self, keys: list[str]) -> sqlite3.Row | None:
        for k in keys:
            if not k:
                continue
            row = self.db.execute("SELECT * FROM vendor_register WHERE company_key=?", (k,)).fetchone()
            if row:
                return row
        return None

    def earlier_job_for(self, job_id: str, keys: list[str]) -> sqlite3.Row | None:
        """Another OPEN job (running or waiting for approval) that resolved to the same company.
        Closed jobs do not count: a declined check can be run again, and an approved or rejected
        vendor is caught by register_lookup instead."""
        for row in self.db.execute(
            "SELECT j.id, j.verdict, j.status, s.output_json FROM jobs j JOIN stage_outputs s "
            "ON s.job_id=j.id AND s.stage='identify' WHERE j.id<>? AND j.status<>'closed' "
            "AND j.started_at <= (SELECT started_at FROM jobs WHERE id=?) ORDER BY j.started_at",
            (job_id, job_id)
        ):
            out = json.loads(row["output_json"])
            if any(k and k in (out.get("company_keys") or []) for k in keys):
                return row
        return None
