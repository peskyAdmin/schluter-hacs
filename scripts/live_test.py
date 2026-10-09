"""Read-only check of the Schluter API client against the real service.

Usage:  pip install aiohttp && python scripts/live_test.py
Prompts for your Schluter email and password, logs in, and prints each
thermostat's status. It never changes a setting.
"""

import asyncio
import getpass
import importlib
from pathlib import Path
import sys
import types

import aiohttp

# Load api.py without the package __init__ (which needs Home Assistant).
_pkg = types.ModuleType("schluter_ditra_heat")
_pkg.__path__ = [str(Path(__file__).resolve().parent.parent / "custom_components" / "schluter_ditra_heat")]
sys.modules["schluter_ditra_heat"] = _pkg
api = importlib.import_module("schluter_ditra_heat.api")

MODES = {1: "schedule", 2: "temporary hold", 3: "permanent hold", 4: "vacation"}


async def main() -> None:
    email = input("Schluter email: ").strip()
    password = getpass.getpass("Password: ")
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        client = api.SchluterApi(session, email, password)
        direct = await client._login_direct()
        print(f"Direct /api/authenticate/user login: {'works' if direct else 'not available'}")
        await client.login()
        print("Login: OK")
        for serial, t in (await client.get_thermostats()).items():
            print(
                f"\n{t.get('Room')} ({serial})\n"
                f"  online:      {t.get('Online')}\n"
                f"  heating:     {t.get('Heating')}\n"
                f"  floor temp:  {t.get('Temperature', 0) / 100:.1f} °C\n"
                f"  set point:   {t.get('SetPointTemp', 0) / 100:.1f} °C\n"
                f"  mode:        {MODES.get(t.get('RegulationMode'), t.get('RegulationMode'))}\n"
                f"  error code:  {t.get('ErrorCode')}"
            )


asyncio.run(main())
