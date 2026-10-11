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
- 9.4.1: setpoint in °C × 200. The cloud API uses °C × 100, so the integration converts. 9.4.2 is the hold's end time.
- 9.3, 9.5 and 9.6 aren't interpreted; commands copy them back unchanged.
- Device info 7.2 is the room name. 7.9 mirrors the setpoint.

## Commands to the thermostat

A command has the same shape as a state change: field 1 sequence, 5 = 1, 8 the thermostat's own last schedule, and 9 a copy of its last state with the new mode and setpoint. In relay mode, changes made in the Schluter app reach the thermostat from the cloud in this same shape, so the gateway decodes those too.

## Time

The gateway sends UTC as the server time, as OJ's cloud does. The thermostat applies its own time zone.

## Not decoded yet

- Fault codes, which is why the Error sensor keeps the cloud's last value in local modes.
- Telemetry 15.4 to 15.7, and device-info fields 7.6, 7.7, 7.10, 7.11, 7.13 and 7.14.
