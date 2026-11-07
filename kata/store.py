"""SQLite persistence for observed history, compiled katas, versions, runs and audit.

One file, WAL mode, shared by the agent process (writer) and the dashboard API
(reader and approval writer). Every state change on a kata is appended to ``audit``
so a user can always answer "who turned this on, when, and what did it run".
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .models import KataSpec, Status, ToolEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args TEXT NOT NULL,
    result TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    ended_at REAL NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_turn ON events(conversation_id, turn_id, id);
CREATE INDEX IF NOT EXISTS events_time ON events(ended_at);

CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    duration_s REAL NOT NULL,
    total_tokens INTEGER NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS api_turn ON api_calls(turn_id, started_at);

CREATE TABLE IF NOT EXISTS katas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signature TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    active_version INTEGER,
    status_reason TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kata_versions (
    kata_id INTEGER NOT NULL REFERENCES katas(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    spec TEXT NOT NULL,
    validation TEXT NOT NULL,
    created_at REAL NOT NULL,
    approved_at REAL,
    PRIMARY KEY (kata_id, version)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kata_id INTEGER NOT NULL REFERENCES katas(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    conversation_id TEXT NOT NULL,
    ok INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    steps_run INTEGER NOT NULL,
    failed_step INTEGER,
    error TEXT,
    trace TEXT NOT NULL,
    started_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_kata ON runs(kata_id, started_at);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kata_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, ended_at REAL NOT NULL);
"""


SCHEMA_VERSION = 2

# Forward-only migrations: MIGRATIONS[n] upgrades a database at version n to n + 1.
# Each runs in one transaction. Never edit a released migration; append a new one.
MIGRATIONS: dict[int, str] = {
    # v2: order tool calls and model requests by a shared sequence instead of wall-clock time,
    # whose resolution is too coarse on some platforms to attribute requests between steps.
    1: """
    ALTER TABLE events ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE api_calls ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
    """,
}
INDEXES = """
CREATE INDEX IF NOT EXISTS events_seq ON events(seq);
CREATE INDEX IF NOT EXISTS api_seq ON api_calls(turn_id, seq);
"""


class StoreError(Exception):
    pass


