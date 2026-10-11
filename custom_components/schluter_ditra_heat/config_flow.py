"""Config and options flow for Schluter DITRA-HEAT-E-WiFi."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import socket
from typing import Any

from aiohttp import CookieJar
import voluptuous as vol

from homeassistant.components.network import async_get_source_ip
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import SchluterApi, SchluterAuthError, SchluterError
from .const import (
    CONF_MODE,
    CONN_CLOUD,
    CONN_LOCAL,
    CONN_RELAY,
    DEFAULT_MODE,
    DOCS_LOCAL_URL,
    DOMAIN,
    GATEWAY_HOST,
    GATEWAY_PORT,
    LOCAL_MODES,
)

_LOGGER = logging.getLogger(__name__)

CONF_ACKNOWLEDGE = "acknowledge"

PASSWORD_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(
        type=selector.TextSelectorType.PASSWORD, autocomplete="current-password"
    )
)
DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.EMAIL, autocomplete="username"
            )
        ),
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)

MODE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[CONN_CLOUD, CONN_RELAY, CONN_LOCAL],
        mode=selector.SelectSelectorMode.LIST,
        translation_key="connection_mode",
    )
)


class SchluterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Schluter."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SchluterOptionsFlow:
        return SchluterOptionsFlow()

    async def _validate(self, email: str, password: str) -> str | None:
        """Try to log in; return an error key or None."""
        session = async_create_clientsession(
            self.hass, auto_cleanup=False, cookie_jar=CookieJar()
        )
        api = SchluterApi(session, email, password)
        try:
            await api.login()
            await api.get_thermostats()
        except SchluterAuthError:
            return "invalid_auth"
        except SchluterError:
            return "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error logging in to Schluter")
            return "unknown"
        finally:
            # The connector is Home Assistant's shared one; release, don't close it.
            session.detach()
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()
            if (error := await self._validate(email, user_input[CONF_PASSWORD])) is None:
                return self.async_create_entry(
                    title=email,
                    data={CONF_EMAIL: email, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                DATA_SCHEMA, {CONF_EMAIL: (user_input or {}).get(CONF_EMAIL)}
            ),
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
            email = entry.data[CONF_EMAIL]
            if (error := await self._validate(email, user_input[CONF_PASSWORD])) is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR}
            ),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )


class SchluterOptionsFlow(OptionsFlow):
    """Let advanced users switch between cloud and local control."""

    def __init__(self) -> None:
        self._mode: str = DEFAULT_MODE
        self._dns_warned = False
        self._cloud_warned = False

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = self.config_entry.options.get(CONF_MODE, DEFAULT_MODE)
        if user_input is not None:
            self._mode = user_input[CONF_MODE]
            if self._mode in LOCAL_MODES:
                return await self.async_step_local()
            if (
                current in LOCAL_MODES
                and not self._cloud_warned
                and await self._dns_points_here(await self._host_ip())
            ):
                # Left in place, the record keeps the thermostat dialing this host,
                # so it never reaches the cloud. Warn once; a second submit goes on.
                self._cloud_warned = True
                errors["base"] = "dns_still_local"
            else:
                return self.async_create_entry(title="", data={CONF_MODE: CONN_CLOUD})

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_MODE): MODE_SELECTOR}),
                {CONF_MODE: self._mode if user_input else current},
            ),
            errors=errors,
            description_placeholders={"docs_url": DOCS_LOCAL_URL},
        )

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the DNS record to create, and check it before saving."""
        errors: dict[str, str] = {}
        host_ip = await self._host_ip()
        if user_input is not None:
            if not user_input.get(CONF_ACKNOWLEDGE):
                errors["base"] = "must_acknowledge"
            elif not self._dns_warned and not await self._dns_points_here(host_ip):
                # Home Assistant may use a different DNS server than the
                # thermostat, so warn once and accept a second submit.
                self._dns_warned = True
                errors["base"] = "dns_not_detected"
            else:
                return self.async_create_entry(title="", data={CONF_MODE: self._mode})

        return self.async_show_form(
            step_id="local",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ACKNOWLEDGE,
                        default=bool((user_input or {}).get(CONF_ACKNOWLEDGE)),
                    ): bool
                }
            ),
            errors=errors,
            description_placeholders={
                "hostname": GATEWAY_HOST,
                "ip": host_ip,
                "port": str(GATEWAY_PORT),
                "docs_url": DOCS_LOCAL_URL,
            },
        )

    async def _host_ip(self) -> str:
        try:
            return await async_get_source_ip(self.hass)
        except Exception:  # noqa: BLE001 - only used in the instructions
            return "?"

    async def _dns_points_here(self, host_ip: str) -> bool:
        """Whether this host already resolves the gateway name to itself."""
        try:
            infos = await self.hass.loop.getaddrinfo(
                GATEWAY_HOST, GATEWAY_PORT, family=socket.AF_INET
            )
        except OSError:
            return False
        return host_ip in {info[4][0] for info in infos}
