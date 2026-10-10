"""Fetches Alexa's voice history on demand.

Alexa Devices reads the voice history only at startup and when an Echo
pushes an equalizer change, which a spoken ask does and a typed command
(alexa_devices.send_text_command) doesn't. So a script that types to Alexa
can't see what Alexa heard or said back, until someone in the house speaks
to an Echo. alexa_history.fetch reads the history now and returns the
device's latest record. It uses Alexa Devices' own coordinator, so an HA
update that changes its internals can break it.
"""

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

DOMAIN = "alexa_history"
ALEXA = "alexa_devices"

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
FETCH_SCHEMA = vol.Schema({vol.Required("device_id"): cv.string})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def fetch(call: ServiceCall) -> ServiceResponse:
        device_id = call.data["device_id"]
        device = dr.async_get(hass).async_get(device_id)
        if device is None:
            raise ServiceValidationError(f"No device {device_id}")
        serial = next((ident for domain, ident in device.identifiers if domain == ALEXA), None)
        if serial is None:
            raise ServiceValidationError(f"{device.name} isn't an Alexa device")
        entry = next((e for e in hass.config_entries.async_loaded_entries(ALEXA) if e.entry_id in device.config_entries), None)
        if entry is None:
            raise ServiceValidationError("Alexa Devices isn't loaded")
        coordinator = entry.runtime_data
        await coordinator.sync_history_state()
        # The device's voice event takes the new record too.
        coordinator.async_update_listeners()
        record = coordinator.vocal_records.get(serial)
        if record is None:
            return {}
        return {
            "time": dt_util.as_local(dt_util.utc_from_timestamp(record.timestamp / 1000)).isoformat(),
            "heard": record.title,
            "intent": record.intent,
            "reply": record.sub_title,
        }

    hass.services.async_register(DOMAIN, "fetch", fetch, schema=FETCH_SCHEMA, supports_response=SupportsResponse.ONLY)
    return True
