"""
Mock SiNet2 / P2K device for unit and integration testing.

Runs a real UDP socket on localhost and optionally a TCP server, allowing
tests to exercise the full protocol stack without physical hardware.

Two implementations are provided:

  MockSironaDevice  — async implementation using the confirmed CSiNetData
                      TCP header format (``">HHHHIII"``, 20 bytes) from
                      tcp.py.  Proactively broadcasts UDP discovery frames
                      and responds to TCPConnect / TCPReqInfo / TCPAliveData
                      / TCPDisconnect.  Use this for testing SiNet2Client and
                      OrthophosXG against the new protocol stack.

  MockUDPDevice / MockTCPDevice / MockDevice  — legacy threaded mocks kept
                      for backward compatibility with test_protocol.py.  They
                      speak the older packets.py wire format (4-byte DWORD
                      magic in the TCP header) and are not intended for use
                      with the async SiNet2Client driver.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import threading
import time
from typing import Callable

from src.protocol.constants import (
    DEFAULT_PORT,
    FUNC_ACK,
    FUNC_CONNECT,
    FUNC_DISCONNECT,
    FUNC_DISCOVER,
    FUNC_ERROR,
    FUNC_GET_IMAGE,
    FUNC_GET_PARAM,
    FUNC_SET_PARAM,
    FUNC_STATUS,
    FUNC_TRIGGER,
    MAGIC,
    TCP_HEADER_SIZE,
    UDP_HEADER_SIZE,
)
from src.protocol.packets import (
    build_tcp_header,
    build_udp_header,
    decode_ba,
    decode_s,
    decode_w,
    encode_ba,
    encode_s,
    encode_w,
    parse_tcp_header,
    parse_udp_header,
)

log = logging.getLogger(__name__)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  MockSironaDevice  — async, new protocol stack (tcp.py CSiNetData format)
# ╚══════════════════════════════════════════════════════════════════════════════

class MockSironaDevice:
    """Async mock SiNet2 / P2K device for testing the new protocol stack.

    Proactively broadcasts a UDP discovery frame every *broadcast_interval*
    seconds (configurable; disable by setting to ``0``).  Also accepts
    incoming UDP probes and replies immediately.

    On the TCP side the mock accepts connections and handles:

    - ``TCPConnect``    (0x0001) → ``TCPAck`` (0xFF00) with assigned session ID
    - ``TCPReqInfo``   (0x0003) → ``TCPInfo`` (0x0004) with configured device info
    - ``TCPAliveData`` (0x0009) → ``TCPAliveData`` echo (keepalive)
    - ``TCPDisconnect``(0x0002) → ``TCPAck`` then close
    - All other codes           → ``TCPError`` (0xFF01) with UNKNOWN_COMMAND

    The TCP frame format is the **confirmed** CSiNetData layout::

        struct.Struct(">HHHHIII")   # 20 bytes, big-endian
        WORD  magic          = 0x072D
        WORD  func_code
        WORD  api_version    = 0x0001
        WORD  api_revision   = 0x0000
        DWORD session_id
        DWORD payload_length
        DWORD reserved       = 0x00000000

    TCP S-type payload fields use a **2-byte WORD** char-count prefix (not
    the 4-byte DWORD used in UDP payloads).

    Usage (context manager)::

        async with MockSironaDevice(port=19991, udp_port=19990) as mock:
            client = SiNet2Client()
            await client.connect("127.0.0.1", mock.port)
            info = await client.request_info()
            assert info.serial_number == "SN-TEST-001"

    Usage (manual lifecycle)::

        mock = MockSironaDevice()
        await mock.start()
        try:
            ...
        finally:
            await mock.stop()
    """

    # ── CSiNetData TCP header — confirmed wire format from tcp.py ─────────────
    _HDR_STRUCT: struct.Struct = struct.Struct(">HHHHIII")  # 20 bytes
    _HDR_SIZE: int = _HDR_STRUCT.size
    _MAGIC: int = 0x072D
    _API_VERSION: int = 0x0001
    _API_REVISION: int = 0x0000

    # TCP function codes (Netapi114.xml names)
    _FC_CONNECT: int = 0x0001     # TCPConnect
    _FC_DISCONNECT: int = 0x0002  # TCPDisconnect
    _FC_REQ_INFO: int = 0x0003    # TCPReqInfo
    _FC_INFO: int = 0x0004        # TCPInfo
    _FC_ALIVE: int = 0x0009       # TCPAliveData
    _FC_ACK: int = 0xFF00         # TCPAck
    _FC_ERROR: int = 0xFF01       # TCPError

    def __init__(
        self,
        ip: str = "127.0.0.1",
        port: int = 19991,
        udp_port: int = 19990,
        device_type: int = 0x0029,        # DX41 = ORTHOPHOS XG
        serial_number: str = "SN-TEST-001",
        firmware_version: str = "3.2.1",
        hardware_rev: int = 1,
        broadcast_interval: float = 5.0,
        broadcast_addr: str = "127.0.0.1",
        mac: bytes = b"\xDE\xAD\xBE\xEF\x00\x01",
        gateway: bytes = b"\x7F\x00\x00\x01",
        netmask: bytes = b"\xFF\x00\x00\x00",
    ) -> None:
        self.ip = ip
        self.port = port
        self.udp_port = udp_port
        self.device_type = device_type
        self.serial_number = serial_number
        self.firmware_version = firmware_version
        self.hardware_rev = hardware_rev
        self.broadcast_interval = broadcast_interval
        self.broadcast_addr = broadcast_addr
        self.mac = mac
        self.gateway = gateway
        self.netmask = netmask

        self._session_counter: int = 0
        self._server: asyncio.Server | None = None
        self._tasks: list[asyncio.Task] = []

        # Observability counters for test assertions
        self.connections_accepted: int = 0
        self.commands_received: list[tuple[int, bytes]] = []

    # ── async context manager ─────────────────────────────────────────────────

    async def __aenter__(self) -> "MockSironaDevice":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the TCP server and UDP broadcast/listener tasks."""
        self._server = await asyncio.start_server(
            self._handle_tcp,
            self.ip,
            self.port,
        )
        udp_task = asyncio.create_task(
            self._run_udp(), name="mock-udp"
        )
        self._tasks.append(udp_task)

    async def stop(self) -> None:
        """Shut down all server tasks and close all sockets."""
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ── UDP: listener + periodic broadcaster ──────────────────────────────────

    async def _run_udp(self) -> None:
        """Bind a UDP socket, respond to probes, and send periodic broadcasts."""
        loop = asyncio.get_running_loop()

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.udp_port))
        sock.setblocking(False)

        try:
            next_broadcast = (
                loop.time() + self.broadcast_interval
                if self.broadcast_interval > 0 else float("inf")
            )

            while True:
                now = loop.time()
                deadline = min(next_broadcast, now + 0.2)
                remaining = max(deadline - now, 0.0)

                try:
                    async with asyncio.timeout(remaining):
                        data, addr = await loop.sock_recvfrom(sock, 4096)
                    self._handle_udp_packet(sock, data, addr)
                except TimeoutError:
                    pass

                if loop.time() >= next_broadcast and self.broadcast_interval > 0:
                    frame = self._build_udp_response(seq_num=0)
                    try:
                        await loop.sock_sendto(
                            sock, frame, (self.broadcast_addr, self.udp_port)
                        )
                        log.info(
                            "UDP broadcast → %s:%d  (device_type=0x%04X)",
                            self.broadcast_addr, self.udp_port, self.device_type,
                        )
                    except OSError as exc:
                        log.debug("UDP broadcast error: %s", exc)
                    next_broadcast = loop.time() + self.broadcast_interval

        finally:
            sock.close()

    def _handle_udp_packet(
        self,
        sock: socket.socket,
        data: bytes,
        addr: tuple[str, int],
    ) -> None:
        """Parse an incoming UDP packet and reply to discovery probes."""
        try:
            hdr = parse_udp_header(data)
        except ValueError:
            return

        if hdr["func_code"] == FUNC_DISCOVER:
            response = self._build_udp_response(seq_num=hdr["seq_num"])
            try:
                sock.sendto(response, addr)
                log.info("UDP probe from %s:%d → replied", addr[0], addr[1])
            except OSError as exc:
                log.debug("UDP reply error: %s", exc)

    def _build_udp_response(self, seq_num: int = 0) -> bytes:
        """Encode a UDP discovery response frame."""
        ip_bytes = socket.inet_aton(self.ip)
        payload = (
            encode_s(str(self.port))        # NameP2K = TCP port as ASCII
            + encode_ba(self.gateway)
            + encode_ba(self.netmask)
            + encode_ba(ip_bytes)
            + encode_ba(self.mac)
            + encode_w(0)                   # config_time
            + encode_w(self.device_type)
        )
        return build_udp_header(FUNC_DISCOVER, payload, seq_num=seq_num)

    # ── TCP: per-connection handler ───────────────────────────────────────────

    async def _handle_tcp(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle one incoming TCP connection for its full lifetime."""
        self.connections_accepted += 1
        self._session_counter += 1
        session_id = self._session_counter
        peer = writer.get_extra_info("peername", ("?", 0))
        log.info(
            "TCP connect from %s:%d  sid=0x%08X",
            peer[0], peer[1], session_id,
        )

        try:
            while True:
                try:
                    raw_hdr = await reader.readexactly(self._HDR_SIZE)
                except asyncio.IncompleteReadError:
                    break  # client disconnected cleanly

                (
                    magic, func_code, _api_version, _api_revision,
                    _client_sid, payload_len, _reserved,
                ) = self._HDR_STRUCT.unpack(raw_hdr)

                if magic != self._MAGIC:
                    log.warning(
                        "Bad TCP magic 0x%04X from %s:%d — closing",
                        magic, peer[0], peer[1],
                    )
                    break

                payload = b""
                if payload_len:
                    try:
                        payload = await reader.readexactly(payload_len)
                    except asyncio.IncompleteReadError:
                        break

                log.info(
                    "← 0x%04X  payload=%d B  from %s:%d",
                    func_code, len(payload), peer[0], peer[1],
                )
                self.commands_received.append((func_code, payload))

                response = self._dispatch(func_code, payload, session_id)
                if response is not None:
                    writer.write(response)
                    await writer.drain()

                if func_code == self._FC_DISCONNECT:
                    break

        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            log.info("TCP closed  %s:%d  sid=0x%08X", peer[0], peer[1], session_id)

    def _dispatch(
        self, func_code: int, _payload: bytes, session_id: int
    ) -> bytes | None:
        """Map an incoming func_code to the appropriate response frame."""
        if func_code == self._FC_CONNECT:
            return self._build_frame(self._FC_ACK, b"", session_id=session_id)

        if func_code == self._FC_DISCONNECT:
            return self._build_frame(self._FC_ACK, b"", session_id=session_id)

        if func_code == self._FC_REQ_INFO:
            info_payload = (
                self._enc_s(self.firmware_version)
                + self._enc_s(self.serial_number)
                + self._enc_w(self.device_type)
                + self._enc_w(self.hardware_rev)
            )
            return self._build_frame(self._FC_INFO, info_payload, session_id=session_id)

        if func_code == self._FC_ALIVE:
            # Echo the keepalive back
            return self._build_frame(self._FC_ALIVE, b"", session_id=session_id)

        # Unknown command → TCPError with UNKNOWN_COMMAND (0x0001)
        error_payload = self._enc_w(0x0001)
        return self._build_frame(self._FC_ERROR, error_payload, session_id=session_id)

    # ── frame / field helpers (CSiNetData format) ─────────────────────────────

    def _build_frame(
        self, func_code: int, payload: bytes, *, session_id: int = 0
    ) -> bytes:
        """Pack a 20-byte CSiNetData header followed by *payload*."""
        header = self._HDR_STRUCT.pack(
            self._MAGIC,
            func_code,
            self._API_VERSION,
            self._API_REVISION,
            session_id,
            len(payload),
            0,            # reserved
        )
        return header + payload

    @staticmethod
    def _enc_w(value: int) -> bytes:
        """W-type: 2-byte big-endian WORD."""
        return struct.pack(">H", value)

    @staticmethod
    def _enc_s(text: str) -> bytes:
        """TCP S-type: 2-byte BE WORD char-count + UTF-16LE data.

        Note: TCP uses a 2-byte WORD prefix; UDP uses a 4-byte DWORD prefix.
        """
        encoded = text.encode("utf-16-le")
        return struct.pack(">H", len(text)) + encoded


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  MockUDPDevice  — legacy threaded UDP mock (packets.py format)
# ╚══════════════════════════════════════════════════════════════════════════════

class MockUDPDevice(threading.Thread):
    """Listens on a UDP port and responds to SiNet2 discovery probes.

    Usage::

        with MockUDPDevice(port=19990, device_type=0x0001) as dev:
            # discovery can now find the mock on 127.0.0.1
            ...
    """

    def __init__(
        self,
        port: int = 19990,
        tcp_port: int = 19991,
        device_type: int = 0x0001,
        ip: str = "127.0.0.1",
        mac: bytes = b"\xDE\xAD\xBE\xEF\x00\x01",
        gateway: bytes = b"\x7F\x00\x00\x01",
        netmask: bytes = b"\xFF\x00\x00\x00",
        config_time: int = 42,
    ) -> None:
        super().__init__(name="mock-udp-device", daemon=True)
        self.port = port
        self.tcp_port = tcp_port
        self.device_type = device_type
        self.ip_bytes = socket.inet_aton(ip)
        self.mac = mac
        self.gateway = gateway
        self.netmask = netmask
        self.config_time = config_time
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self.packets_received: list[bytes] = []

    def __enter__(self) -> "MockUDPDevice":
        self.start()
        time.sleep(0.05)  # let the thread bind
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            self._sock.close()

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(0.2)
        self._sock = sock

        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            self.packets_received.append(data)
            try:
                hdr = parse_udp_header(data)
            except ValueError:
                continue

            if hdr["func_code"] == FUNC_DISCOVER:
                response = self._build_discovery_response(hdr["seq_num"])
                sock.sendto(response, addr)

    def _build_discovery_response(self, seq_num: int) -> bytes:
        name_p2k = str(self.tcp_port)
        payload = (
            encode_s(name_p2k)
            + encode_ba(self.gateway)
            + encode_ba(self.netmask)
            + encode_ba(self.ip_bytes)
            + encode_ba(self.mac)
            + encode_w(self.config_time)
            + encode_w(self.device_type)
        )
        return build_udp_header(FUNC_DISCOVER, payload, seq_num=seq_num)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  MockTCPDevice  — legacy threaded TCP mock (packets.py format)
# ╚══════════════════════════════════════════════════════════════════════════════

_SESSION_COUNTER = 0
_SESSION_LOCK = threading.Lock()


def _next_session_id() -> int:
    global _SESSION_COUNTER
    with _SESSION_LOCK:
        _SESSION_COUNTER += 1
        return _SESSION_COUNTER


class MockTCPDevice(threading.Thread):
    """Accepts TCP connections and speaks the 20-byte P2K frame protocol.

    Supports FUNC_CONNECT, FUNC_DISCONNECT, FUNC_STATUS, FUNC_GET_PARAM,
    FUNC_SET_PARAM, FUNC_TRIGGER, FUNC_GET_IMAGE.

    Arbitrary image data and params can be injected via the constructor.

    Usage::

        with MockTCPDevice(port=19991) as srv:
            with TCPSession("127.0.0.1", 19991) as sess:
                sess.connect()
                status = sess.get_status()
    """

    def __init__(
        self,
        port: int = 19991,
        image_data: bytes = b"\x00" * 256,
        params: dict[int, bytes] | None = None,
        status_code: int = 0x0000,
        on_trigger: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(name="mock-tcp-device", daemon=True)
        self.port = port
        self.image_data = image_data
        self.params: dict[int, bytes] = params or {}
        self.status_code = status_code
        self.on_trigger = on_trigger
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        self.connections_accepted: int = 0

    def __enter__(self) -> "MockTCPDevice":
        self.start()
        time.sleep(0.05)
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            self._server.close()

    def run(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(5)
        srv.settimeout(0.2)
        self._server = srv

        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connections_accepted += 1
            t = threading.Thread(
                target=self._handle,
                args=(conn,),
                daemon=True,
            )
            t.start()

    def _handle(self, conn: socket.socket) -> None:
        session_id = _next_session_id()
        conn.settimeout(5.0)
        try:
            while not self._stop.is_set():
                try:
                    header_data = self._recv_exactly(conn, TCP_HEADER_SIZE)
                except OSError:
                    break

                hdr = parse_tcp_header(header_data)
                payload = (
                    self._recv_exactly(conn, hdr["payload_len"])
                    if hdr["payload_len"]
                    else b""
                )

                response = self._dispatch(hdr["func_code"], payload, session_id)
                if response is not None:
                    conn.sendall(response)

                if hdr["func_code"] == FUNC_DISCONNECT:
                    break
        finally:
            conn.close()

    def _dispatch(
        self, func_code: int, payload: bytes, session_id: int
    ) -> bytes | None:
        if func_code == FUNC_CONNECT:
            # ACK with session_id assigned
            return build_tcp_header(FUNC_ACK, b"", session_id=session_id)

        if func_code == FUNC_DISCONNECT:
            return build_tcp_header(FUNC_ACK, b"", session_id=session_id)

        if func_code == FUNC_STATUS:
            return build_tcp_header(
                FUNC_ACK, encode_w(self.status_code), session_id=session_id
            )

        if func_code == FUNC_GET_PARAM and len(payload) >= 2:
                param_id, _ = decode_w(payload, 0)
                value = self.params.get(param_id, b"\x00\x00")
                return build_tcp_header(
                    FUNC_ACK, encode_w(param_id) + value, session_id=session_id
                )

        if func_code == FUNC_SET_PARAM:
            if len(payload) >= 2:
                param_id, n = decode_w(payload, 0)
                self.params[param_id] = payload[n:]
            return build_tcp_header(FUNC_ACK, b"", session_id=session_id)

        if func_code == FUNC_TRIGGER:
            if self.on_trigger:
                self.on_trigger()
            return build_tcp_header(FUNC_ACK, b"", session_id=session_id)

        if func_code == FUNC_GET_IMAGE:
            return build_tcp_header(
                FUNC_ACK, self.image_data, session_id=session_id
            )

        # Unknown command → error
        return build_tcp_header(
            FUNC_ERROR, encode_w(0x0001), session_id=session_id
        )

    @staticmethod
    def _recv_exactly(conn: socket.socket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError(f"Connection closed after {len(buf)}/{n} bytes")
            buf.extend(chunk)
        return bytes(buf)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  MockDevice  — legacy combined mock (UDP + TCP, packets.py format)
# ╚══════════════════════════════════════════════════════════════════════════════

class MockDevice:
    """Convenience wrapper that starts both a UDP and TCP mock on the same host.

    Usage::

        with MockDevice(udp_port=19990, tcp_port=19991) as mock:
            with UDPDiscovery(listen_port=19990, target_port=19990) as disc:
                responses = disc.scan(timeout=1.0)
            assert len(responses) == 1
    """

    def __init__(
        self,
        udp_port: int = 19990,
        tcp_port: int = 19991,
        device_type: int = 0x0001,
        image_data: bytes = b"\xAB\xCD" * 128,
    ) -> None:
        self.udp = MockUDPDevice(
            port=udp_port,
            tcp_port=tcp_port,
            device_type=device_type,
        )
        self.tcp = MockTCPDevice(
            port=tcp_port,
            image_data=image_data,
        )

    def __enter__(self) -> "MockDevice":
        self.tcp.__enter__()
        self.udp.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        self.udp.__exit__(*args)
        self.tcp.__exit__(*args)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  __main__  — standalone async mock server
# ╚══════════════════════════════════════════════════════════════════════════════

async def _main(argv: list[str]) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m tests.mock_device",
        description=(
            "Run an async SiNet2/P2K mock device server.  "
            "Broadcasts UDP discovery every --broadcast-interval seconds "
            "and accepts TCP connections on --port."
        ),
    )
    parser.add_argument(
        "--ip",
        default="127.0.0.1",
        metavar="ADDR",
        help="IP address to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=19991,
        metavar="PORT",
        help="TCP listen port (default: 19991)",
    )
    parser.add_argument(
        "--udp-port",
        type=int,
        default=19990,
        metavar="PORT",
        help="UDP listen/broadcast port (default: 19990)",
    )
    parser.add_argument(
        "--device-type",
        type=lambda s: int(s, 0),
        default=0x0029,
        metavar="HEX",
        help="DeviceType WORD, e.g. 0x0029 for ORTHOPHOS XG (default: 0x0029)",
    )
    parser.add_argument(
        "--serial",
        default="SN-TEST-001",
        metavar="SN",
        help="Serial number string returned in TCPInfo (default: SN-TEST-001)",
    )
    parser.add_argument(
        "--firmware",
        default="3.2.1",
        metavar="VER",
        help="Firmware version string returned in TCPInfo (default: 3.2.1)",
    )
    parser.add_argument(
        "--broadcast-interval",
        type=float,
        default=5.0,
        metavar="SEC",
        help="Seconds between proactive UDP broadcasts; 0 = disabled (default: 5.0)",
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    mock = MockSironaDevice(
        ip=args.ip,
        port=args.port,
        udp_port=args.udp_port,
        device_type=args.device_type,
        serial_number=args.serial,
        firmware_version=args.firmware,
        broadcast_interval=args.broadcast_interval,
    )

    await mock.start()

    print(
        f"MockSironaDevice running\n"
        f"  TCP  {args.ip}:{args.port}\n"
        f"  UDP  {args.ip}:{args.udp_port}  "
        f"(broadcast every {args.broadcast_interval:.0f}s)\n"
        f"  device_type=0x{args.device_type:04X}  "
        f"serial={args.serial!r}  fw={args.firmware!r}\n"
        f"Press Ctrl+C to stop."
    )

    try:
        # Run until interrupted
        while True:
            await asyncio.sleep(1.0)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nShutting down …")

    await mock.stop()
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
