"""The location source: OwnTracks posts, waypoints, and privacy between
people."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from huvu.config import Config
from huvu.http import create_app
from huvu.kernel import Kernel
from huvu.spec import Caller
from huvu.tools import ToolError
from huvu.triggers import TriggerSpec
from huvu_drivers.location import LocationSource, setup_link

from .test_triggers import settle, wakes
from .fakes import WAKE

HOME = {"lat": 17.4, "lon": 78.4, "radius_m": 150}
# About 1.1 km north of home.
NEARBY = (17.41, 78.4)

MY_TRACKER = Caller(kind="device", user="me", device="t_me")
MOMS_TRACKER = Caller(kind="device", user="mom", device="t_mom")
ME = Caller(kind="hermes", session="s-me", user="me")
MOM = Caller(kind="hermes", session="s-mom", user="mom")

pytestmark = pytest.mark.usefixtures("family")


@pytest.fixture
async def kernel(tmp_path, clock, home, hermes):
    location = LocationSource({"home": HOME, "near_home_km": 3})
    k = Kernel(Config(data_dir=tmp_path), hermes=hermes, sources={"home": home, "location": location}, clock=clock)
    await home.run()
    await location.run()
    await k.triggers.load()
    yield k
    await k.triggers.stop()
    await k.conversations.close()
    k.data.close()
    k.db.close()


@pytest.fixture
def location(kernel) -> LocationSource:
    return kernel.registry.get("location")


def fix(clock, lat: float = HOME["lat"], lon: float = HOME["lon"], **extra) -> dict:
    return {"_type": "location", "lat": lat, "lon": lon, "tst": int(clock.now.timestamp()), "acc": 20, "batt": 80, "SSID": "homenet", **extra}


def crossing(clock, event: str, desc: str = "home") -> dict:
    return {"_type": "transition", "event": event, "desc": desc, "tst": int(clock.now.timestamp()), "lat": HOME["lat"], "lon": HOME["lon"], "acc": 30}


async def sql(kernel, caller: Caller, query: str) -> list[list]:
    return (await kernel.tools.call("data_sql", {"sql": query}, caller)).data["rows"]


async def place(kernel, caller: Caller = ME) -> list[list]:
    return await sql(kernel, caller, "SELECT place FROM location_people")


def waypoint_names(reply: list[dict]) -> list[str]:
    sets = [c for c in reply if c.get("action") == "setWaypoints"]
    return [w["desc"] for w in sets[-1]["waypoints"]["waypoints"]] if sets else []


async def test_a_phone_gets_its_waypoints_once(location, clock):
    first = await location.ingest(fix(clock), MY_TRACKER)
    assert waypoint_names(first) == ["home", "near_home"]
    assert first[0]["waypoints"]["waypoints"][0]["rad"] == 150
    clock.advance(minutes=15)
    assert await location.ingest(fix(clock), MY_TRACKER) == []


async def test_an_arrival_is_only_its_persons(kernel, location, clock):
    await location.ingest(crossing(clock, "enter"), MY_TRACKER)
    query = "SELECT event, waypoint, user_id FROM location_transitions WHERE changed = 1"
    assert await sql(kernel, ME, query) == [["enter", "home", "me"]]
    assert await sql(kernel, MOM, query) == []
    assert await place(kernel) == [["home"]]
    # Mom sees no row of mine, even asking for it.
    assert await sql(kernel, MOM, "SELECT place FROM location_people WHERE user_id = 'me'") == []

    clock.advance(hours=2)
    await location.ingest(crossing(clock, "leave"), MY_TRACKER)
    assert await sql(kernel, ME, "SELECT stayed_min FROM location_transitions WHERE event = 'leave'") == [[120]]
    assert await place(kernel) == [["elsewhere"]]


async def test_repeated_and_late_crossings_change_nothing(kernel, location, clock):
    entered = crossing(clock, "enter")
    await location.ingest(entered, MY_TRACKER)
    clock.advance(minutes=10)
    left = crossing(clock, "leave")
    await location.ingest(left, MY_TRACKER)
    # OwnTracks sends the same post twice.
    await location.ingest(left, MY_TRACKER)
    # A crossing older than the latest, from a queue the phone sent late.
    late = {**entered, "tst": entered["tst"] + 300}
    await location.ingest(late, MY_TRACKER)
    rows = await sql(kernel, ME, "SELECT event, changed FROM location_transitions ORDER BY id")
    assert rows == [["enter", 1], ["leave", 1], ["enter", 0]]
    assert await place(kernel) == [["elsewhere"]]


async def test_a_late_crossing_says_so(kernel, location, clock):
    sent = crossing(clock, "enter")
    clock.advance(minutes=25)
    await location.ingest(sent, MY_TRACKER)
    assert await sql(kernel, ME, "SELECT delay_min, ts FROM location_transitions") == [[25, sent["tst"]]]


async def test_nobody_triggers_on_someone_elses_location(kernel, location, clock, hermes):
    near = "SELECT id FROM location_transitions WHERE changed = 1 AND event = 'enter' AND waypoint = 'near_home'"
    await kernel.triggers.register(TriggerSpec.model_validate({"note": "Mom is nearly home.", "script": WAKE, "sql": near, "fire": "rows", "once": False}), MOM)
    await kernel.triggers.register(TriggerSpec.model_validate({"note": "Porch light on.", "script": WAKE, "sql": near, "fire": "rows", "once": False}), ME)
    await location.ingest(crossing(clock, "enter", "near_home"), MY_TRACKER)
    await settle(kernel)
    assert len(wakes(hermes)) == 1
    assert "Porch light on." in wakes(hermes)[0]
    # A query for my place, as mom, finds nothing to fire on.
    peek = {"note": "Tell mom where he is.", "script": WAKE, "sql": "SELECT 1 FROM location_people WHERE user_id = 'me'", "fire": "edge", "once": False}
    report = (await kernel.tools.call("triggers_register", {**peek, "dry_run": True}, MOM)).data
    assert report["rows_now"] == 0


async def test_people_follow_the_latest_fix(kernel, location, clock):
    await location.ingest(fix(clock, *NEARBY, batt=55, SSID=None), MY_TRACKER)
    older = fix(clock, *NEARBY)
    older["tst"] -= 3600
    await location.ingest(older, MY_TRACKER)
    [[distance, battery, wifi, where, fix_at]] = await sql(kernel, ME, "SELECT distance_home_km, battery, wifi, place, fix_at FROM location_people")
    assert distance == pytest.approx(1.11, abs=0.01)
    assert (battery, wifi, where) == (55, None, "elsewhere") and fix_at == clock.now.isoformat()
    assert await sql(kernel, MOM, "SELECT * FROM location_people") == []


async def test_where_everyone_is_survives_a_restart(kernel, location, clock, tmp_path, hermes):
    await location.ingest(crossing(clock, "enter"), MY_TRACKER)
    again = Kernel(Config(data_dir=tmp_path), hermes=hermes, sources={"location": LocationSource({"home": HOME})}, clock=clock)
    try:
        from huvu.scope import EVERYONE

        await again.registry.get("location").run()
        assert (await again.data.sql("SELECT place FROM location_people", EVERYONE)).rows == [["home"]]
    finally:
        again.data.close()
        again.db.close()


async def test_a_waypoint_from_a_window_is_the_median_of_its_fixes(kernel, location, clock):
    start = clock.now
    for lat in (17.4501, 17.4502, 17.4500, 17.4503, 17.6):
        await location.ingest(fix(clock, lat, 78.45), MY_TRACKER)
        clock.advance(minutes=15)
    args = {"name": "Grandma's  Place", "from": start.isoformat(), "until": clock.now.isoformat()}
    added = await kernel.tools.call("location_waypoint_add", args, ME)
    assert added.data["name"] == "grandma's place"
    assert added.data["lat"] == pytest.approx(17.4502)
    assert added.data["centre_from"]["fixes"] == 5
    assert added.data["owner"] == "me"

    assert waypoint_names(await location.ingest(fix(clock), MY_TRACKER)) == ["grandma's place", "home", "near_home"]
    # Mom's phone doesn't get it until it's shared.
    assert "grandma's place" not in waypoint_names(await location.ingest(fix(clock), MOMS_TRACKER))
    assert await sql(kernel, MOM, "SELECT name FROM location_waypoints WHERE name = 'grandma''s place'") == []
    await kernel.tools.call("location_waypoint_share", {"name": "grandma's place", "share": True}, ME)
    assert "grandma's place" in waypoint_names(await location.ingest(fix(clock), MOMS_TRACKER))
    assert await sql(kernel, MOM, "SELECT name, named_by FROM location_waypoints WHERE name = 'grandma''s place'") == [["grandma's place", "me"]]

    with pytest.raises(ToolError, match="exists already"):
        await kernel.tools.call("location_waypoint_add", {"name": "grandma's place", "lat": 1, "lon": 2}, ME)
    with pytest.raises(ToolError, match="sent no location"):
        await kernel.tools.call("location_waypoint_add", {"name": "office", "from": "2026-01-01T09:00", "until": "2026-01-01T17:00"}, ME)


async def test_removed_waypoints_are_cleared_on_phones(kernel, location, clock):
    await location.ingest(fix(clock, *NEARBY), MY_TRACKER)
    added = await kernel.tools.call("location_waypoint_add", {"name": "office"}, ME)
    assert added.data["lat"] == NEARBY[0]
    assert "office" in waypoint_names(await location.ingest(fix(clock), MY_TRACKER))
    await location.ingest(crossing(clock, "enter", "office"), MY_TRACKER)
    assert await place(kernel) == [["office"]]

    await kernel.tools.call("location_waypoint_remove", {"name": "office"}, ME)
    assert await place(kernel) == [["elsewhere"]]
    reply = await location.ingest(fix(clock), MY_TRACKER)
    assert reply[0] == {"_type": "cmd", "action": "clearWaypoints"}
    assert waypoint_names(reply) == ["home", "near_home"]
    # The region stays on an Android phone; its crossings are ignored.
    clock.advance(minutes=1)
    await location.ingest(crossing(clock, "leave", "office"), MY_TRACKER)
    assert await sql(kernel, ME, "SELECT count(*) FROM location_transitions WHERE event = 'leave' AND changed = 1") == [[0]]

    with pytest.raises(ToolError, match="config"):
        await kernel.tools.call("location_waypoint_remove", {"name": "home"}, ME)


async def test_trackers_post_with_basic_auth_and_do_nothing_else(kernel, clock):
    device, token = kernel.accounts.add_tracker("me", "my phone")
    app = create_app(kernel.config, kernel)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/api/ingest/location", json=fix(clock), auth=("me", token))
        assert res.status_code == 200
        assert waypoint_names(res.json()) == ["home", "near_home"]
        # A payload huvu can't read is dropped, so it can't jam the phone's queue.
        res = await client.post("/api/ingest/location", json={"_type": "location", "lat": "x"}, auth=("me", token))
        assert (res.status_code, res.json()) == (200, [])

        assert (await client.post("/api/ingest/location", json=fix(clock), auth=("mom", token))).status_code == 401
        assert (await client.post("/api/ingest/location", json=fix(clock), auth=("me", "wrong"))).status_code == 401
        assert (await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})).status_code == 403
        assert (await client.post("/api/ingest/home", json={}, auth=("me", token))).status_code == 404
        assert (await client.post("/api/ingest/nowhere", json={}, auth=("me", token))).status_code == 404
    mine = await kernel.tools.call("data_sql", {"sql": "SELECT device FROM location_fixes"}, ME)
    assert mine.data["rows"] == [[device.id]]


async def test_history_is_in_tables_only_its_person_reads(kernel, location, clock):
    await location.ingest(fix(clock, *NEARBY), MY_TRACKER)
    await location.ingest(crossing(clock, "enter"), MY_TRACKER)
    # The same post again, and a region made on the phone itself.
    await location.ingest(crossing(clock, "enter"), MY_TRACKER)
    clock.advance(minutes=5)
    await location.ingest(crossing(clock, "enter", "gym"), MY_TRACKER)
    await location.ingest(fix(clock), MOMS_TRACKER)

    fixes = await kernel.tools.call("data_sql", {"sql": "SELECT user_id, lat, wifi, battery FROM location_fixes"}, ME)
    assert fixes.data["rows"] == [["me", NEARBY[0], "homenet", 80]]
    crossings = await kernel.tools.call("data_sql", {"sql": "SELECT waypoint, name_on_phone, event, changed FROM location_transitions ORDER BY ts"}, ME)
    assert crossings.data["rows"] == [["home", "home", "enter", 1], [None, "gym", "enter", 0]]
    moms = await kernel.tools.call("data_sql", {"sql": "SELECT count(*) FROM location_fixes WHERE user_id = 'me'"}, MOM)
    assert moms.data["rows"] == [[0]]

    skill = (kernel.config.skills_folder / "huvu-location" / "SKILL.md")
    kernel.write_skills()
    assert "### `location_fixes`" in skill.read_text() and "### `location_people`" in skill.read_text()


def test_the_setup_link_carries_the_tracker():
    link = setup_link("https://huvu.example/api/ingest/location", "mom", "d_123", "secret")
    config = json.loads(base64.b64decode(link.removeprefix("owntracks:///config?inline=")))
    assert config["mode"] == 3
    assert (config["username"], config["password"], config["deviceId"], config["tid"]) == ("mom", "secret", "d_123", "MO")
    assert config["url"] == "https://huvu.example/api/ingest/location"
    assert config["monitoring"] == 1 and config["cmd"] is True
