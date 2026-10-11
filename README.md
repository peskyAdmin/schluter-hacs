# Schluter DITRA-HEAT-E-WiFi for Home Assistant

Control Schluter DITRA-HEAT-E-WiFi floor-heating thermostats from Home Assistant. By default it uses the same Schluter cloud service as the DITRA-HEAT app and website. An optional, advanced local mode keeps Home Assistant in control when your internet is down.

This is a community integration, not made or supported by Schluter. Home Assistant's built-in `schluter` integration targets an older Schluter service; this one has its own domain (`schluter_ditra_heat`), so both can be installed.

## Before you start

- **Thermostat:** the Wi-Fi model, DITRA-HEAT-E-WiFi, registered on the North American Schluter service (the one at ditra-heat-e-wifi.schluter.com). Non-Wi-Fi DITRA-HEAT thermostats have no network connection and can't be supported. Thermostats registered on other regional services are untested.
- **Account:** you need a Schluter account with the thermostat already added and online. This integration can't create an account, pair a thermostat or set up its Wi-Fi. Do that in the Schluter DITRA-HEAT app first, then check you can sign in on the website with the same email and password.
- **Home Assistant:** 2024.11 or newer.

## Install

1. In HACS, open **Custom repositories**, add this repository's URL, and choose the **Integration** category.
2. Install **Schluter DITRA-HEAT-E-WiFi** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration**, choose **Schluter DITRA-HEAT-E-WiFi**, and sign in with your Schluter email and password.

Every thermostat on the account is added. To install manually instead, copy `custom_components/schluter_ditra_heat` into your `config/custom_components/` folder and restart.

## What you get

Each thermostat becomes a device with a climate entity. Changes show up within a few seconds, whether they're made in Home Assistant, the Schluter app or on the thermostat.

