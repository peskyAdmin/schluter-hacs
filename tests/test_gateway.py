"""Protocol unit tests for the local device gateway.

These exercise the wire-format helpers directly and need no Home Assistant
fixtures. Setpoints on the device are Celsius x200, while the coordinator uses
Celsius x100; getting that scale wrong writes half the intended temperature, so
it is pinned here. So is which field each hold keeps its setpoint in: a
permanent hold written to the temporary hold's field leaves the thermostat at
its old permanent setpoint.
"""

from custom_components.schluter_ditra_heat import gateway as g

# A login frame shaped like the thermostat's (account email, serial and a number
# we don't use), with a placeholder email.
_ACCOUNT = (
    g._tag(1, 2) + g.wvarint(16) + b"user@example.com"
    + g._tag(2, 2) + g.wvarint(7) + b"1254414"
    + g._tag(3, 0) + g.wvarint(10207)
)
LOGIN = g._frame(
    g._tag(1, 0) + g.wvarint(29) + g._tag(5, 0) + g.wvarint(1)
    + g._tag(6, 2) + g.wvarint(len(_ACCOUNT)) + _ACCOUNT
)


def _strip(frame: bytes) -> bytes:
    assert frame[0] == 0x08
    length, j = g.uvarint(frame, 1)
    body = frame[j:]
    assert len(body) == length
    return body


def _wrap(field: int, body: bytes) -> bytes:
    return g._tag(field, 2) + g.wvarint(len(body)) + body


def _make_f9(mode: int, *, temporary: int = 3332, permanent: int = 4444) -> bytes:
    """A state shaped like the thermostat's, setpoints in Celsius x200."""
    return (
        g._tag(1, 0) + g.wvarint(1000)
        + g._tag(2, 0) + g.wvarint(mode)
        + _wrap(4, g._tag(1, 0) + g.wvarint(temporary))
        + _wrap(5, g._tag(1, 0) + g.wvarint(permanent))
        + g._tag(6, 0) + g.wvarint(0)
    )


# The state from a real upload while the thermostat held 58 F permanently: mode 3,
# temporary setpoint 9.4.1 = 2666 (13.33 C), permanent setpoint 9.5.1 = 2888
# (14.44 C, 58 F). Its device info reported 7.9 = 2888, the permanent one.
PERMANENT_F9 = bytes.fromhex(
    "08c5e4f4920310031a020800220908ea1410809cf692032a0308c8163000"
)


def test_varint_roundtrip() -> None:
    for n in (0, 1, 127, 128, 2666, 4444, 946684800):
        encoded = g.wvarint(n)
        assert g.uvarint(encoded, 0) == (n, len(encoded))


def test_login_serial() -> None:
    assert g._login_serial(g.parse(_strip(LOGIN))) == "1254414"


def test_decode_state_uses_x200_scale() -> None:
    # 22.22 C held permanently -> device field 4444 (x200) -> 2222 (x100).
    body = _wrap(7, _wrap(2, b"Kitchen Floor")) + _wrap(9, _make_f9(3))
    fields = g._decode_state(body)
    assert fields == {
        "RegulationMode": 3,
        "ComfortTemperature": 1666,
        "ManualTemperature": 2222,
        "SetPointTemp": 2222,
        "Room": "Kitchen Floor",
    }


def test_decode_state_reads_each_holds_own_setpoint() -> None:
    # A real permanent hold at 58 F: the active setpoint is 9.5.1, not 9.4.1.
    fields = g._decode_state(_wrap(9, PERMANENT_F9))
    assert fields == {
        "RegulationMode": 3,
        "ComfortTemperature": 1333,
        "ManualTemperature": 1444,
        "SetPointTemp": 1444,
    }
    # A temporary hold uses 9.4.1.
    assert g._decode_state(_wrap(9, _make_f9(2)))["SetPointTemp"] == 1666
    # The schedule's setpoint isn't in the state; it comes from device info 7.9.
    schedule = g._decode_state(_wrap(9, _make_f9(1)))
    assert "SetPointTemp" not in schedule
    info = _wrap(7, g._tag(9, 0) + g.wvarint(4000))
    assert g._decode_state(_wrap(9, _make_f9(1)) + info)["SetPointTemp"] == 2000


