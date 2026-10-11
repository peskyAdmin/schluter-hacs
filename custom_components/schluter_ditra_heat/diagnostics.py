"""Diagnostics download for Schluter DITRA-HEAT-E-WiFi (Settings > Devices)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import SchluterConfigEntry
from .const import CONF_MODE, DEFAULT_MODE
from .transport import LocalTransport

TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    "Email",
    "SerialNumber",
    "SessionId",
    "MacAddress",
    "Address",
    "Room",
    "GroupName",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SchluterConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    api = coordinator.api
    local: dict[str, Any] | None = None
    if isinstance(api, LocalTransport):
        gateway = api.gateway
        local = {
            "mode": api.mode,
            "gateway_running": gateway is not None,
            "thermostats_connected": len(gateway.connected_serials) if gateway else 0,
        }
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "connection_mode": entry.options.get(CONF_MODE, DEFAULT_MODE),
        "push_connected": coordinator.push_connected,
        "last_update_success": coordinator.last_update_success,
        "local": local,
        # Keyed by serial upstream; list the values so serials stay out.
        "thermostats": [
            async_redact_data(thermostat, TO_REDACT)
            for thermostat in (coordinator.data or {}).values()
        ],
    }
