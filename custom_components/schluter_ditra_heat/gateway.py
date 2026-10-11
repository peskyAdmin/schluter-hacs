"""Local device gateway for Schluter/OJ thermostats (port 9533).

With a LAN DNS override pointing ``na-thermos.ojelectronics.com`` at this host,
the thermostat opens its gateway connection here instead of OJ's cloud. This
module speaks enough of the protocol (see docs/device-protocol.md) to:

- accept the TLS connection (the device does not validate the certificate),
- answer login, schedule and state uploads so the device stays connected,
- decode each state upload into the same field names the cloud API uses,
- send the device a set-temperature command built from its own last upload.

Two shapes:

- **local**: we answer the device ourselves; fully offline.
- **relay**: we also open an upstream connection to OJ's real gateway (by IP so
  the DNS override does not loop) and forward every frame both ways, so the
  Schluter app keeps working. If the upstream is unreachable when the thermostat
  connects, we answer it ourselves for that connection, so Home Assistant keeps
  working in an outage; the next time the thermostat reconnects we try the cloud
  again.

Both shapes send Home Assistant's changes straight to the thermostat.

Nothing here is written to the device firmware, and no account or device data is
baked into this module; the schedule and state templates used to build a command
are captured live from the device at runtime.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
import ssl
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .const import (
    DEVICE_TEMP_SCALE,
    EPOCH_2000,
    GATEWAY_HOST,
    GATEWAY_PORT,
    MODE_COMFORT,
    MODE_MANUAL,
    MODE_SCHEDULE,
    MODE_VACATION,
    UPSTREAM_GATEWAY_IP,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# A decoded protobuf message: field number -> values, each an int (varint) or
# bytes (length-delimited, or a raw fixed-width field).
type Fields = dict[int, list[Any]]

# The thermostat sends a telemetry heartbeat every minute. If nothing arrives for
# three of them the link is dead (power cut, Wi-Fi drop), so close it and mark
# the thermostat offline instead of trusting a half-open TCP connection.
DEVICE_IDLE_TIMEOUT = 180
# Real frames are under 1 KB; refuse anything absurd from a stray LAN client.
MAX_FRAME = 64 * 1024

# Generic frames the real cloud sends, captured from the protocol. They carry no
# account or device identifiers: a customer-service info string, and Schluter's
# default limit/preset set. Each already includes its 0x08<len> frame prefix.
CUSTOMER_SVC = bytes.fromhex(
    "08540800280192014d437573746f6d657220536572766963650a5553413a20312d"
    "3830302d3437322d343538380a43616e6164613a20312d3830302d3636372d3837"
    "34360a696e666f407363686c757465722e636f6d"
)
CONFIG = bytes.fromhex(
    "08360801280152300a0a0802108c1f18e820200a0a0a080310bc1418cc3a20010a"
    "0a0804108014188c47200a0a0a080110941918a43f200a"
)

# The state (field 9) keeps each hold's setpoint (Celsius x200, in field 1) in a
# sub-message of its own: 9.4 for a temporary hold, with the hold's end time in
# 9.4.2, and 9.5 for a permanent hold. A command for a hold has to write that
# hold's sub-message; the thermostat ignores the other one.
_HOLD_FIELD = {MODE_COMFORT: 4, MODE_MANUAL: 5}
# The cloud API field that carries the same setpoint.
_HOLD_CLOUD_FIELD = {4: "ComfortTemperature", 5: "ManualTemperature"}
_MODE_NAMES = {
    MODE_SCHEDULE: "schedule",
    MODE_COMFORT: "temporary hold",
    MODE_MANUAL: "permanent hold",
    MODE_VACATION: "vacation",
}


# --- protobuf helpers -------------------------------------------------------
def uvarint(b: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        x = b[i]
        i += 1
        result |= (x & 0x7F) << shift
        shift += 7
        if not x & 0x80:
            return result, i


def wvarint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def parse(b: bytes) -> Fields:
    """field number -> list of values (int for varint, bytes for length-delimited)."""
    i = 0
    out: Fields = {}
    while i < len(b):
        tag_val, i = uvarint(b, i)
        field = tag_val >> 3
        wire = tag_val & 7
        value: int | bytes
        if wire == 0:
            value, i = uvarint(b, i)
        elif wire == 2:
            ln, i = uvarint(b, i)
            value = b[i : i + ln]
            i += ln
        elif wire == 5:
            value = b[i : i + 4]
            i += 4
        elif wire == 1:
            value = b[i : i + 8]
            i += 8
        else:
            break
        out.setdefault(field, []).append(value)
    return out


def _frame(body: bytes) -> bytes:
    """Wrap a protobuf body in its 0x08<len> frame prefix."""
    return b"\x08" + wvarint(len(body)) + body


def _tag(field: int, wire: int) -> bytes:
    return wvarint(field << 3 | wire)


def _encode(field: int, value: int | bytes) -> bytes:
    """Encode one field as a varint or a length-delimited value.

    State messages only use those two wire types, so re-encoding a parsed one
    gives back the same bytes.
    """
    if isinstance(value, int):
        return _tag(field, 0) + wvarint(value)
    return _tag(field, 2) + wvarint(len(value)) + value


def _with_varint(msg: bytes, field: int, value: int) -> bytes:
    """Return msg with varint field set to value and every other field kept."""
    out = b""
    replaced = False
    for number, values in parse(msg).items():
        for old in values:
            if number == field and not replaced:
                old, replaced = value, True
            out += _encode(number, old)
    if not replaced:
        out += _encode(field, value)
    return out


def _server_time() -> int:
    return int(time.time()) - EPOCH_2000


def _login_ack(seq: int) -> bytes:
    inner = _tag(1, 0) + wvarint(_server_time())
    body = (
        _tag(1, 0) + wvarint(seq) + _tag(3, 0) + wvarint(1)
        + _tag(4, 0) + wvarint(1) + _tag(12, 2) + wvarint(len(inner)) + inner
    )
    return _frame(body)


def _ack(seq: int) -> bytes:
    return _frame(_tag(1, 0) + wvarint(seq) + _tag(3, 0) + wvarint(1))


def _login_serial(top: Fields) -> str | None:
    if 6 not in top:
        return None
    f6 = parse(top[6][0])
    value = f6.get(2, [b""])[0]
    return value.decode(errors="replace") if isinstance(value, bytes) else str(value)


def _x100(x200: int) -> int:
    """Device setpoint (Celsius x200) to the cloud's Celsius x100."""
    return round(x200 / DEVICE_TEMP_SCALE * 100)


