"""Config flow and entity tests."""

import asyncio
from unittest.mock import patch

import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.schluter_ditra_heat.api import SchluterApi, SchluterAuthError, SchluterError
from custom_components.schluter_ditra_heat.const import API_URL, DOMAIN

from .conftest import THERMOSTAT

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
        {"Action": 2, "Thermostat": {"SerialNumber": "1254414", "ManualTemperature": 2400, "Heating": False}}
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


async def test_target_follows_requested_temperature(hass: HomeAssistant, mock_api) -> None:
    """While offline, SetPointTemp is stale; show what was asked for."""
    pushed = asyncio.Queue()

    async def next_notification():
        return await pushed.get()

    mock_api["notify"].side_effect = next_notification
    await _setup(hass)
    await pushed.put({"Action": 2, "Thermostat": {
        "SerialNumber": "1254414", "RegulationMode": 2,
        "ComfortTemperature": "3222", "SetPointTemp": 1888,
    }})
    await hass.async_block_till_done()
    assert hass.states.get("climate.kitchen_floor").attributes["temperature"] == 32.2


async def test_control_error(hass: HomeAssistant, mock_api) -> None:
    await _setup(hass)
    mock_api["update"].side_effect = SchluterError("boom")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "climate", "set_temperature",
            {"entity_id": "climate.kitchen_floor", "temperature": 21}, blocking=True,
        )


async def test_push_survives_bad_payload_and_fast_empty(hass: HomeAssistant, mock_api) -> None:
    """An odd payload doesn't end push, and instant empty answers are throttled."""
    answers = [{"Action": 2, "Thermostat": "garbage"}, KeyError("odd")]
    sleeps = []
    done = asyncio.Event()

    async def next_notification():
        if answers:
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if len(sleeps) < 3:
            return None
        done.set()
        await asyncio.Event().wait()

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    mock_api["notify"].side_effect = next_notification
    with patch("custom_components.schluter_ditra_heat.coordinator.asyncio.sleep", fake_sleep):
        await _setup(hass)
        await asyncio.wait_for(done.wait(), 5)
    # One backoff for the exception, then a pause after each instant empty answer.
    assert sleeps[0] == 5 and len(sleeps) >= 3


async def test_rejected_after_login_is_not_auth_error(hass: HomeAssistant, aioclient_mock) -> None:
    """A 401 straight after a successful login must not ask for the password."""
    aioclient_mock.post(f"{API_URL}/authenticate/user", json={"SessionId": "s", "ErrorCode": 0})
    aioclient_mock.get(f"{API_URL}/thermostats", status=401)
    session = aioclient_mock.create_session(hass.loop)
    api = SchluterApi(session, "me@example.com", "pw")
    with pytest.raises(SchluterError) as err:
        await api.get_thermostats()
    await session.close()
    assert not isinstance(err.value, SchluterAuthError)


async def test_setup_errors(hass: HomeAssistant, mock_api) -> None:
    from homeassistant.config_entries import ConfigEntryState

    mock_api["get"].side_effect = SchluterError("down")
    entry = MockConfigEntry(domain=DOMAIN, data=CREDS, unique_id="a")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    mock_api["get"].side_effect = SchluterAuthError("bad password")
    entry = MockConfigEntry(domain=DOMAIN, data=CREDS, unique_id="b")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "auth_failed"
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


@pytest.mark.parametrize(
    ("error", "detail"), [(SchluterError("down"), "down"), (TimeoutError(), "TimeoutError")]
)
async def test_refresh_error_is_translated(
    hass: HomeAssistant, mock_api, error, detail
) -> None:
    await _setup(hass)
    coordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    mock_api["get"].side_effect = error
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.last_exception.translation_key == "cannot_connect"
    assert coordinator.last_exception.translation_placeholders == {"error": detail}
    assert hass.states.get("climate.kitchen_floor").state == "unavailable"


async def test_push_auth_failure_starts_reauth(hass: HomeAssistant, mock_api) -> None:
    started = asyncio.Event()

    async def next_notification():
        if not started.is_set():
            started.set()
            raise SchluterAuthError("expired")
        await asyncio.Event().wait()

    mock_api["notify"].side_effect = next_notification
    await _setup(hass)
    await asyncio.wait_for(started.wait(), 5)
    await hass.async_block_till_done()
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_push_timeout_and_errors_back_off(hass: HomeAssistant, mock_api) -> None:
    answers = [TimeoutError(), SchluterError("blip"), SchluterError("blip")]
    sleeps = []
    done = asyncio.Event()

    async def next_notification():
        if answers:
            raise answers.pop(0)
        done.set()
        await asyncio.Event().wait()

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    mock_api["notify"].side_effect = next_notification
    with patch("custom_components.schluter_ditra_heat.coordinator.asyncio.sleep", fake_sleep):
        await _setup(hass)
        await asyncio.wait_for(done.wait(), 5)
    # A timeout just re-opens; errors back off and double.
    assert [s for s in sleeps if s] == [5, 10]
    coordinator = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    assert not coordinator.push_connected
    assert coordinator.update_interval.total_seconds() == 60


