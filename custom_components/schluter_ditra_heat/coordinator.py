"""Polling coordinator for Schluter thermostats."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SchluterApi, SchluterAuthError, SchluterError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class SchluterCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Fetches every thermostat on the account in one call."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: SchluterApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            return await self.api.get_thermostats()
        except SchluterAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (SchluterError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Error talking to Schluter: {err}") from err
