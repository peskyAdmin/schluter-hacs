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

Nothing here is written to the device firmware, and no account or device data is
baked into this module; the schedule and state templates used to build a command
are captured live from the device at runtime.
"""

from __future__ import annotations

import asyncio
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

# Map the device state mode to the cloud field that carries its target, so the
# coordinator's requested_temperature() picks it up unchanged.
_MODE_TEMP_FIELD = {
    MODE_SCHEDULE: "SetPointTemp",
    MODE_COMFORT: "ComfortTemperature",
    MODE_MANUAL: "ManualTemperature",
    MODE_VACATION: "VacationTemperature",
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


def _decode_state(body: bytes) -> dict[str, Any]:
    """Pull mode/setpoint/room out of a state upload into cloud field names."""
    top = parse(body)
    fields: dict[str, Any] = {}
    if 9 in top:
        st = parse(top[9][0])
        if 2 in st:
            fields["RegulationMode"] = st[2][0]
        if 4 in st:
            s4 = parse(st[4][0])
            if 1 in s4:
                x100 = round(s4[1][0] / DEVICE_TEMP_SCALE * 100)
                fields["SetPointTemp"] = x100
                mode = fields.get("RegulationMode")
                key = _MODE_TEMP_FIELD.get(mode) if mode is not None else None
                if key:
                    fields[key] = x100
    if 7 in top:
        di = parse(top[7][0])
        if 2 in di and isinstance(di[2][0], bytes):
            fields["Room"] = di[2][0].decode(errors="replace")
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
        self._sched: dict[str, bytes] = {}  # last schedule (f8) per serial
        self._state_raw: dict[str, bytes] = {}  # last state (f9) per serial
        self._desired: dict[str, tuple[int, int | None]] = {}  # pending set
        self._upstream_ok: bool | None = None  # relay only; None until first try
        self._seq = 100

    @property
    def connected_serials(self) -> set[str]:
        """Serials of thermostats connected right now."""
        return set(self._writers)

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
        """Build a set-temperature frame from the device's own last upload."""
        f9raw = self._state_raw.get(serial)
        if f9raw is None:
            return None  # no template captured yet; flushed after next upload
        st = parse(f9raw)
        f3 = st[3][0] if 3 in st else b""
        f5 = st[5][0] if 5 in st else b""
        f6 = st.get(6, [0])[0]
        f4 = parse(st[4][0]) if 4 in st else {}
        until = f4.get(2, [0])[0]
        setpoint = setpoint_x200 if setpoint_x200 is not None else f4.get(1, [0])[0]
        f4b = _tag(1, 0) + wvarint(setpoint)
        if until:
            f4b += _tag(2, 0) + wvarint(until)
        body9 = _tag(1, 0) + wvarint(_server_time()) + _tag(2, 0) + wvarint(mode)
        if f3:
            body9 += _tag(3, 2) + wvarint(len(f3)) + f3
        body9 += _tag(4, 2) + wvarint(len(f4b)) + f4b
        if f5:
            body9 += _tag(5, 2) + wvarint(len(f5)) + f5
        body9 += _tag(6, 0) + wvarint(f6)
        f8 = self._sched.get(serial)
        body = _tag(1, 0) + wvarint(self._next_seq()) + _tag(5, 0) + wvarint(1)
        if f8:
            body += _tag(8, 2) + wvarint(len(f8)) + f8
        body += _tag(9, 2) + wvarint(len(body9)) + body9
        return _frame(body)

    async def queue_set(self, serial: str, mode: int, setpoint_x200: int | None) -> None:
        """Ask the device to hold setpoint_x200 (Celsius x200) in mode.

        If the thermostat is not connected yet, or has not uploaded a state frame
        to use as a template, the change is kept and sent as soon as it can be.
        """
        self._desired[serial] = (mode, setpoint_x200)
        await self._flush(serial)

    async def _flush(self, serial: str) -> None:
        writer = self._writers.get(serial)
        if serial not in self._desired or writer is None:
            return
        mode, setpoint = self._desired[serial]
        frame = self._build_set(serial, mode, setpoint)
        if frame is None:
            return
        try:
            writer.write(frame)
            await writer.drain()
        except OSError as err:
            _LOGGER.debug("Schluter set-command to %s failed: %s", serial, err)
            return
        self._desired.pop(serial, None)
        _LOGGER.debug("Schluter sent set-command to %s (mode %s)", serial, mode)

    def _capture(self, serial: str, top: Fields) -> None:
        if 8 in top:
            self._sched[serial] = top[8][0]
        if 9 in top:
            self._state_raw[serial] = top[9][0]

    # --- connection handling ------------------------------------------------
    def _logged_in(self, serial: str | None, writer: asyncio.StreamWriter) -> None:
        if not serial:
            return
        old = self._writers.get(serial)
        self._writers[serial] = writer
        if old is not None and old is not writer:
            old.close()  # the device reconnected; drop the stale link
        _LOGGER.info("Schluter thermostat %s connected to the local gateway", serial)
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
            seq = top.get(1, [0])[0]
            kind = top.get(2, [None])[0]
            if 6 in top:  # login
                conn["serial"] = _login_serial(top)
                writer.write(_login_ack(seq))
                await writer.drain()
                self._logged_in(conn["serial"], writer)
                continue
            self._dispatch(conn["serial"], body, top)
            if kind == 6:  # schedule upload
                writer.write(_ack(seq))
                writer.write(CUSTOMER_SVC)
            elif kind == 4:  # state upload
                writer.write(_ack(seq))
                writer.write(CONFIG)
            elif 3 in top and 2 not in top:  # the device acking us
                continue
            elif seq:  # telemetry and anything else that wants an ack
                writer.write(_ack(seq))
            else:
                continue
            await writer.drain()
            if 9 in top and conn["serial"]:
                await self._flush(conn["serial"])

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
                up_writer.write(_frame(body))
                await up_writer.drain()
                if 6 in top:
                    conn["serial"] = _login_serial(top)
                    self._logged_in(conn["serial"], writer)
                else:
                    self._dispatch(conn["serial"], body, top)
                    if 9 in top and conn["serial"]:
                        await self._flush(conn["serial"])

        async def cloud_to_device() -> None:
            while True:
                body = await self._read_frame(up_reader)
                writer.write(_frame(body))
                await writer.drain()
                # App changes reach the thermostat as cloud commands; show them too.
                self._dispatch(conn["serial"], body, parse(body))

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
