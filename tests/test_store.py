"""Persistence: schema upgrades, ordering, and refusal of newer databases."""
from __future__ import annotations

import sqlite3

import pytest

from kata.store import SCHEMA_VERSION, Store, StoreError

V1_EVENTS = """
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, session_id TEXT NOT NULL,
  turn_id TEXT NOT NULL, tool_name TEXT NOT NULL, args TEXT NOT NULL, result TEXT NOT NULL, status TEXT NOT NULL,
  duration_ms INTEGER NOT NULL, ended_at REAL NOT NULL);
CREATE TABLE api_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
  started_at REAL NOT NULL, duration_s REAL NOT NULL, total_tokens INTEGER NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO events(conversation_id, session_id, turn_id, tool_name, args, result, status, duration_ms, ended_at)
  VALUES ('c', 's', 't', 'read_file', '{}', '{}', 'ok', 1, 1.0);
"""


def test_v1_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_EVENTS + "INSERT INTO meta VALUES ('schema_version', '1');")
    conn.commit()
    conn.close()
    store = Store.open_path(path)
    assert store._query("SELECT value FROM meta WHERE key='schema_version'")[0][0] == str(SCHEMA_VERSION)
    assert store.event_count() == 1  # data kept
    store.add_api_call(session_id="s", turn_id="t", started_at=2.0, duration_s=0.1, total_tokens=10)
    store.close()


def test_unversioned_legacy_database_is_detected(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_EVENTS)
    conn.commit()
    conn.close()
    store = Store.open_path(path)
    cols = {r[1] for r in store._query("PRAGMA table_info(events)")}
    assert "seq" in cols
    store.close()


def test_newer_database_is_refused_not_corrupted(tmp_path):
    path = tmp_path / "future.db"
    Store.open_path(path).close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION + 1),))
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="newer than this plugin"):
        Store.open_path(path)


def test_model_requests_are_attributed_by_order_not_clock(tmp_path):
    store = Store.open_path(tmp_path / "k.db")
    # Identical wall-clock times, as on platforms with a coarse clock.
    common = {"conversation_id": "c", "session_id": "s", "turn_id": "t", "args": {}, "result": "{}",
              "status": "ok", "duration_ms": 0, "ended_at": 100.0}
    store.add_event(tool_name="a", **common)
    store.add_api_call(session_id="s", turn_id="t", started_at=100.0, duration_s=0.5, total_tokens=700)
    store.add_event(tool_name="b", **common)
    store.add_api_call(session_id="s", turn_id="t", started_at=100.0, duration_s=0.5, total_tokens=800)
    store.add_event(tool_name="c", **common)
    first, _, last = store.events()
    assert [c[2] for c in store.api_calls_between("t", first.seq, last.seq)] == [700, 800]
    store.close()
