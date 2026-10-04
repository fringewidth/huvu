"""The kernel's own SQLite database: the event log, triggers, approvals,
conversations and the built-in source's state. Sources keep their own data
elsewhere.

One connection, used from the event loop. Every statement here is small,
and one owner on one machine never queues enough of them for blocking
to matter. WAL lets a second process (a backup, sqlite3 in a shell) read
while the kernel writes.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    -- Seconds since the epoch, for ordering and ranges. `at` keeps the
    -- local time and offset the event happened in.
    ts REAL NOT NULL,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    entity TEXT,
    summary TEXT NOT NULL,
    refs TEXT NOT NULL,
    data TEXT NOT NULL,
    labels TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_type_ts ON events (type, ts);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5 (
    summary, type, entity, content='events', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS events_fts_insert AFTER INSERT ON events BEGIN
    INSERT INTO events_fts (rowid, summary, type, entity) VALUES (new.rowid, new.summary, new.type, new.entity);
END;

CREATE TABLE IF NOT EXISTS triggers (
    id TEXT PRIMARY KEY,
    spec TEXT NOT NULL,
    status TEXT NOT NULL,
    armed INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT,
    tainted INTEGER NOT NULL,
    last_fired_at TEXT,
    fire_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    action TEXT NOT NULL,
    args TEXT NOT NULL,
    reason TEXT,
    caller TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    resolved_at TEXT,
    result TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    hermes_session TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    ask TEXT NOT NULL,
    answer TEXT NOT NULL,
    parts TEXT NOT NULL,
    ending TEXT,
    running INTEGER NOT NULL,
    hermes_session TEXT,
    -- What the owner did in the app just before this ask, as JSON.
    actions TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS turns_conversation ON turns (conversation_id, seq);

CREATE TABLE IF NOT EXISTS hermes_sessions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    conversation_id TEXT,
    trigger_id TEXT,
    tainted INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def dumps(value: Any) -> str:
    return json.dumps(value, default=_default, ensure_ascii=False)


def loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"can't store {type(value).__name__} as JSON")


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def from_iso(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)
