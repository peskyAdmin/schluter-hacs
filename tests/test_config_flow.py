"""Config, reauth and options flow tests."""

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.schluter_ditra_heat.api import SchluterAuthError, SchluterError
from custom_components.schluter_ditra_heat.const import CONF_MODE, DOMAIN

CREDS = {CONF_EMAIL: "me@example.com", CONF_PASSWORD: "pw"}
HOST_IP = "192.168.1.10"


@pytest.fixture
def no_setup():
    """Keep flows from setting the entry up for real."""
    with patch(
        "custom_components.schluter_ditra_heat.async_setup_entry", return_value=True
    ) as setup:
        yield setup


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (SchluterAuthError, "invalid_auth"),
        (SchluterError, "cannot_connect"),
        (RuntimeError, "unknown"),
    ],
)
async def test_user_flow_recovers_from_errors(
    hass: HomeAssistant, mock_api, no_setup, side_effect, error
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    mock_api["login"].side_effect = side_effect
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CREDS, CONF_EMAIL: " me@example.com "}
    )
    assert result["errors"] == {"base": error}

    mock_api["login"].side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "me@example.com"
    assert result["data"] == CREDS
    assert result["result"].unique_id == "me@example.com"


async def test_user_flow_rejects_duplicate(hass: HomeAssistant, mock_api) -> None:
    MockConfigEntry(domain=DOMAIN, data=CREDS, unique_id="me@example.com").add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CREDS, CONF_EMAIL: "Me@Example.com"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, mock_api, no_setup) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=CREDS, unique_id="me@example.com")
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    mock_api["login"].side_effect = SchluterAuthError
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mock_api["login"].side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"


def _entry(hass: HomeAssistant, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data=CREDS, unique_id="me@example.com", options=options or {}
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def host(hass: HomeAssistant):
    """This host's IP, and what it resolves the gateway name to."""
    resolved = {"ips": []}

    async def getaddrinfo(*_args, **_kwargs):
        if resolved["ips"] is None:
            raise OSError("no such host")
        return [(2, 1, 6, "", (ip, 9533)) for ip in resolved["ips"]]

    with (
        patch(
            "custom_components.schluter_ditra_heat.config_flow.async_get_source_ip",
            AsyncMock(return_value=HOST_IP),
        ) as source_ip,
        patch.object(hass.loop, "getaddrinfo", getaddrinfo),
    ):
        yield {"resolved": resolved, "source_ip": source_ip}


async def test_options_back_to_cloud(hass: HomeAssistant, host) -> None:
    entry = _entry(hass, {CONF_MODE: "local"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODE: "cloud"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_MODE: "cloud"}


async def test_options_local_with_dns_in_place(hass: HomeAssistant, host) -> None:
    host["resolved"]["ips"] = [HOST_IP]
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODE: "relay"}
    )
    assert result["step_id"] == "local"
    assert result["description_placeholders"]["ip"] == HOST_IP
    assert result["description_placeholders"]["hostname"] == "na-thermos.ojelectronics.com"

    # The box must be ticked.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"acknowledge": False}
    )
    assert result["errors"] == {"base": "must_acknowledge"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"acknowledge": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_MODE: "relay"}


@pytest.mark.parametrize("resolved", [["52.165.43.242"], None])
async def test_options_local_warns_once_without_dns(
    hass: HomeAssistant, host, resolved
) -> None:
    host["resolved"]["ips"] = resolved
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODE: "local"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"acknowledge": True}
    )
    assert result["errors"] == {"base": "dns_not_detected"}

    # Home Assistant may use a different DNS server; a second submit continues.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"acknowledge": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_MODE: "local"}


async def test_options_local_without_source_ip(hass: HomeAssistant, host) -> None:
    host["source_ip"].side_effect = RuntimeError
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODE: "relay"}
    )
    assert result["description_placeholders"]["ip"] == "?"


async def test_options_change_reloads_entry(hass: HomeAssistant, mock_api) -> None:
    entry = _entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_MODE: "cloud"}
        )
        await hass.async_block_till_done()
    reload.assert_awaited_once_with(entry.entry_id)


async def test_options_cloud_warns_while_dns_still_local(hass: HomeAssistant, host) -> None:
    host["resolved"]["ips"] = [HOST_IP]
    entry = _entry(hass, {CONF_MODE: "relay"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODE: "cloud"}
    )
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "dns_still_local"}

    # They may delete the record afterwards; a second submit switches.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODE: "cloud"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_MODE: "cloud"}


async def test_options_switch_between_local_modes(hass: HomeAssistant, host) -> None:
    host["resolved"]["ips"] = [HOST_IP]
    entry = _entry(hass, {CONF_MODE: "local"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODE: "relay"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"acknowledge": True}
    )
    assert entry.options == {CONF_MODE: "relay"}
