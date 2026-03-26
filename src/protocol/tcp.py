"""
SiNet2 / P2K TCP client for PureXS.

Confirmed wire format — CSiNetData header (20 bytes, big-endian):

  +0x00  WORD   magic           = 0x072D
  +0x02  WORD   func_code
  +0x04  WORD   api_version
  +0x06  WORD   api_revision
  +0x08  DWORD  session_id
  +0x0C  DWORD  payload_length  (bytes following the header)
  +0x10  DWORD  reserved        = 0x00000000

Struct format: ``">HHHHIII"``  (4 × WORD + 3 × DWORD = 20 bytes)

TCP payload field encoding (all big-endian; differs from UDP for S-type):

  W   2-byte BE WORD  scalar
  DW  4-byte BE DWORD scalar
  BA  2-byte BE WORD  byte-count  + raw bytes
  S   2-byte BE WORD  char-count  + UTF-16LE chars   ← 2-byte count on TCP
                                                        (UDP uses 4-byte DWORD)

Session lifecycle:
  1. Open TCP connection to device_ip:1999
  2. Send TCPConnect  (func_code=0x0001, session_id=0)
  3. Device replies with TCPAck (0xFF00); session_id field carries assigned ID
  4. Echo that session_id in every subsequent frame header
  5. Send TCPDisconnect (0x0002) before closing

asyncio + stdlib only — no third-party dependencies.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import sys
from dataclasses import dataclass
from typing import Final

log = logging.getLogger(__name__)

# ── Wire constants (inlined — module is stdlib-only) ──────────────────────────

_MAGIC: Final[int] = 0x072D
_DEFAULT_PORT: Final[int] = 1999
_API_VERSION: Final[int] = 0x0001
_API_REVISION: Final[int] = 0x0000

# CSiNetData header: WORD magic, WORD func, WORD api_ver, WORD api_rev,
#                    DWORD session_id, DWORD payload_len, DWORD reserved
_HDR_STRUCT: Final[struct.Struct] = struct.Struct(">HHHHIII")
_HDR_SIZE: Final[int] = _HDR_STRUCT.size
assert _HDR_SIZE == 20, f"Header struct size mismatch: {_HDR_SIZE}"

# TCP function codes (matching Netapi114.xml names in constants.TCPFuncCode)
_FC_CONNECT: Final[int] = 0x0001     # TCPConnect
_FC_DISCONNECT: Final[int] = 0x0002  # TCPDisconnect
_FC_REQ_INFO: Final[int] = 0x0003    # TCPReqInfo
_FC_INFO: Final[int] = 0x0004        # TCPInfo
_FC_REQ_CAPS: Final[int] = 0x0005    # TCPReqDevCaps
_FC_CAPS: Final[int] = 0x0006        # TCPDevCaps
_FC_ALIVE: Final[int] = 0x0009       # TCPAliveData
_FC_ACK: Final[int] = 0xFF00         # TCPAck
_FC_ERROR: Final[int] = 0xFF01       # TCPError

_DEFAULT_CONNECT_TIMEOUT: Final[float] = 10.0
_DEFAULT_IO_TIMEOUT: Final[float] = 30.0


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Exceptions
# ╚══════════════════════════════════════════════════════════════════════════════

class P2KError(Exception):
    """Base class for all SiNet2 / P2K errors."""


class P2KConnectionError(P2KError):
    """TCP connection could not be established or was lost unexpectedly.

    Wraps ``OSError``, ``asyncio.IncompleteReadError``, and similar low-level
    exceptions so callers can catch a single type for network failures.
    """


class P2KProtocolError(P2KError):
    """Unexpected frame sequence or malformed header received from device.

    Raised when the magic word is wrong, a response func_code doesn't match
    the expected reply, or a payload is too short to be decoded.
    """


class P2KDeviceError(P2KError):
    """Device returned a TCPError frame (func_code 0xFF01).

    Attributes:
        error_code: The 2-byte error code from the TCPError payload.
                    See :class:`~purexs.protocol.constants.ErrorCode`.
        device_message: Optional UTF-16LE error string decoded from the payload.
    """

    def __init__(self, error_code: int, device_message: str = "") -> None:
        self.error_code = error_code
        self.device_message = device_message
        detail = device_message or "(no message)"
        super().__init__(
            f"Device error 0x{error_code:04X}: {detail}"
        )


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  TCP payload field codecs
# ╚══════════════════════════════════════════════════════════════════════════════
#
# IMPORTANT: TCP uses a 2-byte BE WORD char-count prefix for S-type fields.
# UDP uses a 4-byte BE DWORD.  Keep these codec functions separate from the
# UDP codecs in packets.py.

def _enc_w(value: int) -> bytes:
    """Encode a 2-byte big-endian WORD."""
    return struct.pack(">H", value)


def _dec_w(buf: bytes, off: int) -> tuple[int, int]:
    """Decode a 2-byte big-endian WORD.  Returns ``(value, bytes_consumed=2)``."""
    if off + 2 > len(buf):
        raise P2KProtocolError(
            f"W field: need 2 bytes at offset {off}, only {len(buf) - off} remain"
        )
    return struct.unpack_from(">H", buf, off)[0], 2


def _enc_dw(value: int) -> bytes:
    """Encode a 4-byte big-endian DWORD."""
    return struct.pack(">I", value)


def _dec_dw(buf: bytes, off: int) -> tuple[int, int]:
    """Decode a 4-byte big-endian DWORD.  Returns ``(value, bytes_consumed=4)``."""
    if off + 4 > len(buf):
        raise P2KProtocolError(
            f"DW field: need 4 bytes at offset {off}, only {len(buf) - off} remain"
        )
    return struct.unpack_from(">I", buf, off)[0], 4


def _enc_ba(data: bytes) -> bytes:
    """Encode a BA-type field: 2-byte BE WORD byte-count + raw bytes."""
    return struct.pack(">H", len(data)) + data


def _dec_ba(buf: bytes, off: int) -> tuple[bytes, int]:
    """Decode a BA-type field.  Returns ``(raw_bytes, bytes_consumed)``."""
    if off + 2 > len(buf):
        raise P2KProtocolError(
            f"BA field: need 2 header bytes at offset {off}, "
            f"only {len(buf) - off} remain"
        )
    count = struct.unpack_from(">H", buf, off)[0]
    off += 2
    if off + count > len(buf):
        raise P2KProtocolError(
            f"BA field: need {count} data bytes at offset {off}, "
            f"only {len(buf) - off} remain"
        )
    return buf[off : off + count], 2 + count


def _enc_s(text: str) -> bytes:
    """Encode an S-type field (TCP variant): 2-byte BE WORD char-count + UTF-16LE.

    Note: TCP uses a 2-byte WORD prefix; UDP uses a 4-byte DWORD prefix.
    """
    encoded = text.encode("utf-16-le")
    char_count = len(text)       # code units, not bytes
    return struct.pack(">H", char_count) + encoded


def _dec_s(buf: bytes, off: int) -> tuple[str, int]:
    """Decode an S-type field (TCP variant).  Returns ``(text, bytes_consumed)``.

    Note: TCP uses a 2-byte WORD prefix; UDP uses a 4-byte DWORD prefix.
    """
    if off + 2 > len(buf):
        raise P2KProtocolError(
            f"S field: need 2 header bytes at offset {off}, "
            f"only {len(buf) - off} remain"
        )
    char_count = struct.unpack_from(">H", buf, off)[0]
    byte_count = char_count * 2
    off += 2
    if off + byte_count > len(buf):
        raise P2KProtocolError(
            f"S field: need {byte_count} data bytes at offset {off}, "
            f"only {len(buf) - off} remain"
        )
    text = buf[off : off + byte_count].decode("utf-16-le", errors="replace")
    return text, 2 + byte_count


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  _FrameHeader  (named tuple-like view of a parsed CSiNetData header)
# ╚══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class _FrameHeader:
    """Decoded CSiNetData header fields."""
    magic: int
    func_code: int
    api_version: int
    api_revision: int
    session_id: int
    payload_length: int
    reserved: int

    @classmethod
    def decode(cls, data: bytes) -> "_FrameHeader":
        """Parse 20 raw header bytes into a :class:`_FrameHeader`.

        Raises:
            P2KProtocolError: bad magic word or buffer too short.
        """
        if len(data) < _HDR_SIZE:
            raise P2KProtocolError(
                f"Header too short: {len(data)} bytes (need {_HDR_SIZE})"
            )
        magic, func_code, api_version, api_revision, session_id, payload_length, reserved = (
            _HDR_STRUCT.unpack_from(data, 0)
        )
        if magic != _MAGIC:
            raise P2KProtocolError(
                f"Bad magic: 0x{magic:04X} (expected 0x{_MAGIC:04X})"
            )
        return cls(
            magic=magic,
            func_code=func_code,
            api_version=api_version,
            api_revision=api_revision,
            session_id=session_id,
            payload_length=payload_length,
            reserved=reserved,
        )


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  DeviceInfo
# ╚══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Static device information returned by the TCPInfo (0x0004) response.

    All fields are decoded from the TCPInfo payload in wire order:
      1. FirmwareVersion  S  — firmware version string, e.g. ``"2.14.0"``
      2. SerialNumber     S  — factory serial number string
      3. DeviceType       W  — device type code (see :class:`~purexs.protocol.constants.DeviceType`)
      4. HardwareRev      W  — PCB / hardware revision number
    """

    firmware_version: str
    """Firmware version string, e.g. ``"2.14.0"``."""

    serial_number: str
    """Factory serial number, e.g. ``"SN-0042-XG"``."""

    device_type: int
    """Device type WORD value.  Cross-reference with :class:`~purexs.protocol.constants.DeviceType`."""

    hardware_rev: int
    """PCB hardware revision."""

    @classmethod
    def from_payload(cls, payload: bytes) -> "DeviceInfo":
        """Decode a TCPInfo payload into a :class:`DeviceInfo`.

        Raises:
            P2KProtocolError: payload too short or a field is truncated.
        """
        off = 0
        fw,   n = _dec_s(payload, off);  off += n   # field 1: FirmwareVersion
        sn,   n = _dec_s(payload, off);  off += n   # field 2: SerialNumber
        dtype, n = _dec_w(payload, off); off += n   # field 3: DeviceType
        hwrev, n = _dec_w(payload, off); off += n   # field 4: HardwareRev
        return cls(
            firmware_version=fw,
            serial_number=sn,
            device_type=dtype,
            hardware_rev=hwrev,
        )

    def display(self) -> str:
        """Return a compact human-readable summary line."""
        return (
            f"fw={self.firmware_version!r}  sn={self.serial_number!r}  "
            f"type=0x{self.device_type:04X}  hwrev={self.hardware_rev}"
        )


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  SiNet2Client
# ╚══════════════════════════════════════════════════════════════════════════════

