"""The Home Assistant driver against a fake HA: scripts as actions, reads
and search over exposed entities, who caused each change, the history
rows, sensor buckets, and respec when the scripts change."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from huvu.config import Config
from huvu.events import EVERYONE
from huvu.kernel import Kernel
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
    ha.script("history", alias="History", description="Clashes with the query.")
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
    problems = (await kernel.tools.call("query", {"name": "ha_problems"}, MOM)).data["problems"]
    assert any(p.startswith("left out script.history") for p in problems)
    assert "script.tv_netflix has no description; the model only sees its name" in problems
    assert any("huvu_silnt" in p for p in problems)
    skill = (tmp_path / "skills" / "huvu-ha" / "SKILL.md").read_text()
    assert "- `ha_bedroom_ac_set(mode: off|cool|dry, temperature?: number)`: Sets the bedroom AC. Area: Bedroom." in skill
    assert "### `ha_changes`" in skill and "### `ha_sensor_5min`" in skill
    respec = kernel.events.search(type="source.respec")[0]
    assert respec.summary == "ha changed what it offers: added ha_bedroom_ac_set, ha_gate_open, ha_tv_netflix"


async def test_reads_and_search_cover_exposed_entities(house):
    kernel = house.kernel
    keys = [
        "ha.entity_state[climate.bedroom_ac]",
        "ha.entity_number[sensor.bedroom_temperature]",
        "ha.entity_attributes[climate.bedroom_ac]",
        "ha.entity_number[light.hall]",
        "ha.entity_state[lock.garage]",
    ]
    got = await kernel.state.get(keys)
    assert got[keys[0]].value == "off" and got[keys[0]].at is not None
    assert got[keys[1]].value == 28.5
    assert got[keys[2]].value == {"temperature": 26, "fan_mode": "auto", "assumed_state": True}
    assert got[keys[3]].value is None and "not a number" in got[keys[3]].reason
    assert got[keys[4]].value is None and "not an exposed Home Assistant entity" in got[keys[4]].reason
    found = await kernel.state.search("bedroom ac")
    ac = next(m for m in found if m.value == "ha.entity_state[climate.bedroom_ac]")
    assert ac.name == "Bedroom AC (assumed state)" and ac.area == "Bedroom" and ac.actions == ["ha_bedroom_ac_set"]
    assert any(m.value == "ha.entity_state[script.bedroom_ac_set]" for m in found)
    assert [m.value for m in await kernel.state.search("corridor")] == ["ha.entity_state[light.hall]"]
    assert not any("garage" in m.value for m in await kernel.state.search("garage"))


async def test_an_action_runs_its_script_and_records_who(house, ha):
    kernel = house.kernel
    done = await kernel.tools.call("act", {"name": "ha_bedroom_ac_set", "args": {"mode": "cool", "temperature": 24}, "reason": "hot"}, MOM)
    assert done.data == {"status": "done", "script": "script.bedroom_ac_set"}
    assert ha.calls[0]["service"] == "bedroom_ac_set" and ha.calls[0]["service_data"] == {"mode": "cool", "temperature": 24}
    runs = await kernel.data.sql("SELECT script, actor, user_id, ha_user, fields FROM ha_script_runs", EVERYONE)
    assert runs["rows"] == [["bedroom_ac_set", "huvu", "mom", FakeHA.OWN_USER, json.dumps({"mode": "cool", "temperature": 24})]]
    changes = await kernel.data.sql(
        "SELECT entity_id, from_state, to_state, actor, user_id, assumed, json_extract(attributes, '$.temperature') FROM ha_changes", EVERYONE
    )
    assert changes["rows"] == [["climate.bedroom_ac", "off", "cool", "huvu", "mom", 1, 24]]
    # huvu's own runs are already action.ran events.
    assert not kernel.events.search(type="ha.script_run")
    assert (await kernel.state.get(["ha.entity_state[climate.bedroom_ac]"]))["ha.entity_state[climate.bedroom_ac]"].value == "cool"


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
    rows = await kernel.data.sql("SELECT entity_id, to_state, actor, user_id, ha_user, attributes FROM ha_changes ORDER BY ts", EVERYONE)
    assert rows["rows"] == [
        ["light.hall", "on", "ha_user", "mom", "ha-mom", '{"brightness": 200}'],
        ["light.hall", "off", "automation", None, None, '{"brightness": 200}'],
        ["binary_sensor.front_door", "on", "device", None, None, None],
        ["light.hall", "off", "device", None, None, '{"brightness": 120}'],
    ]
    events = kernel.events.search(source="ha")
    assert [e.summary for e in events] == ["Front door: closed to open, by the device itself or someone at it"]
    assert events[0].data == {"entity_id": "binary_sensor.front_door", "domain": "binary_sensor", "device_class": "door", "area": "Hall", "from": "off", "to": "on", "actor": "device"}


async def test_a_script_run_from_the_app_is_an_event(house, ha, clock):
    kernel = house.kernel
    ha.set("script.tv_netflix", "on", user="ha-mom", last_triggered=clock.now.isoformat())
    await settle()
    runs = await kernel.data.sql("SELECT script, actor, user_id, fields FROM ha_script_runs", EVERYONE)
    assert runs["rows"] == [["tv_netflix", "ha_user", "mom", None]]
    event = kernel.events.search(type="ha.script_run")[0]
    assert event.summary == "mom ran TV Netflix" and event.user == "mom"


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
    rows = await kernel.data.sql("SELECT local_minute, entity_id, area, unit, mean, low, high, changes FROM ha_sensor_5min ORDER BY ts", EVERYONE)
    assert rows["rows"] == [
        # 28.5 for 2 minutes, then 26.5 for 3.
        [23 * 60 + 50, "sensor.bedroom_temperature", "Bedroom", "°C", 27.3, 26.5, 28.5, 2],
        [23 * 60 + 55, "sensor.bedroom_temperature", "Bedroom", "°C", 26.5, 26.5, 26.5, 0],
    ]
    # A number sensor is in the buckets, not in the changes.
    assert (await kernel.data.sql("SELECT count(*) FROM ha_changes", EVERYONE))["rows"] == [[0]]


async def test_a_new_script_in_ha_becomes_an_action(house, ha, tmp_path, clock):
    kernel = house.kernel
    clock.advance(minutes=1)
    ha.script("fan_on", alias="Fan on", description="Turns the bedroom fan on.", labels=["huvu_silent"], area="bedroom")
    ha.fire("entity_registry_updated", {"action": "create", "entity_id": "script.fan_on"})
    await until(lambda: "ha_fan_on" in kernel.tools.names())
    assert "`ha_fan_on()`: Turns the bedroom fan on. Area: Bedroom." in (tmp_path / "skills" / "huvu-ha" / "SKILL.md").read_text()
    assert kernel.events.search(type="source.respec")[0].summary == "ha changed what it offers: added ha_fan_on"
    cached = json.loads((tmp_path / "sources" / "ha" / "scripts.json").read_text())
    assert "fan_on" in {s["object_id"] for s in cached}


async def test_the_actions_exist_before_ha_connects(house, tmp_path, hermes, clock, ha):
    later = HomeAssistant({}, connect=ha.connect)
    kernel = Kernel(Config(data_dir=tmp_path), hermes=hermes, sources={"ha": later}, clock=clock)
    try:
        assert {a.name for a in kernel.registry.spec("ha").actions} == {"bedroom_ac_set", "gate_open", "tv_netflix"}
        reading = (await kernel.state.get(["ha.entity_state[light.hall]"]))["ha.entity_state[light.hall]"]
        assert reading.value is None and reading.reason == "Home Assistant is not connected right now"
        with pytest.raises(SourceError, match="not connected"):
            await later.act("bedroom_ac_set", {"mode": "cool"}, MOM)
    finally:
        kernel.data.close()
        kernel.db.close()


async def test_watchers_hear_changes_and_a_drop_ends_the_run(house, ha):
    kernel = house.kernel
    heard = []

    async def watcher(ref, reading):
        heard.append(reading.value)

    await kernel.state.watch("ha.entity_state[binary_sensor.front_door]", watcher)
    ha.set("binary_sensor.front_door", "on")
    await until(lambda: heard)
    assert heard == ["on"]
    ha.drop()
    with pytest.raises(Dropped):
        await house.task
    reading = (await kernel.state.get(["ha.entity_state[light.hall]"]))["ha.entity_state[light.hall]"]
    assert reading.reason == "Home Assistant is not connected right now"


async def test_history_reads_the_recorder(house, ha, clock):
    stamp = clock.now.timestamp()
    ha.history["light.hall"] = [{"s": "on", "lu": stamp - 60}, {"s": "off", "lu": stamp}]
    result = await house.kernel.tools.call("query", {"name": "ha_history", "args": {"entity_id": "light.hall", "hours": 2}}, MOM)
    assert [c["state"] for c in result.data["changes"]] == ["on", "off"]
    assert result.data["changes"][1]["at"] == clock.now


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
