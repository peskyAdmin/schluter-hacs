"""Gateway certificate generation."""

import os

from homeassistant.core import HomeAssistant

from custom_components.schluter_ditra_heat.cert import async_ensure_cert


async def test_cert_created_once(hass: HomeAssistant) -> None:
    cert, key = await async_ensure_cert(hass)
    assert os.path.exists(cert) and os.path.exists(key)
    assert os.stat(key).st_mode & 0o777 == 0o600
    with open(cert, encoding="ascii") as file:
        assert file.read().startswith("-----BEGIN CERTIFICATE-----")
    mtime = os.stat(cert).st_mtime_ns
    assert await async_ensure_cert(hass) == (cert, key)
    assert os.stat(cert).st_mtime_ns == mtime
