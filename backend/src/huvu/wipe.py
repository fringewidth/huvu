"""Wipes what huvu keeps for one person, or for one group's shared devices:
their triggers, conversations, wakes, Hermes session records, notifications,
approvals and attendance, and their rows in every history table (their
chat turns, the kernel's log about them, every source's history). `uv run
huvu wipe <user>` or `uv run huvu wipe --group home`; `wipe.sh` at the repo
root also clears the Hermes profile. Wiping the default group also takes
the rows that belong to nobody, such as doorbell presses.

The account stays: the user, their password, groups, devices and trackers.
Deleting the data folder resets everything.

The backend must be stopped: it keeps live triggers and turns in memory and
would write them back.
"""

from __future__ import annotations

from pathlib import Path

import apsw

from .db import changed, open_db

# Each table, and the rows of it that belong to the one being wiped. `?` is
# the user, or the group.
FOR_USER = {
    "triggers": "user_id = ?",
    "hermes_sessions": "user_id = ?",
    "notifications": "user_id = ?",
    "approvals": "user_id = ?",
    "attendance": "user_id = ?",
    "conversations": "user_id = ?",
    "wakes": "user_id = ?",
}
# A group's own rows have no user. The default group also has the rows that
# belong to nobody, such as the kernel starting.
FOR_GROUP = {
    "triggers": "user_id IS NULL AND group_id = ?",
    "hermes_sessions": "user_id IS NULL AND group_id = ?",
    "notifications": "user_id IS NULL AND group_id = ?",
    "approvals": "user_id IS NULL AND group_id = ?",
    "attendance": "device_id IN (SELECT id FROM devices WHERE user_id IS NULL AND group_id = ?)",
    "conversations": "user_id IS NULL AND id IN (SELECT id FROM devices WHERE user_id IS NULL AND group_id = ?)",
    "wakes": "user_id IS NULL AND group_id = ?",
}


# The same in every history table, by its standard columns.
HISTORY_USER = "user_id = ?"
HISTORY_GROUP = "user_id IS NULL AND (group_id = ? OR (group_id IS NULL AND :default))"


def wipe(
    conn: apsw.Connection,
    history: Path,
    scripts: Path,
    *,
    user: str | None = None,
    group: str | None = None,
    default_group: bool = False,
) -> dict[str, int]:
    """Deletes the rows from the kernel's database (`conn`) and the history
    database at `history`, and returns how many went from each table."""
    if (user is None) == (group is None):
        raise ValueError("wipe one user or one group")
    where = FOR_USER if user else FOR_GROUP
    who = user or group
    counts: dict[str, int] = {}
    conn.execute("BEGIN")
    try:
        triggers = [r[0] for r in conn.execute(f"SELECT id FROM triggers WHERE {where['triggers']}", (who,))]
        conversations = f"SELECT id FROM conversations WHERE {where['conversations']}"
        wakes = f"SELECT id FROM wakes WHERE {where['wakes']}"
        counts["turns"] = changed(conn, f"DELETE FROM turns WHERE conversation_id IN ({conversations}) OR conversation_id IN ({wakes})", (who, who))
        for table, condition in where.items():
            sql = condition.replace(":default", "1" if default_group else "0")
            counts[table] = changed(conn, f"DELETE FROM {table} WHERE {sql}", (who,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    condition = (HISTORY_USER if user else HISTORY_GROUP).replace(":default", "1" if default_group else "0")
    db = open_db(history)
    try:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        with db:
            for table in tables:
                db.execute(f'DELETE FROM "{table}" WHERE {condition}', (who,))
                if db.changes():
                    counts[table] = db.changes()
    finally:
        db.close()
    for trigger in triggers:
        (scripts / f"{trigger}.py").unlink(missing_ok=True)
    return counts
