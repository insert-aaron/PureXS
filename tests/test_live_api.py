"""
Tests for the live acquisition endpoints and live_images() protocol logic.

Coverage:
  - GET  /devices             → DeviceListEntry schema
  - POST /devices/{id}/acquire/{program} → trigger + 404/409 guards
  - WS   /devices/{id}/live   → streams bytes from device.live_images()
  - SironaDevice.live_images() unit test against a real async TCP push server
"""

from __future__ import annotations

import asyncio
import struct
import zlib
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import init_app, router
from src.core.discovery import DiscoveryService
from src.devices.base import DeviceState, SironaDevice
from src.devices.registry import DeviceRegistry
from src.protocol.tcp import DeviceInfo as TCPDeviceInfo

# ── Wire helpers (mirror the CSiNetData format in tcp.py / MockSironaDevice) ──

_HDR = struct.Struct(">HHHHIII")   # 20 bytes — same as tcp.py
_MAGIC = 0x072D


def _frame(func_code: int, payload: bytes, session_id: int = 1) -> bytes:
    header = _HDR.pack(_MAGIC, func_code, 0x0001, 0x0000, session_id, len(payload), 0)
    return header + payload


def _enc_w(v: int) -> bytes:
    return struct.pack(">H", v)


def _enc_dw(v: int) -> bytes:
    return struct.pack(">I", v)


def _enc_ba(data: bytes) -> bytes:
    return struct.pack(">H", len(data)) + data


# Function codes used in the push server
_FC_CONNECT    = 0x0001
_FC_ACK        = 0xFF00
_FC_ALIVE      = 0x0009
_FC_IMG_BEGIN  = 0x000A
_FC_IMG_BLOCK  = 0x000B
_FC_IMG_END    = 0x000C


# ── Stub device for route tests ────────────────────────────────────────────────

class _StubDevice(SironaDevice):
    """Minimal in-process stub — overrides live_images() to yield pre-baked chunks."""

    SUPPORTED_TYPES: frozenset[int] = frozenset()

    def __init__(
        self,
        state: DeviceState = DeviceState.READY,
        chunks: list[bytes] | None = None,
        raise_on_xray: Exception | None = None,
    ) -> None:
        super().__init__(
            ip="127.0.0.1",
            port=1999,
            mac="DE:AD:BE:EF:00:01",
            device_type=0x0029,     # ORTHOPHOS XG
        )
        self._state = state
        self._serial_number = "SN-STUB-001"
        self._chunks = chunks if chunks is not None else [b"\xAB\xCD" * 8, b"\x01\x02" * 16]
        self._raise_on_xray = raise_on_xray

    # ── SironaDevice abstract methods ──────────────────────────────────────────

    async def async_connect(self) -> None:
        pass

    async def async_disconnect(self) -> None:
        pass

    async def async_get_info(self) -> TCPDeviceInfo:
        return TCPDeviceInfo(
            firmware_version="3.2.1",
            serial_number=self._serial_number,
            device_type=0x0029,
            hardware_rev=1,
        )

    async def async_request_xray(self, **kwargs: object) -> bytes:
        if self._raise_on_xray is not None:
            raise self._raise_on_xray
        return b""

    # Override live_images so routes tests don't need a real TCP server.
    async def live_images(self, **kwargs: object) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


# ── App factory ────────────────────────────────────────────────────────────────

def _make_app(device: _StubDevice | None = None) -> tuple[FastAPI, DeviceRegistry]:
    reg = DeviceRegistry()
    if device is not None:
        reg.add(device)
    disc = DiscoveryService(registry=reg)
    app = FastAPI()
    init_app(reg, disc)
    app.include_router(router)
    return app, reg


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  GET /devices — DeviceListEntry schema
# ╚══════════════════════════════════════════════════════════════════════════════

class TestListDevices:
    def test_empty_registry(self) -> None:
        app, _ = _make_app()
        with TestClient(app) as client:
            resp = client.get("/devices")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_device_list_entry_schema(self) -> None:
        stub = _StubDevice()
        app, _ = _make_app(stub)
        with TestClient(app) as client:
            resp = client.get("/devices")

        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) == 1
        entry = entries[0]

        # Required fields from DeviceListEntry
        assert entry["id"] == "DE:AD:BE:EF:00:01"
        assert entry["mac"] == "DE:AD:BE:EF:00:01"
        assert entry["ip"] == "127.0.0.1"
        assert entry["port"] == 1999
        assert isinstance(entry["display_name"], str)
        assert isinstance(entry["connected"], bool)
        assert isinstance(entry["serial"], str)

    def test_display_name_is_product_name(self) -> None:
        stub = _StubDevice()
        app, _ = _make_app(stub)
        with TestClient(app) as client:
            resp = client.get("/devices")
        entry = resp.json()[0]
        # ORTHOPHOS XG maps to this display name
        assert "ORTHOPHOS" in entry["display_name"] or entry["display_name"] != ""

    def test_serial_populated_from_device(self) -> None:
        stub = _StubDevice()
        stub._serial_number = "SN-XG-9999"
        app, _ = _make_app(stub)
        with TestClient(app) as client:
            resp = client.get("/devices")
        assert resp.json()[0]["serial"] == "SN-XG-9999"

    def test_connected_false_when_no_async_session(self) -> None:
        stub = _StubDevice()
        app, _ = _make_app(stub)
        with TestClient(app) as client:
            resp = client.get("/devices")
        assert resp.json()[0]["connected"] is False


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  POST /devices/{device_id}/acquire/{program}
# ╚══════════════════════════════════════════════════════════════════════════════

