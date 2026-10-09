from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from huvu.registry import UnknownName
from huvu.source import SourceError
from huvu.spec import Caller
from huvu.times import format_time, parse_when

OWNER = Caller(kind="hermes", user="me")


def test_unknown_actions_suggest_the_nearest_real_one(kernel):
    with pytest.raises(UnknownName, match="Did you mean turn_on"):
        kernel.registry.resolve_action("home", "turn_onn")


def test_times_for_the_model():
    now = datetime(2026, 10, 3, 14, 30).astimezone()
    assert format_time(now - timedelta(minutes=6), now) == "14:24, 6 min ago"
    assert format_time(now - timedelta(hours=3), now) == "11:30, 3 h ago"
    assert format_time(now - timedelta(hours=20), now) == "yesterday 18:30, 20 h ago"
    assert format_time(now - timedelta(days=3), now) == "Wed 14:30, 3 days ago"
    assert format_time(now - timedelta(days=9), now) == "2026-09-24, 9 days ago"
    assert format_time(now + timedelta(minutes=20), now) == "14:50, in 20 min"
    assert format_time(now, now) == "14:30, just now"


def test_relative_times_from_the_model():
    now = datetime(2026, 10, 3, 14, 30).astimezone()
    assert parse_when("2h", now) == now + timedelta(hours=2)
    assert parse_when("30 min", now) == now + timedelta(minutes=30)
    assert parse_when("tomorrow 09:00", now) == datetime(2026, 10, 4, 9, 0).astimezone()
    assert parse_when("09:00", now) == datetime(2026, 10, 4, 9, 0).astimezone()
    assert parse_when("18:00", now) == datetime(2026, 10, 3, 18, 0).astimezone()
    with pytest.raises(ValueError, match="can't read the time"):
        parse_when("soonish", now)


@pytest.mark.usefixtures("family")
async def test_live_tables_answer_with_what_is_true_now(kernel, home):
    query = "SELECT entity_id, state FROM home_lights WHERE entity_id = 'light.porch'"
    first = await kernel.tools.call("data_sql", {"sql": query}, OWNER)
    assert first.data["rows"] == [["light.porch", "off"]]
    home.lights["light.porch"]["state"] = "on"
    again = await kernel.tools.call("data_sql", {"sql": query}, OWNER)
    assert again.data["rows"] == [["light.porch", "on"]]


@pytest.mark.usefixtures("family")
async def test_live_and_history_join_and_the_time_is_local(kernel, home, clock):
    await home.press("a courier")
    sql = (
        "SELECT d.note, s.battery, now_minute(), now_date(), now_weekday() "
        "FROM home_doorbell d, home_status s WHERE d.ts <= now_ts()"
    )
    result = await kernel.tools.call("data_sql", {"sql": sql}, OWNER)
    assert result.data["rows"] == [["a courier", 80, 23 * 60 + 50, "2026-10-03", 6]]
    assert result.labels == ["external", "house"]


async def test_a_source_that_cant_answer_fails_the_query_by_name(kernel, home, family):
    home.down = True
    from huvu.tools import ToolError

    with pytest.raises(ToolError, match="home_lights: the house is offline"):
        await kernel.tools.call("data_sql", {"sql": "SELECT * FROM home_lights"}, OWNER)


async def test_history_rows_only_go_into_declared_history_tables(kernel, home):
    with pytest.raises(SourceError, match="history table"):
        await home.ctx.record("lights", {"entity_id": "light.porch"})
    with pytest.raises(SourceError, match="no columns"):
        await home.ctx.record("doorbell", {"colour": "red"})


@pytest.mark.usefixtures("family")
async def test_aliases_are_a_table(kernel):
    await kernel.huvu.act("set_alias", {"alias": "Hall light", "target": "light.light_3"}, Caller(kind="kernel"))
    result = await kernel.tools.call("data_sql", {"sql": "SELECT alias, target FROM huvu_aliases"}, OWNER)
    assert result.data["rows"] == [["hall light", "light.light_3"]]
    mode = await kernel.tools.call("data_sql", {"sql": "SELECT mode FROM huvu_mode"}, OWNER)
    assert mode.data["rows"] == [["normal"]]


@pytest.mark.usefixtures("family")
async def test_the_kernels_log_is_a_table(kernel):
    await kernel.kernel_event("started", "kernel started")
    result = await kernel.tools.call("data_sql", {"sql": "SELECT type, summary FROM huvu_events"}, OWNER)
    assert result.data["rows"] == [["started", "kernel started"]]


def test_the_backend_never_imports_sqlite3():
    # Two copies of SQLite in one process can corrupt a shared file, and APSW
    # brings its own (see db.py).
    import pathlib
    import re

    root = pathlib.Path(__file__).parents[1] / "src"
    found = [str(p.relative_to(root)) for p in root.rglob("*.py") if re.search(r"^\s*(import|from)\s+sqlite3\b", p.read_text(), re.M)]
    assert found == []
