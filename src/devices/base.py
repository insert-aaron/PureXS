"""
Abstract base class for all SiNet2 / P2K dental imaging devices.

Every concrete Sirona device driver inherits :class:`SironaDevice` and
implements the four abstract coroutines (``async_connect``, ``async_disconnect``,
``async_get_info``, ``async_request_xray``).  Common protocol-level operations
(ping, network config) are implemented here using :class:`SiNet2Client`.

The class also provides a **synchronous** surface (``connect``, ``disconnect``,
``get_status``, ``acquire_image``, ``get_param``, ``set_param``) built on the
sync :class:`~purexs.protocol.tcp.TCPSession`.  These are used by the FastAPI
routes, which run in thread-pool workers rather than the asyncio event loop.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import struct
import time
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

from ..protocol.tcp import (
    DeviceInfo as TCPDeviceInfo,
    P2KConnectionError,
    P2KDeviceError,
    P2KProtocolError,
    SiNet2Client,
    TCPSession,         # synchronous session for REST API use
    _dec_ba,            # same package — BA field decoder from tcp.py
    _dec_dw,            # same package — DW field decoder from tcp.py
    _dec_w,             # same package — W field decoder from tcp.py
)
from ..protocol.udp import DeviceAnnounce

log = logging.getLogger(__name__)

# ── TCP function codes used in common operations ──────────────────────────────
_FC_REQ_NETWORK_CONFIG: Final[int] = 0x0007   # TCPReqNetWorkConfig
_FC_NETWORK_CONFIG: Final[int] = 0x0008        # TCPNetWorkConfig
_FC_ALIVE: Final[int] = 0x0009                 # TCPAliveData


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  DeviceState
# ╚══════════════════════════════════════════════════════════════════════════════

class DeviceState(IntEnum):
    """Lifecycle state of a SiNet2 / P2K device driver instance."""

    DISCONNECTED = 0
    """No TCP session is open.  Call :meth:`SironaDevice.connect` first."""

    CONNECTING = 1
    """TCP connection is being established (transient)."""

    CONNECTED = 2
    """TCP session is open and the P2K handshake has completed."""

    READY = 3
    """Device is connected and its status register reports READY (0x0000)."""

    BUSY = 4
    """An acquisition or long operation is in progress."""

    ERROR = 5
    """The last operation failed; the session may need to be re-established."""


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  DeviceInfo
# ╚══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Snapshot of a device's static identity fields (from UDP discovery).

    Populated at construction from the :class:`~purexs.protocol.udp.DeviceAnnounce`
    or :class:`~purexs.protocol.packets.DiscoveryResponse`; does not change
    while the driver instance lives.
    """

    mac: str
    """Device MAC address as ``"AA:BB:CC:DD:EE:FF"``."""

    ip: str
    """Device IPv4 address as dotted-decimal, e.g. ``"192.168.1.50"``."""

    tcp_port: int
    """TCP port the device listens on (normally 1999)."""

    device_type: int
    """Device type WORD from the UDP discovery payload."""

    device_type_name: str
    """Human-readable product name, e.g. ``"ORTHOPHOS XG"``."""


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  NetworkConfig
# ╚══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class NetworkConfig:
    """Network configuration reported by a P2K device via TCPNetWorkConfig.

    Fields are decoded from the payload in wire order:
      1. IpAddress         BA4 — current device IPv4
      2. SubNetMask        BA4 — subnet mask
      3. DefGatewayAddress BA4 — default gateway
      4. TCPPort           W   — active P2K listening port
      5. DHCPEnabled       W   — 0 = static, 1 = DHCP
    """

    ip: str
    """Current device IPv4 address, e.g. ``"192.168.1.50"``."""

    netmask: str
    """Subnet mask, e.g. ``"255.255.255.0"``."""

    gateway: str
    """Default gateway, e.g. ``"192.168.1.1"``."""

    tcp_port: int
    """Active P2K TCP listening port (normally 1999)."""

    dhcp_enabled: bool
    """``True`` when the device is configured for DHCP."""

    @classmethod
    def from_payload(cls, payload: bytes) -> "NetworkConfig":
        """Decode a TCPNetWorkConfig payload.

        Raises:
            P2KProtocolError: payload is truncated or a field is malformed.
        """
        off = 0
        ip_bytes,   n = _dec_ba(payload, off); off += n   # field 1
        mask_bytes, n = _dec_ba(payload, off); off += n   # field 2
        gw_bytes,   n = _dec_ba(payload, off); off += n   # field 3
        tcp_port,   n = _dec_w(payload, off);  off += n   # field 4
        dhcp_raw,   n = _dec_w(payload, off);  off += n   # field 5

        return cls(
            ip=_fmt_ip(ip_bytes),
            netmask=_fmt_ip(mask_bytes),
            gateway=_fmt_ip(gw_bytes),
            tcp_port=tcp_port,
            dhcp_enabled=bool(dhcp_raw),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "ip":           self.ip,
            "netmask":      self.netmask,
            "gateway":      self.gateway,
            "tcp_port":     self.tcp_port,
            "dhcp_enabled": self.dhcp_enabled,
        }


