"""Protocol unit tests for the local device gateway.

These exercise the wire-format helpers directly and need no Home Assistant
fixtures. The setpoint field on the device is Celsius x200, while the
coordinator uses Celsius x100; getting that scale wrong writes half the
intended temperature, so it is pinned here.
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


def _make_f9(mode: int, setpoint_x200: int) -> bytes:
    f4 = g._tag(1, 0) + g.wvarint(setpoint_x200)
    return (
        g._tag(1, 0) + g.wvarint(1000)
        + g._tag(2, 0) + g.wvarint(mode)
        + _wrap(4, f4)
        + g._tag(6, 0) + g.wvarint(0)
    )


def test_varint_roundtrip() -> None:
    for n in (0, 1, 127, 128, 2666, 4444, 946684800):
        encoded = g.wvarint(n)
        assert g.uvarint(encoded, 0) == (n, len(encoded))


def test_login_serial() -> None:
    assert g._login_serial(g.parse(_strip(LOGIN))) == "1254414"


def test_decode_state_uses_x200_scale() -> None:
    # 22.22 C held manually -> device field 4444 (x200) -> 2222 (x100).
    body = _wrap(7, _wrap(2, b"Kitchen Floor")) + _wrap(9, _make_f9(3, 4444))
    fields = g._decode_state(body)
    assert fields["RegulationMode"] == 3
    assert fields["SetPointTemp"] == 2222
    assert fields["ManualTemperature"] == 2222
    assert fields["Room"] == "Kitchen Floor"


def test_build_set_roundtrip() -> None:
    gw = g.LocalGateway("cert", "key", relay=False, on_state=lambda *a: None)
    gw._state_raw["1254414"] = _make_f9(3, 2666)
    frame = gw._build_set("1254414", 3, 5000)
    top = g.parse(_strip(frame))
    assert top[5][0] == 1
    f9 = g.parse(top[9][0])
    assert f9[2][0] == 3  # mode preserved
    assert g.parse(f9[4][0])[1][0] == 5000  # new setpoint, x200


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
        + _wrap(9, _make_f9(3, setpoint_x200))
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
        {"RegulationMode": 3, "SetPointTemp": 2222, "ManualTemperature": 2222, "Room": "Kitchen Floor"},
    )
    assert events[2] == (SERIAL, {"Temperature": 2033, "Heating": False})
    # A change made at the wall reaches Home Assistant.
    assert events[3] == (
        SERIAL, {"RegulationMode": 2, "SetPointTemp": 2166, "ComfortTemperature": 2166}
    )
    assert events[-1] == (SERIAL, {"Online": False})
    assert len(events) == 5  # the info-only frame carries nothing new
    assert writer.closed
    assert gw.connected_serials == set()


async def test_set_waits_for_connection_and_template() -> None:
    gw, _ = _gateway()
    await gw.queue_set(SERIAL, 3, 5000)  # not connected yet: kept
    writer = FakeWriter()
    await gw._handle(_reader(_strip(LOGIN), _state_upload()), writer)
    command = writer.frames()[-1]
    assert command[5] == [1] and 8 in command
    f9 = g.parse(command[9][0])
    assert f9[2] == [3] and g.parse(f9[4][0])[1] == [5000]
    assert SERIAL not in gw._desired

    # Once sent it isn't repeated, and a set with no connection does nothing.
    await gw._flush(SERIAL)
    await gw.queue_set("other", 1, None)
    assert "other" in gw._desired


async def test_set_write_failure_keeps_it_pending() -> None:
    gw, _ = _gateway()
    gw._state_raw[SERIAL] = _make_f9(3, 4444)
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
    caplog.set_level(logging.INFO)
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
        device_reader.feed_eof()
        await task

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


async def test_relay_falls_back_to_local_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gw, events = _gateway(relay=True)
    with patch.object(g.asyncio, "open_connection", AsyncMock(side_effect=OSError("down"))):
        for _ in range(2):
            writer = FakeWriter()
            await gw._handle(_reader(_strip(LOGIN)), writer)
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


def test_build_set_keeps_real_template_fields() -> None:
    # Built from a real state frame: the hold's end time and fields 3/5/6 that we
    # don't interpret must go back to the device unchanged.
    gw, _ = _gateway()
    top = g.parse(WALL_STATE)
    gw._capture(SERIAL, top)
    f9 = g.parse(g.parse(_strip(gw._build_set(SERIAL, 3, 4500)))[9][0])
    original = g.parse(top[9][0])
    assert f9[2] == [3]
    assert (f9[3], f9[5], f9[6]) == (original[3], original[5], original[6])
    f4 = g.parse(f9[4][0])
    assert f4[1] == [4500] and f4[2] == g.parse(original[4][0])[2]


def test_parse_fixed_width_and_unknown_wire_types() -> None:
    body = g._tag(1, 5) + b"\x01\x02\x03\x04" + g._tag(2, 1) + b"\x00" * 8 + g._tag(3, 3)
    assert g.parse(body) == {1: [b"\x01\x02\x03\x04"], 2: [b"\x00" * 8]}


async def test_frames_before_login_are_ignored() -> None:
    gw, events = _gateway()
    writer = FakeWriter()
    await gw._handle(_reader(TELEMETRY, g._tag(1, 0) + g.wvarint(9) + _wrap(6, b"")), writer)
    assert events == []  # no serial yet, and a login without one isn't trusted
    assert writer.frames()[0] == {1: [403], 3: [1]}
