"""Local control (relay / local-only) through Home Assistant."""

from datetime import timedelta
import os
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.schluter_ditra_heat.api import SchluterError
from custom_components.schluter_ditra_heat.const import CONF_MODE, DOMAIN, LOCAL_CONNECT_GRACE
from custom_components.schluter_ditra_heat.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.schluter_ditra_heat.transport import LocalTransport, issue_id

from .conftest import THERMOSTAT

CREDS = {"email": "me@example.com", "password": "pw"}
SERIAL = "1254414"


@pytest.fixture
def gateway():
    """Run the real gateway code without opening a port or making a cert."""
    with (
        patch(
            "custom_components.schluter_ditra_heat.transport.LocalGateway.start",
            AsyncMock(),
        ) as start,
        patch(
            "custom_components.schluter_ditra_heat.transport.LocalGateway.queue_set",
            AsyncMock(),
        ) as queue_set,
        patch(
            "custom_components.schluter_ditra_heat.transport.async_ensure_cert",
            AsyncMock(return_value=("cert", "key")),
        ),
        patch(
            "custom_components.schluter_ditra_heat.transport.async_get_source_ip",
            AsyncMock(return_value="192.168.1.10"),
        ),
    ):
        yield {"start": start, "queue_set": queue_set}


async def _setup(hass: HomeAssistant, mode: str = "relay") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data=CREDS, unique_id="me@example.com", options={CONF_MODE: mode}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _transport(entry: MockConfigEntry) -> LocalTransport:
    return entry.runtime_data.api


async def test_local_setup_and_control(
    hass: HomeAssistant, mock_api, gateway, hass_storage
) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    gateway["start"].assert_awaited_once()
    # Entities come from the cloud snapshot, offline until the thermostat connects.
    assert hass.states.get("climate.kitchen_floor").state == "unavailable"
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}.seed"]["data"][SERIAL]["Room"] == "Kitchen Floor"

    transport = _transport(entry)
    transport._on_state(SERIAL, {"Online": True})
    transport._on_state(SERIAL, {"Temperature": 2100, "Heating": False})
    await hass.async_block_till_done()
    state = hass.states.get("climate.kitchen_floor")
    assert state.state == "heat"
    assert state.attributes["current_temperature"] == 21.0
    assert state.attributes["hvac_action"] == "idle"
    assert hass.states.get("binary_sensor.kitchen_floor_heating").state == "off"

    target = {"entity_id": "climate.kitchen_floor"}
    await hass.services.async_call(
        "climate", "set_temperature", {**target, "temperature": 22.5}, blocking=True
    )
    gateway["queue_set"].assert_awaited_with(SERIAL, 3, 4500)  # x200 on the wire
    assert hass.states.get("climate.kitchen_floor").attributes["temperature"] == 22.5
    await hass.services.async_call(
        "climate", "set_hvac_mode", {**target, "hvac_mode": "auto"}, blocking=True
    )
    gateway["queue_set"].assert_awaited_with(SERIAL, 1, None)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["connection_mode"] == "relay"
    assert diag["local"] == {
        "mode": "relay",
        "gateway_running": True,
        "thermostats_connected": 0,
        "thermostats_relayed": 0,
    }
    assert diag["entry"]["email"] == "**REDACTED**"
    assert diag["thermostats"][0]["SerialNumber"] == "**REDACTED**"

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert transport.gateway is None


async def test_local_starts_from_saved_snapshot_offline(
    hass: HomeAssistant, mock_api, gateway, hass_storage
) -> None:
    hass_storage[f"{DOMAIN}.saved.seed"] = {
        "version": 1, "key": f"{DOMAIN}.saved.seed", "data": {SERIAL: dict(THERMOSTAT)}
    }
    mock_api["get"].side_effect = SchluterError("no internet")
    entry = MockConfigEntry(
        domain=DOMAIN, data=CREDS, unique_id="me@example.com",
        options={CONF_MODE: "local"}, entry_id="saved",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("climate.kitchen_floor") is not None


async def test_thermostat_seen_later_gets_entities(
    hass: HomeAssistant, mock_api, gateway
) -> None:
    entry = await _setup(hass, "local")
    _transport(entry)._on_state("999", {"Online": True, "Temperature": 1900})
    await hass.async_block_till_done()
    assert hass.states.get("climate.schluter_999").attributes["current_temperature"] == 19.0
    assert hass.states.get("binary_sensor.schluter_999_online").state == "on"


async def test_repair_issue_until_thermostat_connects(
    hass: HomeAssistant, mock_api, gateway
) -> None:
    entry = await _setup(hass)
    registry = ir.async_get(hass)
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=LOCAL_CONNECT_GRACE + 1)
    )
    await hass.async_block_till_done()
    issue = registry.async_get_issue(DOMAIN, issue_id(entry.entry_id))
    assert issue is not None
    assert issue.translation_placeholders["ip"] == "192.168.1.10"

    _transport(entry)._on_state(SERIAL, {"Online": True})
    assert registry.async_get_issue(DOMAIN, issue_id(entry.entry_id)) is None


