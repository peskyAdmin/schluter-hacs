"""Coordinator for Schluter thermostats: long-poll push with a polling safety net."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SchluterApi, SchluterAuthError, SchluterError
from .const import (
    ACTION_REMOVED,
    ACTION_UPDATED,
    DOMAIN,
    FALLBACK_POLL_INTERVAL,
    PUSH_MIN_EMPTY_SECONDS,
    PUSH_RETRY_MAX,
    PUSH_RETRY_MIN,
    SAFETY_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class SchluterCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Holds every thermostat on the account, keyed by serial number."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: SchluterApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=FALLBACK_POLL_INTERVAL,
        )
        self.api = api
        self.push_connected = False

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            return await self.api.get_thermostats()
        except SchluterAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (SchluterError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Error talking to Schluter: {err}") from err

    def device_info(self, serial: str) -> DeviceInfo:
        data = self.data.get(serial, {})
        return DeviceInfo(
            identifiers={(DOMAIN, serial)},
            manufacturer="Schluter",
            model="DITRA-HEAT-E-WiFi",
            name=data.get("Room") or f"Schluter {serial}",
            serial_number=serial,
            sw_version=data.get("SWVersion"),
        )

    def start_push(self) -> None:
        """Start the long-poll listener; it stops when the entry unloads."""
        self.config_entry.async_create_background_task(
            self.hass, self._listen(), f"{DOMAIN} notifications"
        )

    def _set_push_connected(self, connected: bool) -> None:
        if connected == self.push_connected:
            return
        self.push_connected = connected
        self.update_interval = SAFETY_POLL_INTERVAL if connected else FALLBACK_POLL_INTERVAL
        _LOGGER.debug("Schluter push %s", "connected" if connected else "disconnected")

    async def _listen(self) -> None:
        delay = PUSH_RETRY_MIN
        while True:
            started = time.monotonic()
            try:
                notification = await self.api.wait_for_notification()
                if notification:
                    self._apply(notification)
            except TimeoutError:
                # The server held the request longer than our timeout; just re-open.
                continue
            except SchluterAuthError:
                _LOGGER.warning("Schluter rejected the login; push updates stopped")
                self._set_push_connected(False)
                self.config_entry.async_start_reauth(self.hass)
                return
            except (SchluterError, aiohttp.ClientError, ValueError) as err:
                _LOGGER.debug("Schluter push failed, retrying in %s s: %s", delay, err)
            except Exception:
                # Never let an odd payload end push updates for good.
                _LOGGER.exception("Unexpected error in Schluter push; retrying in %s s", delay)
            else:
                delay = PUSH_RETRY_MIN
                self._set_push_connected(True)
                if notification or time.monotonic() - started >= PUSH_MIN_EMPTY_SECONDS:
                    continue
                # An instant empty answer would otherwise become a tight loop.
                await asyncio.sleep(PUSH_RETRY_MIN)
                continue
            self._set_push_connected(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, PUSH_RETRY_MAX)

    def _apply(self, notification: dict[str, Any]) -> None:
        thermostat = notification["Thermostat"]
        if thermostat.get("SerialNumber") is None:
            return
        serial = str(thermostat["SerialNumber"])
        data = dict(self.data or {})
        action = notification.get("Action")
        if action == ACTION_UPDATED:
            data[serial] = {**data.get(serial, {}), **thermostat}
        elif action == ACTION_REMOVED:
            data.pop(serial, None)
        else:
            return
        self.async_set_updated_data(data)