class SiNet2Client:
    """Async TCP client for SiNet2 / P2K device communication.

    Manages the CSiNetData framing layer, session-ID negotiation, and
    sequential request/response flow over a single asyncio TCP stream.

    All public coroutines are safe to ``await`` from any coroutine in the
    same event loop.  Concurrent callers are serialised by an internal
    :class:`asyncio.Lock`; the device protocol is strictly request/response
    so only one in-flight exchange is permitted at a time.

    Usage — context manager (preferred)::

        async with SiNet2Client() as client:
            await client.connect("192.168.1.50")
            info = await client.request_info()
            print(info.display())

    Usage — manual lifecycle::

        client = SiNet2Client()
        await client.connect("192.168.1.50", port=1999)
        payload = await client.send(0x0030, b"")   # TCPReqStatus
        await client.close()

    Args:
        connect_timeout: Seconds to wait for the TCP handshake.
        io_timeout:      Seconds to wait for each read/write after connect.
    """

    def __init__(
        self,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        io_timeout: float = _DEFAULT_IO_TIMEOUT,
    ) -> None:
        self._connect_timeout = connect_timeout
        self._io_timeout = io_timeout

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._session_id: int = 0
        self._seq: int = 0
        self._lock = asyncio.Lock()

        # Set by connect(); used in log messages
        self._ip: str = ""
        self._port: int = _DEFAULT_PORT

    # ── async context manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> "SiNet2Client":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ── connection lifecycle ──────────────────────────────────────────────────

    async def connect(self, ip: str, port: int = _DEFAULT_PORT) -> None:
        """Open a TCP connection and negotiate a P2K session.

        Sends a TCPConnect frame (func_code ``0x0001``) with ``session_id=0``.
        The device replies with TCPAck; the session ID it assigns is extracted
        from the response header's ``session_id`` field and echoed in all
        subsequent frames.

        Args:
            ip:   Device IPv4 address, e.g. ``"192.168.1.50"``.
            port: TCP port reported in the UDP discovery response (default 1999).

        Raises:
            P2KConnectionError: TCP connection failed (host unreachable, refused,
                                 or timed out).
            P2KDeviceError:     Device rejected the session (TCPError response).
            P2KProtocolError:   Unexpected magic or malformed response header.
        """
        if self._writer is not None:
            raise P2KConnectionError(
                f"Already connected to {self._ip}:{self._port}. Call close() first."
            )

        self._ip = ip
        self._port = port

        try:
            async with asyncio.timeout(self._connect_timeout):
                reader, writer = await asyncio.open_connection(ip, port)
        except TimeoutError as exc:
            raise P2KConnectionError(
                f"TCP connect to {ip}:{port} timed out "
                f"({self._connect_timeout:.1f}s)"
            ) from exc
        except OSError as exc:
            raise P2KConnectionError(
                f"TCP connect to {ip}:{port} failed: {exc}"
            ) from exc

        self._reader = reader
        self._writer = writer
        self._seq = 0

        log.debug("TCP connected to %s:%d", ip, port)

        # ── session handshake ─────────────────────────────────────────────────
        # Send TCPConnect with session_id=0; device assigns an ID in its reply.
        frame = self._build_frame(_FC_CONNECT, b"", session_id=0)
        try:
            async with asyncio.timeout(self._io_timeout):
                writer.write(frame)
                await writer.drain()
                hdr, _ = await self._recv_frame()
        except TimeoutError as exc:
            await self._force_close()
            raise P2KConnectionError(
                f"TCPConnect handshake with {ip}:{port} timed out"
            ) from exc
        except (OSError, asyncio.IncompleteReadError) as exc:
            await self._force_close()
            raise P2KConnectionError(
                f"Connection lost during TCPConnect handshake: {exc}"
            ) from exc

        if hdr.func_code == _FC_ERROR:
            await self._force_close()
            raise P2KDeviceError(0, "Device refused the session (TCPError on connect)")

        self._session_id = hdr.session_id
        log.info(
            "P2K session established  id=0x%08X  host=%s:%d",
            self._session_id, ip, port,
        )

    async def close(self) -> None:
        """Send TCPDisconnect (if connected) and close the TCP stream.

        Safe to call even if not connected — does nothing in that case.
        Errors during the disconnect frame are suppressed; the socket is
        always closed.
        """
        if self._writer is None:
            return

        if self._session_id:
            try:
                frame = self._build_frame(_FC_DISCONNECT, b"")
                async with asyncio.timeout(3.0):
                    self._writer.write(frame)
                    await self._writer.drain()
            except Exception:
                pass  # best-effort; socket is closing anyway

        await self._force_close()
        log.info("P2K session closed  host=%s:%d", self._ip, self._port)

    # ── public API ────────────────────────────────────────────────────────────

    async def send(self, func_code: int, payload: bytes = b"") -> bytes:
        """Send a P2K frame and return the response payload bytes.

        This is the low-level transport method.  It handles framing,
        sequence numbering, session ID injection, and TCPError detection.
        Higher-level methods such as :meth:`request_info` are built on top.

        Args:
            func_code: TCP function code (see :data:`~purexs.protocol.constants.TCPFuncCode`).
            payload:   Encoded payload bytes (use the ``_enc_*`` helpers or
                       build manually).  Empty by default.

        Returns:
            The raw payload bytes from the device's response frame.

        Raises:
            P2KConnectionError: Not connected, or stream closed mid-exchange.
            P2KDeviceError:     Device returned TCPError.
            P2KProtocolError:   Unexpected response magic or truncated header.
        """
        _, response_payload = await self._exchange(func_code, payload)
        return response_payload

    async def request_info(self) -> DeviceInfo:
        """Send TCPReqInfo (0x0003) and decode the TCPInfo (0x0004) response.

        Returns:
            :class:`DeviceInfo` populated with firmware version, serial number,
            device type, and hardware revision.

        Raises:
            P2KConnectionError: Not connected or stream error.
            P2KDeviceError:     Device returned TCPError.
            P2KProtocolError:   Response func_code is not TCPInfo, or payload
                                is truncated.
        """
        hdr, payload = await self._exchange(_FC_REQ_INFO, b"")

        if hdr.func_code != _FC_INFO:
            raise P2KProtocolError(
                f"Expected TCPInfo (0x{_FC_INFO:04X}), "
                f"got 0x{hdr.func_code:04X}"
            )

        try:
            return DeviceInfo.from_payload(payload)
        except P2KProtocolError as exc:
            raise P2KProtocolError(
                f"Could not decode TCPInfo payload ({len(payload)} bytes): {exc}"
            ) from exc

    async def send_alive(self) -> None:
        """Send a TCPAliveData keepalive frame.

        Call periodically (e.g. every 20 s) to prevent the device from
        dropping an idle session.  No response payload is returned.
        """
        await self.send(_FC_ALIVE, b"")

    # ── internal helpers ──────────────────────────────────────────────────────

    def _build_frame(
        self,
        func_code: int,
        payload: bytes,
        *,
        session_id: int | None = None,
    ) -> bytes:
        """Pack a CSiNetData header prepended to *payload*.

        Args:
            func_code:  TCP function code.
            payload:    Pre-encoded payload bytes.
            session_id: Override session ID (used for TCPConnect before the
                        device assigns one).  Defaults to :attr:`_session_id`.

        Returns:
            ``header_bytes + payload``
        """
        if session_id is None:
            session_id = self._session_id
        self._seq = (self._seq + 1) & 0xFFFF_FFFF
        header = _HDR_STRUCT.pack(
            _MAGIC,           # +0x00  WORD  magic
            func_code,        # +0x02  WORD  func_code
            _API_VERSION,     # +0x04  WORD  api_version
            _API_REVISION,    # +0x06  WORD  api_revision
            session_id,       # +0x08  DWORD session_id
            len(payload),     # +0x0C  DWORD payload_length
            0,                # +0x10  DWORD reserved
        )
        return header + payload

    async def _exchange(
        self, func_code: int, payload: bytes
    ) -> tuple[_FrameHeader, bytes]:
        """Send a frame and receive the response, serialised by a lock.

        Returns:
            ``(response_header, response_payload)``

        Raises:
            P2KConnectionError: Not connected or IO failure.
            P2KDeviceError:     Device returned TCPError frame.
            P2KProtocolError:   Malformed response magic/header.
        """
        self._require_connected()

        async with self._lock:
            frame = self._build_frame(func_code, payload)
            log.debug(
                "→ 0x%04X  payload=%d B  seq=%d  sid=0x%08X",
                func_code, len(payload), self._seq, self._session_id,
            )

            try:
                async with asyncio.timeout(self._io_timeout):
                    assert self._writer is not None
                    self._writer.write(frame)
                    await self._writer.drain()
                    hdr, resp_payload = await self._recv_frame()
            except TimeoutError as exc:
                raise P2KConnectionError(
                    f"I/O timeout during func_code=0x{func_code:04X} exchange "
                    f"with {self._ip}:{self._port}"
                ) from exc
            except (OSError, asyncio.IncompleteReadError) as exc:
                raise P2KConnectionError(
                    f"Stream error during exchange with {self._ip}:{self._port}: {exc}"
                ) from exc

            log.debug(
                "← 0x%04X  payload=%d B  sid=0x%08X",
                hdr.func_code, len(resp_payload), hdr.session_id,
            )

        if hdr.func_code == _FC_ERROR:
            self._raise_device_error(resp_payload)

        return hdr, resp_payload

    async def _recv_frame(self) -> tuple[_FrameHeader, bytes]:
        """Read exactly one CSiNetData frame (header + payload) from the stream.

        Uses :meth:`asyncio.StreamReader.readexactly` so partial reads are
        handled transparently.

        Returns:
            ``(header, payload_bytes)``

        Raises:
            asyncio.IncompleteReadError: Connection closed mid-frame.
            P2KProtocolError:            Bad magic in header.
        """
        assert self._reader is not None
        raw_header = await self._reader.readexactly(_HDR_SIZE)
        hdr = _FrameHeader.decode(raw_header)

        payload = b""
        if hdr.payload_length:
            payload = await self._reader.readexactly(hdr.payload_length)

        return hdr, payload

    def _require_connected(self) -> None:
        if self._writer is None:
            raise P2KConnectionError(
                "Not connected. Call await client.connect(ip) first."
            )

    async def _force_close(self) -> None:
        """Close the stream without sending any frames."""
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
        self._session_id = 0
        self._seq = 0

    @staticmethod
    def _raise_device_error(payload: bytes) -> None:
        """Parse a TCPError payload and raise :class:`P2KDeviceError`."""
        error_code = 0
        device_message = ""

        if len(payload) >= 2:
            error_code = struct.unpack_from(">H", payload, 0)[0]

        # Optional UTF-16LE message: 2-byte WORD char-count at offset 2
        if len(payload) >= 4:
            try:
                msg_char_count = struct.unpack_from(">H", payload, 2)[0]
                msg_bytes = msg_char_count * 2
                if 4 + msg_bytes <= len(payload):
                    device_message = payload[4 : 4 + msg_bytes].decode(
                        "utf-16-le", errors="replace"
                    )
            except struct.error:
                pass

        raise P2KDeviceError(error_code, device_message)

    # ── repr ──────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        state = (
            f"connected sid=0x{self._session_id:08X} {self._ip}:{self._port}"
            if self._writer
            else "disconnected"
        )
        return f"<SiNet2Client {state}>"