def _hold_setpoints(st: Fields) -> dict[int, int]:
    """Each hold's stored setpoint (Celsius x200), keyed by its sub-message."""
    out: dict[int, int] = {}
    for sub in (4, 5):
        raw = st.get(sub, [None])[0]
        if isinstance(raw, bytes):
            value = parse(raw).get(1, [None])[0]
            if isinstance(value, int):
                out[sub] = value
    return out


def _decode_state(body: bytes) -> dict[str, Any]:
    """Pull mode/setpoints/room out of a state frame into cloud field names."""
    top = parse(body)
    fields: dict[str, Any] = {}
    if 9 in top:
        st = parse(top[9][0])
        mode = st.get(2, [None])[0]
        if mode is not None:
            fields["RegulationMode"] = mode
        holds = _hold_setpoints(st)
        for sub, x200 in holds.items():
            fields[_HOLD_CLOUD_FIELD[sub]] = _x100(x200)
        hold = _HOLD_FIELD.get(mode) if isinstance(mode, int) else None
        if hold in holds:
            fields["SetPointTemp"] = _x100(holds[hold])
    if 7 in top:
        di = parse(top[7][0])
        if 2 in di and isinstance(di[2][0], bytes):
            fields["Room"] = di[2][0].decode(errors="replace")
        # 7.9 is the setpoint the thermostat is aiming for right now, in any mode.
        if 9 in di and isinstance(di[9][0], int):
            fields["SetPointTemp"] = _x100(di[9][0])
    return fields


