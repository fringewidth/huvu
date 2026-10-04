"""What the kernel knows about Hermes sessions: what each one is for."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Literal

from .db import iso

Kind = Literal["chat", "wake"]


class SessionBook:
    def __init__(self, conn: sqlite3.Connection, now) -> None:
        self._conn = conn
        self._now = now

    def record(self, session_id: str, kind: Kind, *, conversation_id: str | None = None, trigger_id: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO hermes_sessions (id, kind, conversation_id, trigger_id, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
            (session_id, kind, conversation_id, trigger_id, iso(self._now())),
        )

    def conversation_of(self, session_id: str | None) -> str | None:
        row = self._conn.execute("SELECT conversation_id FROM hermes_sessions WHERE id = ?", (session_id,)).fetchone() if session_id else None
        return row["conversation_id"] if row else None

    def created(self, session_id: str) -> datetime | None:
        row = self._conn.execute("SELECT created_at FROM hermes_sessions WHERE id = ?", (session_id,)).fetchone()
        return datetime.fromisoformat(row["created_at"]) if row else None