# ── backward-compat alias ─────────────────────────────────────────────────────

#: Async alias kept for devices/ drivers and MockSironaDevice tests.
_AsyncTCPSession = SiNet2Client


# ── Synchronous TCPSession (packets.py framing) ───────────────────────────────

import socket as _socket

from .constants import (
    FUNC_ACK as _FUNC_ACK_SYNC,
    FUNC_CONNECT as _FUNC_CONNECT_SYNC,
    FUNC_DISCONNECT as _FUNC_DISCONNECT_SYNC,
    FUNC_ERROR as _FUNC_ERROR_SYNC,
    FUNC_GET_IMAGE as _FUNC_GET_IMAGE_SYNC,
    FUNC_GET_PARAM as _FUNC_GET_PARAM_SYNC,
    FUNC_SET_PARAM as _FUNC_SET_PARAM_SYNC,
    FUNC_STATUS as _FUNC_STATUS_SYNC,
    FUNC_TRIGGER as _FUNC_TRIGGER_SYNC,
    TCP_HEADER_SIZE as _TCP_HEADER_SIZE_SYNC,
)
from .packets import (
    build_tcp_header as _build_tcp_header,
    decode_w as _decode_w_sync,
    encode_w as _encode_w_sync,
    parse_tcp_header as _parse_tcp_header,
)