class Store:
    def __init__(self, conn: sqlite3.Connection | None = None,
                 factory: Callable[[], sqlite3.Connection] | None = None):
        """Wrap an open connection, or a ``factory`` that opens one on first use.

        Lazy opening matters inside Hermes: loading the plugin (including Hermes's own
        validation probes) must not create or lock the database until Kata needs it."""
        self._lock = threading.RLock()
        self._raw: sqlite3.Connection | None = None
        self._factory = factory
        if conn is not None:
            self._init(conn)

    @classmethod
    def lazy(cls, factory: Callable[[], sqlite3.Connection]) -> Store:
        return cls(factory=factory)

    def _init(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            self._raw = conn
            conn.execute("PRAGMA busy_timeout=5000")  # dashboard and agent may write concurrently
            conn.executescript(SCHEMA)
            conn.commit()
            self._migrate()

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._raw is None:
            with self._lock:
                if self._raw is None:
                    if self._factory is None:
                        raise StoreError("store is closed")
                    self._init(self._factory())
        return self._raw

    def _migrate(self) -> None:
        rows = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchall()
        if rows:
            current = int(rows[0][0])
        else:  # unversioned: a fresh database, or one written before versioning existed
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(events)")}
            current = SCHEMA_VERSION if "seq" in cols else 1
        if current > SCHEMA_VERSION:
            raise StoreError(f"Kata database is schema v{current}, newer than this plugin (v{SCHEMA_VERSION}). "
                             "Update the plugin; your data is untouched.")
        while current < SCHEMA_VERSION:
            with self._conn:
                self._conn.executescript(MIGRATIONS[current])
            current += 1
        self._conn.executescript(INDEXES)
        self._conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(current),))
        self._conn.commit()

    @classmethod
    def open_path(cls, path: Path) -> Store:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return cls(conn)

    def release(self) -> None:
        """Close the file handle if this store can reopen it on demand (lazy stores only)."""
        if self._factory is not None:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._raw is not None:
                self._raw.close()
                self._raw = None

    # -- internals ---------------------------------------------------------
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # -- generation counter: lets other processes see that katas changed ---
    def bump_generation(self) -> int:
        gen = self.generation() + 1
        self._exec("INSERT INTO meta(key, value) VALUES('generation', ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(gen),))
        return gen

    def generation(self) -> int:
        rows = self._query("SELECT value FROM meta WHERE key='generation'")
        return int(rows[0][0]) if rows else 0

    # -- shared sequence: total order of tool calls and model requests -----
    _NEXT_SEQ = ("SELECT COALESCE(MAX(m), 0) + 1 FROM (SELECT MAX(seq) AS m FROM events "
                 "UNION ALL SELECT MAX(seq) FROM api_calls)")

    # -- events ------------------------------------------------------------
    def add_event(self, *, conversation_id: str, session_id: str, turn_id: str, tool_name: str,
                  args: dict, result: str, status: str, duration_ms: int, ended_at: float) -> int:
        cur = self._exec(
            "INSERT INTO events(conversation_id, session_id, turn_id, tool_name, args, result, status,"
            f" duration_ms, ended_at, seq) VALUES (?,?,?,?,?,?,?,?,?,({self._NEXT_SEQ}))",
            (conversation_id, session_id, turn_id, tool_name, json.dumps(args, sort_keys=True, default=str),
             result, status, int(duration_ms), ended_at),
        )
        return int(cur.lastrowid)

    def events(self, since: float = 0.0, limit: int = 0) -> list[ToolEvent]:
        """Events since ``since``; with ``limit``, only the most recent ``limit`` of them."""
        where = "ended_at >= ?"
        params: tuple = (since,)
        if limit:
            where += " AND id >= COALESCE((SELECT id FROM events WHERE ended_at >= ? ORDER BY id DESC LIMIT 1 OFFSET ?), 0)"
            params = (since, since, limit - 1)
        rows = self._query(
            "SELECT id, conversation_id, session_id, turn_id, tool_name, args, result, status, duration_ms,"
            f" ended_at, seq FROM events WHERE {where} ORDER BY conversation_id, turn_id, id", params)
        return [ToolEvent(r[0], r[1], r[2], r[3], r[4], json.loads(r[5]), r[6], r[7], r[8], r[9], r[10]) for r in rows]

    def events_by_ids(self, ids: list[int]) -> list[ToolEvent]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        rows = self._query(
            "SELECT id, conversation_id, session_id, turn_id, tool_name, args, result, status, duration_ms,"
            f" ended_at, seq FROM events WHERE id IN ({marks}) ORDER BY id", ids)
        return [ToolEvent(r[0], r[1], r[2], r[3], r[4], json.loads(r[5]), r[6], r[7], r[8], r[9], r[10]) for r in rows]

    def event_count(self) -> int:
        return int(self._query("SELECT COUNT(*) FROM events")[0][0])

    def prune(self, older_than: float) -> int:
        """Drop observation data past retention. Katas, versions and the audit log are kept;
        run traces are kept for the latest 500 runs per kata."""
        keep: set[int] = set()
        for (spec,) in self._query("SELECT spec FROM kata_versions"):
            for occ in json.loads(spec).get("evidence", {}).get("example_event_ids", []):
                keep.update(int(i) for i in occ)
        with self._lock:
            self._conn.execute("CREATE TEMP TABLE IF NOT EXISTS kata_keep(id INTEGER PRIMARY KEY)")
            self._conn.execute("DELETE FROM kata_keep")
            self._conn.executemany("INSERT INTO kata_keep(id) VALUES (?)", [(i,) for i in keep])
            cur = self._conn.execute("DELETE FROM events WHERE ended_at < ? AND id NOT IN (SELECT id FROM kata_keep)",
                                     (older_than,))
            self._conn.commit()
        self._exec("DELETE FROM api_calls WHERE started_at < ?", (older_than,))
        self._exec("DELETE FROM runs WHERE id IN (SELECT id FROM (SELECT id, ROW_NUMBER() OVER "
                   "(PARTITION BY kata_id ORDER BY id DESC) AS rn FROM runs) WHERE rn > 500)")
        return cur.rowcount

    # -- sessions: which work is finished and safe to learn from ----------
    def end_session(self, session_id: str) -> None:
        if session_id:
            self._exec("INSERT INTO sessions(session_id, ended_at) VALUES (?, ?) ON CONFLICT(session_id) "
                       "DO UPDATE SET ended_at=excluded.ended_at", (session_id, time.time()))

    def ended_sessions(self) -> set[str]:
        return {r[0] for r in self._query("SELECT session_id FROM sessions")}

    # -- api calls (for measuring the model overhead a kata removes) -------
    def add_api_call(self, *, session_id: str, turn_id: str, started_at: float, duration_s: float,
                     total_tokens: int) -> None:
        self._exec("INSERT INTO api_calls(session_id, turn_id, started_at, duration_s, total_tokens, seq)"
                   f" VALUES (?,?,?,?,?,({self._NEXT_SEQ}))",
                   (session_id, turn_id, started_at, duration_s, int(total_tokens)))

    def api_calls_between(self, turn_id: str, after_seq: int, before_seq: int) -> list[tuple[float, float, int]]:
        """Model requests that completed strictly between two recorded tool calls of one turn."""
        return [tuple(r) for r in self._query(
            "SELECT started_at, duration_s, total_tokens FROM api_calls WHERE turn_id=? AND seq>? AND seq<?",
            (turn_id, after_seq, before_seq))]

    # -- katas -------------------------------------------------------------
    def kata_by_signature(self, signature: str) -> dict | None:
        rows = self._query("SELECT id FROM katas WHERE signature=?", (signature,))
        return self.kata(rows[0][0]) if rows else None

    def kata_by_name(self, name: str) -> dict | None:
        rows = self._query("SELECT id FROM katas WHERE name=?", (name,))
        return self.kata(rows[0][0]) if rows else None

    def kata(self, kata_id: int) -> dict | None:
        rows = self._query(
            "SELECT id, signature, name, status, active_version, status_reason, consecutive_failures,"
            " created_at, updated_at FROM katas WHERE id=?", (kata_id,))
        if not rows:
            return None
        r = rows[0]
        return {"id": r[0], "signature": r[1], "name": r[2], "status": r[3], "active_version": r[4],
                "status_reason": r[5], "consecutive_failures": r[6], "created_at": r[7], "updated_at": r[8]}

    def katas(self, status: str | None = None) -> list[dict]:
        if status:
            ids = self._query("SELECT id FROM katas WHERE status=? ORDER BY id", (status,))
        else:
            ids = self._query("SELECT id FROM katas ORDER BY id")
        return [k for (i,) in ids if (k := self.kata(i))]

    def add_candidate(self, spec: KataSpec, validation: dict) -> int:
        now = time.time()
        cur = self._exec(
            "INSERT INTO katas(signature, name, status, active_version, created_at, updated_at)"
            " VALUES (?,?,?,NULL,?,?)", (spec.signature, spec.name, Status.CANDIDATE.value, now, now))
        kata_id = int(cur.lastrowid)
        self.add_version(kata_id, spec, validation)
        self.audit(kata_id, "proposed", "kata", f"support={spec.evidence.support} "
                   f"confidence={spec.evidence.confidence:.2f}")
        return kata_id

    def add_version(self, kata_id: int, spec: KataSpec, validation: dict) -> int:
        rows = self._query("SELECT COALESCE(MAX(version), 0) FROM kata_versions WHERE kata_id=?", (kata_id,))
        version = int(rows[0][0]) + 1
        self._exec("INSERT INTO kata_versions(kata_id, version, spec, validation, created_at) VALUES (?,?,?,?,?)",
                   (kata_id, version, json.dumps(spec.to_json()), json.dumps(validation), time.time()))
        return version

    def update_version_spec(self, kata_id: int, version: int, spec: dict) -> None:
        """Metadata-only update of a stored version (tool schema fingerprints)."""
        self._exec("UPDATE kata_versions SET spec=? WHERE kata_id=? AND version=?",
                   (json.dumps(spec), kata_id, version))

    def versions(self, kata_id: int) -> list[dict]:
        rows = self._query("SELECT version, spec, validation, created_at, approved_at FROM kata_versions"
                           " WHERE kata_id=? ORDER BY version", (kata_id,))
        return [{"version": r[0], "spec": json.loads(r[1]), "validation": json.loads(r[2]),
                 "created_at": r[3], "approved_at": r[4]} for r in rows]

    def latest_version(self, kata_id: int) -> dict | None:
        v = self.versions(kata_id)
        return v[-1] if v else None

    def version(self, kata_id: int, version: int) -> dict | None:
        for v in self.versions(kata_id):
            if v["version"] == version:
                return v
        return None

    def set_status(self, kata_id: int, status: Status, *, actor: str, reason: str = "",
                   active_version: int | None = None) -> None:
        if active_version is None:
            self._exec("UPDATE katas SET status=?, status_reason=?, updated_at=? WHERE id=?",
                       (status.value, reason, time.time(), kata_id))
        else:
            self._exec("UPDATE katas SET status=?, status_reason=?, active_version=?, updated_at=? WHERE id=?",
                       (status.value, reason, active_version, time.time(), kata_id))
        self.audit(kata_id, status.value, actor, reason)
        self.bump_generation()

    def mark_approved(self, kata_id: int, version: int, actor: str) -> None:
        self._exec("UPDATE kata_versions SET approved_at=? WHERE kata_id=? AND version=?",
                   (time.time(), kata_id, version))
        self.set_status(kata_id, Status.ACTIVE, actor=actor, reason=f"approved v{version}",
                        active_version=version)

    def rename(self, kata_id: int, name: str) -> None:
        self._exec("UPDATE katas SET name=?, updated_at=? WHERE id=?", (name, time.time(), kata_id))

    def record_failure(self, kata_id: int) -> int:
        self._exec("UPDATE katas SET consecutive_failures = consecutive_failures + 1 WHERE id=?", (kata_id,))
        return int(self._query("SELECT consecutive_failures FROM katas WHERE id=?", (kata_id,))[0][0])

    def reset_failures(self, kata_id: int) -> None:
        self._exec("UPDATE katas SET consecutive_failures = 0 WHERE id=?", (kata_id,))

    # -- runs --------------------------------------------------------------
    def add_run(self, *, kata_id: int, version: int, conversation_id: str, ok: bool, duration_ms: int,
                steps_run: int, failed_step: int | None, error: str | None, trace: dict,
                started_at: float) -> int:
        cur = self._exec(
            "INSERT INTO runs(kata_id, version, conversation_id, ok, duration_ms, steps_run, failed_step, error,"
            " trace, started_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (kata_id, version, conversation_id, int(ok), duration_ms, steps_run, failed_step, error,
             json.dumps(trace, default=str), started_at))
        return int(cur.lastrowid)

    def runs(self, kata_id: int | None = None, limit: int = 50) -> list[dict]:
        sql = ("SELECT id, kata_id, version, conversation_id, ok, duration_ms, steps_run, failed_step, error,"
               " trace, started_at FROM runs")
        params: tuple = ()
        if kata_id is not None:
            sql += " WHERE kata_id=?"
            params = (kata_id,)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self._query(sql, params + (limit,))
        return [{"id": r[0], "kata_id": r[1], "version": r[2], "conversation_id": r[3], "ok": bool(r[4]),
                 "duration_ms": r[5], "steps_run": r[6], "failed_step": r[7], "error": r[8],
                 "trace": json.loads(r[9]), "started_at": r[10]} for r in rows]

    def run_stats(self, kata_id: int) -> dict:
        rows = self._query("SELECT COUNT(*), COALESCE(SUM(ok),0), COALESCE(AVG(duration_ms),0) FROM runs"
                           " WHERE kata_id=?", (kata_id,))
        n, ok, avg = rows[0]
        return {"runs": int(n), "ok": int(ok), "avg_duration_ms": float(avg)}

    # -- audit -------------------------------------------------------------
    def audit(self, kata_id: int | None, action: str, actor: str, detail: str) -> None:
        self._exec("INSERT INTO audit(kata_id, action, actor, detail, at) VALUES (?,?,?,?,?)",
                   (kata_id, action, actor, detail, time.time()))

    def audit_log(self, kata_id: int | None = None, limit: int = 100) -> list[dict]:
        if kata_id is None:
            rows = self._query("SELECT id, kata_id, action, actor, detail, at FROM audit ORDER BY id DESC LIMIT ?",
                               (limit,))
        else:
            rows = self._query("SELECT id, kata_id, action, actor, detail, at FROM audit WHERE kata_id=?"
                               " ORDER BY id DESC LIMIT ?", (kata_id, limit))
        return [{"id": r[0], "kata_id": r[1], "action": r[2], "actor": r[3], "detail": r[4], "at": r[5]}
                for r in rows]
