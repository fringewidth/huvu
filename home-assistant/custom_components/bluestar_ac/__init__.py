"""Blue Star Smart AC: Blue Star's Wi-Fi ACs, through Blue Star's own cloud.

Each AC on the account is one climate entity. Its state comes over MQTT the
moment it changes, whoever changed it (the app, Alexa, the remote), and a
command returns only once the AC has reported the change.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import BlueStarError, InvalidAuth
from .const import CONF_PHONE
from .hub import Hub

PLATFORMS = [Platform.CLIMATE]

type BlueStarConfigEntry = ConfigEntry[Hub]


async def async_setup_entry(hass: HomeAssistant, entry: BlueStarConfigEntry) -> bool:
    hub = Hub(async_get_clientsession(hass), entry.data[CONF_PHONE], entry.data[CONF_PASSWORD])
    try:
        await hub.start()
    except InvalidAuth as error:
        raise ConfigEntryAuthFailed(str(error)) from error
    except BlueStarError as error:
        raise ConfigEntryNotReady(str(error)) from error
    entry.runtime_data = hub
    entry.async_create_background_task(hass, hub.run(), "bluestar_ac mqtt")
    entry.async_create_background_task(hass, hub.poll(), "bluestar_ac poll")
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BlueStarConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