async def test_no_repair_issue_when_connected(
    hass: HomeAssistant, mock_api, gateway
) -> None:
    entry = await _setup(hass)
    transport = _transport(entry)
    transport.gateway._writers[SERIAL] = object()
    with patch(
        "custom_components.schluter_ditra_heat.transport.async_get_source_ip",
        AsyncMock(side_effect=RuntimeError),
    ):
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=LOCAL_CONNECT_GRACE + 1)
        )
        await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry.entry_id)) is None
    transport.gateway._writers.clear()

    # With nothing connected and no source IP, the issue still appears.
    with patch(
        "custom_components.schluter_ditra_heat.transport.async_get_source_ip",
        AsyncMock(side_effect=RuntimeError),
    ):
        await transport._async_check_connected(dt_util.utcnow())
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id(entry.entry_id))
    assert issue.translation_placeholders["ip"] == "?"


async def test_port_in_use_retries(hass: HomeAssistant, mock_api, gateway) -> None:
    gateway["start"].side_effect = OSError(98, "Address in use")
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.error_reason_translation_key == "gateway_port_in_use"
    assert entry.error_reason_translation_placeholders["port"] == "9533"


async def test_remove_deletes_saved_files(
    hass: HomeAssistant, mock_api, gateway, hass_storage
) -> None:
    entry = await _setup(hass)
    cert_dir = hass.config.path(DOMAIN)
    os.makedirs(cert_dir, exist_ok=True)
    assert f"{DOMAIN}.{entry.entry_id}.seed" in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{entry.entry_id}.seed" not in hass_storage
    assert not os.path.exists(cert_dir)


async def test_remove_keeps_cert_for_other_accounts(hass: HomeAssistant, mock_api) -> None:
    MockConfigEntry(domain=DOMAIN, data=CREDS, unique_id="other").add_to_hass(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=CREDS, unique_id="me@example.com")
    entry.add_to_hass(hass)
    cert_dir = hass.config.path(DOMAIN)
    os.makedirs(cert_dir, exist_ok=True)
    await hass.config_entries.async_remove(entry.entry_id)
    assert os.path.exists(cert_dir)


async def test_update_needs_running_gateway(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=CREDS)
    transport = LocalTransport(hass, entry, mode="local")
    with pytest.raises(SchluterError):
        await transport.update_thermostat(SERIAL, {"RegulationMode": 1})


async def test_update_without_mode_keeps_current(
    hass: HomeAssistant, mock_api, gateway
) -> None:
    entry = await _setup(hass)
    transport = _transport(entry)
    await transport.update_thermostat(SERIAL, {"ManualTemperature": 2000})
    gateway["queue_set"].assert_awaited_with(SERIAL, 3, 4000)  # seed says manual
    transport._live[SERIAL]["RegulationMode"] = 2
    await transport.update_thermostat(SERIAL, {"ComfortTemperature": 2100})
    gateway["queue_set"].assert_awaited_with(SERIAL, 2, 4200)
    transport._seed.clear()
    transport._live.clear()
    await transport.update_thermostat(SERIAL, {"SetPointTemp": 1500})
    gateway["queue_set"].assert_awaited_with(SERIAL, 3, 3000)


@pytest.fixture
def cloud_update():
    with patch(
        "custom_components.schluter_ditra_heat.api.SchluterApi.update_thermostat",
        AsyncMock(),
    ) as update:
        yield update


async def test_relay_sends_changes_straight_to_the_thermostat(
    hass: HomeAssistant, mock_api, gateway, cloud_update
) -> None:
    entry = await _setup(hass)
    transport = _transport(entry)
    transport._on_state(SERIAL, {"Online": True})
    await hass.async_block_till_done()
    target = {"entity_id": "climate.kitchen_floor"}
    # Even while the thermostat's link reaches OJ's cloud, changes go to it locally.
    with patch.object(transport.gateway, "is_relayed", return_value=True):
        await hass.services.async_call(
            "climate", "set_temperature", {**target, "temperature": 22.5}, blocking=True
        )
        gateway["queue_set"].assert_awaited_once_with(SERIAL, 3, 4500)
        assert hass.states.get("climate.kitchen_floor").attributes["temperature"] == 22.5
        await hass.services.async_call(
            "climate", "set_hvac_mode", {**target, "hvac_mode": "auto"}, blocking=True
        )
        gateway["queue_set"].assert_awaited_with(SERIAL, 1, None)
    cloud_update.assert_not_awaited()


async def test_local_only_never_uses_the_cloud_for_changes(
    hass: HomeAssistant, mock_api, gateway, cloud_update
) -> None:
    entry = await _setup(hass, mode="local")
    transport = _transport(entry)
    await transport.update_thermostat(SERIAL, {"RegulationMode": 3, "ManualTemperature": 2000})
    cloud_update.assert_not_awaited()
    gateway["queue_set"].assert_awaited_once_with(SERIAL, 3, 4000)