def _fmt_ip(b: bytes) -> str:
    return ".".join(str(x) for x in b) if len(b) == 4 else b.hex()


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  SironaDevice
# ╚══════════════════════════════════════════════════════════════════════════════

class SironaDevice(abc.ABC):
    """Abstract base class for all SiNet2 / P2K dental imaging devices.

    Provides two parallel APIs:

    **Synchronous** (``connect``, ``disconnect``, ``get_status``, …):
        Built on the stdlib-only :class:`~purexs.protocol.tcp.TCPSession`.
        Used by FastAPI route handlers, which run in a thread-pool executor.

    **Asynchronous** (``async_connect``, ``async_disconnect``, …):
        Built on the asyncio :class:`~purexs.protocol.tcp.SiNet2Client`.
        Used for high-throughput scripts and direct async callers.
        Access via ``async with device:``.

    Concrete subclasses must implement the four ``async_*`` abstract methods.
    The sync surface works out of the box without subclass changes.
    """

    #: Concrete subclasses set this to the device type WORD values they handle.
    SUPPORTED_TYPES: frozenset[int] = frozenset()

    def __init__(
        self,
        ip: str,
        port: int = 1999,
        mac: str = "",
        device_type: int = 0,
    ) -> None:
        self._ip = ip
        self._port = port
        self._mac = mac
        self._device_type = device_type

        # ── sync session (REST API / threaded callers) ─────────────────────
        self._session: TCPSession | None = None
        self._state: DeviceState = DeviceState.DISCONNECTED

        # ── async client (SiNet2Client-based direct usage) ─────────────────
        self._client: SiNet2Client | None = None

        # Populated after async_get_info()
        self._serial_number: str = ""
        self._firmware_version: str = ""

    # ── alternate constructor ─────────────────────────────────────────────────

    @classmethod
    def from_announce(cls, announce: DeviceAnnounce) -> "SironaDevice":
        """Construct from a UDP discovery response (DeviceAnnounce or duck-typed).

        Works with both :class:`~purexs.protocol.udp.DeviceAnnounce` and
        :class:`~purexs.protocol.packets.DiscoveryResponse` objects.
        """
        return cls(
            ip=announce.ip,
            port=announce.tcp_port,
            mac=announce.mac,
            device_type=announce.device_type,
        )

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def ip(self) -> str:
        """Device IPv4 address as dotted-decimal string."""
        return self._ip

    @property
    def port(self) -> int:
        """TCP port the device listens on (normally 1999)."""
        return self._port

    @property
    def mac(self) -> str:
        """Device MAC address as ``"AA:BB:CC:DD:EE:FF"``."""
        return self._mac

    @property
    def device_type(self) -> int:
        """Device type WORD value from UDP discovery payload."""
        return self._device_type

    @property
    def serial_number(self) -> str:
        """Factory serial number.  Empty until :meth:`async_get_info` is called."""
        return self._serial_number

    @property
    def firmware_version(self) -> str:
        """Firmware version string.  Empty until :meth:`async_get_info` is called."""
        return self._firmware_version

    @property
    def state(self) -> DeviceState:
        """Current lifecycle state of the driver."""
        return self._state

    @property
    def info(self) -> DeviceInfo:
        """Static identity snapshot (populated from UDP discovery)."""
        from ..protocol.constants import DEVICE_TYPES
        return DeviceInfo(
            mac=self._mac,
            ip=self._ip,
            tcp_port=self._port,
            device_type=self._device_type,
            device_type_name=DEVICE_TYPES.get(self._device_type, "UNKNOWN"),
        )

    @property
    def is_connected(self) -> bool:
        """``True`` when an async TCP session is open to the device."""
        return (
            self._client is not None
            and self._client._writer is not None  # noqa: SLF001
        )

    # ── synchronous API (REST API / threaded callers) ─────────────────────────

    def connect(self) -> None:
        """Open a synchronous TCP session and negotiate a P2K handshake.

        Uses :class:`~purexs.protocol.tcp.TCPSession` (blocking sockets, no
        asyncio).  Safe to call from any thread including FastAPI route workers.

        Raises:
            P2KConnectionError: TCP connect failed, refused, or timed out.
            P2KDeviceError:     Device rejected the session.
        """
        if self._session is not None:
            return  # already connected — no-op
        self._state = DeviceState.CONNECTING
        try:
            sess = TCPSession(self._ip, self._port)
            sess.__enter__()        # opens + connects the socket
            sess.connect()          # TCPConnect handshake → assigns session_id
            self._session = sess
            self._state = DeviceState.CONNECTED
            log.info("sync session open  ip=%s  port=%d", self._ip, self._port)
        except Exception:
            self._state = DeviceState.ERROR
            raise

    def disconnect(self) -> None:
        """Send TCPDisconnect (if connected) and close the synchronous session.

        Safe to call when already disconnected — does nothing in that case.
        """
        if self._session is None:
            return
        try:
            self._session.__exit__(None, None, None)  # sends disconnect + closes
        except Exception:
            pass
        self._session = None
        self._state = DeviceState.DISCONNECTED
        log.info("sync session closed  ip=%s", self._ip)

    def get_status(self) -> dict:
        """Query live device status over the open synchronous TCP session.

        Returns:
            Dict with at least ``{"status_code": <int>}``.

        Raises:
            P2KConnectionError: Not connected.
            P2KDeviceError:     Device returned an error frame.
        """
        self._require_session()
        assert self._session is not None
        result = self._session.get_status()
        if result.get("status_code") == 0x0000:
            self._state = DeviceState.READY
        return result

    def acquire_image(self) -> bytes:
        """Trigger image acquisition and return the raw image bytes.

        Raises:
            P2KConnectionError: Not connected or stream error.
            P2KDeviceError:     Device reported an acquisition error.
        """
        self._require_session()
        self._state = DeviceState.BUSY
        try:
            assert self._session is not None
            raw = self._session.get_image()
            self._state = DeviceState.READY
            return raw
        except Exception:
            self._state = DeviceState.ERROR
            raise

    def get_param(self, param_id: int) -> bytes:
        """Read a raw device parameter by numeric ID.

        Returns raw value bytes (without the echoed param_id prefix).

        Raises:
            P2KConnectionError: Not connected.
            P2KDeviceError:     Device error.
        """
        self._require_session()
        assert self._session is not None
        return self._session.get_param(param_id)

    def set_param(self, param_id: int, value: bytes) -> None:
        """Write a raw device parameter.

        Raises:
            P2KConnectionError: Not connected.
            P2KDeviceError:     Device rejected the write.
        """
        self._require_session()
        assert self._session is not None
        self._session.set_param(param_id, value)

    def _require_session(self) -> None:
        """Raise :class:`P2KConnectionError` if no sync session is open."""
        if self._session is None:
            raise P2KConnectionError(
                f"Not connected to {self._ip}:{self._port}. "
                "Call device.connect() first."
            )

    # ── abstract async methods ────────────────────────────────────────────────

    @abc.abstractmethod
    async def async_connect(self) -> None:
        """Open an async TCP session and negotiate a P2K session ID.

        Raises:
            P2KConnectionError: TCP connect failed or device refused.
            P2KDeviceError:     Device sent TCPError during handshake.
        """

    @abc.abstractmethod
    async def async_disconnect(self) -> None:
        """Send TCPDisconnect and close the async TCP stream.

        Safe to call when already disconnected — must be a no-op.
        """

    @abc.abstractmethod
    async def async_get_info(self) -> TCPDeviceInfo:
        """Request static device info (firmware, serial, hardware rev).

        Populates :attr:`serial_number` and :attr:`firmware_version` as a
        side-effect.

        Raises:
            P2KConnectionError: Not connected or stream error.
            P2KDeviceError:     Device returned TCPError.
            P2KProtocolError:   Malformed TCPInfo payload.
        """

    @abc.abstractmethod
    async def async_request_xray(self, **kwargs: object) -> bytes:
        """Trigger an X-ray exposure and return the raw image bytes.

        Returns:
            Raw image bytes in the device's native format.

        Raises:
            P2KConnectionError: Not connected or stream dropped during transfer.
            P2KDeviceError:     Device reported an acquisition error.
            P2KProtocolError:   Unexpected frame sequence during image push.
        """

    # ── live image streaming ──────────────────────────────────────────────────

    async def live_images(self, **kwargs: object) -> AsyncIterator[bytes]:
        """Trigger an X-ray and stream raw pixel blocks as the device pushes them.

        Protocol sequence:

        1. Calls :meth:`async_request_xray` (``**kwargs`` forwarded) to
           trigger the exposure.
        2. Reads device-pushed frames via :meth:`_pop_frame` until
           ``TCPXRayImgEnd`` (0x000C) is received.
        3. Yields the ``Data`` bytes from each ``TCPXRayImgBlock`` (0x000B)
           as they arrive — suitable for progressive WebSocket streaming.
        4. On ``TCPXRayImgEnd``, assembles the complete image, verifies the
           CRC-32 checksum, and yields the full image bytes before returning.

        ``TCPXRayImgBegin`` (0x000A) and ``TCPProgressBar`` (0x000D) frames
        are consumed silently.

        Yields:
            Raw pixel bytes from each ``TCPXRayImgBlock``, followed by the
            complete reassembled image after ``TCPXRayImgEnd``.

        Raises:
            P2KConnectionError: Not connected to the device.
            P2KDeviceError:     Device reported a non-zero status in
                                ``TCPXRayImgEnd``.
            P2KProtocolError:   CRC-32 mismatch on the reassembled image.
        """
        from ..protocol.constants import TCPFuncCode

        _FC_IMG_BEGIN = int(TCPFuncCode.TCPXRayImgBegin)  # 0x000A
        _FC_IMG_BLOCK = int(TCPFuncCode.TCPXRayImgBlock)  # 0x000B
        _FC_IMG_END   = int(TCPFuncCode.TCPXRayImgEnd)    # 0x000C
        _FC_PROGRESS  = int(TCPFuncCode.TCPProgressBar)   # 0x000D

        self._require_connected()

        # Trigger the exposure; the device will push image frames asynchronously.
        await self.async_request_xray(**kwargs)

        chunks: list[bytes] = []

        while True:
            func_code, payload = await self._pop_frame()

            if func_code == _FC_IMG_BEGIN:
                # TCPXRayImgBegin: reset accumulation for this image_id.
                chunks.clear()

            elif func_code == _FC_IMG_BLOCK:
                # Payload: ImageId(W=2) BlockIndex(W=2) Data(BA)
                data, _ = _dec_ba(payload, 4)   # skip ImageId + BlockIndex
                chunks.append(data)
                yield data

            elif func_code == _FC_IMG_END:
                # Payload: ImageId(W=2) Status(W=2) Checksum(DW=4)
                status, _ = _dec_w(payload, 2)
                if status != 0:
                    raise P2KDeviceError(
                        status, "Acquisition failed (TCPXRayImgEnd status)"
                    )
                checksum, _ = _dec_dw(payload, 4)
                full_image = b"".join(chunks)
                actual_crc = zlib.crc32(full_image) & 0xFFFF_FFFF
                if actual_crc != checksum:
                    raise P2KProtocolError(
                        f"Image CRC-32 mismatch: "
                        f"expected 0x{checksum:08X}, got 0x{actual_crc:08X}"
                    )
                yield full_image
                return

            elif func_code == _FC_PROGRESS:
                pass  # swallow progress frames silently

            else:
                log.warning(
                    "live_images: unexpected frame 0x%04X (len=%d) from %s — skipping",
                    func_code, len(payload), self._ip,
                )

    # ── async context manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> "SironaDevice":
        await self.async_connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.async_disconnect()

    # ── common async operations (concrete) ────────────────────────────────────

    async def ping(self, timeout: float = 3.0) -> float:
        """Measure round-trip latency to the device in milliseconds.

        **When connected:** sends a TCPAliveData keepalive and times the
        response.

        **When not connected:** opens a bare TCP socket, measures the time to
        complete the three-way handshake, and closes immediately.

        Args:
            timeout: Maximum wait in seconds before raising
                     :class:`P2KConnectionError`.

        Returns:
            Round-trip time in milliseconds.

        Raises:
            P2KConnectionError: Device unreachable within *timeout*.
        """
        if self.is_connected:
            return await self._ping_alive(timeout)
        return await self._ping_tcp(timeout)

    async def get_network_config(self) -> NetworkConfig:
        """Query the device's current network configuration.

        Sends TCPReqNetWorkConfig (0x0007) and decodes the TCPNetWorkConfig
        (0x0008) response.

        Returns:
            :class:`NetworkConfig` with IP, mask, gateway, port, and DHCP flag.

        Raises:
            P2KConnectionError: Not connected or stream error.
            P2KDeviceError:     Device returned TCPError.
            P2KProtocolError:   Response has wrong func_code or truncated payload.
        """
        self._require_connected()
        assert self._client is not None

        hdr, payload = await self._client._exchange(  # noqa: SLF001
            _FC_REQ_NETWORK_CONFIG, b""
        )
        if hdr.func_code != _FC_NETWORK_CONFIG:
            raise P2KProtocolError(
                f"Expected TCPNetWorkConfig (0x{_FC_NETWORK_CONFIG:04X}), "
                f"got 0x{hdr.func_code:04X}"
            )
        return NetworkConfig.from_payload(payload)

    # ── protected async helpers ────────────────────────────────────────────────

    def _require_connected(self) -> None:
        """Raise :class:`P2KConnectionError` if async client is not connected."""
        if not self.is_connected:
            raise P2KConnectionError(
                f"Not connected to {self._ip}:{self._port}. "
                "Call await device.async_connect() first."
            )

    async def _pop_frame(self) -> tuple[int, bytes]:
        """Receive one device-pushed frame outside of a request/response cycle.

        Returns:
            ``(func_code, payload_bytes)``

        Raises:
            P2KConnectionError: Stream was closed mid-frame.
            P2KProtocolError:   Bad magic in received header.
        """
        self._require_connected()
        assert self._client is not None

        async with self._client._lock:  # noqa: SLF001
            try:
                hdr, payload = await self._client._recv_frame()  # noqa: SLF001
            except asyncio.IncompleteReadError as exc:
                raise P2KConnectionError(
                    f"Stream closed while reading pushed frame from {self._ip}"
                ) from exc

        log.debug(
            "← pushed 0x%04X  len=%d  from %s",
            hdr.func_code, len(payload), self._ip,
        )
        return hdr.func_code, payload

    # ── ping internals ────────────────────────────────────────────────────────

    async def _ping_alive(self, timeout: float) -> float:
        assert self._client is not None
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(timeout):
                await self._client.send_alive()
        except TimeoutError as exc:
            raise P2KConnectionError(
                f"Ping timeout ({timeout:.1f}s) for {self._ip}"
            ) from exc
        return (time.perf_counter() - t0) * 1000.0

    async def _ping_tcp(self, timeout: float) -> float:
        t0 = time.perf_counter()
        try:
            async with asyncio.timeout(timeout):
                _, writer = await asyncio.open_connection(self._ip, self._port)
            writer.close()
            await writer.wait_closed()
        except TimeoutError as exc:
            raise P2KConnectionError(
                f"TCP connect timeout ({timeout:.1f}s) pinging {self._ip}:{self._port}"
            ) from exc
        except OSError as exc:
            raise P2KConnectionError(
                f"TCP connect failed pinging {self._ip}:{self._port}: {exc}"
            ) from exc
        return (time.perf_counter() - t0) * 1000.0

    # ── repr ──────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} "
            f"ip={self._ip} mac={self._mac} "
            f"type=0x{self._device_type:04X} state={self._state.name}>"
        )


# ── backward-compat aliases ───────────────────────────────────────────────────

#: Old name used by registry.py, routes.py, and tests.
BaseDevice = SironaDevice
