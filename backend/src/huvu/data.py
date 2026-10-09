"""History tables: the rows sources keep for analysis, and read-only SQL
over them.

The dream learns preferences from data: who did what, when, and in what
state the house was. The event log holds one-line summaries for reading;
these tables hold typed rows for joining and aggregating. Every source
declares its tables in its spec (TableSpec) and writes rows through
`ctx.record`. The kernel keeps them all in one SQLite database,
`<data_dir>/analytics.db`, as `<source>_<table>`, and keeps them for good.

Everything in huvu is SQLite. One database for every source's history is
what lets one query join the HA driver's script runs with the location
source's arrivals and the event log, and what lets the kernel fill in the
same standard columns (spec.STANDARD_COLUMNS) on every row: the time keys
and whose row it is.

`data_sql` runs one read-only query for a caller. It opens its own
connection on a worker thread, attaches the kernel's database read-only,
and puts a temporary view in front of every table that keeps only the
rows the caller may see, the same rule as the event log (events.py). The
view shadows the table's name, so queries use the plain names. An
authorizer refuses every read that doesn't go through a view, so the raw
tables and the rest of the kernel's database (users, tokens) stay out of
reach, as do writes, pragmas and ATTACH. A progress handler stops a query
that runs too long.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .events import Everyone, Viewer, _visible
from .source import SourceError
from .spec import STANDARD_COLUMNS, SourceSpec, TableSpec

if TYPE_CHECKING:
    from .kernel import Kernel

FILE = "analytics.db"
SQL_LIMIT = 200
SQL_MAX = 1000
SQL_TIMEOUT = 10.0

_SQL_TYPES = {"text": "TEXT", "integer": "INTEGER", "real": "REAL", "boolean": "INTEGER", "json": "TEXT"}

# The event log as a table, with the same time keys as every history table.
# `at` keeps the local offset, so its first characters are the local date
# and time.
_EVENTS_VIEW = """
SELECT id, at, ts,
       substr(at, 1, 10) AS local_date,
       CAST(substr(at, 12, 2) AS INTEGER) * 60 + CAST(substr(at, 15, 2) AS INTEGER) AS local_minute,
       (CAST(strftime('%w', substr(at, 1, 10)) AS INTEGER) + 6) % 7 + 1 AS weekday,
       source, type, entity, summary, refs, data, labels, user_id, group_id, device_id
