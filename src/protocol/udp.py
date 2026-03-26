"""
Async SiNet2 / P2K UDP device discovery.

Wire format (confirmed by reverse engineering SiNet2.dll + SiPanCtl.dll):

UDP header — 18 bytes, little-endian
  +0x00  WORD  magic        = 0x072D
  +0x02  WORD  reserved
  +0x04  WORD  func_code    = 0x8000 (DISCOVER)
  +0x06  WORD  reserved
  +0x08  WORD  reserved
  +0x0A  WORD  api_version
  +0x0C  WORD  payload_len  (= 0x000E for probe / includes func_code..seq_num)
  +0x0E  WORD  reserved
  +0x10  WORD  seq_num

Discovery response payload — 7 fields, big-endian
  1. NameP2K           S    4-byte BE DWORD char-count + UTF-16LE data
  2. DefGatewayAddress BA   2-byte BE WORD count (=4) + 4 raw bytes
  3. SubNetMask        BA   2-byte BE WORD count (=4) + 4 raw bytes
  4. IpAddress         BA   2-byte BE WORD count (=4) + 4 raw bytes
  5. EthernetAddress   BA   2-byte BE WORD count (=6) + 6 raw bytes
  6. ConfigTime        W    2-byte BE WORD
  7. DeviceType        W    2-byte BE WORD

NameP2K encodes the device's TCP port as a decimal ASCII string in UTF-16LE.
Fallback TCP port when absent or unparseable: 1999.

No third-party dependencies — stdlib only.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Final

log = logging.getLogger(__name__)

# ── Wire constants (inlined so this module is stdlib-only) ────────────────────

_MAGIC: Final[int] = 0x072D
_FUNC_DISCOVER: Final[int] = 0x8000
_DEFAULT_PORT: Final[int] = 1999
_LISTEN_PORT: Final[int] = 55999
_API_VERSION: Final[int] = 0x0001

_HEADER_FMT: Final[str] = "<HHHHHHHHH"   # 9 × LE WORD = 18 bytes
_HEADER_SIZE: Final[int] = struct.calcsize(_HEADER_FMT)
assert _HEADER_SIZE == 18, "Header format size mismatch"

# payload_len for a header-only frame: counts from func_code (+0x04) to end
_PAYLOAD_LEN_EMPTY: Final[int] = 0x000E

# Pre-built probe: 18-byte header, no payload
_PROBE: Final[bytes] = struct.pack(
    _HEADER_FMT,
    _MAGIC,              # +0x00  magic
    0x0000,              # +0x02  reserved
    _FUNC_DISCOVER,      # +0x04  func_code
    0x0000,              # +0x06  reserved
    0x0000,              # +0x08  reserved
    _API_VERSION,        # +0x0A  api_version
    _PAYLOAD_LEN_EMPTY,  # +0x0C  payload_len
    0x0000,              # +0x0E  reserved
    0x0000,              # +0x10  seq_num
)
assert len(_PROBE) == _HEADER_SIZE


# ── DeviceAnnounce ────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class DeviceAnnounce:
    """All seven SiNet2 discovery payload fields plus transport metadata.

    Field names match the SiNet2.dll static template array in wire order:
    NameP2K → DefGatewayAddress → SubNetMask → IpAddress →
    EthernetAddress → ConfigTime → DeviceType.
    """

    # ── transport metadata ────────────────────────────────────────────────────
    src_ip: str
    """IP address of the interface the response arrived on."""
    src_port: int
    """Source port of the UDP response packet."""

    # ── payload field 1: NameP2K (S) ─────────────────────────────────────────
    name_p2k: str
    """Raw NameP2K string decoded from UTF-16LE.

    Contains the device's TCP listening port as decimal ASCII, e.g. ``"1999"``.
    Empty when the device did not populate the field.
    """
    tcp_port: int
    """TCP port derived from :attr:`name_p2k`; fallback 1999 on parse failure."""

    # ── payload field 2: DefGatewayAddress (BA4) ─────────────────────────────
    gateway: str
    """Default gateway as dotted-decimal IPv4, e.g. ``"192.168.1.1"``."""

    # ── payload field 3: SubNetMask (BA4) ─────────────────────────────────────
    netmask: str
    """Subnet mask as dotted-decimal IPv4, e.g. ``"255.255.255.0"``."""

    # ── payload field 4: IpAddress (BA4) ──────────────────────────────────────
    ip: str
    """Device IPv4 address as dotted-decimal, e.g. ``"192.168.1.50"``."""

    # ── payload field 5: EthernetAddress (BA6) ────────────────────────────────
    mac: str
    """Device MAC address as ``"AA:BB:CC:DD:EE:FF"``."""

    # ── payload field 6: ConfigTime (W) ───────────────────────────────────────
    config_time: int
    """Opaque configuration timestamp WORD; incremented on each network change."""

    # ── payload field 7: DeviceType (W) ───────────────────────────────────────
    device_type: int
    """Device type code; see :class:`~purexs.protocol.constants.DeviceType`."""

    # ── frame header fields ───────────────────────────────────────────────────
    api_version: int
    """Protocol API version from the UDP frame header."""
    seq_num: int
    """Sequence number from the UDP frame header."""

    # ── raw bytes (excluded from equality / hash / repr) ─────────────────────
    raw: bytes = field(repr=False, compare=False, hash=False)
    """Verbatim bytes of the received UDP datagram, for debugging."""

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dict of all fields (excluding raw bytes)."""
        return {
            "src_ip":      self.src_ip,
            "src_port":    self.src_port,
            "name_p2k":   self.name_p2k,
            "tcp_port":    self.tcp_port,
            "gateway":     self.gateway,
            "netmask":     self.netmask,
            "ip":          self.ip,
            "mac":         self.mac,
            "config_time": self.config_time,
            "device_type": self.device_type,
            "api_version": self.api_version,
            "seq_num":     self.seq_num,
        }