def test_build_set_roundtrip() -> None:
    gw = g.LocalGateway("cert", "key", relay=False, on_state=lambda *a: None)
    gw._state_raw["1254414"] = _make_f9(3, temporary=2666, permanent=2888)
    top = g.parse(gw._build_set("1254414", 3, 5000))
    assert top[5][0] == 1
    f9 = g.parse(top[9][0])
    assert f9[2][0] == 3  # permanent hold
    assert g.parse(f9[5][0])[1][0] == 5000  # new permanent setpoint, x200
    assert g.parse(f9[4][0])[1][0] == 2666  # temporary setpoint untouched


def test_build_set_without_template_returns_none() -> None:
    gw = g.LocalGateway("cert", "key", relay=False, on_state=lambda *a: None)
    assert gw._build_set("unknown", 3, 5000) is None


def test_decode_telemetry_heating_flag() -> None:
    # Real telemetry frames captured across a heat/stop: field 1 is the current
    # temperature (x100) and field 2 is the live heating flag (1 on, 0 idle).
    idle = bytes.fromhex("08f10f1000200028003000388c04")
    heating = bytes.fromhex("08ee0f1001200028003000389904")
    assert g._decode_telemetry(idle) == {"Temperature": 2033, "Heating": False}
    assert g._decode_telemetry(heating) == {"Temperature": 2030, "Heating": True}


# --- connection handling ----------------------------------------------------
import asyncio  # noqa: E402
import logging  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

SERIAL = "1254414"
# Real frames from a heat/stop capture: telemetry, a setpoint change made at the
# thermostat (field 9 only, no field 2), and its device-info follow-up.
TELEMETRY = bytes.fromhex("08930328017a0e08f10f1000200028003000388c04")
WALL_STATE = bytes.fromhex(
    "08960328014a1e08cedbf6920310021a020800220908ec2110e0c4f792032a0308c8163000"
)
WALL_INFO = bytes.fromhex("08970328013a1708d0dbf6920330e80738c03e48ec215001581968017064")
DEVICE_ACK = g._tag(1, 0) + g.wvarint(0) + g._tag(3, 0) + g.wvarint(1)
UNKNOWN = g._tag(1, 0) + g.wvarint(55) + g._tag(20, 0) + g.wvarint(1)
NO_SEQ = g._tag(20, 0) + g.wvarint(1)


def _state_upload(seq: int = 31, setpoint_x200: int = 4444) -> bytes:
    return (
        g._tag(1, 0) + g.wvarint(seq) + g._tag(2, 0) + g.wvarint(4)
        + g._tag(5, 0) + g.wvarint(1)
        + _wrap(7, _wrap(2, b"Kitchen Floor"))
        + _wrap(8, b"\x08\x01")
        + _wrap(9, _make_f9(3, permanent=setpoint_x200))
    )


def _schedule_upload(seq: int = 30) -> bytes:
    return g._tag(1, 0) + g.wvarint(seq) + g._tag(2, 0) + g.wvarint(6)


class FakeWriter:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def get_extra_info(self, _name: str):
        return ("192.168.1.171", 50000)

    def frames(self) -> list[dict]:
        out, i = [], 0
        while i < len(self.data):
            length, j = g.uvarint(self.data, i + 1)
            out.append(g.parse(bytes(self.data[j : j + length])))
            i = j + length
        return out


