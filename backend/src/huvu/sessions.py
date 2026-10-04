"""What the kernel knows about Hermes sessions: what each one is for, and
whose it is.

A chat session belongs to a user and the device it runs on, or, on a
shared device, to the device's group. A wake session belongs to its
trigger's user and to no device. Hermes's own cron jobs start sessions the
kernel never hears of; their tool calls still carry the user or group, from
the profile's MCP token.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .db import iso

Kind = Literal["chat", "wake"]


@dataclass(frozen=True)
class SessionOwner:
    user: str | None
    group: str | None
    device: str | None


class SessionBook:
    def __init__(self, conn: sqlite3.Connection, now) -> None:
        self._conn = conn
        self._now = now

    def record(
        self, session_id: str, kind: Kind, *, user: str | None, group: str | None = None, device: str | None = None, trigger_id: str | None = None
    ) -> None:
        self._conn.execute(
            "INSERT INTO hermes_sessions (id, kind, user_id, group_id, device_id, trigger_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO NOTHING",
            (session_id, kind, user, group, device, trigger_id, iso(self._now())),
        )

    def owner(self, session_id: str | None) -> SessionOwner | None:
        query = "SELECT user_id, group_id, device_id FROM hermes_sessions WHERE id = ?"
        row = self._conn.execute(query, (session_id,)).fetchone() if session_id else None
        return SessionOwner(user=row["user_id"], group=row["group_id"], device=row["device_id"]) if row else None

    def created(self, session_id: str) -> datetime | None:
        row = self._conn.execute("SELECT created_at FROM hermes_sessions WHERE id = ?", (session_id,)).fetchone()
        return datetime.fromisoformat(row["created_at"]) if row else None
