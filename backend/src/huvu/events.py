"""The event log: one stream of thin envelopes for everything that happened.

Cross-source triggers, the daily digest and cross-source search all need a
single stream, so the envelope lives here. The detail stays in each source
and is reached through the event's refs.

An event belongs to one user, to one group, or to the house:

1. A user's event (their actions, approvals, triggers, chat turns, and what
   they did at a shared device while recognized there, see attendance.py)
   shows only to them.
2. A group's event (a shared device's turn with nobody recognized) shows to
   the group's members.
3. The house's events (a doorbell press, the kernel starting) show to every
   member of any group.

A Hermes profile reads the log through the tools, so a user's dream sees
their own events and their groups', never another user's. A guest sees only
their own.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .db import dumps, from_iso, iso, loads
from .spec import Event, Scope

# How many example summaries a digest keeps per event type.
DIGEST_EXAMPLES = 5


class Everyone:
    """Reads with no user filter, for the kernel itself."""


EVERYONE = Everyone()
Viewer = Scope | Everyone


def _visible(viewer: Viewer, prefix: str = "") -> tuple[str, list[Any]]:
    """The SQL that keeps what `viewer` may see (see Scope)."""
    clause, args = "", []
    if isinstance(viewer, Scope):
        parts, args = [], []
        if viewer.user is not None:
            parts.append(f"{prefix}user_id = ?")
            args.append(viewer.user)
        if viewer.groups:
            parts.append(f"({prefix}user_id IS NULL AND {prefix}group_id IN ({', '.join('?' * len(viewer.groups))}))")
            args.extend(viewer.groups)
        if viewer.house:
            parts.append(f"({prefix}user_id IS NULL AND {prefix}group_id IS NULL)")
        clause = f"({' OR '.join(parts)})" if parts else "0"
    return clause, args


class EventLog:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(self, event: Event) -> Event:
        stored = event.model_copy(update={"id": event.id or new_id(event.at)})
        self._conn.execute(
            "INSERT INTO events (id, at, ts, source, type, entity, summary, refs, data, labels, user_id, group_id, device_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stored.id,
                iso(stored.at),
                stored.at.timestamp(),
                stored.source,
                stored.type,
                stored.entity,
                stored.summary,
                dumps(stored.refs),
                dumps(stored.data),
                dumps(stored.labels),
                stored.user,
                stored.group,
                stored.device,
            ),
        )
        return stored

    def claim(self, device: str, user: str, since: datetime) -> int:
        """Gives a user a shared device's unowned events since a time: what
        they did there before huvu knew it was them."""
        cursor = self._conn.execute(
            "UPDATE events SET user_id = ? WHERE device_id = ? AND user_id IS NULL AND ts >= ?",
            (user, device, since.timestamp()),
        )
        return cursor.rowcount

    def count(self, *, type: str, since: datetime, entity: str | None = None, user: str | None = None) -> int:
        """How many events of a type since a time, for one entity or user."""
        where, args = ["type = ?", "ts >= ?"], [type, since.timestamp()]
        if entity is not None:
            where.append("entity = ?")
            args.append(entity)
        if user is not None:
            where.append("user_id = ?")
            args.append(user)
        return self._conn.execute(f"SELECT COUNT(*) AS n FROM events WHERE {' AND '.join(where)}", args).fetchone()["n"]

    def get(self, event_id: str, viewer: Viewer = EVERYONE) -> Event | None:
        clause, args = _visible(viewer)
        where = f" AND {clause}" if clause else ""
        row = self._conn.execute(f"SELECT * FROM events WHERE id = ?{where}", (event_id, *args)).fetchone()
        return None if row is None else _event(row)

    def search(
        self,
        text: str | None = None,
        *,
        source: str | None = None,
        type: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 20,
        viewer: Viewer = EVERYONE,
    ) -> list[Event]:
        """Newest first. With `text`, only events whose summary, type or
        entity has every word, still newest first. `viewer` limits it to
        what that user may see."""
        where: list[str] = []
        args: list[Any] = []
        visible, visible_args = _visible(viewer, "e.")
        if visible:
            where.append(visible)
            args.extend(visible_args)
        if source:
            where.append("e.source = ?")
            args.append(source)
        if type:
            where.append("e.type = ?")
            args.append(type)
        if since:
            where.append("e.ts >= ?")
            args.append(since.timestamp())
        if until:
            where.append("e.ts < ?")
            args.append(until.timestamp())
        match = fts_query(text or "")
        joins = ""
        if match:
            joins = "JOIN events_fts f ON f.rowid = e.rowid"
            where.append("events_fts MATCH ?")
            args.append(match)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self._conn.execute(
            f"SELECT e.* FROM events e {joins} {clause} ORDER BY e.ts DESC, e.id DESC LIMIT ?",
            (*args, max(1, min(limit, 200))),
        ).fetchall()
        return [_event(r) for r in rows]

    def digest(self, start: datetime, end: datetime, viewer: Viewer = EVERYONE) -> dict[str, Any]:
        """A rollup of everything between `start` and `end` that `viewer` may
        see: per event type, how many, the first and last, and a few
        summaries; and per hour, how many events of any type."""
        visible, visible_args = _visible(viewer)
        rows = self._conn.execute(
            f"SELECT type, at, summary FROM events WHERE ts >= ? AND ts < ?{' AND ' + visible if visible else ''} ORDER BY ts",
            (start.timestamp(), end.timestamp(), *visible_args),
        ).fetchall()
        by_type: dict[str, dict[str, Any]] = {}
        hours: dict[str, int] = {}
        for row in rows:
            at = from_iso(row["at"])
            entry = by_type.setdefault(row["type"], {"type": row["type"], "count": 0, "first": at, "last": at, "examples": []})
            entry["count"] += 1
            entry["last"] = at
            entry["examples"].append(row["summary"])
            hour = f"{at.astimezone(start.tzinfo):%Y-%m-%d %H}:00" if at else "?"
            hours[hour] = hours.get(hour, 0) + 1
        for entry in by_type.values():
            entry["examples"] = entry["examples"][-DIGEST_EXAMPLES:]
        return {
            "from": start,
            "to": end,
            "total": len(rows),
            "by_type": sorted(by_type.values(), key=lambda e: -e["count"]),
            "by_hour": hours,
        }


def new_id(at: datetime) -> str:
    """Sorts by time, then at random within the same millisecond."""
    return f"ev_{int(at.timestamp() * 1000):013d}_{secrets.token_hex(3)}"


def fts_query(text: str) -> str:
    """Quotes every word, so a model's text can't use FTS syntax by
    accident. Every word must match."""
    words = re.findall(r"[\w.]+", text)
    return " ".join(f'"{w}"' for w in words)


def day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        at=from_iso(row["at"]),
        source=row["source"],
        type=row["type"],
        entity=row["entity"],
        summary=row["summary"],
        refs=loads(row["refs"]),
        data=loads(row["data"]),
        labels=loads(row["labels"]),
        user=row["user_id"],
        group=row["group_id"],
        device=row["device_id"],
    )
