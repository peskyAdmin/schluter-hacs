"""The Schluter DITRA-HEAT-E-WiFi integration."""

from __future__ import annotations

from aiohttp import CookieJar

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import SchluterApi
from .coordinator import SchluterCoordinator

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.CLIMATE]

type SchluterConfigEntry = ConfigEntry[SchluterCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SchluterConfigEntry) -> bool:
    """Set up Schluter from a config entry."""
    # The login flow relies on cookies, so give this entry its own jar.
    session = async_create_clientsession(hass, cookie_jar=CookieJar())
    api = SchluterApi(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    coordinator = SchluterCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start_push()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SchluterConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
