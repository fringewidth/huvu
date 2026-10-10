"""Set up a Blue Star account with the Blue Star Smart AC app's login."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from . import api
from .api import BlueStarError, InvalidAuth
from .const import CONF_PHONE, DOMAIN

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PHONE): str,
        vol.Required(CONF_PASSWORD): PASSWORD,
    }
)
REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): PASSWORD,
    }
)


class BlueStarConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                login = await api.login(
                    async_get_clientsession(self.hass),
                    user_input[CONF_PHONE],
                    user_input[CONF_PASSWORD],
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except BlueStarError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(login.user_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Blue Star ({login.name or user_input[CONF_PHONE]})",
                    data=user_input,
                )
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                await api.login(
                    async_get_clientsession(self.hass),
                    entry.data[CONF_PHONE],
                    user_input[CONF_PASSWORD],
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except BlueStarError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
        return self.async_show_form(step_id="reauth_confirm", data_schema=REAUTH_SCHEMA, errors=errors)