async def test_push_removal_and_odd_actions(hass: HomeAssistant, mock_api) -> None:
    pushed = asyncio.Queue()

    async def next_notification():
        return await pushed.get()

    mock_api["notify"].side_effect = next_notification
    await _setup(hass)
    await pushed.put({"Action": 2, "Thermostat": {"Room": "no serial"}})
    await pushed.put({"Action": 9, "Thermostat": {"SerialNumber": "1254414", "Online": False}})
    await hass.async_block_till_done()
    assert hass.states.get("climate.kitchen_floor").state == "heat"

    await pushed.put({"Action": 3, "Thermostat": {"SerialNumber": "1254414"}})
    await hass.async_block_till_done()
    assert hass.states.get("climate.kitchen_floor").state == "unavailable"
    assert hass.states.get("binary_sensor.kitchen_floor_error").state == "unavailable"


async def test_hvac_modes(hass: HomeAssistant, mock_api) -> None:
    pushed = asyncio.Queue()

    async def next_notification():
        return await pushed.get()

    mock_api["notify"].side_effect = next_notification
    await _setup(hass)
    target = {"entity_id": "climate.kitchen_floor"}

    # Off, and not heating, reads as off.
    await pushed.put({"Action": 2, "Thermostat": {
        "SerialNumber": "1254414", "ManualTemperature": 500, "Heating": False,
    }})
    await hass.async_block_till_done()
    state = hass.states.get("climate.kitchen_floor")
    assert state.state == "off" and state.attributes["hvac_action"] == "off"

    # Heat from off goes back to the comfort temperature.
    await hass.services.async_call("climate", "set_hvac_mode", {**target, "hvac_mode": "heat"}, blocking=True)
    mock_api["update"].assert_awaited_with(
        "1254414", {"RegulationMode": 3, "ManualTemperature": 2333, "VacationEnabled": False}
    )
    # Heat while already holding keeps the held temperature.
    await hass.services.async_call("climate", "set_hvac_mode", {**target, "hvac_mode": "heat"}, blocking=True)
    mock_api["update"].assert_awaited_with(
        "1254414", {"RegulationMode": 3, "ManualTemperature": 2333, "VacationEnabled": False}
    )
    # Turning on means following the schedule; vacation is cleared when leaving it.
    await pushed.put({"Action": 2, "Thermostat": {"SerialNumber": "1254414", "VacationEnabled": True}})
    await hass.async_block_till_done()
    await hass.services.async_call("climate", "turn_on", target, blocking=True)
    mock_api["update"].assert_awaited_with("1254414", {"RegulationMode": 1, "VacationEnabled": True})

    entity = hass.data["climate"].get_entity("climate.kitchen_floor")
    calls = mock_api["update"].await_count
    await entity.async_set_temperature()
    assert mock_api["update"].await_count == calls
    assert entity.extra_state_attributes["error_code"] == 0


async def test_delete_device_only_once_off_the_account(
    hass: HomeAssistant, mock_api, hass_ws_client
) -> None:
    from homeassistant.helpers import device_registry as dr, entity_registry as er
    from homeassistant.setup import async_setup_component

    pushed = asyncio.Queue()

    async def next_notification():
        return await pushed.get()

    mock_api["notify"].side_effect = next_notification
    await _setup(hass)
    assert await async_setup_component(hass, "config", {})
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, "1254414")})
    client = await hass_ws_client(hass)

    async def delete() -> bool:
        await client.send_json_auto_id(
            {
                "type": "config/device_registry/remove_config_entry",
                "config_entry_id": entry.entry_id,
                "device_id": device.id,
            }
        )
        return (await client.receive_json())["success"]

    # Still on the account: refused.
    assert not await delete()
    assert dr.async_get(hass).async_get(device.id) is not None

    # Removed from the account: the device and its entities can go.
    await pushed.put({"Action": 3, "Thermostat": {"SerialNumber": "1254414"}})
    await hass.async_block_till_done()
    assert await delete()
    await hass.async_block_till_done()
    assert dr.async_get(hass).async_get(device.id) is None
    assert er.async_get(hass).async_get("climate.kitchen_floor") is None

    # Added back later: it gets fresh entities without a restart.
    await pushed.put({"Action": 2, "Thermostat": dict(THERMOSTAT)})
    await hass.async_block_till_done()
    assert hass.states.get("climate.kitchen_floor").state == "heat"
    assert hass.states.get("binary_sensor.kitchen_floor_online").state == "on"
