"""Tables: everything the sources know, and read-only SQL over it.

Everything huvu knows is a table. A source declares its tables in its spec
(TableSpec), in two kinds:

1. History tables hold what happened, one row per thing, kept for good. A
   source adds rows with `ctx.record`, and the kernel keeps every source's
   history in one SQLite database, `<data_dir>/analytics.db`, as
   `<source>_<table>`. One database is what lets one query join sources,
   and the kernel fills in the same standard columns
   (prompts.STANDARD_COLUMNS) on every row: `id`, the time keys and whose
   row it is, so two sources can't disagree on what "22:30 on a Tuesday"
   is. `id` numbers rows in the order they were recorded, across every
   table, so a trigger can tell which rows it has seen, even one a phone
   sent late with an old time.
2. Live tables hold what is true now. Nothing is stored: each read asks
   the source (`Source.live`), which answers from what it already holds.
   They are SQLite virtual tables, through APSW, since Python's sqlite3
   can't define them.

Every recorded row tells the trigger engine which source wrote it, so the
triggers that read that source run at once (see triggers.py).

Every connection is APSW's, here as everywhere in the backend (see db.py).

A query runs for one caller, on a connection of its own, on a worker
thread, so a long one never blocks the event loop; WAL lets it read while
sources write. Each live table is registered on the connection, and a
temporary view goes in front of every table, with the table's name, that
keeps only the rows the caller may see (scope.py). An authorizer refuses
every read that doesn't go through a view, every write, pragma and ATTACH.
A progress handler stops a query that runs too long. huvu.db, with the
accounts and tokens, is never attached.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import apsw
import apsw.ext

from .db import BUSY_WAIT_MS, open_db
from .prompts import LIVE_COLUMNS, STANDARD_COLUMNS
from .scope import Everyone, Viewer, visible
from .source import SourceError
from .spec import Label, SourceSpec, TableSpec, merge_labels

if TYPE_CHECKING:
    from .kernel import Kernel

FILE = "analytics.db"
SQL_LIMIT = 200
SQL_MAX = 1000
SQL_TIMEOUT = 10.0

_SQL_TYPES = {"text": "TEXT", "integer": "INTEGER", "real": "REAL", "boolean": "INTEGER", "json": "TEXT"}

# The time, in local time from the kernel's clock, for queries that ask
# what time it is now. SQLite's own time functions read the wall clock in
# UTC, unless told 'localtime'.
NOW_FUNCTIONS = ("now_ts", "now_date", "now_minute", "now_weekday")
# A query that calls one of these depends on the clock (see triggers.py).
CLOCK_FUNCTIONS = {
    *NOW_FUNCTIONS,
    "date",
    "time",
    "datetime",
    "julianday",
    "unixepoch",
    "strftime",
    "timediff",
    "current_date",
    "current_time",
    "current_timestamp",
}


# SQLite's table-valued functions that a query may read like tables.
TABLE_FUNCTIONS = {"json_each", "json_tree"}


class DataError(ValueError):
    """A query that failed in a way the model can fix. The message goes
    back to it."""


class LiveError(Exception):
    """A source that couldn't answer for one of its live tables, such as
    Home Assistant while it is disconnected."""


@dataclass
class Answer:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    labels: list[Label]

    def public(self) -> dict[str, Any]:
        return {"columns": self.columns, "rows": self.rows, "truncated": self.truncated}


@dataclass
class Compiled:
    """What a query reads, found without running it."""

    columns: list[str]
    # The sources whose tables it reads.
    sources: set[str] = field(default_factory=set)
    # Whether it reads a live table, or the time.
    live: bool = False
    clock: bool = False
    labels: list[Label] = field(default_factory=list)


class DataStore:
    def __init__(self, kernel: Kernel) -> None:
        self._kernel = kernel
        self.path = kernel.config.data_dir / FILE
        self._conn = open_db(self.path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._last_id: int | None = None
        # Told the source of every recorded row: the trigger engine.
        self.on_record: Callable[[str], None] | None = None

    def close(self) -> None:
        self._conn.close()

    # History

    def ensure(self, spec: SourceSpec) -> None:
        """Creates a source's history tables, and adds the columns a newer
        version of the driver declares. Columns are never dropped or
        retyped, so old rows stay readable."""
        for table in spec.tables:
            if not table.live:
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
        for index in [["id"], ["ts"], ["user_id", "ts"], *table.indexes]:
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
    ) -> int:
        """Adds a row to a history table, and returns its id."""
        spec = self._kernel.registry.spec(source).table(table)
        if spec is None or spec.live:
            raise SourceError(f"{source} recorded into table {table!r}, which its spec doesn't declare as a history table")
        unknown = [k for k in row if spec.column(k) is None]
        if unknown:
            raise SourceError(f"{source}_{table} has no columns {', '.join(unknown)}")
        local = at.astimezone()
        row_id = self._next_id()
        values: dict[str, Any] = {
            "id": row_id,
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
        if self.on_record is not None:
            self.on_record(source)
        return row_id

    def last_id(self) -> int:
        """The id of the latest row recorded, in any table."""
        if self._last_id is None:
            tables = [r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            self._last_id = max((self._one(f'SELECT max(id) FROM "{t}"') or 0 for t in tables), default=0)
        return self._last_id

    def _next_id(self) -> int:
        self._last_id = self.last_id() + 1
        return self._last_id

    def rows(self, source: str, table: str, where: str, args: Sequence[Any], *, order: str = "ts", limit: int | None = None) -> list[dict[str, Any]]:
        """A source's own rows, for a source that works from its own past,
        such as the location source's latest fix. `where` and `order` are
        SQL the driver writes, never text from a model or a device; values
        go in `args`. JSON columns come back as text."""
        self._history(source, table)
        tail = " LIMIT ?" if limit is not None else ""
        cursor = self._conn.execute(
            f'SELECT * FROM "{source}_{table}" WHERE {where} ORDER BY {order}{tail}', [*args, *([limit] if limit is not None else [])]
        )
        try:
            names = [d[0] for d in cursor.getdescription()]
        except apsw.ExecutionCompleteError:
            # No rows: the statement is done before it says its columns.
            return []
        return [dict(zip(names, r, strict=True)) for r in cursor]

    def count(self, source: str, table: str, where: str, args: Sequence[Any]) -> int:
        """How many rows match, with `where` written by kernel code."""
        self._history(source, table)
        return self._one(f'SELECT count(*) FROM "{source}_{table}" WHERE {where}', list(args))

    def claim(self, device: str, user: str, since: datetime) -> int:
        """Gives a user a shared device's unowned rows since a time: what
        they did there before huvu knew it was them. Every history table
        with a `device_id` column takes part."""
        claimed = 0
        for spec in self._kernel.registry.specs():
            for table in spec.tables:
                if not table.live and table.column("device_id") is not None:
                    self._conn.execute(
                        f'UPDATE "{spec.name}_{table.name}" SET user_id = ? WHERE device_id = ? AND user_id IS NULL AND ts >= ?',
                        (user, device, since.timestamp()),
                    )
                    claimed += self._conn.changes()
        return claimed

    def _one(self, sql: str, args: Sequence[Any] = ()) -> Any:
        """The first column of the first row."""
        rows = list(self._conn.execute(sql, args))
        return rows[0][0] if rows else None

    def _history(self, source: str, table: str) -> None:
        spec = self._kernel.registry.spec(source).table(table)
        if spec is None or spec.live:
            raise SourceError(f"{source} read table {table!r}, which its spec doesn't declare as a history table")

    # Queries

    async def sql(self, query: str, viewer: Viewer, limit: int = SQL_LIMIT, *, after: int | None = None) -> Answer:
        """Runs one read-only statement for `viewer`, and returns at most
        `limit` rows (SQL_MAX at most). With `after`, only the result's rows
        whose `id` is past it."""
        limit = max(1, min(int(limit), SQL_MAX))
        return await asyncio.to_thread(self._sql, query, viewer, limit, after)

    async def compile(self, query: str, viewer: Viewer) -> Compiled:
        """What a query reads, without running it: its columns, the sources
        whose tables it reads, and whether it reads a live table or the
        time. Fails the way running it would on a bad name or a second
        statement."""
        return await asyncio.to_thread(self._compile, query, viewer)

    def _sql(self, query: str, viewer: Viewer, limit: int, after: int | None) -> Answer:
        text = query if after is None else f"SELECT * FROM ({_bare(query)}) WHERE id > ?"
        conn = self._open(viewer)
        try:
            info = _info(conn, text)
            deadline = time.monotonic() + SQL_TIMEOUT
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
            try:
                rows = list(itertools.islice(conn.execute(text, () if after is None else (after,)), limit + 1))
            except apsw.Error as err:
                raise DataError(_message(err)) from err
            except LiveError as err:
                raise DataError(str(err)) from err
        finally:
            conn.close()
        tables, _ = _touched(info)
        return Answer(
            columns=[d[0] for d in info.description or ()],
            rows=[list(r) for r in rows[:limit]],
            truncated=len(rows) > limit,
            labels=self._labels(tables),
        )

    def _compile(self, query: str, viewer: Viewer) -> Compiled:
        conn = self._open(viewer)
        try:
            info = _info(conn, query)
        finally:
            conn.close()
        tables, functions = _touched(info)
        known = self._tables()
        read = [known[t] for t in tables if t in known]
        return Compiled(
            columns=[d[0] for d in info.description or ()],
            sources={source for source, _ in read},
            live=any(table.live for _, table in read),
            clock=bool(functions & CLOCK_FUNCTIONS),
            labels=self._labels(tables),
        )

    def _open(self, viewer: Viewer) -> apsw.Connection:
        try:
            return self._prepare(viewer)
        except apsw.Error as err:
            raise DataError(_message(err)) from err

    def _prepare(self, viewer: Viewer) -> apsw.Connection:
        conn = apsw.Connection(f"file:{self.path}?mode=ro", flags=apsw.SQLITE_OPEN_READONLY | apsw.SQLITE_OPEN_URI)
        conn.set_busy_timeout(BUSY_WAIT_MS)
        where = _inline(viewer)
        stored = [r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for name in stored:
            conn.execute(f'CREATE TEMP VIEW "{name}" AS SELECT * FROM main."{name}" WHERE {where}')
        for name, (source, table) in self._tables().items():
            if table.live:
                self._mount(conn, name, source, table)
                conn.execute(f'CREATE TEMP VIEW "{name}" AS SELECT * FROM main."{name}" WHERE {where}')
        now = self._kernel.now
        functions: dict[str, Callable[[], Any]] = {
            "now_ts": lambda: now().timestamp(),
            "now_date": lambda: now().astimezone().date().isoformat(),
            "now_minute": lambda: _minute(now()),
            "now_weekday": lambda: now().astimezone().isoweekday(),
        }
        for name, fn in functions.items():
            conn.create_scalar_function(name, fn, 0)
        conn.execute("PRAGMA query_only = ON")
        conn.authorizer = _authorize
        return conn

    def _mount(self, conn: apsw.Connection, name: str, source_name: str, table: TableSpec) -> None:
        """Registers one live table on a connection, answered by its
        source each time a query reads it."""
        columns = (*(c.name for c in table.columns), *LIVE_COLUMNS)
        kinds = {c.name: c.type for c in table.columns}
        source = self._kernel.registry.get(source_name)

        def rows():
            try:
                live = source.live(table.name)
            except Exception as err:
                # Raised through SQLite as it is; the query turns it into a
                # DataError that names the table.
                raise LiveError(f"{name}: {err}") from err
            for row in live:
                yield {c: _stored(kinds.get(c, "text"), row.get(c)) for c in columns}

        rows.columns = columns
        rows.column_access = apsw.ext.VTColumnAccess.By_Name
        apsw.ext.make_virtual_module(conn, name, rows)

    def _tables(self) -> dict[str, tuple[str, TableSpec]]:
        """Every table any source declares, by the name SQL reads it as."""
        return {f"{spec.name}_{t.name}": (spec.name, t) for spec in self._kernel.registry.specs() for t in spec.tables}

    def _labels(self, tables: set[str]) -> list[Label]:
        known = self._tables()
        return merge_labels(*(known[t][1].labels for t in sorted(tables) if t in known))


def _info(conn: apsw.Connection, text: str):
    """The statement prepared, not run: its columns, what it reads, and
    whether more statements follow it."""
    try:
        info = apsw.ext.query_info(conn, text, actions=True)
    except apsw.Error as err:
        raise DataError(_message(err)) from err
    if (info.query_remaining or "").strip():
        raise DataError("data_sql runs one statement; send one at a time")
    return info


def _touched(info) -> tuple[set[str], set[str]]:
    """The tables a prepared statement reads, and the functions it calls."""
    tables = {a.table_name for a in info.actions or () if a.action_name == "SQLITE_READ" and a.table_name}
    functions = {a.function_name.lower() for a in info.actions or () if a.action_name == "SQLITE_FUNCTION" and a.function_name}
    return tables, functions


def _message(err: apsw.Error) -> str:
    message = str(err)
    if isinstance(err, apsw.InterruptError):
        message = f"the query ran longer than {SQL_TIMEOUT:.0f} s; narrow it with a WHERE on ts, or aggregate"
    elif "prohibited" in message or "not authorized" in message:
        message = f"{message}: data_sql only reads the tables in the skills, by their plain names"
    return message


def _bare(query: str) -> str:
    """A query with no trailing semicolon, to wrap in another."""
    return query.strip().rstrip(";").strip()


def _minute(at: datetime) -> int:
    local = at.astimezone()
    return local.hour * 60 + local.minute


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
    """The scope clause with its values written in. The values are user and
    group ids; quoting doubles any quote anyway."""
    # Not "1" for everyone: a view must read a column for the authorizer to
    # see that a count(*) through it comes from the view.
    out = "(user_id IS NULL OR user_id IS NOT NULL)"
    if not isinstance(viewer, Everyone):
        clause, args = visible(viewer)
        pieces = clause.split("?")
        out = pieces[0]
        for arg, piece in zip(args, pieces[1:], strict=True):
            out += _quoted(str(arg)) + piece
    return out


def _authorize(action: int, arg1: str | None, arg2: str | None, db: str | None, view: str | None) -> int:
    """Lets a query read only through the scope views, and do nothing but
    read."""
    if action == apsw.SQLITE_READ:
        # Reads of a CTE have no database; reads inside a view name it.
        # SQLite's JSON table functions read like tables in main.
        allowed = view is not None or db != "main" or arg1 in TABLE_FUNCTIONS
    else:
        allowed = action in (apsw.SQLITE_SELECT, apsw.SQLITE_FUNCTION, apsw.SQLITE_RECURSIVE)
    return apsw.SQLITE_OK if allowed else apsw.SQLITE_DENY
