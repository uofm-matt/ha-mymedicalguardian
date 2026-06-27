"""Config + options + reauth flow for MyMedicalGuardian Tracker."""

import logging
from collections.abc import Mapping
from typing import Any

import httpx
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.ssl import client_context

from .api import AuthError, FetchError, fetch_snapshot
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MIN,
    DOMAIN,
    MIN_SCAN_INTERVAL_MIN,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default="MyMedicalGuardian"): str,
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


async def _validate(hass: HomeAssistant, email: str, password: str) -> str | None:
    """Return None on success, or an error key for the form."""
    async with httpx.AsyncClient(
        verify=client_context(), follow_redirects=True, timeout=30.0
    ) as client:
        try:
            await fetch_snapshot(client, email, password)
        except AuthError:
            return "invalid_auth"
        except (FetchError, httpx.HTTPError, OSError):
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception validating MyGuardian login")
            return "unknown"
    return None


class MGConfigFlow(ConfigFlow, domain=DOMAIN):
    """User-driven setup + reauth."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()
            if err := await _validate(
                self.hass, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            ):
                errors["base"] = err
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data=user_input,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            if err := await _validate(
                self.hass, entry.data[CONF_EMAIL], user_input[CONF_PASSWORD]
            ):
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": entry.data[CONF_EMAIL]},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_mismatch()
            if err := await _validate(
                self.hass, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            ):
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    title=user_input[CONF_NAME],
                    data=user_input,
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=entry.title): str,
                    vol.Required(CONF_EMAIL, default=entry.data[CONF_EMAIL]): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders={"current_email": entry.data[CONF_EMAIL]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return MGOptionsFlow()


class MGOptionsFlow(OptionsFlow):
    """Tune the scan interval after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MIN
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                    int, vol.Range(min=MIN_SCAN_INTERVAL_MIN, max=120)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