def _decode_telemetry(f15: bytes) -> dict[str, Any]:
    """Pull current temperature and the live heating flag from a telemetry frame.

    Field 1 is the current floor temperature (Celsius x100, same scale the cloud
    uses). Field 2 is 1 while the floor is drawing power and 0 while idle; it
    tracks the relay exactly, confirmed against a heat/stop capture.
    """
    tele = parse(f15)
    fields: dict[str, Any] = {}
    if 1 in tele:
        fields["Temperature"] = tele[1][0]
    if 2 in tele:
        fields["Heating"] = bool(tele[2][0])
    return fields


def _temp(x200: int) -> str:
    """Celsius x200 as readable Celsius and Fahrenheit, for debug logs."""
    celsius = x200 / DEVICE_TEMP_SCALE
    return f"{celsius:.2f}C/{celsius * 9 / 5 + 32:.1f}F"


def _when(seconds: int) -> str:
    """A device timestamp (seconds since 2000) as UTC, for debug logs."""
    return datetime.fromtimestamp(seconds + EPOCH_2000, UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _brief(value: int | bytes) -> str:
    """A field we don't interpret, shown generically for debug logs."""
    if isinstance(value, int):
        return str(value)
    return "{" + ", ".join(
        f"{number}: {v if isinstance(v, int) else v.hex()}"
        for number, values in parse(value).items()
        for v in values
    ) + "}"


def _describe(top: Fields) -> str:
    """One-line summary of a frame for debug logs; never includes the login."""
    parts = [f"seq={top.get(1, [None])[0]}", "fields=" + ",".join(map(str, sorted(top)))]
    if 2 in top:
        parts.append(f"kind={top[2][0]}")
    if 9 in top:
        st = parse(top[9][0])
        mode = st.get(2, [None])[0]
        parts.append(f"mode={mode} ({_MODE_NAMES.get(mode, 'unknown')})")
        holds = _hold_setpoints(st)
        if 4 in holds:
            parts.append(f"temporary={_temp(holds[4])}")
            until = parse(st[4][0]).get(2, [None])[0]
            if isinstance(until, int):
                parts.append(f"until={_when(until)}")
        if 5 in holds:
            parts.append(f"permanent={_temp(holds[5])}")
        parts.extend(
            f"9.{number}={_brief(st[number][0])}"
            for number in sorted(st)
            if number not in (1, 2, 4, 5)
        )
    if 7 in top:
        target = parse(top[7][0]).get(9, [None])[0]
        if isinstance(target, int):
            parts.append(f"target={_temp(target)}")
    if 15 in top:
        tele = parse(top[15][0])
        if isinstance(floor := tele.get(1, [None])[0], int):
            parts.append(f"floor={_temp(floor * 2)}")
        if 2 in tele:
            parts.append(f"heating={tele[2][0]}")
    return " ".join(parts)


class LocalGateway:
    """Runs the TLS gateway the thermostats connect to.

    Each thermostat holds its own connection, so every per-device fact (its
    serial, its writer, its last schedule and state frames) is keyed by serial
    and several thermostats can share one gateway.
    """

    def __init__(
        self,
        certfile: str,
        keyfile: str,
        *,
        relay: bool,
        on_state: Callable[[str, dict[str, Any]], None],
        port: int = GATEWAY_PORT,
    ) -> None:
        self._certfile = certfile
        self._keyfile = keyfile
        self._relay = relay
        self._port = port
        self.on_state = on_state
        self._server: asyncio.Server | None = None
        self._server_ctx: ssl.SSLContext | None = None
        self._client_ctx: ssl.SSLContext | None = None
        self._conns: set[asyncio.StreamWriter] = set()  # every open device link
        self._writers: dict[str, asyncio.StreamWriter] = {}  # active link per serial
        self._relayed: set[str] = set()  # serials whose active link reaches OJ's cloud
        self._sched: dict[str, bytes] = {}  # last schedule (f8) per serial
        self._state_raw: dict[str, bytes] = {}  # last state (f9) per serial
        self._desired: dict[str, tuple[int, int | None]] = {}  # pending set
        self._unacked: dict[str, int] = {}  # seq of the last command sent per serial
        self._upstream_ok: bool | None = None  # relay only; None until first try
        self._seq = 100

    @property
    def connected_serials(self) -> set[str]:
        """Serials of thermostats connected right now."""
        return set(self._writers)

    def is_relayed(self, serial: str) -> bool:
        """Whether the thermostat's current link is being relayed to OJ's cloud."""
        return serial in self._relayed

    # --- lifecycle ----------------------------------------------------------
    def _build_contexts(self) -> None:
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            server_ctx.set_ciphers(
                "AES256-SHA256:AES128-SHA256:AES256-SHA:AES128-SHA:DEFAULT"
            )
        except ssl.SSLError:  # pragma: no cover - depends on the OpenSSL build
            server_ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        server_ctx.load_cert_chain(self._certfile, self._keyfile)
        self._server_ctx = server_ctx
        if self._relay:
            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.check_hostname = False
            client_ctx.verify_mode = ssl.CERT_NONE
            client_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            try:
                client_ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
            except ssl.SSLError:  # pragma: no cover - depends on the OpenSSL build
                pass
            self._client_ctx = client_ctx

    async def start(self, hass: HomeAssistant) -> None:
        await hass.async_add_executor_job(self._build_contexts)
        self._server = await asyncio.start_server(
            self._handle, "0.0.0.0", self._port, ssl=self._server_ctx
        )
        _LOGGER.info(
            "Schluter local gateway listening on port %s (%s mode); waiting for "
            "the thermostat to connect",
            self._port,
            "relay" if self._relay else "local-only",
        )

    async def stop(self) -> None:
        for writer in list(self._conns):
            writer.close()
        self._conns.clear()
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            try:
                # Connections were closed above; don't let a stuck one hold up unload.
                await asyncio.wait_for(self._server.wait_closed(), 5)
            except (TimeoutError, OSError):
                pass
            self._server = None

    # --- framing ------------------------------------------------------------
    @staticmethod
    async def _read_frame(
        reader: asyncio.StreamReader, idle_timeout: float | None = None
    ) -> bytes:
        """Read one 0x08<len><body> frame; TimeoutError if the link goes quiet."""
        async with asyncio.timeout(idle_timeout):
            hdr = await reader.readexactly(1)
            if hdr[0] != 0x08:
                raise ValueError(f"unexpected frame start {hdr[0]:#x}")
            length = shift = 0
            while True:
                b = (await reader.readexactly(1))[0]
                length |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
                if shift > 28:
                    raise ValueError("frame length varint too long")
            if length > MAX_FRAME:
                raise ValueError(f"frame of {length} bytes exceeds {MAX_FRAME}")
            return await reader.readexactly(length)

    # --- commands -----------------------------------------------------------
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _build_set(self, serial: str, mode: int, setpoint_x200: int | None) -> bytes | None:
        """Build a set-command body from the thermostat's own last state.

        The command's state is a copy of that state with a fresh timestamp and
        the new mode. A hold's setpoint goes into that hold's own sub-message
        (9.4 temporary, 9.5 permanent); everything else, including the other
        hold's setpoint and the temporary hold's end time, goes back exactly as
        the thermostat sent it. Without a setpoint (back to the schedule) only
        the mode changes.
        """
        f9raw = self._state_raw.get(serial)
        if f9raw is None:
            return None  # no template captured yet; flushed after next upload
        st = parse(f9raw)
        st[1] = [_server_time()]
        st[2] = [mode]
        hold = _HOLD_FIELD.get(mode)
        if hold is not None and setpoint_x200 is not None:
            current = st.get(hold, [b""])[0]
            sub = current if isinstance(current, bytes) else b""
            st[hold] = [_with_varint(sub, 1, setpoint_x200)]
        body9 = b"".join(
            _encode(number, value) for number in sorted(st) for value in st[number]
        )
        body = _tag(1, 0) + wvarint(self._next_seq()) + _tag(5, 0) + wvarint(1)
        if f8 := self._sched.get(serial):
            body += _encode(8, f8)
        return body + _encode(9, body9)

    async def queue_set(self, serial: str, mode: int, setpoint_x200: int | None) -> None:
        """Ask the device to hold setpoint_x200 (Celsius x200) in mode.

        If the thermostat is not connected yet, or has not uploaded a state frame
        to use as a template, the change is kept and sent as soon as it can be.
        """
        self._desired[serial] = (mode, setpoint_x200)
        _LOGGER.debug(
            "Schluter change queued for %s: mode %s (%s), setpoint %s",
            serial,
            mode,
            _MODE_NAMES.get(mode, "unknown"),
            "unchanged" if setpoint_x200 is None else _temp(setpoint_x200),
        )
        await self._flush(serial)

    async def _flush(self, serial: str) -> None:
        if serial not in self._desired:
            return
        writer = self._writers.get(serial)
        if writer is None:
            _LOGGER.debug(
                "Schluter thermostat %s isn't connected; the change will be sent "
                "when it connects",
                serial,
            )
            return
        mode, setpoint = self._desired[serial]
        body = self._build_set(serial, mode, setpoint)
        if body is None:
            _LOGGER.debug(
                "Schluter has no state from %s to build the command from yet; the "
                "change will be sent after its next state upload",
                serial,
            )
            return
        top = parse(body)
        self._log_frame("command", serial, top)
        try:
            writer.write(_frame(body))
            await writer.drain()
        except OSError as err:
            _LOGGER.debug("Schluter set-command to %s failed: %s", serial, err)
            return
        self._desired.pop(serial, None)
        self._unacked[serial] = top[1][0]
        _LOGGER.debug(
            "Schluter sent set-command seq %s to %s (mode %s, setpoint %s)",
            top[1][0],
            serial,
            mode,
            "unchanged" if setpoint is None else _temp(setpoint),
        )

    def _note_ack(self, serial: str | None, top: Fields) -> None:
        """Log when the thermostat acknowledges the command we last sent it."""
        if (
            serial
            and 3 in top
            and 2 not in top
            and top.get(1, [None])[0] == self._unacked.get(serial)
        ):
            del self._unacked[serial]
            _LOGGER.debug(
                "Schluter thermostat %s acknowledged set-command seq %s (3=%s)",
                serial,
                top[1][0],
                top[3][0],
            )

    def _capture(self, serial: str, top: Fields) -> None:
        if 8 in top:
            self._sched[serial] = top[8][0]
        if 9 in top:
            self._state_raw[serial] = top[9][0]

    # --- connection handling ------------------------------------------------
    def _logged_in(
        self, serial: str | None, writer: asyncio.StreamWriter, *, relayed: bool
    ) -> None:
        if not serial:
            return
        old = self._writers.get(serial)
        self._writers[serial] = writer
        if relayed:
            self._relayed.add(serial)
        else:
            self._relayed.discard(serial)
        if old is not None and old is not writer:
            old.close()  # the device reconnected; drop the stale link
        _LOGGER.info(
            "Schluter thermostat %s connected to the local gateway (%s)",
            serial,
            "relayed to OJ's cloud" if relayed else "served locally",
        )
        self.on_state(serial, {"Online": True})

    def _dispatch(self, serial: str | None, body: bytes, top: Fields) -> None:
        """Feed a frame that describes the thermostat to Home Assistant.

        State arrives three ways, all carrying field 9 (and often 7/8): the full
        upload after login (field 2 = 4), the short frame the thermostat sends
        when someone changes it at the wall, and, in relay mode, the cloud's
        command when someone changes it in the Schluter app.
        """
        if not serial:
            return
        if 8 in top or 9 in top:
            self._capture(serial, top)
        if 7 in top or 9 in top:
            if fields := _decode_state(body):
                self.on_state(serial, fields)
        elif 15 in top:
            self.on_state(serial, _decode_telemetry(top[15][0]))

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        _LOGGER.debug("Schluter device connection from %s", peer)
        self._conns.add(writer)
        conn: dict[str, str | None] = {"serial": None}
        try:
            if self._relay and await self._try_relay(reader, writer, conn):
                return
            await self._serve_local(reader, writer, conn)
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        except TimeoutError:
            _LOGGER.debug("Schluter device %s went quiet; closing", conn["serial"] or peer)
        except ValueError as err:
            _LOGGER.debug("Schluter dropped a malformed connection from %s: %s", peer, err)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Schluter gateway connection error")
        finally:
            self._conns.discard(writer)
            serial = conn["serial"]
            # Only report offline if a newer connection hasn't replaced this one.
            if serial and self._writers.get(serial) is writer:
                del self._writers[serial]
                self._relayed.discard(serial)
                _LOGGER.info(
                    "Schluter thermostat %s disconnected from the local gateway", serial
                )
                self.on_state(serial, {"Online": False})
            try:
                writer.close()
            except OSError:  # pragma: no cover - already closed by the peer
                pass

    async def _serve_local(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        conn: dict[str, str | None],
    ) -> None:
        """Answer the device ourselves (local mode, or relay fallback)."""
        while True:
            body = await self._read_frame(reader, DEVICE_IDLE_TIMEOUT)
            top = parse(body)
            self._log_frame("device", conn["serial"], top)
            seq = top.get(1, [0])[0]
            kind = top.get(2, [None])[0]
            if 6 in top:  # login
                conn["serial"] = _login_serial(top)
                writer.write(_login_ack(seq))
                await writer.drain()
                self._logged_in(conn["serial"], writer, relayed=False)
                continue
            self._dispatch(conn["serial"], body, top)
            if kind == 6:  # schedule upload
                writer.write(_ack(seq))
                writer.write(CUSTOMER_SVC)
            elif kind == 4:  # state upload
                writer.write(_ack(seq))
                writer.write(CONFIG)
            elif 3 in top and 2 not in top:  # the device acking us
                self._note_ack(conn["serial"], top)
                continue
            elif seq:  # telemetry and anything else that wants an ack
                writer.write(_ack(seq))
            else:
                continue
            await writer.drain()
            if 9 in top and conn["serial"]:
                await self._flush(conn["serial"])

    @staticmethod
    def _log_frame(source: str, serial: str | None, top: Fields) -> None:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Schluter %s frame (%s): %s",
                source,
                serial or "before login",
                "login" if 6 in top else _describe(top),
            )

    def _note_upstream(self, ok: bool, err: Exception | None = None) -> None:
        """Log the relay's cloud link once per change, not once per attempt."""
        if ok == self._upstream_ok:
            return
        self._upstream_ok = ok
        if ok:
            _LOGGER.info("Schluter relay connected to OJ's cloud gateway")
        else:
            _LOGGER.warning(
                "Schluter relay can't reach OJ's cloud gateway (%s); serving the "
                "thermostat locally so Home Assistant keeps working",
                err,
            )

    async def _try_relay(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        conn: dict[str, str | None],
    ) -> bool:
        """Proxy device<->cloud, parsing in passing. False if upstream is down."""
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    UPSTREAM_GATEWAY_IP,
                    GATEWAY_PORT,
                    ssl=self._client_ctx,
                    server_hostname=GATEWAY_HOST,
                ),
                timeout=10,
            )
        except (OSError, TimeoutError) as err:
            self._note_upstream(False, err)
            return False
        self._note_upstream(True)

        async def device_to_cloud() -> None:
            while True:
                body = await self._read_frame(reader, DEVICE_IDLE_TIMEOUT)
                top = parse(body)
                self._log_frame("device->cloud", conn["serial"], top)
                up_writer.write(_frame(body))
                await up_writer.drain()
                if 6 in top:
                    conn["serial"] = _login_serial(top)
                    self._logged_in(conn["serial"], writer, relayed=True)
                else:
                    self._note_ack(conn["serial"], top)
                    self._dispatch(conn["serial"], body, top)
                    if 9 in top and conn["serial"]:
                        await self._flush(conn["serial"])

        async def cloud_to_device() -> None:
            while True:
                body = await self._read_frame(up_reader)
                writer.write(_frame(body))
                await writer.drain()
                top = parse(body)
                self._log_frame("cloud->device", conn["serial"], top)
                # App changes reach the thermostat as cloud commands; show them too.
                self._dispatch(conn["serial"], body, top)

        tasks = [
            asyncio.create_task(device_to_cloud()),
            asyncio.create_task(cloud_to_device()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            up_writer.close()
        for task in done:
            if (exc := task.exception()) is not None and not isinstance(
                exc, (asyncio.IncompleteReadError, ConnectionError)
            ):
                raise exc
        return True
