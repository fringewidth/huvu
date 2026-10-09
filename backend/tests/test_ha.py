"""The Home Assistant driver against a fake HA: scripts as actions, the
live tables of exposed entities and scripts, who caused each change, the
history rows, sensor buckets, and respec when the scripts change."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from huvu.config import Config
from huvu.data import DataError
from huvu.kernel import Kernel
from huvu.scope import EVERYONE
from huvu.source import SourceError
from huvu.spec import Caller
from huvu_drivers.ha import HomeAssistant
from huvu_drivers.ha import source as ha_source
from huvu_drivers.ha.client import HAError
from huvu_drivers.ha.history import SensorBuckets

from .fake_ha import Dropped, FakeHA

MOM = Caller(kind="hermes", user="mom")


async def until(condition, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.005)


async def settle() -> None:
    await asyncio.sleep(0.02)


@pytest.fixture
def ha(clock) -> FakeHA:
    ha = FakeHA(clock)
    ha.entity("climate.bedroom_ac", "off", area="bedroom", friendly_name="Bedroom AC", temperature=26, fan_mode="auto", assumed_state=True)
    ha.entity("binary_sensor.front_door", "off", area="hall", friendly_name="Front door", device_class="door")
    ha.entity("light.hall", "off", area="hall", friendly_name="Hall light", aliases=["corridor light"])
    ha.entity("sensor.bedroom_temperature", "28.5", area="bedroom", friendly_name="Bedroom temperature", device_class="temperature", unit_of_measurement="°C")
    ha.entity("lock.garage", "locked", exposed=False)
    ha.script(
        "bedroom_ac_set",
        alias="Bedroom AC set",
        description="Sets the bedroom AC.",
        labels=["huvu_silent"],
        area="bedroom",
        fields={
            "mode": {"description": "off, cool or dry.", "required": True, "selector": {"select": {"options": ["off", "cool", "dry"]}}},
            "temperature": {"description": "Target.", "selector": {"number": {"min": 16, "max": 30, "unit_of_measurement": "°C"}}},
        },
        sequence=[{"action": "climate.set_hvac_mode", "target": {"entity_id": "climate.bedroom_ac"}, "data": {"hvac_mode": "{{ mode }}"}}],
    )
    ha.effects["bedroom_ac_set"] = [("climate.bedroom_ac", "cool", {"temperature": 24})]
    ha.script("gate_open", alias="Gate open", description="Opens the gate.", labels=["huvu_human"])
    ha.script("tv_netflix", alias="TV Netflix", labels=["huvu_silnt"])
    ha.script("2nd_floor_off", alias="2nd floor off", description="Starts with a digit, so it can't be an action name.")
    return ha


@dataclass
class House:
    kernel: Kernel
    source: HomeAssistant
    task: asyncio.Task[None]


@pytest.fixture
async def house(tmp_path, clock, hermes, ha, monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "secret")
    monkeypatch.setattr(ha_source, "REFRESH_DEBOUNCE", 0.0)
    source = HomeAssistant({"users": {"ha-mom": "mom"}}, connect=ha.connect)
    kernel = Kernel(Config(data_dir=tmp_path), hermes=hermes, sources={"ha": source}, clock=clock)
    await kernel.triggers.load()
    task = asyncio.create_task(source.run())
    await until(lambda: source._connected)
    yield House(kernel, source, task)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await kernel.triggers.stop()
    await kernel.conversations.close()
    kernel.data.close()
    kernel.db.close()


async def test_scripts_become_actions(house, tmp_path):
    kernel = house.kernel
    actions = {a.name: a for a in kernel.registry.spec("ha").actions}
    assert set(actions) == {"bedroom_ac_set", "gate_open", "tv_netflix"}
    assert (actions["bedroom_ac_set"].autonomy, actions["bedroom_ac_set"].requires_human) == ("silent", False)
    assert actions["gate_open"].requires_human
    # No huvu label (a typo doesn't count): it asks.
    assert actions["tv_netflix"].autonomy == "ask"
    assert actions["bedroom_ac_set"].description == "Sets the bedroom AC. Area: Bedroom."
    schema = actions["bedroom_ac_set"].input_schema
    assert schema["required"] == ["mode"]
    assert schema["properties"]["mode"] == {"type": "string", "enum": ["off", "cool", "dry"], "description": "off, cool or dry."}
    assert schema["properties"]["temperature"] == {"type": "number", "minimum": 16, "maximum": 30, "description": "In °C. Target."}
    [[problems]] = (await kernel.data.sql("SELECT problems FROM ha_setup", EVERYONE)).rows
    problems = json.loads(problems)
    assert any(p.startswith("left out script.2nd_floor_off") for p in problems)
    assert "script.tv_netflix has no description; the model only sees its name" in problems
    assert any("huvu_silnt" in p for p in problems)
    skill = (tmp_path / "skills" / "huvu-ha" / "SKILL.md").read_text()
    assert "- `ha_bedroom_ac_set(mode: off|cool|dry, temperature?: number)`: Sets the bedroom AC. Area: Bedroom." in skill
    assert "### `ha_changes`" in skill and "### `ha_sensor_5min`" in skill and "### `ha_entities`" in skill
    [respec] = kernel.data.rows("huvu", "events", "type = 'source.respec'", ())
    assert respec["summary"] == "ha changed what it offers: added ha_bedroom_ac_set, ha_gate_open, ha_tv_netflix"


async def rows(kernel, sql: str) -> list[list]:
    return (await kernel.data.sql(sql, EVERYONE)).rows


async def test_exposed_entities_and_scripts_are_live_tables(house):
    kernel = house.kernel
    entities = await rows(kernel, "SELECT entity_id, state, number, attributes, assumed FROM ha_entities ORDER BY entity_id")
    assert [r[0] for r in entities] == ["binary_sensor.front_door", "climate.bedroom_ac", "light.hall", "sensor.bedroom_temperature"]
    ac = entities[1]
    assert ac[1] == "off" and ac[2] is None and json.loads(ac[3]) == {"temperature": 26, "fan_mode": "auto", "assumed_state": True} and ac[4] == 1
    assert entities[3][2] == 28.5
    found = await rows(kernel, "SELECT entity_id, name, area FROM ha_entities WHERE name LIKE '%bedroom ac%'")
    assert found == [["climate.bedroom_ac", "Bedroom AC", "Bedroom"]]
    assert await rows(kernel, "SELECT entity_id FROM ha_entities WHERE aliases LIKE '%corridor%'") == [["light.hall"]]
    works = await rows(kernel, "SELECT s.action FROM ha_scripts s, json_each(s.targets) t WHERE t.value = 'climate.bedroom_ac' AND s.usable")
    assert works == [["ha_bedroom_ac_set"]]
    by_area = await rows(kernel, "SELECT action FROM ha_scripts WHERE area = 'Bedroom' AND usable")
    assert by_area == [["ha_bedroom_ac_set"]]


async def test_an_action_runs_its_script_and_records_who(house, ha):
    kernel = house.kernel
    done = await kernel.tools.call("act", {"name": "ha_bedroom_ac_set", "args": {"mode": "cool", "temperature": 24}, "reason": "hot"}, MOM)
    assert done.data == {"status": "done", "script": "script.bedroom_ac_set"}
    assert ha.calls[0]["service"] == "bedroom_ac_set" and ha.calls[0]["service_data"] == {"mode": "cool", "temperature": 24}
    runs = await rows(kernel, "SELECT script, actor, user_id, ha_user, fields FROM ha_script_runs")
    assert runs == [["bedroom_ac_set", "huvu", "mom", FakeHA.OWN_USER, json.dumps({"mode": "cool", "temperature": 24})]]
    changes = await rows(kernel, "SELECT entity_id, from_state, to_state, actor, user_id, assumed, json_extract(attributes, '$.temperature') FROM ha_changes")
    assert changes == [["climate.bedroom_ac", "off", "cool", "huvu", "mom", 1, 24]]
    assert await rows(kernel, "SELECT state FROM ha_entities WHERE entity_id = 'climate.bedroom_ac'") == [["cool"]]


async def test_an_action_that_ha_refuses_fails(house, ha):
    del ha.scripts["script.gate_open"]
    ha.calls.clear()
    original = ha._call_service

    def refuse(message):
        raise KeyError("script.gate_open")

    ha._call_service = refuse
    with pytest.raises(SourceError, match="Home Assistant refused script.gate_open"):
        await house.source.act("gate_open", {}, MOM)
    ha._call_service = original


async def test_every_change_says_who_caused_it(house, ha, clock):
    kernel = house.kernel
    ha.set("light.hall", "on", user="ha-mom", brightness=200)
    clock.advance(minutes=1)
    ha.set("light.hall", "off", parent="automation-run")
    clock.advance(minutes=1)
    ha.set("binary_sensor.front_door", "on")
    clock.advance(minutes=1)
    ha.set("light.hall", "off", brightness=120)
    ha.set("lock.garage", "unlocked")
    await settle()
    changes = await rows(kernel, "SELECT entity_id, to_state, actor, user_id, ha_user, attributes FROM ha_changes ORDER BY ts")
    assert changes == [
        ["light.hall", "on", "ha_user", "mom", "ha-mom", '{"brightness": 200}'],
        ["light.hall", "off", "automation", None, None, '{"brightness": 200}'],
        ["binary_sensor.front_door", "on", "device", None, None, None],
        ["light.hall", "off", "device", None, None, '{"brightness": 120}'],
    ]


async def test_a_door_opening_runs_a_trigger_at_once(house, ha, hermes):
    kernel = house.kernel
    kernel.accounts.add_user("mom", "Mom", "battery staple", "key-mom")
    kernel.accounts.join("home", "mom", "member")
    sql = "SELECT id, entity_id FROM ha_changes WHERE device_class = 'door' AND to_state = 'on'"
    script = 'def run(ctx):\n    ctx.wake("Door: " + ctx.rows[0]["entity_id"])\n'
    await kernel.tools.call("triggers_register", {"note": "Front door.", "sql": sql, "fire": "rows", "once": False, "script": script}, MOM)
    ha.set("binary_sensor.front_door", "on")
    await until(lambda: any(s.startswith("wake-") for s, _ in hermes.messages), timeout=10)
    [message] = [m for s, m in hermes.messages if s.startswith("wake-")]
    assert "Door: binary_sensor.front_door" in message
    await kernel.triggers.settle()


async def test_a_sensor_reading_runs_a_trigger_at_once(house, ha, hermes):
    kernel = house.kernel
    kernel.accounts.add_user("mom", "Mom", "battery staple", "key-mom")
    kernel.accounts.join("home", "mom", "member")
    sql = "SELECT number FROM ha_entities WHERE entity_id = 'sensor.bedroom_temperature' AND number > 30"
    script = 'def run(ctx):\n    ctx.wake("Hot: " + str(ctx.rows[0]["number"]))\n'
    await kernel.tools.call("triggers_register", {"note": "Too hot.", "sql": sql, "fire": "edge", "once": False, "script": script}, MOM)
    # Nothing runs the trigger but the reading itself.
    ha.set("sensor.bedroom_temperature", "31.5")
    await until(lambda: any(s.startswith("wake-") for s, _ in hermes.messages), timeout=10)
    assert "Hot: 31.5" in [m for s, m in hermes.messages if s.startswith("wake-")][0]
    await kernel.triggers.settle()


async def test_a_script_run_from_the_app_is_a_row(house, ha, clock):
    kernel = house.kernel
    ha.set("script.tv_netflix", "on", user="ha-mom", last_triggered=clock.now.isoformat())
    await settle()
    assert await rows(kernel, "SELECT script, actor, user_id, fields FROM ha_script_runs") == [["tv_netflix", "ha_user", "mom", None]]


def test_sensor_buckets_are_time_weighted():
    buckets = SensorBuckets()
    start = datetime(2026, 10, 6, 10, 1).astimezone()
    assert buckets.add("s", 20.0, start) == []
    assert buckets.add("s", 30.0, start + timedelta(minutes=2)) == []
    rows = buckets.flush(start + timedelta(minutes=10))
    assert [(at.strftime("%H:%M"), row) for _, at, row in rows] == [
        ("10:00", {"mean": 25.0, "low": 20.0, "high": 30.0, "last": 30.0, "changes": 2}),
        ("10:05", {"mean": 30.0, "low": 30.0, "high": 30.0, "last": 30.0, "changes": 0}),
    ]
    dropped = buckets.drop("s", start + timedelta(minutes=11))
    assert [(at.strftime("%H:%M"), row["mean"]) for _, at, row in dropped] == [("10:10", 30.0)]
    assert buckets.flush(start + timedelta(hours=1)) == []


async def test_sensors_land_in_5_minute_rows(house, ha, clock):
    kernel, source = house.kernel, house.source
    clock.advance(minutes=2)
    ha.set("sensor.bedroom_temperature", "26.5")
    await settle()
    await source._write_buckets(source._buckets.flush(clock.advance(minutes=8)))
    buckets = await rows(kernel, "SELECT local_minute, entity_id, area, unit, mean, low, high, changes FROM ha_sensor_5min ORDER BY ts")
    assert buckets == [
        # 28.5 for 2 minutes, then 26.5 for 3.
        [23 * 60 + 50, "sensor.bedroom_temperature", "Bedroom", "°C", 27.3, 26.5, 28.5, 2],
        [23 * 60 + 55, "sensor.bedroom_temperature", "Bedroom", "°C", 26.5, 26.5, 26.5, 0],
    ]
    # A number sensor is in the buckets, not in the changes.
    assert await rows(kernel, "SELECT count(*) FROM ha_changes") == [[0]]


async def test_a_new_script_in_ha_becomes_an_action(house, ha, tmp_path, clock):
    kernel = house.kernel
    clock.advance(minutes=1)
    ha.script("fan_on", alias="Fan on", description="Turns the bedroom fan on.", labels=["huvu_silent"], area="bedroom")
    ha.fire("entity_registry_updated", {"action": "create", "entity_id": "script.fan_on"})
    await until(lambda: "ha_fan_on" in kernel.tools.names())
    assert "`ha_fan_on()`: Turns the bedroom fan on. Area: Bedroom." in (tmp_path / "skills" / "huvu-ha" / "SKILL.md").read_text()
    respec = kernel.data.rows("huvu", "events", "type = 'source.respec'", ())
    assert respec[-1]["summary"] == "ha changed what it offers: added ha_fan_on"
    cached = json.loads((tmp_path / "sources" / "ha" / "scripts.json").read_text())
    assert "fan_on" in {s["object_id"] for s in cached}


async def test_the_actions_exist_before_ha_connects(house, tmp_path, hermes, clock, ha):
    later = HomeAssistant({}, connect=ha.connect)
    kernel = Kernel(Config(data_dir=tmp_path), hermes=hermes, sources={"ha": later}, clock=clock)
    try:
        assert {a.name for a in kernel.registry.spec("ha").actions} == {"bedroom_ac_set", "gate_open", "tv_netflix"}
        with pytest.raises(DataError, match="ha_entities: ha hasn't handed this table over yet"):
            await kernel.data.sql("SELECT * FROM ha_entities", EVERYONE)
        with pytest.raises(SourceError, match="not connected"):
            await later.act("bedroom_ac_set", {"mode": "cool"}, MOM)
    finally:
        kernel.data.close()
        kernel.db.close()


async def test_a_drop_ends_the_run_and_the_tables_say_so(house, ha):
    kernel = house.kernel
    ha.drop()
    with pytest.raises(Dropped):
        await house.task
    with pytest.raises(DataError, match="not connected"):
        await kernel.data.sql("SELECT * FROM ha_entities", EVERYONE)
    assert (await kernel.data.sql("SELECT connected FROM ha_setup", EVERYONE)).rows == [[0]]


async def test_a_bad_token_or_a_plain_user_is_refused(tmp_path, clock, hermes, ha, monkeypatch):
    monkeypatch.setenv("HA_TOKEN", "wrong")
    source = HomeAssistant({}, connect=ha.connect)
    kernel = Kernel(Config(data_dir=tmp_path), hermes=hermes, sources={"ha": source}, clock=clock)
    try:
        with pytest.raises(HAError, match="Invalid access token"):
            await source.run()
        monkeypatch.setenv("HA_TOKEN", "secret")
        ha.admin = False
        with pytest.raises(SourceError, match="must be an administrator"):
            await source.run()
    finally:
        kernel.data.close()
        kernel.db.close()
