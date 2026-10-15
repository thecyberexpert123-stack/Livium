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

#: Bumped when the schema gains something an older database must be told about.
SCHEMA_VERSION = "4"

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
    promoted_by TEXT,
    description TEXT,
    tools TEXT NOT NULL DEFAULT '[]',
    dependencies TEXT NOT NULL DEFAULT '[]',
    required_permissions TEXT NOT NULL DEFAULT '[]',
    implementation_ref TEXT,
    limitations TEXT,
    version INTEGER NOT NULL DEFAULT 1
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

-- Learning-efficiency ledger: one row per finished task. Every field is a real
-- measurement or an explicitly-labelled estimate; nothing here is inferred from a
-- model's own account of what it did.
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    category TEXT,
    objective_chars INTEGER,
    tools_exposed INTEGER,
    tools_available INTEGER,
    capabilities_selected TEXT NOT NULL DEFAULT '[]',
    memory_ids TEXT NOT NULL DEFAULT '[]',
    capability_reused INTEGER NOT NULL DEFAULT 0,
    new_development INTEGER NOT NULL DEFAULT 0,
    experience_influenced INTEGER NOT NULL DEFAULT 0,
    experience_evidence TEXT NOT NULL DEFAULT '{}',
    model_calls INTEGER NOT NULL DEFAULT 0,
    model_failures INTEGER NOT NULL DEFAULT 0,
    prompt_tokens_actual INTEGER,
    completion_tokens_actual INTEGER,
    usage_reported_calls INTEGER NOT NULL DEFAULT 0,
    tokens_estimated INTEGER,
    tools_executed INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    denials INTEGER NOT NULL DEFAULT 0,
    approvals_requested INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    route_decisions TEXT NOT NULL DEFAULT '[]',
    verification_state TEXT,
    status TEXT,
    duration_ms INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_task ON ledger(task_id);
CREATE INDEX IF NOT EXISTS ledger_category ON ledger(category);

