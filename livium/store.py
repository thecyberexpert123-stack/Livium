"""Durable storage.

One SQLite database holds everything that must survive a process restart:
memory records, task continuity, the write-ahead intent log, the append-only
event journal, capability lifecycle records and context reports.

Durability choices, and why:

* ``journal_mode=WAL`` + ``synchronous=FULL`` -- a committed transaction has
  reached stable storage before the call returns. Slower; correct after a kill.
* ``event`` is append-only, enforced by database triggers rather than by
  convention, so not even Livium's own code can rewrite history.
* Consequential operations write an ``intent`` row *before* touching the
  outside world and resolve it afterwards. If the process dies in between, the
  intent stays ``pending`` and recovery can ask the real world what happened
  instead of guessing (see ``livium.recovery``).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import pathlib
import sqlite3
import threading
from typing import Any, Iterator, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Append-only journal. Triggers make this a database-enforced guarantee.
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    task_id TEXT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS event_no_update BEFORE UPDATE ON event
BEGIN SELECT RAISE(ABORT, 'event journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS event_no_delete BEFORE DELETE ON event
BEGIN SELECT RAISE(ABORT, 'event journal is append-only'); END;

CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    key TEXT,
    content TEXT NOT NULL,
    detail TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL NOT NULL DEFAULT 0.5,
    verification TEXT NOT NULL DEFAULT 'unverified',
    evidence TEXT,
    source TEXT,
    protected INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    supersedes INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS memory_active_key
    ON memory(kind, scope, key) WHERE key IS NOT NULL AND status = 'active';
CREATE INDEX IF NOT EXISTS memory_kind_status ON memory(kind, status);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(content, tags);

CREATE TABLE IF NOT EXISTS task (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    acceptance TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    next_action TEXT,
    summary TEXT,
    model TEXT,
    steps INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_step (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    evidence TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE INDEX IF NOT EXISTS task_step_task ON task_step(task_id, idx);

-- Write-ahead intent log for consequential operations.
CREATE TABLE IF NOT EXISTS intent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    target TEXT,
    expected TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS intent_status ON intent(status);

CREATE TABLE IF NOT EXISTS capability (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    requirement TEXT NOT NULL,
    rationale TEXT,
    implementation TEXT,
    tests TEXT,
    verification TEXT,
    evidence TEXT,
    reuse_analysis TEXT,
    lines INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    promoted_at TEXT,
    promoted_by TEXT
);

CREATE TABLE IF NOT EXISTS context_report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    step INTEGER,
    ts TEXT NOT NULL,
    budget INTEGER NOT NULL,
    estimated INTEGER NOT NULL,
    prompt_chars INTEGER NOT NULL,
    included TEXT NOT NULL,
    omitted TEXT NOT NULL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS context_report_task ON context_report(task_id, step);

CREATE TABLE IF NOT EXISTS guardian_baseline (
    path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verification (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    criterion TEXT NOT NULL,
    kind TEXT NOT NULL,
    spec TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT,
    ts TEXT NOT NULL
);
"""


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


class Store:
    """Thread-safe handle to the Livium database."""

    def __init__(self, path: str | pathlib.Path, *, durability: bool = True):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._durability = durability
        self._configure()
        self._migrate()

    # -- setup ------------------------------------------------------------
    def _configure(self) -> None:
        cur = self._conn
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=%s" % ("FULL" if self._durability else "NORMAL"))
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT INTO meta(key, value, updated_at) VALUES('schema_version', '1', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (now(),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transactions -----------------------------------------------------
    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic unit of work. Rolls back on any exception."""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # -- generic helpers --------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    # -- event journal ----------------------------------------------------
    def append_event(
        self, event_type: str, payload: Any = None, *, task_id: str | None = None
    ) -> int:
        body = json.dumps(payload if payload is not None else {}, default=str, sort_keys=True)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO event(ts, task_id, type, payload) VALUES(?,?,?,?)",
                (now(), task_id, event_type, body),
            )
            return int(cur.lastrowid or 0)

    def events(
        self, *, task_id: str | None = None, event_type: str | None = None, limit: int = 200
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM event WHERE 1=1"
        params: list[Any] = []
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        if event_type:
            sql += " AND type = ?"
            params.append(event_type)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self.query(sql, params)

    # -- intents (write-ahead log for side effects) -----------------------
    def record_intent(
        self, tool: str, args: Any, *, task_id: str | None = None,
        target: str | None = None, expected: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO intent(task_id, tool, args, target, expected, status, created_at) "
                "VALUES(?,?,?,?,?, 'pending', ?)",
                (task_id, tool, json.dumps(args, default=str, sort_keys=True), target,
                 expected, now()),
            )
            return int(cur.lastrowid or 0)

    def resolve_intent(self, intent_id: int, status: str, result: Any = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE intent SET status=?, result=?, resolved_at=? WHERE id=?",
                (status, json.dumps(result, default=str), now(), intent_id),
            )

    def pending_intents(self, task_id: str | None = None) -> list[sqlite3.Row]:
        if task_id:
            return self.query(
                "SELECT * FROM intent WHERE status='pending' AND task_id=? ORDER BY id",
                (task_id,),
            )
        return self.query("SELECT * FROM intent WHERE status='pending' ORDER BY id")

    # -- meta -------------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value, default=str), now()),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return default

    def stats(self) -> dict[str, int]:
        tables = (
            "memory", "event", "task", "task_step", "intent", "capability",
            "context_report", "verification",
        )
        out: dict[str, int] = {}
        for table in tables:
            row = self.one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 - fixed tuple
            out[table] = int(row["n"]) if row else 0
        return out
