"""Shared fixtures."""

from unittest.mock import AsyncMock, patch

import pytest

THERMOSTAT = {
    "SerialNumber": "1254414",
    "Room": "Kitchen Floor",
    "Temperature": 2150,
    "SetPointTemp": 2000,
    "RegulationMode": 3,
    "ManualTemperature": 2000,
    "ComfortTemperature": 2333,
    "VacationEnabled": False,
    "Online": True,
    "Heating": True,
    "EarlyStartOfHeating": False,
    "MaxTemp": 4000,
    "MinTemp": 500,
    "ErrorCode": 0,
    "SWVersion": "1012P201",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def mock_api():
    with (
        patch("custom_components.schluter_ditra_heat.api.SchluterApi.login", AsyncMock()) as login,
        patch(
            "custom_components.schluter_ditra_heat.api.SchluterApi.get_thermostats",
            AsyncMock(side_effect=lambda: {"1254414": dict(THERMOSTAT)}),
        ) as get,
        patch(
            "custom_components.schluter_ditra_heat.api.SchluterApi.update_thermostat",
            AsyncMock(),
        ) as update,
    ):
        yield {"login": login, "get": get, "update": update}