FROM kernel.events
"""

# Conversations as a table, one row per ended turn, with the same time keys.
# A turn is its user's on their own device, and the device's group's on a
# shared one. A shared device's turns go once the device is removed. `here`
# is filled in for the caller (see _sql).
_TURNS_VIEW = """
SELECT * FROM (
    SELECT t.id, t.seq + 1 AS n, t.at,
           CAST(strftime('%s', t.at) AS INTEGER) AS ts,
           substr(t.at, 1, 10) AS local_date,
           CAST(substr(t.at, 12, 2) AS INTEGER) * 60 + CAST(substr(t.at, 15, 2) AS INTEGER) AS local_minute,
           (CAST(strftime('%w', substr(t.at, 1, 10)) AS INTEGER) + 6) % 7 + 1 AS weekday,
           t.ask AS them, t.answer AS you, t.ending, t.actions,
           d.name AS device, c.id AS device_id, c.id IS {here} AS here,
           c.user_id, CASE WHEN c.user_id IS NULL THEN d.group_id END AS group_id
    FROM kernel.turns t
    JOIN kernel.conversations c ON c.id = t.conversation_id
    LEFT JOIN kernel.devices d ON d.id = c.id
    WHERE t.running = 0 AND (c.user_id IS NOT NULL OR d.id IS NOT NULL)
)
"""


class DataError(ValueError):
    """A query that failed in a way the model can fix. The message goes
    back to it."""


class DataStore:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self.path = kernel.config.data_dir / FILE
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        self._conn.close()

    def ensure(self, spec: SourceSpec) -> None:
        """Creates a source's tables, and adds the columns a newer version
        of the driver declares. Columns are never dropped or retyped, so old
        rows stay readable."""
        for table in spec.tables:
            self._ensure(f"{spec.name}_{table.name}", table)

    def _ensure(self, name: str, table: TableSpec) -> None:
        existing = {row[1] for row in self._conn.execute(f'PRAGMA table_info("{name}")')}
        columns = [(c, kind) for c, (kind, _) in STANDARD_COLUMNS.items()] + [(c.name, c.type) for c in table.columns]
        if not existing:
            body = ", ".join(f'"{c}" {_SQL_TYPES[kind]}' for c, kind in columns)
            self._conn.execute(f'CREATE TABLE "{name}" ({body})')
        for column, kind in columns:
            if existing and column not in existing:
                self._conn.execute(f'ALTER TABLE "{name}" ADD COLUMN "{column}" {_SQL_TYPES[kind]}')
        for index in [["ts"], ["user_id", "ts"], *table.indexes]:
            unknown = [c for c in index if c not in dict(columns)]
            if unknown:
                raise ValueError(f"table {name}: index on unknown columns {', '.join(unknown)}")
            quoted = ", ".join(f'"{c}"' for c in index)
            self._conn.execute(f'CREATE INDEX IF NOT EXISTS "{name}__{"_".join(index)}" ON "{name}" ({quoted})')

    def record(
        self,
        source: str,
        table: str,
        row: dict[str, Any],
        *,
        at: datetime,
        user: str | None = None,
        group: str | None = None,
    ) -> None:
        spec = self._kernel.registry.spec(source).table(table)
        if spec is None:
            raise SourceError(f"{source} recorded into table {table!r}, which its spec doesn't declare")
        unknown = [k for k in row if spec.column(k) is None]
        if unknown:
            raise SourceError(f"{source}_{table} has no columns {', '.join(unknown)}")
        local = at.astimezone()
        values: dict[str, Any] = {
            "at": local.isoformat(),
            "ts": local.timestamp(),
            "local_date": local.date().isoformat(),
            "local_minute": local.hour * 60 + local.minute,
            "weekday": local.isoweekday(),
            "user_id": user,
            "group_id": None if user else group,
        }
        for key, value in row.items():
            values[key] = _stored(spec.column(key).type, value)
        names = ", ".join(f'"{k}"' for k in values)
        marks = ", ".join("?" * len(values))
        self._conn.execute(f'INSERT INTO "{source}_{table}" ({names}) VALUES ({marks})', list(values.values()))

    def rows(self, source: str, table: str, where: str, args: Sequence[Any], *, order: str = "ts", limit: int | None = None) -> list[dict[str, Any]]:
        """A source's own rows, for a source that works from its own past,
        such as the location source's latest fix. `where` and `order` are
        SQL the driver writes, never text from a model or a device; values
        go in `args`. JSON columns come back as text."""
        if self._kernel.registry.spec(source).table(table) is None:
            raise SourceError(f"{source} read table {table!r}, which its spec doesn't declare")
        tail = " LIMIT ?" if limit is not None else ""
        cursor = self._conn.execute(
            f'SELECT * FROM "{source}_{table}" WHERE {where} ORDER BY {order}{tail}', [*args, *([limit] if limit is not None else [])]
        )
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]

    async def sql(self, query: str, viewer: Viewer, limit: int = SQL_LIMIT, here: str | None = None) -> dict[str, Any]:
        """Runs one read-only statement for `viewer`, and returns at most
        `limit` rows (SQL_MAX at most). `here` is the conversation the
        caller is in, if any."""
        limit = max(1, min(int(limit), SQL_MAX))
        return await asyncio.to_thread(self._sql, query, viewer, limit, here)

    def _sql(self, query: str, viewer: Viewer, limit: int, here: str | None) -> dict[str, Any]:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            conn.execute("ATTACH DATABASE ? AS kernel", (f"file:{self._kernel.config.data_dir / 'huvu.db'}?mode=ro",))
            where = _inline(viewer)
            tables = [r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                conn.execute(f'CREATE TEMP VIEW "{table}" AS SELECT * FROM main."{table}" WHERE {where}')
            conn.execute(f"CREATE TEMP VIEW events AS {_EVENTS_VIEW} WHERE {where}")
            conn.execute(f"CREATE TEMP VIEW turns AS {_TURNS_VIEW.format(here=_quoted(here))} WHERE {where}")
            conn.execute("PRAGMA query_only = ON")
            conn.set_authorizer(_authorize)
            deadline = time.monotonic() + SQL_TIMEOUT
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
            try:
                cursor = conn.execute(query)
                rows = cursor.fetchmany(limit + 1)
            except (sqlite3.Warning, sqlite3.ProgrammingError) as err:
                raise DataError(f"{err}; data_sql runs one statement") from err
            except sqlite3.DatabaseError as err:
                message = str(err)
                if "interrupted" in message:
                    message = f"the query ran longer than {SQL_TIMEOUT:.0f} s; narrow it with a WHERE on ts, or aggregate"
                elif "prohibited" in message or "not authorized" in message:
                    message = f"{message}: data_sql only reads the history tables, events and turns, by their plain names"
                raise DataError(message) from err
            columns = [d[0] for d in cursor.description] if cursor.description else []
        finally:
            conn.close()
        return {"columns": columns, "rows": [list(r) for r in rows[:limit]], "truncated": len(rows) > limit}


def _stored(kind: str, value: Any) -> Any:
    if value is None:
        stored = None
    elif kind == "json":
        stored = json.dumps(value, default=str, ensure_ascii=False)
    elif kind == "boolean":
        stored = int(bool(value))
    else:
        stored = value
    return stored


def _quoted(value: str | None) -> str:
    """A value written into a view's SQL, since a view can't take
    parameters."""
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def _inline(viewer: Viewer) -> str:
    """The scope clause with its values written in, since a view can't take
    parameters. The values are user and group ids; quoting doubles any
    quote anyway."""
    # Not "1" for everyone: a view must read a column for the authorizer to
    # see that a count(*) through it comes from the view.
    out = "(user_id IS NULL OR user_id IS NOT NULL)"
    if not isinstance(viewer, Everyone):
        clause, args = _visible(viewer)
        pieces = clause.split("?")
        out = pieces[0]
        for arg, piece in zip(args, pieces[1:], strict=True):
            out += _quoted(str(arg)) + piece
    return out


def _authorize(action: int, arg1: str | None, arg2: str | None, db: str | None, view: str | None) -> int:
    """Lets a query read only through the scope views, and do nothing but
    read."""
    if action == sqlite3.SQLITE_READ:
        # Reads of a CTE have no database; reads inside a view name it.
        allowed = view is not None or db not in ("main", "kernel")
    else:
        allowed = action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE)
    return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
