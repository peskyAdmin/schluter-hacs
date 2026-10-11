"""Cloud API client tests (login paths, retries, payload handling)."""

import aiohttp
import pytest

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.schluter_ditra_heat.api import (
    SchluterApi,
    SchluterAuthError,
    SchluterError,
    _parse_form,
    requested_temperature,
)
from custom_components.schluter_ditra_heat.const import API_URL, BASE_URL

IDP = "https://identity-schluter.mythermostat.info"
LOGIN_FORM = (
    '<form action="/Account/Login?ReturnUrl=x" method="post">'
    '<input name="__RequestVerificationToken" value="tok">'
    '<input type="checkbox" name="RememberLogin" value="true">'
    '<input name="Username"><input name="Password" type="password">'
    "</form><form><input name=\"ignored\" value=\"1\"></form>"
)
CALLBACK_FORM = (
    '<form method="post" action="https://ditra-heat-e-wifi.schluter.com/signin-oidc">'
    '<input type="hidden" name="code" value="c"><input type="hidden" name="state" value="s">'
    "</form>"
)
HOME = '<script>var session_object = {"SessionId": "oidc-session", "ErrorCode": 0};</script>'
GROUPS = {"Groups": [{"Thermostats": [{"SerialNumber": 1254414, "Room": "Kitchen"}]}]}


@pytest.fixture
async def api(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker):
    session = aioclient_mock.create_session(hass.loop)
    yield SchluterApi(session, "me@example.com", "pw")
    await session.close()


def test_requested_temperature() -> None:
    assert requested_temperature({"RegulationMode": 3, "ManualTemperature": 2100}) == 2100
    assert requested_temperature({"RegulationMode": 2, "ComfortTemperature": "2333"}) == 2333
    assert requested_temperature({"RegulationMode": 4, "VacationTemperature": ""}) is None
    assert requested_temperature({"RegulationMode": 1, "SetPointTemp": 1900}) == 1900
    assert requested_temperature({}) is None


def test_form_parser_skips_unchecked_boxes_and_later_forms() -> None:
    form = _parse_form(LOGIN_FORM)
    assert form.action == "/Account/Login?ReturnUrl=x"
    assert form.fields == {"__RequestVerificationToken": "tok", "Username": "", "Password": ""}
    checked = _parse_form('<form><input type="radio" name="r" value="a" checked></form>')
    assert checked.fields == {"r": "a"}


async def test_direct_login(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, api) -> None:
    aioclient_mock.post(f"{API_URL}/authenticate/user", json={"SessionId": "s1", "ErrorCode": 0})
    aioclient_mock.get(f"{API_URL}/thermostats", json=GROUPS)
    assert await api.get_thermostats() == {"1254414": GROUPS["Groups"][0]["Thermostats"][0]}
    # Every call carries the session id.
    assert str(aioclient_mock.mock_calls[-1][1]).endswith("sessionid=s1")


@pytest.mark.parametrize(
    "direct",
    [
        {"status": 404},
        {"json": {"ErrorCode": 7}},
        {"text": "not json"},
        {"exc": aiohttp.ClientError()},
    ],
)
async def test_oidc_login_after_direct_login_fails(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, api, direct
) -> None:
    aioclient_mock.post(f"{API_URL}/authenticate/user", **direct)
    aioclient_mock.get(BASE_URL, text=LOGIN_FORM)
    aioclient_mock.post(f"{BASE_URL}/Account/Login", text=CALLBACK_FORM)
    aioclient_mock.post(f"{BASE_URL}/signin-oidc", text=HOME)
    aioclient_mock.get(f"{API_URL}/thermostats", json=GROUPS)
    await api.login()
    posted = [call for call in aioclient_mock.mock_calls if call[0] == "POST"]
    assert posted[-2][2]["Username"] == "me@example.com"
    assert posted[-1][2] == {"code": "c", "state": "s"}
    assert str(aioclient_mock.mock_calls[-1][1]).endswith("sessionid=oidc-session")


async def test_oidc_already_signed_in(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, api) -> None:
    aioclient_mock.post(f"{API_URL}/authenticate/user", status=500)
    aioclient_mock.get(BASE_URL, text=HOME)
    aioclient_mock.get(f"{API_URL}/thermostats", json=GROUPS)
    await api.login()


@pytest.mark.parametrize(
    ("home", "post", "error"),
    [
        ("<html>maintenance</html>", None, SchluterError),
        (LOGIN_FORM, LOGIN_FORM, SchluterAuthError),  # wrong password re-renders the form
        (LOGIN_FORM, "<html>done</html>", SchluterAuthError),  # no session at the end
        ('<script>var session_object = {"ErrorCode": 3};</script>', None, SchluterAuthError),
    ],
)
async def test_oidc_login_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, api, home, post, error
) -> None:
    aioclient_mock.post(f"{API_URL}/authenticate/user", status=500)
    aioclient_mock.get(BASE_URL, text=home)
    if post is not None:
        aioclient_mock.post(f"{BASE_URL}/Account/Login", text=post)
    with pytest.raises(error):
        await api.login()


def test_extract_session_id_ignores_bad_json() -> None:
    assert SchluterApi._extract_session_id("var session_object = {nope};") is None
    assert SchluterApi._extract_session_id("<html></html>") is None


async def test_expired_session_logs_in_again(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, api
) -> None:
    api._session_id = "old"
    aioclient_mock.get(f"{API_URL}/useraccount?sessionid=old", status=401)
    aioclient_mock.post(f"{API_URL}/authenticate/user", json={"SessionId": "new"})
    aioclient_mock.get(f"{API_URL}/thermostats", json=GROUPS)
    aioclient_mock.get(f"{API_URL}/useraccount?sessionid=new", json={"Email": "me@example.com"})
    assert await api.get_user_account() == {"Email": "me@example.com"}


async def test_http_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, api) -> None:
    api._session_id = "s"
    aioclient_mock.get(f"{API_URL}/thermostats", status=500)
    with pytest.raises(SchluterError, match="HTTP 500"):
        await api.get_thermostats()


async def test_update_thermostat(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, api) -> None:
    api._session_id = "s"
    aioclient_mock.post(f"{API_URL}/thermostat?serialnumber=1", json={"Success": True})
    await api.update_thermostat("1", {"RegulationMode": 1})
    assert aioclient_mock.mock_calls[-1][2] == {"RegulationMode": 1}

    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{API_URL}/thermostat?serialnumber=1", json={"Success": False})
    with pytest.raises(SchluterError, match="rejected"):
        await api.update_thermostat("1", {"RegulationMode": 1})


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"Action": 2, "Thermostat": {"SerialNumber": 1}}, {"Action": 2, "Thermostat": {"SerialNumber": 1}}),
        ({"Action": 2, "Thermostat": None}, None),
        ({}, None),
    ],
)
async def test_wait_for_notification(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, api, body, expected
) -> None:
    api._session_id = "s"
    aioclient_mock.get(f"{API_URL}/notification", json=body)
    assert await api.wait_for_notification() == expected
