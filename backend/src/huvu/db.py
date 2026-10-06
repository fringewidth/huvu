"""The kernel's own SQLite database: the event log, triggers, approvals,
conversations and the built-in source's state. Sources keep their own data
elsewhere.

One connection, used from the event loop. Every statement here is small,
and one family on one machine never queues enough of them for blocking
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
    labels TEXT NOT NULL,
    -- Whose event it is. With no user, the members of group_id see it;
    -- with neither, every member of any group does.
    user_id TEXT,
    group_id TEXT,
    -- The device whose conversation it came from, if any.
    device_id TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_device_ts ON events (device_id, ts);
CREATE INDEX IF NOT EXISTS events_type_ts ON events (type, ts);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5 (
    summary, type, entity, content='events', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS events_fts_insert AFTER INSERT ON events BEGIN
    INSERT INTO events_fts (rowid, summary, type, entity) VALUES (new.rowid, new.summary, new.type, new.entity);
END;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    -- scrypt, see accounts.py.
    password TEXT NOT NULL,
    -- The API key of the user's Hermes profile.
    hermes_key TEXT NOT NULL,
    -- SHA-256 of the token the user's Hermes profile sends to /mcp.
    mcp_token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- A set of people who share devices and trust each other. The default
-- group uses Hermes's default profile and the environment's secrets, so its
-- hermes_key and mcp_token are NULL.
CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    hermes_key TEXT,
    mcp_token TEXT UNIQUE,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    group_id TEXT NOT NULL REFERENCES groups (id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    -- member: trusted with the group's events, approvals and devices.
    -- guest: uses the group's shared devices, trusts nothing.
    role TEXT NOT NULL,
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    -- NULL on a shared device.
    user_id TEXT REFERENCES users (id) ON DELETE CASCADE,
    -- The group a shared device belongs to.
    group_id TEXT REFERENCES groups (id) ON DELETE CASCADE,
    -- SHA-256 of the device's bearer token.
    token TEXT UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS triggers (
    id TEXT PRIMARY KEY,
    spec TEXT NOT NULL,
    status TEXT NOT NULL,
    armed INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT,
    -- NULL while a trigger from a shared device waits for its person.
    user_id TEXT,
    -- The group and shared device it was set up through, if any.
    group_id TEXT,
    device_id TEXT,
    -- model, user or limit, while paused. One the limit paused resumes only
    -- by its user's tap.
    paused_by TEXT,
    -- When it was last resumed; the wake limit counts from here.
    resumed_at TEXT,
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
    -- Whose phones answer it: the user's, or with no user, the members of
    -- group_id.
    user_id TEXT,
    group_id TEXT,
    status TEXT NOT NULL,
    -- While pending: the action, its args and who answers, so the same
    -- request twice (a double press) can't open a second approval.
    pending_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    resolved_at TEXT,
    result TEXT
);

-- The last reading of every pushed value, so a restart doesn't forget a
-- value that only updates when its device wakes, and triggers don't see
-- every input go unknown at once. See state.py.
CREATE TABLE IF NOT EXISTS readings (
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    -- The value's params as a JSON list of [name, value] pairs.
    params TEXT NOT NULL,
    reading TEXT NOT NULL,
    PRIMARY KEY (source, name, params)
);

CREATE TABLE IF NOT EXISTS kv (
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);

-- One conversation per device, keyed by the device's id.
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT,
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

-- Who is at a household device: a question to a user's phone, and, once
-- they say yes, their stay there. See attendance.py.
CREATE TABLE IF NOT EXISTS attendance (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    asked_by TEXT NOT NULL,
    -- Why they're asked, shown on the phone, such as the trigger being set up.
    reason TEXT,
    status TEXT NOT NULL,
    asked_at TEXT NOT NULL,
    confirmed_at TEXT,
    ended_at TEXT,
    last_active_at TEXT
);
CREATE INDEX IF NOT EXISTS attendance_device ON attendance (device_id, status);
-- One open question per person per device, so a double press asks once.
CREATE UNIQUE INDEX IF NOT EXISTS attendance_one_question ON attendance (device_id, user_id) WHERE status = 'pending';

-- Notifications sent to phones, so a phone that wasn't connected can
-- still show what it missed. Kept for NOTIFY_KEEP (kernel.py).
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    ts REAL NOT NULL,
    text TEXT NOT NULL,
    image TEXT,
    urgency TEXT NOT NULL,
    -- The app screen that deals with it, such as "triggers".
    opens TEXT,
    -- Whose phones it went to: the user's, or with no user, the members of
    -- group_id.
    user_id TEXT,
    group_id TEXT
);
CREATE INDEX IF NOT EXISTS notifications_ts ON notifications (ts);

-- Each phone's FCM token, for native push while its app is closed
-- (native_push.py).
CREATE TABLE IF NOT EXISTS push_tokens (
    token TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    -- "android" or "ios".
    platform TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS push_tokens_device ON push_tokens (device_id);

CREATE TABLE IF NOT EXISTS hermes_sessions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    -- The user it runs for, or NULL for a group's shared device.
    user_id TEXT,
    group_id TEXT,
    -- The device whose conversation this is. NULL for wakes.
    device_id TEXT,
    trigger_id TEXT,
    created_at TEXT NOT NULL,
    -- When a turn in it last ended: the prompt cache's clock, and the
    -- retention sweep's.
    last_used_at TEXT
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def dumps(value: Any, *, sort_keys: bool = False) -> str:
    return json.dumps(value, default=_default, ensure_ascii=False, sort_keys=sort_keys)


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
