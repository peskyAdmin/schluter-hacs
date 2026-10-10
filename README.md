# Schluter DITRA-HEAT-E-WiFi for Home Assistant

A HACS custom integration for Schluter DITRA-HEAT-E-WiFi floor heating thermostats, using the same cloud API as the web app at <https://ditra-heat-e-wifi.schluter.com>.

Home Assistant's built-in `schluter` integration targets an older API, so this one uses its own domain, `schluter_ditra_heat`, and can be installed alongside it.

## Install

1. In HACS, open **Custom repositories**, add this repository's URL, and choose the **Integration** category.
2. Install **Schluter DITRA-HEAT-E-WiFi** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration**, search for **Schluter DITRA-HEAT-E-WiFi**, and sign in with your Schluter email and password.

Manual install: copy `custom_components/schluter_ditra_heat` into your Home Assistant `config/custom_components/` folder and restart.

## What you get

One climate entity per thermostat on the account. Changes show up within a few seconds, whether they come from Home Assistant, the Schluter app or the thermostat itself.

| Home Assistant | Thermostat |
| --- | --- |
| **Auto** | Follow the thermostat's schedule |
| **Heat** | Permanent hold at the target temperature |
| **Off** | The web app's "away" setting (hold at 5 °C / 41 °F) |
| Set temperature | Switches to a permanent hold at that temperature |

The climate entity shows the floor temperature, the target you asked for, and whether the floor is heating. The target comes from the active hold (or the schedule's set point in Auto), so a change shows right away even before the thermostat confirms it. It is marked unavailable while the thermostat is offline in the Schluter cloud.

Each thermostat also gets binary sensors:

- **Online** (diagnostic): whether the thermostat is currently connected to the Schluter cloud. Use it to alert when the floor drops off Wi-Fi.
- **Heating**: whether the floor is drawing power right now.
- **Error** (diagnostic): on when the thermostat reports a non-zero error code (for example a floor sensor fault).

## Schedules

The integration does not edit the thermostat's built-in schedule. Pick one of two ways to run it:

- **Let Home Assistant own the schedule (recommended).** Leave the thermostat in **Heat** and change the target temperature from automations or a Schedule helper. Manual holds are permanent, so the thermostat's own schedule stays out of the way.
- **Let the thermostat own it.** Put the entity in **Auto** and it follows the schedule set in the Schluter app. This keeps working if Home Assistant or your internet goes down.

## Testing

- `pytest` runs the Home Assistant tests (needs `pip install pytest-homeassistant-custom-component`).
- `python scripts/live_test.py` signs in to the real Schluter service and prints each thermostat's status without changing anything (needs `pip install aiohttp`). Add `--watch` to stay connected and print push notifications as they arrive.

## How it talks to Schluter

- Login tries the legacy `POST /api/authenticate/user` first, then falls back to the website's OpenID Connect flow against `identity-schluter.mythermostat.info`. Either way the result is a `SessionId` used as `?sessionid=` on every API call. An expired session returns HTTP 401 and the integration signs in again.
- `GET /api/thermostats` returns all thermostats (grouped). `POST /api/thermostat?serialnumber=…` changes one.
- `GET /api/notification?sequencenr=0` is a long-poll: the server holds it open until a thermostat changes, then returns `{"Action": 2, "Thermostat": {...}}` (3 means removed). With nothing to report it answers after about a minute and the integration re-opens it. A new session only gets notifications after it has listed the thermostats once, so every login does that first.
- A full fetch still runs every 15 minutes as a safety net, and every 60 seconds while the push connection is down.
- Temperatures are integers in hundredths of a degree Celsius (`2000` = 20 °C).
- `RegulationMode`: `1` schedule, `2` temporary hold until `ComfortEndTime`, `3` permanent hold at `ManualTemperature`, `4` vacation.

## Status

Version 1.0.0. Tested and working in a live Home Assistant install with a DITRA-HEAT-E-WiFi thermostat.

This uses Schluter's unofficial cloud API, so a change on Schluter's side could break it. If it stops working, please [open an issue](https://github.com/peskyAdmin/schluter-hacs/issues).
