"""The Blue Star integration in a test Home Assistant, with Blue Star's cloud faked.

The listing below is the shape `GET /things` returned for the home theatre's
AC on 2026-10-10, cut to what the integration reads.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bluestar_ac.api import BlueStarError, InvalidAuth, Login
from custom_components.bluestar_ac.const import CONF_PHONE, DOMAIN
from custom_components.bluestar_ac.hub import Hub

THING_ID = "d0cf13b4667c"
ENTITY_ID = "climate.home_theatre_ac"

LOGIN = Login(
    user_id="49201227-3623-4cec-9a14-4e50b455c7f1",
    name="Hrishik",
    session="session",
    endpoint="example-ats.iot.ap-south-1.amazonaws.com",
    access_key_id="AKIAEXAMPLE",
    secret_access_key="secret",
)

LISTING = {
    "things": [
        {
            "thing_id": THING_ID,
            "type": 1,
            "f_ver": "1.6.3",
            "user_config": {
                "name": "home theatre AC",
            },
            "model_config": {
                "min_temp": 16,
                "max_temp": 30,
                "mode": {
                    "0": {
                        "name": "fan",
                        "fspd": {"fixed": False, "default": 2},
                        "stemp": {"fixed": True},
                    },
                    "2": {
                        "name": "cool",
                        "fspd": {"fixed": False, "default": 7},
                        "stemp": {"fixed": False, "default": "24"},
                    },
                    "3": {
                        "name": "dry",
                        "fspd": {"fixed": True, "default": 2},
                        "stemp": {"fixed": False, "default": "24"},
                    },
                    "4": {
                        "name": "auto",
                        "fspd": {"fixed": True, "default": 7},
                        "stemp": {"fixed": False, "default": "24"},
                    },
                },
                "fspd": {"2": "low", "3": "med", "4": "high", "6": "turbo", "7": "auto"},
                "vswing": {
                    "toggle": {"0": "On", "6": "Off"},
                    "angles": {"1": "L1", "2": "L2", "3": "L3", "4": "L4", "5": "L5"},
                },
                "hswing": {
                    "toggle": {"0": "On", "6": "Off"},
                    "angles": {},
                },
                "critical_error": {"E6": "ODU-IDU Communication Error"},
                "non_critical_error": {"E4": "Discharge Temp High Error"},
            },
        },
    ],
    "states": {
        THING_ID: {
            "state_ts": 1791633975000,
            "state": {
                "pow": 0,
                "mode": 2,
                "stemp": "25.0",
                "ctemp": "29.5",
                "fspd": 4,
                "vswing": 0,
                "hswing": 6,
                "err": 0,
                "src": "anble",
                "lcmd": "pow",
                "ts": 1791633975000,
            },
            "connected": True,
            "conn_ts": 1791633975000,
        },
    },
}


async def _idle(self: Hub) -> None:
    """Stands in for the MQTT loop: online, and nothing ever arrives."""
    self.online = True
    self._notify()
    await asyncio.Event().wait()


async def _apply_command(self: Hub, thing_id: str, desired: dict[str, Any]) -> None:
    """Stands in for a command the AC takes at once."""
    thing = self.things[thing_id]
    thing.state = {**thing.state, **desired}
    self._notify()


@pytest.fixture
def commands():
    sent: list[dict[str, Any]] = []

    async def record(self: Hub, thing_id: str, desired: dict[str, Any]) -> None:
        sent.append(desired)
        await _apply_command(self, thing_id, desired)

    with (
        patch("custom_components.bluestar_ac.api.login", return_value=LOGIN),
        patch("custom_components.bluestar_ac.api.things", return_value=copy.deepcopy(LISTING)),
        patch.object(Hub, "run", _idle),
        patch.object(Hub, "poll", _idle),
        patch.object(Hub, "command", record),
    ):
        yield sent


async def _set_up(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=LOGIN.user_id,
        data={
            CONF_PHONE: "9999999999",
            CONF_PASSWORD: "password",
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _call(hass: HomeAssistant, service: str, **data: Any) -> None:
    await hass.services.async_call(
        "climate",
        service,
        {"entity_id": ENTITY_ID, **data},
        blocking=True,
    )


async def test_state(hass: HomeAssistant, commands) -> None:
    entry = await _set_up(hass)
    assert entry.state is ConfigEntryState.LOADED
    state = hass.states.get(ENTITY_ID)
    assert state.state == "off"
    assert state.attributes["current_temperature"] == 29.5
    assert state.attributes["temperature"] == 25
    assert state.attributes["fan_mode"] == "high"
    assert state.attributes["fan_modes"] == ["low", "medium", "high", "turbo", "auto"]
    assert state.attributes["hvac_modes"] == ["off", "fan_only", "cool", "dry", "auto"]
    assert state.attributes["swing_mode"] == "on"
    assert state.attributes["swing_modes"] == ["on", "off", "position_1", "position_2", "position_3", "position_4", "position_5"]
    assert state.attributes["swing_horizontal_mode"] == "off"
    assert state.attributes["min_temp"] == 16
    assert state.attributes["max_temp"] == 30
    assert state.attributes["last_change_by"] == "app over Bluetooth"
    assert state.attributes["last_change"] == "pow"
    assert state.attributes["error"] is None


async def test_commands(hass: HomeAssistant, commands) -> None:
    await _set_up(hass)
    await _call(hass, "set_hvac_mode", hvac_mode="cool")
    await _call(hass, "set_temperature", temperature=24)
    await _call(hass, "set_fan_mode", fan_mode="medium")
    await _call(hass, "set_swing_mode", swing_mode="position_3")
    await _call(hass, "set_swing_horizontal_mode", swing_horizontal_mode="on")
    await _call(hass, "turn_off")
    assert commands == [
        {"pow": 1, "mode": 2},
        {"stemp": "24.0"},
        {"fspd": 3},
        {"vswing": 3},
        {"hswing": 0},
        {"pow": 0},
    ]
    assert hass.states.get(ENTITY_ID).state == "off"


async def test_set_temperature_with_mode(hass: HomeAssistant, commands) -> None:
    await _set_up(hass)
    await _call(hass, "set_temperature", temperature=22, hvac_mode="dry")
    assert commands == [
        {"pow": 1, "mode": 3},
        {"stemp": "22.0"},
    ]
    assert hass.states.get(ENTITY_ID).state == "dry"


async def test_refusals(hass: HomeAssistant, commands) -> None:
    await _set_up(hass)
    with pytest.raises(ServiceValidationError, match="whole degrees"):
        await _call(hass, "set_temperature", temperature=23.5)
    await _call(hass, "set_hvac_mode", hvac_mode="dry")
    with pytest.raises(ServiceValidationError, match="fan speed is fixed"):
        await _call(hass, "set_fan_mode", fan_mode="low")
    await _call(hass, "set_hvac_mode", hvac_mode="fan_only")
    assert hass.states.get(ENTITY_ID).attributes["temperature"] is None
    with pytest.raises(ServiceValidationError, match="no set temperature"):
        await _call(hass, "set_temperature", temperature=24)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "set_hvac_mode", hvac_mode="heat")
    assert commands == [
        {"pow": 1, "mode": 3},
        {"pow": 1, "mode": 0},
    ]


async def test_error_attribute(hass: HomeAssistant, commands) -> None:
    listing = copy.deepcopy(LISTING)
    listing["states"][THING_ID]["state"]["err"] = "E6"
    with patch("custom_components.bluestar_ac.api.things", return_value=listing):
        await _set_up(hass)
    assert hass.states.get(ENTITY_ID).attributes["error"] == "ODU-IDU Communication Error"


async def test_offline_is_unavailable(hass: HomeAssistant, commands) -> None:
    listing = copy.deepcopy(LISTING)
    listing["states"][THING_ID]["connected"] = False
    listing["states"][THING_ID]["conn_ts"] = 1791634000000
    with patch("custom_components.bluestar_ac.api.things", return_value=listing):
        await _set_up(hass)
    assert hass.states.get(ENTITY_ID).state == "unavailable"


async def test_bad_password_starts_reauth(hass: HomeAssistant) -> None:
    with patch("custom_components.bluestar_ac.api.login", side_effect=InvalidAuth("refused")):
        entry = await _set_up(hass)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_cloud_down_retries(hass: HomeAssistant) -> None:
    with patch("custom_components.bluestar_ac.api.login", side_effect=BlueStarError("down")):
        entry = await _set_up(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_config_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"
    with patch("custom_components.bluestar_ac.api.login", side_effect=InvalidAuth("refused")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PHONE: "9999999999", CONF_PASSWORD: "wrong"},
        )
    assert result["errors"] == {"base": "invalid_auth"}
    with (
        patch("custom_components.bluestar_ac.api.login", return_value=LOGIN),
        patch("custom_components.bluestar_ac.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_PHONE: "9999999999", CONF_PASSWORD: "password"},
        )
    assert result["type"] == "create_entry"
    assert result["title"] == "Blue Star (Hrishik)"
    assert result["result"].unique_id == LOGIN.user_id


class _FakeConnection:
    """An MQTT connection whose AC reports what it is sent, or nothing."""

    def __init__(self, hub: Hub, takes: bool) -> None:
        self.hub = hub
        self.takes = takes
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, payload: bytes) -> None:
        message = json.loads(payload)
        self.published.append((topic, message))
        if self.takes:
            desired = message["state"]["desired"]
            thing = self.hub.things[THING_ID]
            report = {**thing.state, **desired, "lcmd": next(iter(desired)), "ts": thing.state_ms + 1}
            self.hub._on_message(f"things/{THING_ID}/state/reported", json.dumps(report).encode())


async def _hub() -> Hub:
    with (
        patch("custom_components.bluestar_ac.api.login", return_value=LOGIN),
        patch("custom_components.bluestar_ac.api.things", return_value=copy.deepcopy(LISTING)),
    ):
        hub = Hub(None, "9999999999", "password")
        await hub.start()
    return hub


async def test_hub_command_waits_for_the_report() -> None:
    hub = await _hub()
    connection = _FakeConnection(hub, takes=True)
    hub._connection = connection
    await hub.command(THING_ID, {"stemp": "24.0"})
    topic, message = connection.published[0]
    assert topic == f"$aws/things/{THING_ID}/shadow/update"
    assert message["state"]["desired"]["stemp"] == "24.0"
    assert message["state"]["desired"]["src"] == "anmq"
    assert hub.things[THING_ID].state["stemp"] == "24.0"
    assert hub.things[THING_ID].changed == "stemp"


async def test_hub_command_unconfirmed() -> None:
    hub = await _hub()
    hub._connection = _FakeConnection(hub, takes=False)
    with (
        patch("custom_components.bluestar_ac.hub.CONFIRM_SECONDS", 0.05),
        pytest.raises(BlueStarError, match="didn't confirm"),
    ):
        await hub.command(THING_ID, {"stemp": "24.0"})
    assert hub._waiters == []


async def test_hub_command_already_so() -> None:
    hub = await _hub()
    connection = _FakeConnection(hub, takes=False)
    hub._connection = connection
    await hub.command(THING_ID, {"stemp": "25.0"})
    assert connection.published == []


async def test_hub_refuses_offline() -> None:
    hub = await _hub()
    hub._connection = _FakeConnection(hub, takes=True)
    hub.things[THING_ID].connected = False
    with pytest.raises(BlueStarError, match="offline"):
        await hub.command(THING_ID, {"pow": 1})


async def test_hub_skips_older_and_requested_reports() -> None:
    hub = await _hub()
    thing = hub.things[THING_ID]
    older = {**thing.state, "stemp": "18.0", "ts": thing.state_ms - 1}
    hub._on_message(f"things/{THING_ID}/state/reported", json.dumps(older).encode())
    assert thing.state["stemp"] == "25.0"
    requested = {**thing.state, "ctemp": "28.0", "src": "fpush", "lcmd": "", "ts": thing.state_ms + 1}
    hub._on_message(f"things/{THING_ID}/state/reported", json.dumps(requested).encode())
    assert thing.state["ctemp"] == "28.0"
    assert thing.changed_by == "anble"
    assert thing.changed == "pow"
