"""Read-only check of the Schluter API client against the real service.

Usage:  pip install aiohttp && python scripts/live_test.py [--watch]
Prompts for your Schluter email and password, logs in, and prints each
thermostat's status. With --watch it then stays connected and prints every
push notification as it arrives (change the setpoint in the Schluter app to
see one). It never changes a setting.
"""

import asyncio
import getpass
import importlib
from pathlib import Path
import sys
import time
import types

import aiohttp

# Load api.py without the package __init__ (which needs Home Assistant).
_pkg = types.ModuleType("schluter_ditra_heat")
_pkg.__path__ = [str(Path(__file__).resolve().parent.parent / "custom_components" / "schluter_ditra_heat")]
sys.modules["schluter_ditra_heat"] = _pkg
api = importlib.import_module("schluter_ditra_heat.api")


def _c(value) -> str:
    return "?" if value is None else f"{value / 100:.1f} °C ({value / 100 * 9 / 5 + 32:.0f} °F)"


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
                f"  target:      {_c(api.requested_temperature(t))} (requested)\n"
                f"  set point:   {_c(t.get('SetPointTemp'))} (last reported by the thermostat)\n"
                f"  mode:        {MODES.get(t.get('RegulationMode'), t.get('RegulationMode'))}\n"
                f"  error code:  {t.get('ErrorCode')}"
            )
        if "--watch" in sys.argv:
            print("\nWaiting for push notifications (Ctrl+C to stop)...")
            while True:
                try:
                    note = await client.wait_for_notification()
                except TimeoutError:
                    continue
                if note is None:
                    print(time.strftime("%H:%M:%S"), "no change (long-poll renewed)")
                    continue
                t = note["Thermostat"]
                print(
                    time.strftime("%H:%M:%S"),
                    f"action {note.get('Action')}: {t.get('Room')} online={t.get('Online')}"
                    f" heating={t.get('Heating')} mode={MODES.get(t.get('RegulationMode'), t.get('RegulationMode'))}"
                    f" target={_c(api.requested_temperature(t))} set point={_c(t.get('SetPointTemp'))}",
                )


asyncio.run(main())
