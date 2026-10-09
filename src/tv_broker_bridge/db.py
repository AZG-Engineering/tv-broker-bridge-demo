"""Thin, thread-safe SQLite wrapper.

One connection per ``Database`` guarded by a re-entrant lock, with explicit
``BEGIN IMMEDIATE`` transactions. ``IMMEDIATE`` takes the write lock up front,
which is what makes "check for an existing alert_id, then insert it" atomic
across threads *and* across processes sharing the same database file (for
example the CLI kill switch running next to the server).

Rule for callers: never perform network I/O while inside ``transaction()``.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    alert_id     TEXT PRIMARY KEY,
    state        TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    qty          TEXT NOT NULL,
    order_type   TEXT NOT NULL,
    limit_price  TEXT,
    reason       TEXT,
    received_at  TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    alert_id        TEXT UNIQUE REFERENCES alerts(alert_id),
    broker_order_id TEXT,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    limit_price     TEXT,
    status          TEXT NOT NULL,
    filled_qty      TEXT NOT NULL DEFAULT '0',
    avg_fill_price  TEXT,
    reject_reason   TEXT,
    source          TEXT NOT NULL DEFAULT 'bridge',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_broker_id ON orders(broker_order_id);

CREATE TABLE IF NOT EXISTS positions (
    symbol     TEXT PRIMARY KEY,
    qty        TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._depth = 0
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: we issue BEGIN/COMMIT ourselves.
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=5.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic unit of work. Re-entrant: only the outermost call commits."""
        with self._lock:
            if self._depth == 0:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if self._depth == 0:
                    self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a write statement; returns the affected row count."""
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()  # type: ignore[no-any-return]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
