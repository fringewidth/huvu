from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from huvu.names import UnknownName
from huvu.times import format_time, parse_when


def test_value_keys_resolve_by_position_and_by_name(kernel):
    ref, spec = kernel.registry.resolve_value("home.light[light.porch]")
    assert ref.key == "home.light[light.porch]"
    assert ref.params_dict == {"entity_id": "light.porch"}
    same, _ = kernel.registry.resolve_value("home.light", {"entity_id": "light.porch"})
    assert same == ref
    named, _ = kernel.registry.resolve_value("home.light[entity_id=light.porch]")
    assert named == ref
    assert spec.type == "string"


def test_unknown_names_suggest_the_nearest_real_one(kernel):
    with pytest.raises(UnknownName, match="Did you mean home.light"):
        kernel.registry.resolve_value("home.lights[light.porch]")
    with pytest.raises(UnknownName, match="takes params entity_id"):
        kernel.registry.resolve_value("home.light")


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


async def test_events_search_and_digest(kernel, clock):
    ctx = kernel.registry.get("home").ctx
    await ctx.emit("doorbell", "doorbell pressed", refs={"snapshot": "home:snap/1"})
    clock.advance(minutes=5)
    await ctx.emit("unlock", "front door unlocked by fingerprint (Hrishik)", data={"method": "fingerprint", "person": "Hrishik"})
    found = kernel.events.search("fingerprint")
    assert [e.type for e in found] == ["home.unlock"]
    newest_first = kernel.events.search(source="home")
    assert [e.type for e in newest_first] == ["home.unlock", "home.doorbell"]
    start = clock.now.replace(hour=0, minute=0)
    digest = kernel.events.digest(start, start + timedelta(days=1))
    assert digest["total"] == 2
    assert {t["type"] for t in digest["by_type"]} == {"home.doorbell", "home.unlock"}


async def test_undeclared_event_types_are_refused(kernel):
    with pytest.raises(Exception, match="doesn't declare"):
        await kernel.registry.get("home").ctx.emit("explosion", "boom")


async def test_state_reads_pushed_values_with_their_age(kernel, clock):
    ctx = kernel.registry.get("home").ctx
    await ctx.update("light", "off", params={"entity_id": "light.porch"})
    readings = await kernel.state.get(["home.light[light.porch]", "home.nothing"])
    assert readings["home.light[light.porch]"].value == "off"
    assert readings["home.light[light.porch]"].at == clock.now
    assert "unknown value" in readings["home.nothing"].reason


async def test_state_search_finds_aliases_first(kernel):
    await kernel.huvu.act("set_alias", {"alias": "hall light", "value": "home.light[light.light_3]"})
    results = await kernel.state.search("hall light")
    assert results[0].value == "home.light[light.light_3]"
    porch = await kernel.state.search("porch")
    assert porch[0].value == "home.light[light.porch]"
