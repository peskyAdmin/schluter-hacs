# Device protocol

Maintainer notes on how the thermostat talks to OJ Electronics' device gateway, as implemented in [`gateway.py`](../custom_components/schluter_ditra_heat/gateway.py). There's no published spec; field numbers are as observed, and the tests in [`tests/test_gateway.py`](../tests/test_gateway.py) pin the behavior with real frames.

## Transport

- The thermostat connects to `na-thermos.ojelectronics.com` on TCP port 9533 using TLS 1.2. It doesn't validate the server's certificate.
- Each message is the byte `0x08`, a varint length, then a protobuf message.
- Field 1 is a sequence number. An acknowledgement echoes it with field 3 set to 1.

## From the thermostat

| Message | Meaning | The gateway answers |
| --- | --- | --- |
| Has field 6 | Login. 6.1 is the account email, 6.2 the serial number, 6.3 a number we don't use | Ack plus 4 = 1 and 12 = {1: server time} |
| 2 = 6 | Schedule and settings upload | Ack, then a customer-service info message (field 18) |
| 2 = 4 | Full state upload after login: 7 device info, 8 schedule, 9 state | Ack, then a configuration message (field 10) |
| Field 9, no field 2 | Someone changed the thermostat at the wall | Ack |
| Field 7, no field 2 | Device info changed | Ack |
| Field 15 | Heartbeat about once a minute: 15.1 floor temperature (°C × 100), 15.2 heating (1 or 0) | Ack |
| Fields 1 and 3 only | The thermostat acknowledging us | Nothing |

## State (field 9)

- 9.1: timestamp, in seconds since 2000-01-01 UTC.
- 9.2: regulation mode. 1 schedule, 2 temporary hold, 3 permanent hold, 4 vacation; these match the cloud API's `RegulationMode`.
- 9.4: the temporary hold. 9.4.1 is its setpoint in °C × 200 (the cloud API's `ComfortTemperature`, which uses °C × 100, so the integration converts). 9.4.2 is the hold's end time.
- 9.5: the permanent hold. 9.5.1 is its setpoint in °C × 200 (the cloud API's `ManualTemperature`).
- The thermostat keeps both setpoints all the time and uses the one for the current mode. In a captured permanent hold, 9.4.1 was 13.33 °C, 9.5.1 was 14.44 °C (58 °F), and device info 7.9 reported 14.44 °C as the setpoint.
- 9.3 and 9.6 aren't interpreted. From the thermostat, 9.3 is `{1: 0}`; in the cloud's commands it carries more (two 7-byte values and a number that looks like a temperature), probably the vacation settings.
- Device info 7.2 is the room name. 7.9 is the setpoint the thermostat is aiming for right now, in any mode, which is the only place the schedule's current setpoint appears.

## Commands to the thermostat

A command has the same shape as a state change: field 1 sequence, 5 = 1, 8 the thermostat's own last schedule, and 9 a copy of its last state with a fresh timestamp and the new mode. A hold's setpoint goes into that hold's own field: 9.5.1 for a permanent hold (what Home Assistant sets), 9.4.1 for a temporary one. Everything else, including the other hold's setpoint, goes back exactly as the thermostat sent it. Returning to the schedule changes only the mode. The thermostat acknowledges a command with its sequence number. The gateway's sequence numbers start at 101, so they're easy to tell apart from the cloud's.

Writing a permanent hold's setpoint into 9.4.1 doesn't work: the thermostat switches to the permanent hold and uses the setpoint already in 9.5.1. Version 1.1.0 did this, so every change from Home Assistant landed on the stored permanent-hold temperature.

In relay mode, changes made in the Schluter app reach the thermostat from the cloud in this same shape, so the gateway decodes those too. Home Assistant's own changes go straight to the thermostat in both local modes.

With debug logging on, the gateway logs a one-line summary of every frame in each direction, and of each command it sends: sequence number, field numbers, mode, both hold setpoints in °C and °F, the temporary hold's end time, the current target, floor temperature and heating, and the raw value of any state field it doesn't interpret. Login frames are logged only as "login", so the account email never appears. It also logs each change Home Assistant asks for, why a change is waiting (the thermostat isn't connected, or hasn't sent its state yet), when the thermostat acknowledges a command, and the values handed to Home Assistant.

## Time

The gateway sends UTC as the server time, as OJ's cloud does. The thermostat applies its own time zone.

## Not decoded yet

- Fault codes, which is why the Error sensor keeps the cloud's last value in local modes.
- Telemetry 15.4 to 15.7, and device-info fields 7.6, 7.7, 7.10, 7.11, 7.13 and 7.14.