# ── asyncio DatagramProtocol ──────────────────────────────────────────────────

class _DatagramQueue(asyncio.DatagramProtocol):
    """Minimal DatagramProtocol that pushes (data, addr) into an asyncio Queue.

    One instance is created per :meth:`SiNet2Discovery._open_endpoint` call.
    The queue is drained by the public API methods; this class never touches
    the received bytes itself.
    """

    def __init__(
        self,
        queue: asyncio.Queue[tuple[bytes, tuple[str, int]]],
    ) -> None:
        self._q = queue
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        # put_nowait: the queue is unbounded so this never blocks.
        self._q.put_nowait((data, addr))

    def error_received(self, exc: Exception) -> None:
        # ICMP port-unreachable, TTL-exceeded, etc. — not fatal for discovery.
        log.debug("UDP error_received: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        log.debug("UDP connection_lost: %s", exc)


# ── SiNet2Discovery ───────────────────────────────────────────────────────────

class SiNet2Discovery:
    """Async SiNet2 / P2K UDP device discovery.

    All network I/O is non-blocking and driven by the running asyncio event
    loop.  No threads, no blocking calls, no third-party dependencies.

    One-shot scan::

        disc = SiNet2Discovery()
        devices = await disc.discover(timeout=5.0)
        for d in devices:
            print(d.ip, d.mac, d.tcp_port)

    Passive listen (runs until cancelled)::

        async for device in SiNet2Discovery().listen_passive():
            handle(device)

    Args:
        listen_port:     Local UDP port to bind for receiving replies.
                         Defaults to 55999 (unprivileged; avoids clashing with
                         Sidexis which holds port 1999 exclusively on Windows).
        target_port:     Destination port for the broadcast probe. Devices
                         listen on 1999 by default.
        broadcast_addr:  Broadcast address; override for subnet-specific scans,
                         e.g. ``"192.168.1.255"``.
    """

    def __init__(
        self,
        listen_port: int = _LISTEN_PORT,
        target_port: int = _DEFAULT_PORT,
        broadcast_addr: str = "255.255.255.255",
    ) -> None:
        self._listen_port = listen_port
        self._target_port = target_port
        self._broadcast_addr = broadcast_addr

    # ── public API ────────────────────────────────────────────────────────────

    async def discover(self, timeout: float = 5.0) -> list[DeviceAnnounce]:
        """Broadcast a P2K probe and return every valid response within *timeout*.

        The probe is sent once immediately after the socket is bound.
        Responses are collected until the timeout expires or the event loop is
        cancelled.  Duplicate responses (same IP + port) are silently dropped.

        Args:
            timeout: Listen window in seconds after the probe is sent.

        Returns:
            List of :class:`DeviceAnnounce`, one per unique responding device.
            Empty list when no devices reply within *timeout*.
        """
        queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
        transport, _ = await self._open_endpoint(queue)

        results: list[DeviceAnnounce] = []
        seen: set[tuple[str, int]] = set()

        try:
            transport.sendto(_PROBE, (self._broadcast_addr, self._target_port))
            log.debug(
                "Probe sent → %s:%d  (listen :%d  timeout %.1fs)",
                self._broadcast_addr, self._target_port,
                self._listen_port, timeout,
            )

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout

            while True:
                remaining = deadline - loop.time()
                if remaining <= 0.0:
                    break

                try:
                    async with asyncio.timeout(remaining):
                        data, addr = await queue.get()
                except TimeoutError:
                    break

                # Skip our own probe echo (header-only, no payload)
                if len(data) <= _HEADER_SIZE:
                    continue

                key = (addr[0], addr[1])
                if key in seen:
                    continue
                seen.add(key)

                try:
                    ann = self._parse_response(data, addr[0], addr[1])
                    results.append(ann)
                    log.info(
                        "Found  ip=%-15s  mac=%s  type=0x%04X  tcp=%d",
                        ann.ip, ann.mac, ann.device_type, ann.tcp_port,
                    )
                except (ValueError, struct.error) as exc:
                    log.warning(
                        "Malformed packet from %s:%d — %s\n%s",
                        addr[0], addr[1], exc, _hexdump(data),
                    )
        finally:
            transport.close()

        log.debug("discover() done: %d device(s)", len(results))
        return results

    async def listen_passive(self) -> AsyncIterator[DeviceAnnounce]:
        """Yield :class:`DeviceAnnounce` objects as SiNet2 broadcasts arrive.

        Does **not** send a probe — purely passive.  Runs indefinitely until
        the caller breaks out of the ``async for`` loop or the coroutine is
        cancelled.  The UDP socket is guaranteed to be closed on exit via the
        ``finally`` block, even on ``asyncio.CancelledError``.

        Typical usage::

            async for device in SiNet2Discovery(listen_port=1999).listen_passive():
                registry.update(device)
        """
        queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
        transport, _ = await self._open_endpoint(queue)
        log.debug("listen_passive: socket open on :%d", self._listen_port)

        try:
            while True:
                data, addr = await queue.get()

                if len(data) <= _HEADER_SIZE:
                    continue

                try:
                    yield self._parse_response(data, addr[0], addr[1])
                except (ValueError, struct.error) as exc:
                    log.debug(
                        "Skipping malformed packet from %s — %s", addr[0], exc
                    )
        finally:
            transport.close()
            log.debug("listen_passive: socket closed")

    # ── socket / endpoint creation ────────────────────────────────────────────

    async def _open_endpoint(
        self,
        queue: asyncio.Queue[tuple[bytes, tuple[str, int]]],
    ) -> tuple[asyncio.DatagramTransport, _DatagramQueue]:
        """Create a broadcast-capable UDP socket and attach it to the event loop.

        We build the socket manually (rather than using the ``local_addr``
        shortcut) so we can set ``SO_REUSEADDR`` and ``SO_BROADCAST`` before
        binding.  The socket is handed off to asyncio via the ``sock=``
        parameter, which takes ownership and closes it on transport close.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", self._listen_port))
        sock.setblocking(False)

        protocol = _DatagramQueue(queue)
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            sock=sock,
        )
        return transport, protocol  # type: ignore[return-value]

    # ── frame parser ──────────────────────────────────────────────────────────

    def _parse_response(
        self, data: bytes, src_ip: str, src_port: int
    ) -> DeviceAnnounce:
        """Decode a SiNet2 UDP frame into a :class:`DeviceAnnounce`.

        Validates the magic word and function code, then walks the payload
        calling the three field-type parsers in wire order.

        Raises:
            ValueError:  Bad magic, unexpected func_code, or truncated field.
            struct.error: Buffer shorter than the claimed header size.
        """
        if len(data) < _HEADER_SIZE:
            raise ValueError(
                f"Packet too short: {len(data)} bytes (need {_HEADER_SIZE})"
            )

        magic, _r1, func_code, _r2, _r3, api_version, _payload_len, _r4, seq_num = (
            struct.unpack_from(_HEADER_FMT, data, 0)
        )

        if magic != _MAGIC:
            raise ValueError(f"Bad magic: 0x{magic:04X} (expected 0x{_MAGIC:04X})")
        if func_code != _FUNC_DISCOVER:
            raise ValueError(
                f"Unexpected func_code: 0x{func_code:04X} "
                f"(expected 0x{_FUNC_DISCOVER:04X})"
            )

        payload = data[_HEADER_SIZE:]
        off = 0

        name_str,   n = self._parse_s(payload, off);    off += n  # field 1
        gw_bytes,   n = self._parse_ba(payload, off);   off += n  # field 2
        mask_bytes, n = self._parse_ba(payload, off);   off += n  # field 3
        ip_bytes,   n = self._parse_ba(payload, off);   off += n  # field 4
        mac_bytes,  n = self._parse_ba(payload, off);   off += n  # field 5
        config_time, n = self._parse_word(payload, off); off += n  # field 6
        device_type, n = self._parse_word(payload, off); off += n  # field 7

        name_stripped = name_str.strip("\x00").strip()
        tcp_port = _DEFAULT_PORT
        try:
            tcp_port = int(name_stripped)
        except (ValueError, TypeError):
            pass  # keep fallback 1999

        return DeviceAnnounce(
            src_ip=src_ip,
            src_port=src_port,
            name_p2k=name_stripped,
            tcp_port=tcp_port,
            gateway=self._fmt_ip(gw_bytes),
            netmask=self._fmt_ip(mask_bytes),
            ip=self._fmt_ip(ip_bytes),
            mac=self._fmt_mac(mac_bytes),
            config_time=config_time,
            device_type=device_type,
            api_version=api_version,
            seq_num=seq_num,
            raw=data,
        )

    # ── field-type parsers (private, kept on the class per spec) ─────────────

    @staticmethod
    def _parse_s(buf: bytes, off: int) -> tuple[str, int]:
        """S-type: 4-byte BE DWORD char-count + UTF-16LE encoded characters.

        Returns:
            (decoded_string, total_bytes_consumed)
        """
        if off + 4 > len(buf):
            raise ValueError(
                f"S field: need 4 header bytes at offset {off}, "
                f"only {len(buf) - off} remain"
            )
        char_count = struct.unpack_from(">I", buf, off)[0]
        byte_count = char_count * 2
        off += 4
        if off + byte_count > len(buf):
            raise ValueError(
                f"S field: need {byte_count} data bytes at offset {off}, "
                f"only {len(buf) - off} remain"
            )
        text = buf[off : off + byte_count].decode("utf-16-le", errors="replace")
        return text, 4 + byte_count

    @staticmethod
    def _parse_ba(buf: bytes, off: int) -> tuple[bytes, int]:
        """BA-type: 2-byte BE WORD count + *count* raw bytes.

        Returns:
            (raw_bytes, total_bytes_consumed)
        """
        if off + 2 > len(buf):
            raise ValueError(
                f"BA field: need 2 header bytes at offset {off}, "
                f"only {len(buf) - off} remain"
            )
        count = struct.unpack_from(">H", buf, off)[0]
        off += 2
        if off + count > len(buf):
            raise ValueError(
                f"BA field: need {count} data bytes at offset {off}, "
                f"only {len(buf) - off} remain"
            )
        return buf[off : off + count], 2 + count

    @staticmethod
    def _parse_word(buf: bytes, off: int) -> tuple[int, int]:
        """W-type: 2-byte BE WORD scalar.

        Returns:
            (value, bytes_consumed=2)
        """
        if off + 2 > len(buf):
            raise ValueError(
                f"W field: need 2 bytes at offset {off}, "
                f"only {len(buf) - off} remain"
            )
        return struct.unpack_from(">H", buf, off)[0], 2

    # ── address formatters ────────────────────────────────────────────────────

    @staticmethod
    def _fmt_ip(b: bytes) -> str:
        return ".".join(str(x) for x in b) if len(b) == 4 else b.hex()

    @staticmethod
    def _fmt_mac(b: bytes) -> str:
        return ":".join(f"{x:02X}" for x in b) if b else "??:??:??:??:??:??"


# Backward-compat async alias
_AsyncUDPDiscovery = SiNet2Discovery


# ── Synchronous UDPDiscovery (packets.py framing) ─────────────────────────────

from .packets import (  # noqa: E402
    DISCOVERY_PROBE as _DISCOVERY_PROBE,
    DiscoveryResponse as _DiscoveryResponse,
    build_udp_header as _build_udp_header,
    parse_udp_header as _parse_udp_header,
)
from .constants import FUNC_DISCOVER as _FUNC_DISCOVER_SYNC  # noqa: E402

_UDP_HEADER_SIZE_SYNC: int = 18


class UDPDiscovery:
    """Synchronous SiNet2 UDP device discovery using the packets.py wire format.

    Designed to work with the legacy threaded MockUDPDevice in tests.

    Usage::

        with UDPDiscovery(listen_port=55999, target_port=1999,
                          broadcast_addr="127.0.0.1") as disc:
            devices = disc.scan(timeout=2.0)
    """

    def __init__(
        self,
        listen_port: int = _LISTEN_PORT,
        target_port: int = _DEFAULT_PORT,
        broadcast_addr: str = "255.255.255.255",
    ) -> None:
        self._listen_port = listen_port
        self._target_port = target_port
        self._broadcast_addr = broadcast_addr
        self._sock: socket.socket | None = None

    def __enter__(self) -> "UDPDiscovery":
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", self._listen_port))
        self._sock = sock
        return self

    def __exit__(self, *_: object) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def scan(self, timeout: float = 5.0) -> "list[_DiscoveryResponse]":
        """Broadcast a probe and return all valid responses within *timeout*."""
        return list(self.scan_iter(timeout=timeout))

    def scan_iter(self, timeout: float = 5.0) -> "Iterator[_DiscoveryResponse]":
        """Broadcast a probe and yield valid responses until *timeout*."""
        from collections.abc import Iterator  # noqa: F401
        assert self._sock is not None, "Use as context manager"

        self._sock.sendto(
            _DISCOVERY_PROBE, (self._broadcast_addr, self._target_port)
        )

        import time as _time
        deadline = _time.monotonic() + timeout
        seen: set[tuple[str, int]] = set()

        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0.0:
                break
            self._sock.settimeout(min(remaining, 0.2))
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            if len(data) <= _UDP_HEADER_SIZE_SYNC:
                continue

            key = (addr[0], addr[1])
            if key in seen:
                continue
            seen.add(key)

            try:
                yield _DiscoveryResponse.from_wire(data, addr[0], addr[1])
            except (ValueError, struct.error):
                pass


# ── __main__ ─────────────────────────────────────────────────────────────────

def _hexdump(data: bytes, indent: str = "  ") -> str:
    lines: list[str] = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        asc_part = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        lines.append(f"{indent}{i:04X}  {hex_part:<47}  {asc_part}")
    return "\n".join(lines)


async def _main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m purexs.protocol.udp",
        description="SiNet2 / P2K UDP device discovery",
    )
    parser.add_argument(
        "--timeout", "-t",
        type=float,
        default=5.0,
        metavar="SEC",
        help="listen window in seconds (default: 5)",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=_DEFAULT_PORT,
        metavar="PORT",
        help=f"target UDP port (default: {_DEFAULT_PORT})",
    )
    parser.add_argument(
        "--listen",
        type=int,
        default=_LISTEN_PORT,
        metavar="PORT",
        help=f"local listen port (default: {_LISTEN_PORT})",
    )
    parser.add_argument(
        "--broadcast",
        default="255.255.255.255",
        metavar="ADDR",
        help="broadcast address (default: 255.255.255.255)",
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

    banner = (
        f"SiNet2 P2K discovery  "
        f"(listen :{args.listen} → send :{args.port}, "
        f"timeout {args.timeout:.0f}s)"
    )
    print(banner)
    print("=" * len(banner))

    disc = SiNet2Discovery(
        listen_port=args.listen,
        target_port=args.port,
        broadcast_addr=args.broadcast,
    )
    devices = await disc.discover(timeout=args.timeout)

    if not devices:
        print("\nNo devices found.")
    else:
        for dev in devices:
            print(f"\nDevice from {dev.src_ip}:{dev.src_port}")
            print(f"  IP:          {dev.ip}")
            print(f"  Port (TCP):  {dev.tcp_port}")
            print(f"  MAC:         {dev.mac}")
            print(f"  Netmask:     {dev.netmask}")
            print(f"  Gateway:     {dev.gateway}")
            print(f"  DeviceType:  {dev.device_type:#06x}")
            print(f"  ConfigTime:  {dev.config_time}")
            print(f"  APIVersion:  {dev.api_version}")
            print(f"  SeqNum:      {dev.seq_num}")
            if args.debug:
                print(f"  Raw ({len(dev.raw)} bytes):")
                print(_hexdump(dev.raw))

    print()
    print("=" * len(banner))
    print(f"Done. {len(devices)} device(s) found.")
    return 0 if devices else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
