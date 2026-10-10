"""Each Blue Star AC as a climate entity.

The modes, fan speeds, swing positions and temperature range come from the
AC's own model config in the device list, so another model gets what it
has. Some settings are fixed in some modes (fan mode has no temperature,
dry and auto have no fan speed): a command to change one is refused with a
reason, since the AC would ignore it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    FAN_MEDIUM,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_HALVES, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BlueStarConfigEntry
from .api import BlueStarError
from .const import (
    DOMAIN,
    MODE_AUTO,
    MODE_COOL,
    MODE_DRY,
    MODE_FAN,
    MODE_HEAT,
    SOURCES,
    SWING_OFF,
    SWING_ON,
)
from .hub import Hub, Thing

HVAC_MODES = {
    MODE_FAN: HVACMode.FAN_ONLY,
    MODE_HEAT: HVACMode.HEAT,
    MODE_COOL: HVACMode.COOL,
    MODE_DRY: HVACMode.DRY,
    MODE_AUTO: HVACMode.AUTO,
}

# Fan speed labels in the model config that differ from Home Assistant's.
FAN_NAMES = {
    "med": FAN_MEDIUM,
}

# Used when a model config leaves a part out.
DEFAULT_MODES = {
    str(MODE_FAN): {},
    str(MODE_COOL): {},
    str(MODE_DRY): {},
    str(MODE_AUTO): {},
}
DEFAULT_FAN_SPEEDS = {
    "2": "low",
    "3": "med",
    "4": "high",
    "6": "turbo",
    "7": "auto",
}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BlueStarConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    hub = entry.runtime_data
    async_add_entities(BlueStarClimate(hub, thing) for thing in hub.things.values())


class BlueStarClimate(ClimateEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_precision = PRECISION_HALVES

    def __init__(self, hub: Hub, thing: Thing) -> None:
        self._hub = hub
        self._thing = thing
        model = thing.model
        self._attr_unique_id = thing.thing_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, thing.thing_id)},
            name=thing.name,
            manufacturer="Blue Star",
            sw_version=thing.firmware,
        )
        self._attr_min_temp = float(model.get("min_temp", 16))
        self._attr_max_temp = float(model.get("max_temp", 30))

        modes = model.get("mode") or DEFAULT_MODES
        self._modes = {int(value): config for value, config in modes.items() if int(value) in HVAC_MODES}
        self._attr_hvac_modes = [HVACMode.OFF, *(HVAC_MODES[value] for value in self._modes)]

        fan_speeds = model.get("fspd") or DEFAULT_FAN_SPEEDS
        self._fan_speeds = {
            FAN_NAMES.get(label.lower(), label.lower()): int(value) for value, label in fan_speeds.items()
        }
        self._attr_fan_modes = list(self._fan_speeds)

        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        vertical = model.get("vswing") or {}
        self._swings = {}
        if vertical.get("toggle"):
            self._swings = {
                "on": SWING_ON,
                "off": SWING_OFF,
            } | {f"position_{value}": int(value) for value in vertical.get("angles") or {}}
            self._attr_swing_modes = list(self._swings)
            features |= ClimateEntityFeature.SWING_MODE
        horizontal = model.get("hswing") or {}
        if horizontal.get("toggle"):
            self._attr_swing_horizontal_modes = ["on", "off"]
            features |= ClimateEntityFeature.SWING_HORIZONTAL_MODE
        self._attr_supported_features = features

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._hub.add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return self._hub.online and self._thing.connected

    @property
    def hvac_mode(self) -> HVACMode | None:
        state = self._thing.state
        mode = HVACMode.OFF if state.get("pow") != 1 else HVAC_MODES.get(state.get("mode"))
        return mode

    @property
    def target_temperature(self) -> float | None:
        temperature = None if self._fixed("stemp") else _number(self._thing.state.get("stemp"))
        return temperature

    @property
    def current_temperature(self) -> float | None:
        return _number(self._thing.state.get("ctemp"))

    @property
    def fan_mode(self) -> str | None:
        speed = self._thing.state.get("fspd")
        return next((name for name, value in self._fan_speeds.items() if value == speed), None)

    @property
    def swing_mode(self) -> str | None:
        swing = self._thing.state.get("vswing")
        return next((name for name, value in self._swings.items() if value == swing), None)

    @property
    def swing_horizontal_mode(self) -> str | None:
        swing = self._thing.state.get("hswing")
        names = {
            SWING_ON: "on",
            SWING_OFF: "off",
        }
        return names.get(swing)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        thing = self._thing
        return {
            "last_change_by": SOURCES.get(thing.changed_by, thing.changed_by),
            "last_change": thing.changed,
            "error": self._error(thing.state.get("err")),
        }

    async def async_turn_on(self) -> None:
        await self._send({"pow": 1})

    async def async_turn_off(self) -> None:
        await self._send({"pow": 0})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self._send({"pow": 0})
            return
        value = next((value for value, mode in HVAC_MODES.items() if mode == hvac_mode and value in self._modes), None)
        if value is None:
            raise ServiceValidationError(f"{self._thing.name} has no {hvac_mode} mode")
        await self._send(
            {
                "pow": 1,
                "mode": value,
            }
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        hvac_mode = kwargs.get(ATTR_HVAC_MODE)
        if hvac_mode is not None:
            await self.async_set_hvac_mode(hvac_mode)
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        if temperature != round(temperature):
            raise ServiceValidationError(f"{self._thing.name} takes whole degrees only")
        if self._fixed("stemp"):
            raise ServiceValidationError(f"{self._thing.name} has no set temperature in {self.hvac_mode} mode")
        await self._send({"stemp": f"{round(temperature)}.0"})

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if self._fixed("fspd"):
            raise ServiceValidationError(f"{self._thing.name}'s fan speed is fixed in {self.hvac_mode} mode")
        await self._send({"fspd": self._fan_speeds[fan_mode]})

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        await self._send({"vswing": self._swings[swing_mode]})

    async def async_set_swing_horizontal_mode(self, swing_horizontal_mode: str) -> None:
        value = SWING_ON if swing_horizontal_mode == "on" else SWING_OFF
        await self._send({"hswing": value})

    def _fixed(self, setting: str) -> bool:
        """Whether `setting` is fixed in the AC's current mode."""
        config = self._modes.get(self._thing.state.get("mode")) or {}
        return bool((config.get(setting) or {}).get("fixed"))

    def _error(self, code: Any) -> str | None:
        """The fault the AC reports, in words when the model config knows it."""
        if not code:
            return None
        model = self._thing.model
        known = (model.get("critical_error") or {}) | (model.get("non_critical_error") or {})
        return known.get(str(code)) or str(code)

    async def _send(self, desired: dict[str, Any]) -> None:
        try:
            await self._hub.command(self._thing.thing_id, desired)
        except BlueStarError as error:
            raise HomeAssistantError(str(error)) from error
