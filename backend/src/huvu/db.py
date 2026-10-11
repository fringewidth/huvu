"""The kernel's own SQLite database: accounts and devices, triggers,
approvals, conversations for the stage, and the built-in source's state.
What happened, the kernel's own log included, is history (data.py), in
another file the model reads; this one it never reads.

One connection, used from the event loop. Every statement here is small,
and one family on one machine never queues enough of them for blocking
to matter. WAL lets a second process (a backup, sqlite3 in a shell) read
while the kernel writes.

Every SQLite file huvu opens, it opens through APSW, with `connect` here
or data.py's own. APSW brings its own copy of SQLite, and two copies in one
process don't see each other's locks on a file, which can corrupt it; so
Python's sqlite3 isn't used anywhere in the backend. A connection from
`connect` gives rows that read by column name (`row["id"]`) or position,
the way sqlite3.Row did.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import apsw

SCHEMA = """
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

-- What one user calls another (mom, dad), for the model and on the
-- notifications one sends the other. Without a row, the person's name.
CREATE TABLE IF NOT EXISTS people_aliases (
    owner_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    PRIMARY KEY (owner_id, user_id),
    UNIQUE (owner_id, alias)
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
    fire_count INTEGER NOT NULL DEFAULT 0,
    -- fire rows: the id of the last history row it has seen.
    after_id INTEGER
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
    group_id TEXT,
    -- The user who sent it through huvu_message. NULL when huvu sent it.
    sender_id TEXT
);
CREATE INDEX IF NOT EXISTS notifications_ts ON notifications (ts);

-- Each phone's FCM token, for native push while its app is closed
-- (native_push.py), or its Web Push subscription's endpoint (web_push.py).
CREATE TABLE IF NOT EXISTS push_tokens (
    token TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    -- "android" or "ios", or "web" for a subscription.
    platform TEXT NOT NULL,
    -- A subscription's keys as JSON (p256dh, auth). NULL for an FCM token.
    keys TEXT,
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
    last_used_at TEXT,
    -- A chat session's model, provider and effort (Route.key in
    -- routing.py), which its prompt cache depends on.
    route TEXT
);
"""

# Columns added to a table after it was first made, which CREATE TABLE IF
# NOT EXISTS doesn't add to an older database.
ADDED_COLUMNS = [("hermes_sessions", "route", "TEXT"), ("notifications", "sender_id", "TEXT"), ("push_tokens", "keys", "TEXT")]


# How long a statement waits for a lock another connection holds, in ms,
# as Python's sqlite3 did by default.
BUSY_WAIT_MS = 5000


class Row(tuple):
    """A row that reads by column name too."""

    _index: dict[str, int]

    def __new__(cls, values: Sequence[Any], index: dict[str, int]) -> Row:
        row = super().__new__(cls, values)
        row._index = index
        return row

    def __getitem__(self, key):  # type: ignore[override]
        return tuple.__getitem__(self, self._index[key] if isinstance(key, str) else key)

    def keys(self) -> list[str]:
        return list(self._index)


_indexes: dict[tuple[str, ...], dict[str, int]] = {}


def _row(cursor: apsw.Cursor, values: Sequence[Any]) -> Row:
    names = tuple(d[0] for d in cursor.get_description())
    index = _indexes.get(names)
    if index is None:
        index = _indexes[names] = {name: i for i, name in enumerate(names)}
    return Row(values, index)


def open_db(path: Path | str, *, readonly: bool = False) -> apsw.Connection:
    """An APSW connection whose rows read by name, waiting for locks the
    way sqlite3 did. `readonly` is for a reader on another thread."""
    flags = apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI if readonly else apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE
    conn = apsw.Connection(f"file:{path}?mode=ro" if readonly else str(path), flags=flags)
    conn.set_busy_timeout(BUSY_WAIT_MS)
    conn.row_trace = _row
    return conn


def connect(path: Path | str) -> apsw.Connection:
    """The kernel's database, with its schema."""
    conn = open_db(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(SCHEMA)
    for table, column, kind in ADDED_COLUMNS:
        if column not in {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {kind}')
    return conn


def changed(conn: apsw.Connection, sql: str, args: Sequence[Any] = ()) -> int:
    """Runs one write, and returns how many rows it changed."""
    conn.execute(sql, args)
    return conn.changes()


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
