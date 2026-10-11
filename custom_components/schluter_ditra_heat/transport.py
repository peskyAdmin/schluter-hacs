"""Local transport: a drop-in for SchluterApi backed by the local gateway.

The coordinator and entities only ever call ``get_thermostats``,
``wait_for_notification`` and ``update_thermostat`` on ``coordinator.api``, so a
local mode just needs an object with those three methods that fills the same
dict shape (keyed by serial, cloud field names, Celsius x100). The entities
never change.

Static facts (room, limits, firmware) come from a snapshot of the cloud account
saved in Home Assistant's storage, so entities are complete and exist from
startup even with no internet. The snapshot is refreshed whenever the cloud is
reachable. Live fields (online, temperature, heating, setpoint, mode) come from
the gateway as the thermostat reports them.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Any

from homeassistant.components.network import async_get_source_ip
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .api import SchluterApi, SchluterError
from .cert import async_ensure_cert
from .const import (
    ACTION_UPDATED,
    CONN_RELAY,
    DEVICE_TEMP_SCALE,
    DOCS_LOCAL_URL,
    DOMAIN,
    GATEWAY_HOST,
    GATEWAY_PORT,
    LOCAL_CONNECT_GRACE,
    MODE_MANUAL,
)
from .gateway import LocalGateway

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


def seed_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, dict[str, Any]]]:
    """Where the cloud snapshot for one config entry lives."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.seed")


def issue_id(entry_id: str) -> str:
    return f"local_not_connected_{entry_id}"


class LocalTransport:
    """Speaks to the thermostat over the LAN; looks like SchluterApi."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        mode: str,
        cloud_api: SchluterApi | None = None,
    ) -> None:
        self.hass = hass
        self._entry_id = entry.entry_id
        self.mode = mode
        self._relay = mode == CONN_RELAY
        self._cloud_api = cloud_api
        self._store = seed_store(hass, entry.entry_id)
        self._seed: dict[str, dict[str, Any]] = {}
        self._live: dict[str, dict[str, Any]] = {}
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._gateway: LocalGateway | None = None
        self._cancel_check: CALLBACK_TYPE | None = None

    @property
    def gateway(self) -> LocalGateway | None:
        return self._gateway

    async def login(self) -> None:
        """Load the saved snapshot, refresh it if the cloud answers, start the gateway."""
        self._seed = {
            serial: dict(fields)
            for serial, fields in ((await self._store.async_load()) or {}).items()
        }
        if self._cloud_api is not None:
            try:
                fresh = await self._cloud_api.get_thermostats()
            except Exception as err:  # noqa: BLE001 - offline is expected here
                _LOGGER.debug("Schluter cloud snapshot not refreshed: %s", err)
            else:
                self._seed = {serial: dict(fields) for serial, fields in fresh.items()}
                await self._store.async_save(self._seed)
        certfile, keyfile = await async_ensure_cert(self.hass)
        self._gateway = LocalGateway(
            certfile, keyfile, relay=self._relay, on_state=self._on_state
        )
        await self._gateway.start(self.hass)
        # If no thermostat shows up, DNS is almost always the cause; say so.
        self._cancel_check = async_call_later(
            self.hass, LOCAL_CONNECT_GRACE, self._async_check_connected
        )

    async def _async_check_connected(self, _now: datetime) -> None:
        self._cancel_check = None
        if self._gateway is None or self._gateway.connected_serials:
            return
        try:
            host_ip = await async_get_source_ip(self.hass)
        except Exception:  # noqa: BLE001 - only used in the message
            host_ip = "?"
        _LOGGER.warning(
            "No Schluter thermostat has connected to the local gateway yet. Check "
            "that %s resolves to this host (%s) for the thermostat, then "
            "power-cycle it",
            GATEWAY_HOST,
            host_ip,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id(self._entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="local_not_connected",
            translation_placeholders={
                "hostname": GATEWAY_HOST,
                "ip": host_ip,
                "port": str(GATEWAY_PORT),
            },
            learn_more_url=DOCS_LOCAL_URL,
        )

    @callback
    def _on_state(self, serial: str, fields: dict[str, Any]) -> None:
        _LOGGER.debug("Schluter %s update for Home Assistant: %s", serial, fields)
        if fields.get("Online"):
            ir.async_delete_issue(self.hass, DOMAIN, issue_id(self._entry_id))
        live = self._live.setdefault(serial, {})
        live.update(fields)
        self._queue.put_nowait(
            {"Action": ACTION_UPDATED, "Thermostat": self._merged(serial)}
        )

    def _merged(self, serial: str) -> dict[str, Any]:
        # The snapshot's Online flag describes the cloud link, not our gateway, so
        # a thermostat only counts as online once it has connected here.
        return {
            **self._seed.get(serial, {}),
            "Online": False,
            **self._live.get(serial, {}),
            "SerialNumber": serial,
        }

    async def get_thermostats(self) -> dict[str, dict[str, Any]]:
        return {serial: self._merged(serial) for serial in {*self._seed, *self._live}}

    async def wait_for_notification(self) -> dict[str, Any]:
        return await self._queue.get()

    async def update_thermostat(self, serial: str, payload: dict[str, Any]) -> None:
        if self._gateway is None:
            raise SchluterError("Local gateway is not running")
        # Both local modes send changes straight to the thermostat, so they work
        # whether or not OJ's cloud is reachable.
        _LOGGER.debug("Schluter change from Home Assistant for %s: %s", serial, payload)
        mode = payload.get("RegulationMode")
        if mode is None:
            mode = (
                self._live.get(serial, {}).get("RegulationMode")
                or self._seed.get(serial, {}).get("RegulationMode")
                or MODE_MANUAL
            )
        temp_x100 = next(
            (
                payload[key]
                for key in (
                    "ManualTemperature",
                    "ComfortTemperature",
                    "VacationTemperature",
                    "SetPointTemp",
                )
                if payload.get(key)
            ),
            None,
        )
        setpoint_x200 = (
            round(temp_x100 / 100 * DEVICE_TEMP_SCALE) if temp_x100 else None
        )
        await self._gateway.queue_set(serial, mode, setpoint_x200)
        # Reflect the change immediately; the device's next upload confirms it.
        self._on_state(serial, payload)

    async def async_stop(self) -> None:
        if self._cancel_check is not None:
            self._cancel_check()
            self._cancel_check = None
        ir.async_delete_issue(self.hass, DOMAIN, issue_id(self._entry_id))
        if self._gateway is not None:
            await self._gateway.stop()
            self._gateway = None
