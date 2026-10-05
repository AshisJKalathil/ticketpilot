"""SQLite persistence for business data, run checkpoints, traces and caches.

One file keeps the demo deployable on a single free instance. Each table is
small and append-mostly; swap for Postgres by reimplementing this module.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
  phone TEXT, tier TEXT NOT NULL, created_at TEXT NOT NULL, deleted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, status TEXT NOT NULL,
  items_json TEXT NOT NULL, total REAL NOT NULL, currency TEXT NOT NULL,
  placed_at TEXT NOT NULL, shipped_at TEXT, delivered_at TEXT,
  carrier TEXT, tracking_number TEXT, refunded_amount REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shipments (
  tracking_number TEXT PRIMARY KEY, carrier TEXT NOT NULL, status TEXT NOT NULL,
  events_json TEXT NOT NULL, portal_notes TEXT
);
CREATE TABLE IF NOT EXISTS customer_memory (
  id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, note TEXT NOT NULL,
  source_run TEXT, trusted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS past_tickets (
  id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, subject TEXT NOT NULL,
  resolution TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, status TEXT NOT NULL,
  route TEXT, model TEXT, arm TEXT, prompt_version TEXT,
  cost_usd REAL NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0, steps INTEGER NOT NULL DEFAULT 0,
  active_seconds REAL NOT NULL DEFAULT 0, state_json TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool_use_id TEXT NOT NULL, tool TEXT NOT NULL,
  args_json TEXT NOT NULL, preview_json TEXT NOT NULL, risk_notes_json TEXT NOT NULL,
  status TEXT NOT NULL, decided_by TEXT, decision_note TEXT, edited_args_json TEXT,
  created_at TEXT NOT NULL, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS spans (
  span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_id TEXT, name TEXT NOT NULL,
  kind TEXT NOT NULL, start_ts REAL NOT NULL, end_ts REAL, status TEXT NOT NULL,
  attrs_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id, start_ts);
CREATE TABLE IF NOT EXISTS llm_cache (
  key TEXT PRIMARY KEY, response_json TEXT NOT NULL, created_at TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS semantic_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL, normalized TEXT NOT NULL,
  answer_json TEXT NOT NULL, created_at REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tool_cache (
  key TEXT PRIMARY KEY, output_json TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refunds (
  id TEXT PRIMARY KEY, order_id TEXT NOT NULL, amount REAL NOT NULL, reason TEXT NOT NULL,
  idempotency_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
  id TEXT PRIMARY KEY, to_addr TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
  idempotency_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deletion_requests (
  id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, idempotency_key TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS escalations (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, reason TEXT NOT NULL, priority TEXT NOT NULL,
  summary TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, rating TEXT NOT NULL, comment TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS spend_ledger (
  day TEXT PRIMARY KEY, cost_usd REAL NOT NULL DEFAULT 0
);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Store:
    """Thread-safe SQLite wrapper (one connection per thread, WAL mode)."""

    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()  # absolute: sandboxed workers run in a temp cwd
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conn().executescript(SCHEMA)  # executescript manages its own transaction

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn().execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        row = self._conn().execute(sql, params).fetchone()
        return dict(row) if row else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.tx() as conn:
            return conn.execute(sql, params).rowcount

    # --- small helpers used across modules -------------------------------
    def add_spend(self, day: str, cost: float) -> float:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO spend_ledger(day, cost_usd) VALUES(?, ?) "
                "ON CONFLICT(day) DO UPDATE SET cost_usd = cost_usd + excluded.cost_usd",
                (day, cost),
            )
            return conn.execute("SELECT cost_usd FROM spend_ledger WHERE day=?", (day,)).fetchone()[0]

    def spend_today(self, day: str) -> float:
        row = self.one("SELECT cost_usd FROM spend_ledger WHERE day=?", (day,))
        return float(row["cost_usd"]) if row else 0.0


def dumps(obj: Any) -> str:
    """Canonical JSON (sorted keys) - stable for hashing and cache keys."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
