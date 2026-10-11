"""The Schluter DITRA-HEAT-E-WiFi integration."""

from __future__ import annotations

import logging
import shutil

from aiohttp import CookieJar

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import SchluterApi
from .const import CONF_MODE, DEFAULT_MODE, DOMAIN, GATEWAY_PORT, LOCAL_MODES
from .coordinator import SchluterCoordinator
from .transport import LocalTransport, seed_store

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.CLIMATE]

type SchluterConfigEntry = ConfigEntry[SchluterCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SchluterConfigEntry) -> bool:
    """Set up Schluter from a config entry."""
    mode = entry.options.get(CONF_MODE, DEFAULT_MODE)
    # The login flow relies on cookies, so give this entry its own jar. It shares
    # Home Assistant's connector, so release it on unload rather than closing it.
    session = async_create_clientsession(
        hass, auto_cleanup=False, cookie_jar=CookieJar()
    )
    entry.async_on_unload(session.detach)
    cloud_api = SchluterApi(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])

    api: SchluterApi | LocalTransport = cloud_api
    if mode in LOCAL_MODES:
        # Local modes run a gateway on this host; the cloud account only supplies
        # names and limits, refreshed whenever the internet is up.
        api = LocalTransport(hass, entry, mode=mode, cloud_api=cloud_api)
        try:
            await api.login()
        except OSError as err:
            await api.async_stop()
            raise ConfigEntryNotReady(
                f"Could not start the local gateway on port {GATEWAY_PORT}; is "
                f"something else using it? ({err})"
            ) from err
        _LOGGER.info("Schluter is using local control (%s mode)", mode)

    coordinator = SchluterCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start_push()
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SchluterConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        api = entry.runtime_data.api
        if isinstance(api, LocalTransport):
            await api.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: SchluterConfigEntry) -> None:
    """Delete what local control saved once the integration is removed."""
    await seed_store(hass, entry.entry_id).async_remove()
    if not any(
        other.entry_id != entry.entry_id
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        # The gateway certificate is shared by every entry on this host.
        await hass.async_add_executor_job(
            shutil.rmtree, hass.config.path(DOMAIN), True
        )


async def _async_reload_entry(hass: HomeAssistant, entry: SchluterConfigEntry) -> None:
    """Reload when the connection mode changes in the options flow."""
    await hass.config_entries.async_reload(entry.entry_id)