-- Human authorization queue. A row exists only because the permission engine
-- returned 'approval_required' for a concrete operation; only a human moves it to
-- 'approved' or 'rejected', and only an approved row lets that same operation run.
CREATE TABLE IF NOT EXISTS approval (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS approval_status ON approval(status);

CREATE TABLE IF NOT EXISTS guardian_baseline (
    path TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

-- Deferred discoveries: observations captured during a task that are not that
-- task, kept so they can be evaluated after it, instead of derailing it now.
-- `status` is constrained by the transition table in livium.discovery, and the
-- provenance columns exist so that content read from an inspected file can never be
-- mistaken for an instruction the agent gave itself.
CREATE TABLE IF NOT EXISTS discovery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    title TEXT NOT NULL,
    observation TEXT NOT NULL,
    why_it_matters TEXT NOT NULL DEFAULT '',
    potential_benefit TEXT NOT NULL DEFAULT '',
    source_task TEXT,
    source_step INTEGER,
    source_tool TEXT,
    source_evidence TEXT NOT NULL DEFAULT '{}',
    provenance TEXT NOT NULL DEFAULT 'agent_observation',
    confidence REAL NOT NULL DEFAULT 0.5,
    uncertainty TEXT NOT NULL DEFAULT '',
    effort_estimate TEXT,
    dependencies TEXT NOT NULL DEFAULT '[]',
    risk TEXT NOT NULL DEFAULT 'unknown',
    permissions_required TEXT NOT NULL DEFAULT '[]',
    touches_protected TEXT NOT NULL DEFAULT '[]',
    related TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    spawned_by INTEGER,
    depth INTEGER NOT NULL DEFAULT 0,
    history TEXT NOT NULL DEFAULT '[]',
    outcome TEXT,
    verification_evidence TEXT,
    flagged TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS discovery_status ON discovery(status, updated_at);
CREATE INDEX IF NOT EXISTS discovery_task ON discovery(source_task);
CREATE INDEX IF NOT EXISTS discovery_fingerprint ON discovery(fingerprint);

-- Integrity events: the authoritative record of what was detected, decided and
-- done about protected components. Append-only, enforced by triggers rather than
-- by convention, because the row a tamper attempt would most like to rewrite is
-- the row describing the tamper attempt.
CREATE TABLE IF NOT EXISTS integrity_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    tier TEXT,
    paths TEXT NOT NULL DEFAULT '[]',
    observed TEXT NOT NULL DEFAULT '{}',
    expected TEXT NOT NULL DEFAULT '{}',
    detection TEXT NOT NULL DEFAULT 'unknown',
    policy TEXT NOT NULL DEFAULT '{}',
    decision TEXT,
    authorization TEXT,
    evidence TEXT NOT NULL DEFAULT '{}',
    recovery TEXT,
    outcome TEXT NOT NULL DEFAULT 'recorded',
    uncertainty TEXT NOT NULL DEFAULT '',
    task_id TEXT,
    attributable_to TEXT,
    tool TEXT,
    revision TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS integrity_event_kind ON integrity_event(event, ts);
CREATE TRIGGER IF NOT EXISTS integrity_event_no_update
BEFORE UPDATE ON integrity_event
BEGIN
    SELECT RAISE(ABORT, 'integrity_event is append-only: a security record cannot be edited');
END;
CREATE TRIGGER IF NOT EXISTS integrity_event_no_delete
BEFORE DELETE ON integrity_event
BEGIN
    SELECT RAISE(ABORT, 'integrity_event is append-only: a security record cannot be deleted');
END;

-- One snapshot per baselined digest of a protected file: the known-good content
-- that a change is compared against and that recovery can restore. Kept in the
-- database rather than beside the file, so it is not writable by the agent's tools.
CREATE TABLE IF NOT EXISTS integrity_snapshot (
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    content BLOB NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (path, sha256)
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
        """Create what is missing, then add columns that older databases lack.

        ``CREATE TABLE IF NOT EXISTS`` covers new tables but silently does nothing
        for a table that already exists without a newer column, so every additive
        column is checked explicitly. No column is ever dropped or rewritten: a
        database from an earlier build keeps working and keeps its history.
        """

        with self._lock:
            self._conn.executescript(SCHEMA)
            self._add_missing_columns()
            self._conn.execute(
                # Only written when it actually changes, so opening the database twice
                # leaves the row (and its timestamp) untouched.
                "INSERT INTO meta(key, value, updated_at) VALUES('schema_version', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at WHERE value IS NOT excluded.value",
                (SCHEMA_VERSION, now()),
            )

    #: Additive columns introduced after the first schema. (table, column, definition)
    ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("capability", "description", "TEXT"),
        ("capability", "tools", "TEXT NOT NULL DEFAULT '[]'"),
        ("capability", "dependencies", "TEXT NOT NULL DEFAULT '[]'"),
        ("capability", "required_permissions", "TEXT NOT NULL DEFAULT '[]'"),
        ("capability", "implementation_ref", "TEXT"),
        ("capability", "limitations", "TEXT"),
        ("capability", "version", "INTEGER NOT NULL DEFAULT 1"),
        # Ledger columns added after the table's first definition: a database created
        # in between has the table without them, so they must be added explicitly.
        # (Found by running the migration against a database created minutes earlier.)
        ("ledger", "usage_reported_calls", "INTEGER NOT NULL DEFAULT 0"),
        ("ledger", "approvals_requested", "INTEGER NOT NULL DEFAULT 0"),
        ("ledger", "experience_evidence", "TEXT NOT NULL DEFAULT '{}'"),
        # Discovery and ledger columns added when deferred work was introduced.
        ("discovery", "flagged", "TEXT NOT NULL DEFAULT '[]'"),
        ("discovery", "touches_protected", "TEXT NOT NULL DEFAULT '[]'"),
        ("ledger", "discoveries_captured", "INTEGER NOT NULL DEFAULT 0"),
        ("ledger", "followups_investigated", "INTEGER NOT NULL DEFAULT 0"),
        # Added when the follow-up budget started refusing charges mid-investigation:
        # an attempt that the budget stopped is not a conclusion, and counting the two
        # in one column made the ledger disagree with the record it was describing.
        ("ledger", "followups_stopped", "INTEGER NOT NULL DEFAULT 0"),
    )

    def _add_missing_columns(self) -> None:
        for table, column, definition in self.ADDITIVE_COLUMNS:
            existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue  # table absent in this build; nothing to alter
            if column in existing:
                continue
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