class TCPSession:
    """Synchronous TCP client for SiNet2 / P2K using the packets.py wire format.

    Uses the legacy 20-byte TCP header with a 4-byte DWORD magic
    (``">IHHIIII"``), which is the format spoken by MockTCPDevice and the
    original Sidexis firmware stack.

    Usage::

        with TCPSession("127.0.0.1", 1999) as sess:
            sess.connect()
            status = sess.get_status()
    """

    def __init__(
        self,
        ip: str,
        port: int = _DEFAULT_PORT,
        timeout: float = 5.0,
    ) -> None:
        self._ip = ip
        self._port = port
        self._timeout = timeout
        self._sock: _socket.socket | None = None
        self._session_id: int = 0
        self._seq: int = 0

    def __enter__(self) -> "TCPSession":
        self._sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        self._sock.settimeout(self._timeout)
        self._sock.connect((self._ip, self._port))
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._sock is None:
            return
        if self._session_id:
            try:
                frame = _build_tcp_header(
                    _FUNC_DISCONNECT_SYNC, b"", session_id=self._session_id
                )
                self._sock.sendall(frame)
            except Exception:
                pass
        try:
            self._sock.close()
        except Exception:
            pass
        self._sock = None
        self._session_id = 0

    def connect(self) -> int:
        """Send TCPConnect and return the session ID assigned by the device."""
        self._seq += 1
        frame = _build_tcp_header(
            _FUNC_CONNECT_SYNC, b"", session_id=0, seq_num=self._seq
        )
        assert self._sock is not None
        self._sock.sendall(frame)
        hdr, _ = self._recv_frame()
        self._session_id = hdr["session_id"]
        return self._session_id

    def get_status(self) -> dict:
        """Send FUNC_STATUS and return ``{"status_code": <int>}``."""
        payload = self._request(_FUNC_STATUS_SYNC, b"")
        if len(payload) >= 2:
            status_code, _ = _decode_w_sync(payload, 0)
        else:
            status_code = 0
        return {"status_code": status_code}

    def set_param(self, param_id: int, value: bytes) -> None:
        """Send FUNC_SET_PARAM with *param_id* and *value*."""
        self._request(_FUNC_SET_PARAM_SYNC, _encode_w_sync(param_id) + value)

    def get_param(self, param_id: int) -> bytes:
        """Send FUNC_GET_PARAM and return the raw value bytes."""
        payload = self._request(_FUNC_GET_PARAM_SYNC, _encode_w_sync(param_id))
        # response payload: encode_w(param_id) + value
        if len(payload) >= 2:
            return payload[2:]
        return b""

    def trigger(self) -> None:
        """Send FUNC_TRIGGER."""
        self._request(_FUNC_TRIGGER_SYNC, b"")

    def get_image(self) -> bytes:
        """Send FUNC_GET_IMAGE and return the raw image bytes."""
        return self._request(_FUNC_GET_IMAGE_SYNC, b"")

    def _request(self, func_code: int, payload: bytes) -> bytes:
        """Send a frame, await the response, and return its payload.

        Raises:
            P2KDeviceError: if the device replies with FUNC_ERROR.
        """
        assert self._sock is not None, "Not connected"
        self._seq += 1
        frame = _build_tcp_header(
            func_code, payload,
            session_id=self._session_id,
            seq_num=self._seq,
        )
        self._sock.sendall(frame)
        hdr, resp_payload = self._recv_frame()
        if hdr["func_code"] == _FUNC_ERROR_SYNC:
            error_code = 0
            if len(resp_payload) >= 2:
                error_code, _ = _decode_w_sync(resp_payload, 0)
            raise P2KDeviceError(error_code)
        return resp_payload

    def _recv_frame(self) -> tuple[dict, bytes]:
        """Read exactly one TCP frame from the socket."""
        assert self._sock is not None
        raw_header = self._recv_exactly(_TCP_HEADER_SIZE_SYNC)
        hdr = _parse_tcp_header(raw_header)
        payload = b""
        if hdr["payload_len"]:
            payload = self._recv_exactly(hdr["payload_len"])
        return hdr, payload

    def _recv_exactly(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            assert self._sock is not None
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError(
                    f"Connection closed after {len(buf)}/{n} bytes"
                )
            buf.extend(chunk)
        return bytes(buf)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  __main__  — quick connectivity check
# ╚══════════════════════════════════════════════════════════════════════════════

async def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m purexs.protocol.tcp",
        description="Connect to a SiNet2/P2K device and print device info",
    )
    parser.add_argument("ip", help="Device IP address")
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=_DEFAULT_PORT,
        metavar="PORT",
        help=f"TCP port (default: {_DEFAULT_PORT})",
    )
    parser.add_argument(
        "--timeout", "-t",
        type=float,
        default=_DEFAULT_CONNECT_TIMEOUT,
        metavar="SEC",
        help=f"connect timeout in seconds (default: {_DEFAULT_CONNECT_TIMEOUT:.0f})",
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="enable debug logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s  %(name)s  %(message)s",
    )

    print(f"Connecting to {args.ip}:{args.port} …")
    try:
        async with SiNet2Client(connect_timeout=args.timeout) as client:
            await client.connect(args.ip, args.port)
            print(f"  Session ID : 0x{client._session_id:08X}")

            info = await client.request_info()
            print(f"  Firmware   : {info.firmware_version}")
            print(f"  Serial     : {info.serial_number}")
            print(f"  DeviceType : 0x{info.device_type:04X}")
            print(f"  HW Rev     : {info.hardware_rev}")

    except P2KConnectionError as exc:
        print(f"Connection failed: {exc}", file=sys.stderr)
        return 1
    except P2KDeviceError as exc:
        print(f"Device error: {exc}", file=sys.stderr)
        return 2
    except P2KProtocolError as exc:
        print(f"Protocol error: {exc}", file=sys.stderr)
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