| Home Assistant | Thermostat |
| --- | --- |
| **Auto** | Follows the thermostat's own schedule |
| **Heat** | Permanent hold at the target temperature |
| **Off** | Hold at 5 °C / 41 °F (the app's "away"; frost protection, not a true off) |
| Set temperature | Switches to a permanent hold at that temperature |

The climate entity shows the floor temperature, the target you set, and whether the floor is heating. Each thermostat also gets three binary sensors:

- **Heating:** the floor is drawing power right now.
- **Online** (diagnostic): the thermostat is connected. Handy for an alert when it drops off Wi-Fi.
- **Error** (diagnostic): the thermostat reports a fault, such as a floor-sensor problem.

There are no extra actions; use the standard climate actions (`climate.set_temperature`, `climate.set_hvac_mode`, `climate.turn_on`, `climate.turn_off`).

**Schedules:** the integration doesn't edit the thermostat's built-in schedule. Either leave the thermostat in **Heat** and drive the temperature from Home Assistant automations, or use **Auto** and edit the schedule in the Schluter app. Auto keeps working if Home Assistant goes down.

## Options

Sign-in needs only your Schluter email and password. After that there's one setting, under **Settings → Devices & services → Schluter → Configure**:

**Connection mode**

| Mode | Setup | Schluter app | If your internet drops |
| --- | --- | --- | --- |
| **Cloud** (default) | None | Works | Home Assistant loses the thermostat until it's back |
| **Local relay** | DNS record | Works | Home Assistant keeps control; the app goes offline |
| **Local only** | DNS record | Shows offline | Nothing changes; the thermostat never uses the cloud |

Stay on Cloud unless you're comfortable editing DNS on your network. You can switch modes later without losing entities; see [Switching modes](#switching-modes).

## Local control (advanced)

The thermostat keeps a separate connection open to OJ Electronics' device gateway at `na-thermos.ojelectronics.com`, port 9533. OJ Electronics builds the hardware for Schluter. In a local mode, Home Assistant runs its own gateway on that port. You add one DNS record so the thermostat finds Home Assistant instead of OJ. Nothing on the thermostat is modified, and deleting the record undoes it.

### Set it up

1. **Give Home Assistant a fixed IP** (a DHCP reservation on your router).
2. **Add a DNS record** pointing `na-thermos.ojelectronics.com` at that IP, on whatever answers DNS for your thermostat:
   - **Pi-hole:** Local DNS → DNS Records (Settings → Local DNS Records on Pi-hole v6).
   - **AdGuard Home** (also available as a Home Assistant add-on): Filters → DNS rewrites.
   - **UniFi:** add a Host (A) DNS record; on current UniFi Network versions it's under Settings → Policy Engine.
   - **Any other router:** look for "local DNS", "DNS host mapping" or "static DNS". If your router (typical of ISP-supplied ones) can't do this, run Pi-hole or AdGuard Home and set it as your network's DNS server.
3. In Home Assistant, open **Configure**, choose **Local relay** (recommended) or **Local only**, and tick the box. It checks the record and warns you if it can't see it.
4. **Power-cycle the thermostat** so it looks up the address again. Within a few minutes, its **Online** sensor turns on.

To check the record from any computer on your network, run `nslookup na-thermos.ojelectronics.com`; it should answer with Home Assistant's IP. If no thermostat connects within 10 minutes, Home Assistant shows a repair notice under **Settings → System → Repairs**. The notice clears on its own once a thermostat connects.

### Switching modes

You can change modes at any time under **Configure**. Home Assistant reloads the integration, which takes a few seconds. Your devices, entity IDs, names, history, dashboards and automations stay the same in every mode, because they're tied to the thermostat's serial number.

| Switching | What to do | What you'll see |
| --- | --- | --- |
| Cloud → Local relay or Local only | Add the DNS record, pick the mode, then power-cycle the thermostat | Entities are unavailable until the thermostat connects to Home Assistant, usually within a few minutes |
| Local relay ↔ Local only | Nothing else; the DNS record stays as it is | A short unavailable gap while the thermostat reconnects on its own. Power-cycle it if that takes more than a few minutes |
| Local relay or Local only → Cloud | Pick Cloud, delete the DNS record, then power-cycle the thermostat | Entities are unavailable until the thermostat is back on Schluter's cloud. If the record is still in place, Home Assistant warns you before switching |

A few things mean something slightly different in the local modes:

- **Online** means the thermostat is connected to Home Assistant, rather than to Schluter's cloud.
- **Error**, and the extra attributes such as measured load, show the last values the cloud reported.
- The Schluter app keeps working in Local relay and shows the thermostat offline in Local only.

To go back to the cloud for good, switch to **Cloud** as described above.

## Limitations and gotchas

**All modes**

- This uses Schluter's unofficial cloud API and device protocol. A change on Schluter's side could break it.
- Vacation mode and temporary holds started from the app or the thermostat show as **Heat** with the right target. You can't start vacation mode from Home Assistant.
- Temperatures move in 0.5 °C steps. If Home Assistant uses Fahrenheit, it converts for you.
- If you change your Schluter password, Home Assistant asks you to sign in again (a notice under Settings).
- In **Cloud** mode, the thermostat shows as unavailable in Home Assistant while it or Schluter's service is offline.

**Local modes**

- The thermostat has to actually use your DNS record. If your router hands out a public DNS server (such as 8.8.8.8) to devices, the record never reaches it.
- The thermostat must be able to reach Home Assistant on TCP port 9533. If they're on different VLANs, allow that in your firewall.
- Port 9533 must be free on the Home Assistant host. On a Docker install, use host networking or publish port 9533. Only one Schluter account per Home Assistant host can use local control.
- After any DNS change, power-cycle the thermostat; it remembers the old address until then.
- **Relay:** if the internet is down when the thermostat connects, Home Assistant serves it locally and keeps control. The Schluter app stays offline until the thermostat next reconnects, even after the internet returns; power-cycle it to bring the app back sooner. The same fallback happens if OJ ever moves their gateway, until this integration is updated.
- **Local only:** the Schluter app and website show the thermostat as offline, and it won't receive firmware updates. When the internet is up, Home Assistant still reads room names and temperature limits from your Schluter account; the thermostat itself never contacts the cloud.
- **Error sensor:** the local connection doesn't decode fault codes yet, so in local modes the Error sensor shows the last value the cloud reported. Floor temperature, target, mode, heating and online status are all read live from the thermostat.
- A change sent while the thermostat is disconnected is applied when it reconnects.
- Any device on your network can connect to port 9533. Run local control only on a network you trust.

## Troubleshooting

- **"Invalid email or password":** check that the same email and password work on the Schluter website.
- **Thermostat unavailable:** check its **Online** sensor and the Schluter app. In a local mode, see the repair notice and the checks above.
- **Reporting a problem:** download diagnostics from **Settings → Devices & services → Schluter → ⋮ → Download diagnostics**. Your email, password, serial numbers and room names are removed. Attach the file to an [issue](https://github.com/peskyAdmin/schluter-hacs/issues). For more detail, turn on debug logging from the same menu, reproduce the problem, then turn it off to download the log.

## Remove

1. Go to **Settings → Devices & services → Schluter**, open the **⋮** menu and choose **Delete**. The integration deletes the files it saved (the local gateway certificate and the saved account snapshot).
2. If you used local control, delete the DNS record and power-cycle the thermostat so it goes back to the cloud.
3. To uninstall the code, remove the integration in HACS and restart Home Assistant.

## Development

- `pytest` runs the tests (`pip install pytest-homeassistant-custom-component`). CI also runs hassfest and the HACS check, and requires 95% test coverage.
- `python scripts/live_test.py` signs in to the real Schluter service and prints each thermostat's status without changing anything (`pip install aiohttp`). Add `--watch` to print push notifications as they arrive.
- Cloud API: login uses `POST /api/authenticate/user`, falling back to the website's OpenID Connect flow, and yields a `SessionId` passed as `?sessionid=`. `GET /api/thermostats` lists thermostats; `POST /api/thermostat?serialnumber=…` changes one. `GET /api/notification?sequencenr=0` is a long-poll that returns when a thermostat changes. A full fetch runs every 15 minutes as a safety net, or every 60 seconds while push is down.
- Units: the cloud uses hundredths of a degree Celsius (`2000` = 20 °C). On the device protocol, the setpoint field is in two-hundredths; the integration converts between them. `RegulationMode` is `1` schedule, `2` temporary hold, `3` permanent hold, `4` vacation.
- The device protocol used by local control is documented in [docs/device-protocol.md](docs/device-protocol.md).

## Status

Version 1.1.0. Cloud mode is tested and in daily use with a DITRA-HEAT-E-WiFi thermostat. Local relay and local only are new in 1.1.0.