def _reader(*bodies: bytes, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for body in bodies:
        reader.feed_data(g._frame(body))
    if eof:
        reader.feed_eof()
    return reader


def _gateway(relay: bool = False):
    events: list[tuple[str, dict]] = []
    gw = g.LocalGateway("cert", "key", relay=relay, on_state=lambda s, f: events.append((s, f)))
    return gw, events


async def test_local_session_answers_and_decodes() -> None:
    gw, events = _gateway()
    writer = FakeWriter()
    await gw._handle(
        _reader(
            _strip(LOGIN), _schedule_upload(), _state_upload(), TELEMETRY,
            WALL_STATE, WALL_INFO, DEVICE_ACK, UNKNOWN, NO_SEQ,
        ),
        writer,
    )
    replies = writer.frames()
    assert 12 in replies[0] and replies[0][1] == [29]  # login ack
    assert replies[1] == {1: [30], 3: [1]}  # schedule ack, then customer-service info
    assert 18 in replies[2]
    assert replies[3] == {1: [31], 3: [1]}  # state ack, then config
    assert 10 in replies[4]
    acked = [r[1][0] for r in replies[5:]]
    assert acked == [403, 406, 407, 55]  # telemetry, wall change, info, unknown

    assert events[0] == (SERIAL, {"Online": True})
    assert events[1] == (
        SERIAL,
        {
            "RegulationMode": 3,
            "ComfortTemperature": 1666,
            "ManualTemperature": 2222,
            "SetPointTemp": 2222,
            "Room": "Kitchen Floor",
        },
    )
    assert events[2] == (SERIAL, {"Temperature": 2033, "Heating": False})
    # A change made at the wall (a temporary hold at 71 F) reaches Home Assistant,
    # along with the permanent setpoint it keeps in reserve (58 F).
    assert events[3] == (
        SERIAL,
        {
            "RegulationMode": 2,
            "ComfortTemperature": 2166,
            "ManualTemperature": 1444,
            "SetPointTemp": 2166,
        },
    )
    # Its device-info follow-up confirms the setpoint the thermostat aims for.
    assert events[4] == (SERIAL, {"SetPointTemp": 2166})
    assert events[-1] == (SERIAL, {"Online": False})
    assert len(events) == 6
    assert writer.closed
    assert gw.connected_serials == set()


async def test_set_waits_for_connection_and_template(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gw, _ = _gateway()
    await gw.queue_set(SERIAL, 3, 5000)  # not connected yet: kept
    assert f"Schluter thermostat {SERIAL} isn't connected" in caplog.text
    writer = FakeWriter()
    command_ack = g._tag(1, 0) + g.wvarint(101) + g._tag(3, 0) + g.wvarint(1)
    await gw._handle(_reader(_strip(LOGIN), _state_upload(), command_ack), writer)
    command = writer.frames()[-1]
    assert command[1] == [101] and command[5] == [1] and 8 in command
    f9 = g.parse(command[9][0])
    assert f9[2] == [3] and g.parse(f9[5][0])[1] == [5000]
    assert SERIAL not in gw._desired
    assert f"Schluter thermostat {SERIAL} acknowledged set-command seq 101 (3=1)" in caplog.text
    assert SERIAL not in gw._unacked

    # Once sent it isn't repeated, and a set with no connection does nothing.
    await gw._flush(SERIAL)
    await gw.queue_set("other", 1, None)
    assert "other" in gw._desired


async def test_set_write_failure_keeps_it_pending() -> None:
    gw, _ = _gateway()
    gw._state_raw[SERIAL] = _make_f9(3)
    writer = FakeWriter()
    writer.drain = AsyncMock(side_effect=ConnectionResetError)
    gw._writers[SERIAL] = writer
    await gw.queue_set(SERIAL, 3, 5000)
    assert SERIAL in gw._desired


async def test_reconnect_replaces_old_link() -> None:
    gw, events = _gateway()
    first_reader, first = _reader(_strip(LOGIN), eof=False), FakeWriter()
    first_task = asyncio.create_task(gw._handle(first_reader, first))
    await asyncio.sleep(0)
    second_reader, second = _reader(_strip(LOGIN), eof=False), FakeWriter()
    second_task = asyncio.create_task(gw._handle(second_reader, second))
    await asyncio.sleep(0)
    assert first.closed and gw._writers[SERIAL] is second

    # The stale link ending must not mark the thermostat offline.
    first_reader.feed_eof()
    await first_task
    assert events[-1] == (SERIAL, {"Online": True})
    second_reader.feed_eof()
    await second_task
    assert events[-1] == (SERIAL, {"Online": False})


async def test_quiet_link_is_closed() -> None:
    gw, events = _gateway()
    with patch.object(g, "DEVICE_IDLE_TIMEOUT", 0.01):
        await gw._handle(_reader(_strip(LOGIN), eof=False), FakeWriter())
    assert events[-1] == (SERIAL, {"Online": False})


@pytest.mark.parametrize(
    "raw",
    [
        b"\x07\x00",  # wrong frame start
        b"\x08" + g.wvarint(g.MAX_FRAME + 1),  # absurd length
        b"\x08" + b"\xff" * 6,  # runaway length varint
    ],
)
async def test_malformed_input_is_dropped(raw: bytes) -> None:
    gw, events = _gateway()
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    writer = FakeWriter()
    await gw._handle(reader, writer)
    assert writer.closed and events == []


async def test_unexpected_error_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    def boom(serial, fields):
        if "Online" not in fields:
            raise RuntimeError("bad")

    gw = g.LocalGateway("cert", "key", relay=False, on_state=boom)
    await gw._handle(_reader(_strip(LOGIN), TELEMETRY), FakeWriter())
    assert "Schluter gateway connection error" in caplog.text


CLOUD_LOGIN_ACK = bytes.fromhex("081d18012001620608c5e4f49203")
CLOUD_ROOM = g._tag(1, 0) + g.wvarint(2) + g._tag(5, 0) + g.wvarint(1) + _wrap(7, _wrap(2, b"Bathroom"))


async def _until(predicate) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


async def test_relay_forwards_both_ways(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    gw, events = _gateway(relay=True)
    await gw.queue_set(SERIAL, 3, 5000)
    up_reader, up_writer = _reader(CLOUD_LOGIN_ACK, CLOUD_ROOM, eof=False), FakeWriter()
    device_reader = _reader(_strip(LOGIN), _state_upload(), TELEMETRY, eof=False)
    device = FakeWriter()
    with patch.object(
        g.asyncio, "open_connection", AsyncMock(return_value=(up_reader, up_writer))
    ):
        task = asyncio.create_task(gw._handle(device_reader, device))
        await _until(lambda: len(up_writer.frames()) == 3 and len(events) >= 4)
        assert gw.is_relayed(SERIAL)
        device_reader.feed_eof()
        await task
    assert not gw.is_relayed(SERIAL)

    sent_up = up_writer.frames()
    assert sent_up[0][1] == [29] and 6 in sent_up[0]  # the device's own login
    assert sent_up[2][1] == [403]  # telemetry passed through untouched
    to_device = device.frames()
    assert g.parse(CLOUD_LOGIN_ACK) in to_device  # the cloud answers, not us
    assert sum(12 in f for f in to_device) == 1  # and we didn't answer it too
    assert any(5 in f and 9 in f for f in to_device)  # our queued command
    assert (SERIAL, {"Room": "Bathroom"}) in events  # app change seen from the cloud
    assert (SERIAL, {"Temperature": 2033, "Heating": False}) in events
    assert events[-1] == (SERIAL, {"Online": False})
    assert up_writer.closed
    assert "relay connected to OJ's cloud" in caplog.text
    assert f"{SERIAL} connected to the local gateway (relayed to OJ's cloud)" in caplog.text
    # Debug logs show each frame both ways, but never the login's account details.
    assert (
        f"device->cloud frame ({SERIAL}): seq=31 fields=1,2,5,7,8,9 kind=4 mode=3 "
        "(permanent hold) temporary=16.66C/62.0F permanent=22.22C/72.0F 9.6=0"
    ) in caplog.text
    assert "cloud->device frame (" in caplog.text
    assert "device->cloud frame (before login): login" in caplog.text
    assert f"command frame ({SERIAL}): seq=101 fields=1,5,8,9 mode=3 (permanent hold)" in caplog.text
    assert (
        f"sent set-command seq 101 to {SERIAL} (mode 3, setpoint 25.00C/77.0F)"
    ) in caplog.text
    assert "example.com" not in caplog.text


async def test_relay_falls_back_to_local_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gw, events = _gateway(relay=True)
    with patch.object(g.asyncio, "open_connection", AsyncMock(side_effect=OSError("down"))):
        for _ in range(2):
            writer = FakeWriter()
            reader = _reader(_strip(LOGIN), eof=False)
            task = asyncio.create_task(gw._handle(reader, writer))
            await _until(lambda: SERIAL in gw.connected_serials)
            assert not gw.is_relayed(SERIAL)  # served locally, not relayed
            reader.feed_eof()
            await task
            assert 12 in writer.frames()[0]  # we answered the login ourselves
    assert caplog.text.count("can't reach OJ's cloud gateway") == 1
    assert events[0] == (SERIAL, {"Online": True})


async def test_relay_bad_device_frame_ends_link() -> None:
    gw, events = _gateway(relay=True)
    device_reader = asyncio.StreamReader()
    device_reader.feed_data(g._frame(_strip(LOGIN)) + b"\x07")
    up_reader = _reader(eof=False)
    with patch.object(
        g.asyncio, "open_connection", AsyncMock(return_value=(up_reader, FakeWriter()))
    ):
        await gw._handle(device_reader, FakeWriter())
    assert events[-1] == (SERIAL, {"Online": False})


async def test_start_and_stop(hass, tmp_path) -> None:
    from custom_components.schluter_ditra_heat.cert import _generate

    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    _generate(cert, key)
    server = MagicMock()
    server.wait_closed = AsyncMock(side_effect=OSError)
    gw = g.LocalGateway(str(cert), str(key), relay=True, on_state=lambda *a: None, port=0)
    with patch.object(g.asyncio, "start_server", AsyncMock(return_value=server)) as start:
        await gw.start(hass)
    assert start.call_args.kwargs["ssl"] is gw._server_ctx
    assert gw._client_ctx is not None
    link = FakeWriter()
    gw._conns.add(link)
    gw._writers[SERIAL] = link
    await gw.stop()
    assert link.closed and server.close.called and gw.connected_serials == set()
    await gw.stop()  # stopping twice is harmless


def test_decode_telemetry_without_heating_field() -> None:
    assert g._decode_telemetry(g._tag(1, 0) + g.wvarint(2100)) == {"Temperature": 2100}


def test_login_without_serial() -> None:
    assert g._login_serial({1: [1]}) is None


def test_build_set_permanent_hold_writes_its_own_field() -> None:
    # The bug this pins: from a real temporary hold at 71 F with 58 F stored as
    # the permanent setpoint, a permanent hold at 22.5 C must replace the 58 F,
    # and the temporary hold's setpoint and end time stay as they were.
    gw, _ = _gateway()
    top = g.parse(WALL_STATE)
    gw._capture(SERIAL, top)
    f9 = g.parse(g.parse(gw._build_set(SERIAL, 3, 4500))[9][0])
    original = g.parse(top[9][0])
    assert f9[2] == [3]
    assert g.parse(f9[5][0]) == {1: [4500]}
    assert (f9[3], f9[4], f9[6]) == (original[3], original[4], original[6])


def test_build_set_temporary_hold_keeps_end_time() -> None:
    gw, _ = _gateway()
    top = g.parse(WALL_STATE)
    gw._capture(SERIAL, top)
    f9 = g.parse(g.parse(gw._build_set(SERIAL, 2, 4000))[9][0])
    original = g.parse(top[9][0])
    assert f9[2] == [2]
    f4 = g.parse(f9[4][0])
    assert f4[1] == [4000] and f4[2] == g.parse(original[4][0])[2]
    assert (f9[3], f9[5], f9[6]) == (original[3], original[5], original[6])


def test_build_set_schedule_changes_only_the_mode() -> None:
    gw, _ = _gateway()
    gw._state_raw[SERIAL] = PERMANENT_F9
    f9 = g.parse(g.parse(gw._build_set(SERIAL, 1, None))[9][0])
    original = g.parse(PERMANENT_F9)
    assert f9[2] == [1]
    assert {k: v for k, v in f9.items() if k not in (1, 2)} == {
        k: v for k, v in original.items() if k not in (1, 2)
    }


def test_build_set_copies_the_state_byte_for_byte() -> None:
    # Re-encoding must give back the thermostat's own bytes: same mode, same
    # setpoint and same timestamp produce the identical state.
    gw, _ = _gateway()
    gw._state_raw[SERIAL] = PERMANENT_F9
    original = g.parse(PERMANENT_F9)
    with patch.object(g, "_server_time", return_value=original[1][0]):
        body = gw._build_set(SERIAL, 3, 2888)
    assert g.parse(body)[9][0] == PERMANENT_F9


def test_build_set_adds_a_missing_hold() -> None:
    gw, _ = _gateway()
    gw._state_raw[SERIAL] = g._tag(1, 0) + g.wvarint(1000) + g._tag(2, 0) + g.wvarint(1)
    f9 = g.parse(g.parse(gw._build_set(SERIAL, 3, 4500))[9][0])
    assert f9[2] == [3] and g.parse(f9[5][0]) == {1: [4500]}


def test_describe_shows_holds_and_unknown_fields() -> None:
    assert g._describe(g.parse(WALL_STATE)) == (
        "seq=406 fields=1,5,9 mode=2 (temporary hold) temporary=21.66C/71.0F "
        "until=2026-10-11 06:00:00Z permanent=14.44C/58.0F 9.3={1: 0} 9.6=0"
    )
    assert g._describe(g.parse(WALL_INFO)) == "seq=407 fields=1,5,7 target=21.66C/71.0F"
    assert g._describe(g.parse(TELEMETRY)) == (
        "seq=403 fields=1,5,15 floor=20.33C/68.6F heating=0"
    )
    odd = g._tag(2, 0) + g.wvarint(9) + _wrap(3, g._tag(1, 0) + g.wvarint(0) + _wrap(2, b"\x01\x02"))
    assert g._describe({9: [odd]}) == "seq=None fields=9 mode=9 (unknown) 9.3={1: 0, 2: 0102}"


async def test_set_without_template_waits_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gw, _ = _gateway()
    gw._writers[SERIAL] = FakeWriter()
    await gw.queue_set(SERIAL, 1, None)
    assert f"Schluter change queued for {SERIAL}: mode 1 (schedule), setpoint unchanged" in caplog.text
    assert f"Schluter has no state from {SERIAL}" in caplog.text
    assert SERIAL in gw._desired


def test_parse_fixed_width_and_unknown_wire_types() -> None:
    body = g._tag(1, 5) + b"\x01\x02\x03\x04" + g._tag(2, 1) + b"\x00" * 8 + g._tag(3, 3)
    assert g.parse(body) == {1: [b"\x01\x02\x03\x04"], 2: [b"\x00" * 8]}


async def test_frames_before_login_are_ignored() -> None:
    gw, events = _gateway()
    writer = FakeWriter()
    await gw._handle(_reader(TELEMETRY, g._tag(1, 0) + g.wvarint(9) + _wrap(6, b"")), writer)
    assert events == []  # no serial yet, and a login without one isn't trusted
    assert writer.frames()[0] == {1: [403], 3: [1]}