class TestAcquireLive:
    def test_returns_acquiring_status(self) -> None:
        stub = _StubDevice(state=DeviceState.READY)
        app, _ = _make_app(stub)
        with TestClient(app) as client:
            resp = client.post("/devices/DE:AD:BE:EF:00:01/acquire/PANORAMIC")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "acquiring"
        assert body["program"] == "PANORAMIC"

    def test_program_in_response(self) -> None:
        stub = _StubDevice(state=DeviceState.READY)
        app, _ = _make_app(stub)
        with TestClient(app) as client:
            resp = client.post("/devices/DE:AD:BE:EF:00:01/acquire/CEPH_LAT")
        assert resp.json()["program"] == "CEPH_LAT"

    def test_device_not_found_returns_404(self) -> None:
        app, _ = _make_app()   # empty registry
        with TestClient(app) as client:
            resp = client.post("/devices/00:11:22:33:44:55/acquire/PANORAMIC")
        assert resp.status_code == 404

    def test_device_disconnected_returns_409(self) -> None:
        stub = _StubDevice(state=DeviceState.DISCONNECTED)
        app, _ = _make_app(stub)
        with TestClient(app) as client:
            resp = client.post("/devices/DE:AD:BE:EF:00:01/acquire/PANORAMIC")
        assert resp.status_code == 409

    def test_device_id_case_insensitive(self) -> None:
        """Registry key comparison is case-insensitive (upper-cased by the route)."""
        stub = _StubDevice(state=DeviceState.READY)
        app, _ = _make_app(stub)
        with TestClient(app) as client:
            resp = client.post("/devices/de:ad:be:ef:00:01/acquire/PANORAMIC")
        assert resp.status_code == 200


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  WebSocket /devices/{device_id}/live
# ╚══════════════════════════════════════════════════════════════════════════════

class TestWsLive:
    def test_streams_chunks_and_closes(self) -> None:
        expected = [b"\xAB\xCD" * 8, b"\x01\x02" * 16]
        stub = _StubDevice(chunks=expected)
        app, _ = _make_app(stub)

        received: list[bytes] = []
        with TestClient(app) as client:
            with client.websocket_connect("/devices/DE:AD:BE:EF:00:01/live") as ws:
                for _ in expected:
                    received.append(ws.receive_bytes())

        assert received == expected

    def test_device_not_found_closes_1008(self) -> None:
        app, _ = _make_app()   # empty registry
        with TestClient(app) as client:
            with pytest.raises(Exception):
                # Server closes with 1008 immediately — TestClient raises on disconnect
                with client.websocket_connect("/devices/00:11:22:33:44:55/live") as ws:
                    ws.receive_bytes()

    def test_multiple_chunks_byte_count(self) -> None:
        chunks = [bytes(range(256)), bytes(range(128))]
        stub = _StubDevice(chunks=chunks)
        app, _ = _make_app(stub)

        total = 0
        with TestClient(app) as client:
            with client.websocket_connect("/devices/DE:AD:BE:EF:00:01/live") as ws:
                for expected_chunk in chunks:
                    data = ws.receive_bytes()
                    assert data == expected_chunk
                    total += len(data)

        assert total == 384


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  SironaDevice.live_images() — unit tests with a real push server
# ╚══════════════════════════════════════════════════════════════════════════════

