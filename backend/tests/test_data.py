"""History tables: rows sources record, the standard columns the kernel
adds, and data_sql, which reads them for a caller and nothing else."""

from __future__ import annotations

import pytest

from huvu import data as data_module
from huvu import skills
from huvu.config import Config
from huvu.data import DataError
from huvu.events import EVERYONE
from huvu.kernel import Kernel
from huvu.source import Source, SourceError
from huvu.spec import Caller, ColumnSpec, SourceSpec, TableSpec
from huvu.tools import ToolError


class Diary(Source):
    """A source with one history table, whose spec can grow a column."""

    def __init__(self) -> None:
        super().__init__({})
        self.extra = False

    def describe(self) -> SourceSpec:
        columns = [
            ColumnSpec(name="text", type="text", description="What happened"),
            ColumnSpec(name="mood", type="integer", description="1 to 5"),
            ColumnSpec(name="tags", type="json", description="Tags"),
            ColumnSpec(name="shared", type="boolean", description="Shared with the family"),
        ]
        if self.extra:
            columns.append(ColumnSpec(name="place", type="text", description="Where"))
        table = TableSpec(name="entries", description="One row per diary entry.", columns=columns, indexes=[["mood", "ts"]])
        return SourceSpec(name="diary", description="A diary.", tables=[table])


@pytest.fixture
async def house(tmp_path, clock, hermes):
    diary = Diary()
    kernel = Kernel(Config(data_dir=tmp_path), hermes=hermes, sources={"diary": diary}, clock=clock)
    accounts = kernel.accounts
    accounts.add_user("me", "Me", "correct horse", "key-me")
    accounts.add_user("mom", "Mom", "battery staple", "key-mom")
    accounts.add_user("sitter", "Sitter", "nanny pass", "key-sitter")
    accounts.join("home", "me", "member")
    accounts.join("home", "mom", "member")
    accounts.join("home", "sitter", "guest")
    yield kernel, diary
    kernel.data.close()
    kernel.db.close()


async def test_rows_get_the_standard_columns(house, clock):
    kernel, diary = house
    await diary.ctx.record("entries", {"text": "chai", "mood": 4, "tags": ["morning"], "shared": True}, user="me")
    found = await kernel.data.sql("SELECT at, ts, local_date, local_minute, weekday, user_id, text, mood, json_extract(tags, '$[0]'), shared FROM diary_entries", EVERYONE)
    now = clock.now
    assert found["rows"] == [[now.isoformat(), now.timestamp(), "2026-10-03", 23 * 60 + 50, now.isoweekday(), "me", "chai", 4, "morning", 1]]
    assert found["columns"][:3] == ["at", "ts", "local_date"] and found["truncated"] is False


async def test_a_source_records_only_what_it_declared(house):
    kernel, diary = house
    with pytest.raises(SourceError, match="doesn't declare"):
        await diary.ctx.record("pages", {"text": "x"})
    with pytest.raises(SourceError, match="has no columns colour"):
        await diary.ctx.record("entries", {"colour": "red"})


async def test_each_caller_sees_their_own_rows_and_the_house(house):
    kernel, diary = house
    await diary.ctx.record("entries", {"text": "mine"}, user="me")
    await diary.ctx.record("entries", {"text": "moms"}, user="mom")
    await diary.ctx.record("entries", {"text": "house"})
    await diary.ctx.record("entries", {"text": "sitters"}, user="sitter")

    async def texts(user: str) -> list[str]:
        result = await kernel.tools.call("data_sql", {"sql": "SELECT text FROM diary_entries ORDER BY text"}, Caller(kind="hermes", user=user))
        return [row[0] for row in result.data["rows"]]

    assert await texts("me") == ["house", "mine"]
    assert await texts("mom") == ["house", "moms"]
    # A guest sees only their own.
    assert await texts("sitter") == ["sitters"]
    counted = await kernel.tools.call("data_sql", {"sql": "SELECT count(*) FROM diary_entries"}, Caller(kind="hermes", user="me"))
    assert counted.data["rows"] == [[2]]


@pytest.mark.parametrize(
    "sql, error",
    [
        ("SELECT * FROM main.diary_entries", "prohibited"),
        ("SELECT count(*) FROM main.diary_entries", "not authorized"),
        ("SELECT * FROM kernel.users", "prohibited"),
        ("SELECT * FROM kernel.turns", "prohibited"),
        ("SELECT * FROM kernel.devices", "prohibited"),
        ("SELECT name FROM sqlite_master", "prohibited"),
        ("DELETE FROM diary_entries", "it is a view"),
        ("DELETE FROM main.diary_entries", "not authorized"),
        ("INSERT INTO diary_entries (text) VALUES ('x')", "not authorized"),
        ("PRAGMA table_info(diary_entries)", "not authorized"),
        ("ATTACH DATABASE 'x.db' AS x", "not authorized"),
        ("SELECT 1; SELECT 2", "one statement"),
    ],
)
async def test_data_sql_only_reads_through_the_views(house, sql, error):
    kernel, _ = house
    with pytest.raises(ToolError, match=error):
        await kernel.tools.call("data_sql", {"sql": sql}, Caller(kind="hermes", user="me"))


async def test_the_event_log_joins_with_the_same_keys(house, clock):
    kernel, diary = house
    await kernel.kernel_event("note", "me wrote something", user="me")
    await kernel.kernel_event("note", "mom wrote something", user="mom")
    await diary.ctx.record("entries", {"text": "chai"}, user="me")
    result = await kernel.data.sql(
        "SELECT e.summary, d.text FROM events e JOIN diary_entries d ON d.local_date = e.local_date AND d.local_minute = e.local_minute "
        "AND d.weekday = e.weekday AND d.user_id = e.user_id",
        kernel.accounts.scope("me", None),
    )
    assert result["rows"] == [["me wrote something", "chai"]]


async def test_rows_are_capped_and_slow_queries_stop(house, monkeypatch):
    kernel, diary = house
    for n in range(5):
        await diary.ctx.record("entries", {"mood": n})
    capped = await kernel.data.sql("SELECT mood FROM diary_entries", EVERYONE, limit=3)
    assert capped["rows"] == [[0], [1], [2]] and capped["truncated"] is True
    monkeypatch.setattr(data_module, "SQL_TIMEOUT", 0.05)
    with pytest.raises(DataError, match="ran longer"):
        await kernel.data.sql("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c", EVERYONE)


async def test_a_respec_adds_columns_and_keeps_rows(house):
    kernel, diary = house
    await diary.ctx.record("entries", {"text": "before"})
    diary.extra = True
    await diary.ctx.respec()
    await diary.ctx.record("entries", {"text": "after", "place": "balcony"})
    result = await kernel.data.sql("SELECT text, place FROM diary_entries", EVERYONE)
    assert result["rows"] == [["before", None], ["after", "balcony"]]
    assert kernel.events.search(type="source.respec")[0].summary == "diary changed what it offers: changed descriptions or arguments"


def test_tables_are_in_the_skill(house):
    kernel, _ = house
    text = skills.render(kernel.registry.spec("diary"))
    assert "## Tables" in text and "### `diary_entries`" in text
    assert "- `mood` (integer): 1 to 5" in text
    assert "Indexed on (ts), (user_id, ts), (mood, ts)." in text
