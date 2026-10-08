"""Wipes what huvu keeps for one person, or for one group's shared devices:
their triggers, conversations, Hermes session records, notifications,
approvals, attendance and event log entries. `uv run huvu wipe <user>` or
`uv run huvu wipe --group home`; `wipe.sh` at the repo root also clears the
Hermes profile.

The account stays: the user, their password, groups, devices and trackers.
So do the sources' history tables (analytics.db), which are the facts about
the world the sources recorded, not anything the agent made.

The backend must be stopped: it keeps live triggers and turns in memory and
would write them back.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Each table, and the rows of it that belong to the one being wiped. `?` is
# the user, or the group.
FOR_USER = {
    "triggers": "user_id = ?",
    "hermes_sessions": "user_id = ?",
    "notifications": "user_id = ?",
    "approvals": "user_id = ?",
    "attendance": "user_id = ?",
    "events": "user_id = ?",
    "conversations": "user_id = ?",
}
# A group's own rows have no user. The default group also has the rows that
# belong to nobody, such as the kernel starting.
FOR_GROUP = {
    "triggers": "user_id IS NULL AND group_id = ?",
    "hermes_sessions": "user_id IS NULL AND group_id = ?",
    "notifications": "user_id IS NULL AND group_id = ?",
    "approvals": "user_id IS NULL AND group_id = ?",
    "attendance": "device_id IN (SELECT id FROM devices WHERE user_id IS NULL AND group_id = ?)",
    "events": "user_id IS NULL AND (group_id = ? OR (group_id IS NULL AND :default))",
    "conversations": "user_id IS NULL AND id IN (SELECT id FROM devices WHERE user_id IS NULL AND group_id = ?)",
}


def wipe(conn: sqlite3.Connection, scripts: Path, *, user: str | None = None, group: str | None = None, default_group: bool = False) -> dict[str, int]:
    """Deletes the rows, and returns how many went from each table."""
    if (user is None) == (group is None):
        raise ValueError("wipe one user or one group")
    where = FOR_USER if user else FOR_GROUP
    who = user or group
    counts: dict[str, int] = {}
    conn.execute("BEGIN")
    try:
        triggers = [r[0] for r in conn.execute(f"SELECT id FROM triggers WHERE {where['triggers']}", (who,))]
        conversations = f"SELECT id FROM conversations WHERE {where['conversations']}"
        counts["turns"] = conn.execute(f"DELETE FROM turns WHERE conversation_id IN ({conversations})", (who,)).rowcount
        for table, condition in where.items():
            sql = condition.replace(":default", "1" if default_group else "0")
            counts[table] = conn.execute(f"DELETE FROM {table} WHERE {sql}", (who,)).rowcount
        # The search index mirrors the log and has no delete trigger.
        conn.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    for trigger in triggers:
        (scripts / f"{trigger}.py").unlink(missing_ok=True)
    return counts