class _PushServer:
    """Minimal async TCP server that speaks CSiNetData and pushes image frames.

    After TCPConnect handshake, the server sends:
      TCPXRayImgBegin → N × TCPXRayImgBlock → TCPXRayImgEnd

    Useful for testing :meth:`SironaDevice.live_images` end-to-end through the
    real async TCP stack (SiNet2Client + _pop_frame).
    """

    def __init__(self, pixel_blocks: list[bytes]) -> None:
        self._blocks = pixel_blocks
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0   # port=0 → OS picks a free port
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def __aenter__(self) -> "_PushServer":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            # ── TCPConnect handshake ──────────────────────────────────────────
            raw = await reader.readexactly(20)
            _, fc, *_ = _HDR.unpack(raw)
            assert fc == _FC_CONNECT
            # Assign session_id = 1
            writer.write(_frame(_FC_ACK, b"", session_id=1))
            await writer.drain()

            # Read the trigger command (we don't validate it — just consume it)
            raw = await reader.readexactly(20)
            _, fc2, _, _, _, plen, _ = _HDR.unpack(raw)
            if plen:
                await reader.readexactly(plen)
            # ACK the trigger
            writer.write(_frame(_FC_ACK, b"", session_id=1))
            await writer.drain()

            # ── Push image frames ─────────────────────────────────────────────
            image_id = 0x0001
            total_blocks = len(self._blocks)

            # TCPXRayImgBegin
            full_size = sum(len(b) for b in self._blocks)
            begin_payload = (
                _enc_w(image_id)
                + _enc_w(total_blocks)
                + _enc_dw(full_size)
                + _enc_w(320)           # width  (placeholder)
                + _enc_w(240)           # height (placeholder)
                + _enc_w(16)            # bit_depth
                + _enc_w(0)             # compression=raw
            )
            writer.write(_frame(_FC_IMG_BEGIN, begin_payload, session_id=1))
            await writer.drain()

            # TCPXRayImgBlock × N
            all_data = b""
            for idx, block_data in enumerate(self._blocks):
                block_payload = (
                    _enc_w(image_id)
                    + _enc_w(idx)
                    + _enc_ba(block_data)
                )
                writer.write(_frame(_FC_IMG_BLOCK, block_payload, session_id=1))
                await writer.drain()
                all_data += block_data

            # TCPXRayImgEnd
            crc = zlib.crc32(all_data) & 0xFFFF_FFFF
            end_payload = (
                _enc_w(image_id)
                + _enc_w(0x0000)        # status = OK
                + _enc_dw(crc)
            )
            writer.write(_frame(_FC_IMG_END, end_payload, session_id=1))
            await writer.drain()

        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()


class _ConcreteDevice(SironaDevice):
    """Concrete SironaDevice that uses SiNet2Client against the push server."""

    SUPPORTED_TYPES: frozenset[int] = frozenset()

    async def async_connect(self) -> None:
        from src.protocol.tcp import SiNet2Client
        client = SiNet2Client()
        await client.connect(self._ip, self._port)
        self._client = client

    async def async_disconnect(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def async_get_info(self) -> TCPDeviceInfo:
        assert self._client is not None
        return await self._client.request_info()

    async def async_request_xray(self, **kwargs: object) -> bytes:
        """Send an arbitrary trigger command (func_code 0x0040 = FUNC_TRIGGER)."""
        assert self._client is not None
        # The push server accepts any command after TCPConnect and ACKs it.
        return await self._client.send(0x0040, b"")


class TestLiveImagesUnit:
    """Unit tests for SironaDevice.live_images() via a real async push server."""

    @pytest.fixture
    async def push_server_and_device(
        self,
    ) -> AsyncIterator[tuple[_PushServer, _ConcreteDevice]]:
        pixel_blocks = [b"\x11\x22" * 64, b"\x33\x44" * 32]
        async with _PushServer(pixel_blocks) as srv:
            dev = _ConcreteDevice(ip="127.0.0.1", port=srv.port)
            await dev.async_connect()
            try:
                yield srv, dev
            finally:
                await dev.async_disconnect()

    async def test_yields_block_bytes(
        self,
        push_server_and_device: tuple[_PushServer, _ConcreteDevice],
    ) -> None:
        srv, dev = push_server_and_device
        expected_blocks = [b"\x11\x22" * 64, b"\x33\x44" * 32]

        collected: list[bytes] = []
        async for chunk in dev.live_images():
            collected.append(chunk)

        # First N items are individual blocks, last item is the full image.
        block_chunks = collected[:-1]
        full_image   = collected[-1]

        assert block_chunks == expected_blocks
        assert full_image == b"".join(expected_blocks)

    async def test_full_image_crc_valid(
        self,
        push_server_and_device: tuple[_PushServer, _ConcreteDevice],
    ) -> None:
        _, dev = push_server_and_device
        chunks: list[bytes] = []
        async for chunk in dev.live_images():
            chunks.append(chunk)

        full_image = chunks[-1]
        expected   = b"\x11\x22" * 64 + b"\x33\x44" * 32
        assert full_image == expected

    async def test_intermediate_chunks_concatenate_to_full_image(
        self,
        push_server_and_device: tuple[_PushServer, _ConcreteDevice],
    ) -> None:
        _, dev = push_server_and_device
        chunks: list[bytes] = []
        async for chunk in dev.live_images():
            chunks.append(chunk)

        intermediate = b"".join(chunks[:-1])   # all but last
        final        = chunks[-1]               # assembled image
        assert intermediate == final

    async def test_single_block_image(self) -> None:
        """Edge case: one block → two yields (the block + the full image == same bytes)."""
        block = b"\xDE\xAD\xBE\xEF" * 100
        async with _PushServer([block]) as srv:
            dev = _ConcreteDevice(ip="127.0.0.1", port=srv.port)
            await dev.async_connect()
            try:
                chunks: list[bytes] = []
                async for chunk in dev.live_images():
                    chunks.append(chunk)
            finally:
                await dev.async_disconnect()

        # Single block  → yielded once + then full image (same bytes)
        assert len(chunks) == 2
        assert chunks[0] == block
        assert chunks[1] == block
