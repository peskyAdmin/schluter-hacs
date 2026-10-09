"""Config flow and entity tests."""

import asyncio

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.schluter_ditra_heat.api import SchluterAuthError
from custom_components.schluter_ditra_heat.const import DOMAIN

CREDS = {CONF_EMAIL: "me@example.com", CONF_PASSWORD: "pw"}


async def test_config_flow(hass: HomeAssistant, mock_api) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    mock_api["login"].side_effect = SchluterAuthError
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["errors"] == {"base": "invalid_auth"}

    mock_api["login"].side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == CREDS


async def _setup(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=CREDS, unique_id="me@example.com")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_entities(hass: HomeAssistant, mock_api) -> None:
    await _setup(hass)
    state = hass.states.get("climate.kitchen_floor")
    assert state.state == "heat"
    assert state.attributes["current_temperature"] == 21.5
    assert state.attributes["temperature"] == 20.0
    assert state.attributes["hvac_action"] == "heating"
    assert hass.states.get("binary_sensor.kitchen_floor_online").state == "on"
    assert hass.states.get("binary_sensor.kitchen_floor_heating").state == "on"
    assert hass.states.get("binary_sensor.kitchen_floor_error").state == "off"


async def test_controls(hass: HomeAssistant, mock_api) -> None:
    await _setup(hass)
    target = {"entity_id": "climate.kitchen_floor"}
    await hass.services.async_call("climate", "set_temperature", {**target, "temperature": 22.5}, blocking=True)
    mock_api["update"].assert_awaited_with(
        "1254414", {"RegulationMode": 3, "ManualTemperature": 2250, "VacationEnabled": False}
    )
    await hass.services.async_call("climate", "set_hvac_mode", {**target, "hvac_mode": "auto"}, blocking=True)
    mock_api["update"].assert_awaited_with("1254414", {"RegulationMode": 1, "VacationEnabled": False})
    await hass.services.async_call("climate", "turn_off", target, blocking=True)
    mock_api["update"].assert_awaited_with(
        "1254414", {"RegulationMode": 3, "ManualTemperature": 500, "VacationEnabled": False}
    )


async def test_push_update(hass: HomeAssistant, mock_api) -> None:
    pushed = asyncio.Queue()

    async def next_notification():
        return await pushed.get()

    mock_api["notify"].side_effect = next_notification
    await _setup(hass)
    coordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data

    await pushed.put(
        {"Action": 2, "Thermostat": {"SerialNumber": "1254414", "SetPointTemp": 2400, "Heating": False}}
    )
    await hass.async_block_till_done()
    state = hass.states.get("climate.kitchen_floor")
    assert state.attributes["temperature"] == 24.0
    assert state.attributes["hvac_action"] == "idle"
    assert coordinator.push_connected
    assert coordinator.update_interval.total_seconds() == 900

    await pushed.put({"Action": 2, "Thermostat": {"SerialNumber": "1254414", "Online": False}})
    await hass.async_block_till_done()
    assert hass.states.get("climate.kitchen_floor").state == "unavailable"
    assert hass.states.get("binary_sensor.kitchen_floor_online").state == "off"

    # While push is connected, a command does not trigger an extra fetch.
    await pushed.put({"Action": 2, "Thermostat": {"SerialNumber": "1254414", "Online": True}})
    await hass.async_block_till_done()
    calls = mock_api["get"].await_count
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": "climate.kitchen_floor", "temperature": 21}, blocking=True,
    )
    await hass.async_block_till_done()
    assert mock_api["get"].await_count == calls
