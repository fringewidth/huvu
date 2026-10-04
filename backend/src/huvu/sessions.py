"""What the kernel knows about Hermes sessions: what each one is for, and
whether it is tainted.

A session that has read content a stranger controls is tainted from then
on, since a model can't be tracked token by token. The kernel learns it two
ways: its own tool results carry the `external` label, and the huvu Hermes
plugin sends `_tainted` on every call, which also covers Hermes's own tools
like web search.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Literal

from .db import iso

Kind = Literal["chat", "wake", "unknown"]


class SessionBook:
    def __init__(self, conn: sqlite3.Connection, now) -> None:
        self._conn = conn
        self._now = now

    def record(self, session_id: str, kind: Kind, *, conversation_id: str | None = None, trigger_id: str | None = None, tainted: bool = False) -> None:
        self._conn.execute(
            "INSERT INTO hermes_sessions (id, kind, conversation_id, trigger_id, tainted, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET tainted = max(tainted, excluded.tainted)",
            (session_id, kind, conversation_id, trigger_id, int(tainted), iso(self._now())),
        )

    def mark_tainted(self, session_id: str) -> None:
        self.record(session_id, "unknown", tainted=True)

    def is_tainted(self, session_id: str | None) -> bool:
        row = self._conn.execute("SELECT tainted FROM hermes_sessions WHERE id = ?", (session_id,)).fetchone() if session_id else None
        return bool(row and row["tainted"])

    def conversation_of(self, session_id: str | None) -> str | None:
        row = self._conn.execute("SELECT conversation_id FROM hermes_sessions WHERE id = ?", (session_id,)).fetchone() if session_id else None
        return row["conversation_id"] if row else None

    def created(self, session_id: str) -> datetime | None:
        row = self._conn.execute("SELECT created_at FROM hermes_sessions WHERE id = ?", (session_id,)).fetchone()
        return datetime.fromisoformat(row["created_at"]) if row else None
