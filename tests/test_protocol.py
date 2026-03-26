"""
Protocol-layer tests for PureXS.

Covers:
  - UDP header encoding / decoding round-trips
  - TCP header encoding / decoding round-trips
  - Payload field codecs (S, BA, W, DW)
  - DiscoveryResponse.from_wire() with a hand-crafted frame
  - Full UDP discovery against MockUDPDevice
  - Full TCP session against MockTCPDevice
  - Error frame handling
"""

import socket
import struct
import time

import pytest

from src.protocol.constants import (
    DEFAULT_PORT,
    FUNC_DISCOVER,
    FUNC_STATUS,
    FUNC_TRIGGER,
    MAGIC,
    TCP_HEADER_SIZE,
    UDP_HEADER_SIZE,
)
from src.protocol.packets import (
    DISCOVERY_PROBE,
    DiscoveryResponse,
    build_tcp_header,
    build_udp_header,
    decode_ba,
    decode_dw,
    decode_s,
    decode_w,
    encode_ba,
    encode_dw,
    encode_s,
    encode_w,
    parse_tcp_header,
    parse_udp_header,
)
from src.protocol.tcp import P2KError, TCPSession
from src.protocol.udp import UDPDiscovery

from .mock_device import MockDevice, MockTCPDevice, MockUDPDevice

# ── constants used across tests ───────────────────────────────────────────────

UDP_TEST_PORT = 29990
TCP_TEST_PORT = 29991


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  UNIT TESTS — packet codecs
# ╚══════════════════════════════════════════════════════════════════════════════


class TestUDPHeader:
    def test_probe_has_correct_magic(self):
        magic = struct.unpack_from("<H", DISCOVERY_PROBE, 0)[0]
        assert magic == MAGIC

    def test_probe_length(self):
        assert len(DISCOVERY_PROBE) == UDP_HEADER_SIZE

    def test_round_trip_empty_payload(self):
        frame = build_udp_header(FUNC_DISCOVER, b"", seq_num=7)
        hdr = parse_udp_header(frame)
        assert hdr["magic"] == MAGIC
        assert hdr["func_code"] == FUNC_DISCOVER
        assert hdr["seq_num"] == 7

    def test_round_trip_with_payload(self):
        payload = b"\x01\x02\x03\x04"
        frame = build_udp_header(FUNC_DISCOVER, payload, seq_num=99)
        hdr = parse_udp_header(frame)
        assert hdr["func_code"] == FUNC_DISCOVER
        assert frame[UDP_HEADER_SIZE:] == payload

    def test_bad_magic_raises(self):
        bad = bytearray(DISCOVERY_PROBE)
        bad[0] = 0xFF
        bad[1] = 0xFF
        with pytest.raises(ValueError, match="Bad magic"):
            parse_udp_header(bytes(bad))

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="too short"):
            parse_udp_header(b"\x00" * 4)


class TestTCPHeader:
    def test_round_trip_empty_payload(self):
        frame = build_tcp_header(FUNC_STATUS, b"", session_id=0xABCD, seq_num=1)
        assert len(frame) == TCP_HEADER_SIZE
        hdr = parse_tcp_header(frame)
        assert hdr["magic"] == MAGIC
        assert hdr["func_code"] == FUNC_STATUS
        assert hdr["session_id"] == 0xABCD
        assert hdr["seq_num"] == 1
        assert hdr["payload_len"] == 0

    def test_round_trip_with_payload(self):
        payload = b"\xDE\xAD\xBE\xEF"
        frame = build_tcp_header(FUNC_STATUS, payload, session_id=1)
        hdr = parse_tcp_header(frame)
        assert hdr["payload_len"] == 4
        assert frame[TCP_HEADER_SIZE:] == payload

    def test_big_endian_encoding(self):
        frame = build_tcp_header(0x0030, b"", session_id=0x00000001, seq_num=2)
        # magic at offset 0, big-endian 4 bytes → 0x0000072D
        magic_be = struct.unpack_from(">I", frame, 0)[0]
        assert magic_be == MAGIC

    def test_bad_magic_raises(self):
        frame = bytearray(build_tcp_header(FUNC_STATUS, b""))
        frame[0] = 0xFF
        with pytest.raises(ValueError, match="Bad TCP magic"):
            parse_tcp_header(bytes(frame))


class TestFieldCodecs:
    # S-type (UTF-16LE string)
    def test_s_round_trip_ascii(self):
        original = "1999"
        encoded = encode_s(original)
        decoded, consumed = decode_s(encoded, 0)
        assert decoded == original
        assert consumed == len(encoded)

    def test_s_round_trip_empty(self):
        encoded = encode_s("")
        decoded, consumed = decode_s(encoded, 0)
        assert decoded == ""
        assert consumed == 4  # just the 4-byte char count

    def test_s_char_count_is_char_not_byte(self):
        text = "AB"
        encoded = encode_s(text)
        char_count = struct.unpack_from(">I", encoded, 0)[0]
        assert char_count == 2  # 2 chars, not 4 bytes

    def test_s_truncated_raises(self):
        with pytest.raises(ValueError):
            decode_s(b"\x00\x00\x00\x0A", 0)  # claims 10 chars, no data

    # BA-type (raw bytes)
    def test_ba_round_trip_ip(self):
        ip = b"\xC0\xA8\x01\x01"
        encoded = encode_ba(ip)
        decoded, consumed = decode_ba(encoded, 0)
        assert decoded == ip
        assert consumed == 6  # 2-byte count + 4 bytes

    def test_ba_round_trip_mac(self):
        mac = b"\xDE\xAD\xBE\xEF\x00\x01"
        encoded = encode_ba(mac)
        decoded, consumed = decode_ba(encoded, 0)
        assert decoded == mac
        assert consumed == 8

    def test_ba_empty(self):
        encoded = encode_ba(b"")
        decoded, consumed = decode_ba(encoded, 0)
        assert decoded == b""
        assert consumed == 2

    # W-type (2-byte WORD)
    def test_w_round_trip(self):
        for val in (0, 1, 255, 0xFFFF):
            encoded = encode_w(val)
            assert len(encoded) == 2
            decoded, consumed = decode_w(encoded, 0)
            assert decoded == val
            assert consumed == 2

    # DW-type (4-byte DWORD)
    def test_dw_round_trip(self):
        for val in (0, 1, 0xDEADBEEF):
            encoded = encode_dw(val)
            assert len(encoded) == 4
            decoded, consumed = decode_dw(encoded, 0)
            assert decoded == val


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  UNIT TESTS — DiscoveryResponse.from_wire()
# ╚══════════════════════════════════════════════════════════════════════════════

def _make_discovery_frame(
    tcp_port: int = 1999,
    ip: str = "192.168.1.100",
    gateway: str = "192.168.1.1",
    netmask: str = "255.255.255.0",
    mac: bytes = b"\xAA\xBB\xCC\xDD\xEE\xFF",
    config_time: int = 7,
    device_type: int = 0x0001,
    seq_num: int = 1,
) -> bytes:
    ip_bytes = socket.inet_aton(ip)
    gw_bytes = socket.inet_aton(gateway)
    nm_bytes = socket.inet_aton(netmask)
    payload = (
        encode_s(str(tcp_port))
        + encode_ba(gw_bytes)
        + encode_ba(nm_bytes)
        + encode_ba(ip_bytes)
        + encode_ba(mac)
        + encode_w(config_time)
        + encode_w(device_type)
    )
    return build_udp_header(FUNC_DISCOVER, payload, seq_num=seq_num)


class TestDiscoveryResponse:
    def test_parses_ip(self):
        frame = _make_discovery_frame(ip="10.0.0.5")
        resp = DiscoveryResponse.from_wire(frame, "10.0.0.5", 1999)
        assert resp.ip == "10.0.0.5"

    def test_parses_tcp_port(self):
        frame = _make_discovery_frame(tcp_port=8888)
        resp = DiscoveryResponse.from_wire(frame, "1.2.3.4", 1999)
        assert resp.tcp_port == 8888

    def test_fallback_port_on_empty_name(self):
        # Build a frame with NameP2K = ""
        payload = (
            encode_s("")
            + encode_ba(b"\x00\x00\x00\x00")
            + encode_ba(b"\x00\x00\x00\x00")
            + encode_ba(b"\x7F\x00\x00\x01")
            + encode_ba(b"\x00" * 6)
            + encode_w(0)
            + encode_w(0x0001)
        )
        frame = build_udp_header(FUNC_DISCOVER, payload)
        resp = DiscoveryResponse.from_wire(frame, "127.0.0.1", 1999)
        assert resp.tcp_port == 1999  # fallback

    def test_parses_mac(self):
        mac = b"\xDE\xAD\xBE\xEF\x00\x01"
        frame = _make_discovery_frame(mac=mac)
        resp = DiscoveryResponse.from_wire(frame, "1.2.3.4", 1999)
        assert resp.mac == "DE:AD:BE:EF:00:01"

    def test_parses_device_type(self):
        frame = _make_discovery_frame(device_type=0x0003)
        resp = DiscoveryResponse.from_wire(frame, "1.2.3.4", 1999)
        assert resp.device_type == 0x0003

    def test_to_dict_has_required_keys(self):
        frame = _make_discovery_frame()
        resp = DiscoveryResponse.from_wire(frame, "1.2.3.4", 1999)
        d = resp.to_dict()
        for key in ("ip", "mac", "tcp_port", "device_type", "gateway", "netmask"):
            assert key in d, f"Missing key: {key}"

    def test_bad_magic_raises(self):
        frame = bytearray(_make_discovery_frame())
        frame[0] = 0xFF
        with pytest.raises(ValueError):
            DiscoveryResponse.from_wire(bytes(frame), "1.2.3.4", 1999)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  INTEGRATION TESTS — MockUDPDevice
# ╚══════════════════════════════════════════════════════════════════════════════

class TestUDPDiscoveryIntegration:
    def test_discovers_mock_device(self):
        with MockUDPDevice(port=UDP_TEST_PORT, tcp_port=TCP_TEST_PORT, device_type=0x0001):
            time.sleep(0.05)
            with UDPDiscovery(
                listen_port=UDP_TEST_PORT + 1,
                target_port=UDP_TEST_PORT,
                broadcast_addr="127.0.0.1",
            ) as disc:
                responses = disc.scan(timeout=1.0)

        assert len(responses) == 1
        r = responses[0]
        assert r.tcp_port == TCP_TEST_PORT
        assert r.device_type == 0x0001

    def test_scan_iter_yields_device(self):
        results = []
        with MockUDPDevice(port=UDP_TEST_PORT, tcp_port=TCP_TEST_PORT):
            time.sleep(0.05)
            with UDPDiscovery(
                listen_port=UDP_TEST_PORT + 1,
                target_port=UDP_TEST_PORT,
                broadcast_addr="127.0.0.1",
            ) as disc:
                for r in disc.scan_iter(timeout=1.0):
                    results.append(r)

        assert len(results) >= 1

    def test_no_response_returns_empty(self):
        with UDPDiscovery(
            listen_port=UDP_TEST_PORT + 2,
            target_port=UDP_TEST_PORT + 99,  # nothing listening
            broadcast_addr="127.0.0.1",
        ) as disc:
            responses = disc.scan(timeout=0.3)
        assert responses == []


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  INTEGRATION TESTS — MockTCPDevice / TCPSession
# ╚══════════════════════════════════════════════════════════════════════════════

class TestTCPSessionIntegration:
    def test_connect_returns_session_id(self):
        with MockTCPDevice(port=TCP_TEST_PORT) as srv:
            with TCPSession("127.0.0.1", TCP_TEST_PORT) as sess:
                sid = sess.connect()
        assert sid > 0

    def test_get_status(self):
        with MockTCPDevice(port=TCP_TEST_PORT, status_code=0x0000) as srv:
            with TCPSession("127.0.0.1", TCP_TEST_PORT) as sess:
                sess.connect()
                status = sess.get_status()
        assert status.get("status_code") == 0x0000

    def test_set_and_get_param(self):
        with MockTCPDevice(port=TCP_TEST_PORT) as srv:
            with TCPSession("127.0.0.1", TCP_TEST_PORT) as sess:
                sess.connect()
                sess.set_param(0x0010, encode_w(75))  # set kV = 75
                raw = sess.get_param(0x0010)
        val, _ = decode_w(raw, 0)
        assert val == 75

    def test_trigger(self):
        triggered = []
        with MockTCPDevice(port=TCP_TEST_PORT, on_trigger=lambda: triggered.append(1)) as srv:
            with TCPSession("127.0.0.1", TCP_TEST_PORT) as sess:
                sess.connect()
                sess.trigger()
        assert triggered == [1]

    def test_get_image_returns_data(self):
        image = b"\xFF\xD8\xFF\xE0" + b"\x00" * 252  # fake JPEG-like header
        with MockTCPDevice(port=TCP_TEST_PORT, image_data=image) as srv:
            with TCPSession("127.0.0.1", TCP_TEST_PORT) as sess:
                sess.connect()
                result = sess.get_image()
        assert result == image

    def test_unknown_command_raises_p2k_error(self):
        with MockTCPDevice(port=TCP_TEST_PORT) as srv:
            with TCPSession("127.0.0.1", TCP_TEST_PORT) as sess:
                sess.connect()
                with pytest.raises(P2KError) as exc_info:
                    # Send an unrecognised function code directly
                    sess._request(0x9999, b"")
        assert exc_info.value.error_code == 0x0001  # UNKNOWN_COMMAND

    def test_multiple_sequential_requests(self):
        with MockTCPDevice(port=TCP_TEST_PORT, status_code=0x0000) as srv:
            with TCPSession("127.0.0.1", TCP_TEST_PORT) as sess:
                sess.connect()
                for _ in range(5):
                    status = sess.get_status()
                    assert status.get("status_code") == 0x0000


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  INTEGRATION — full stack (MockDevice = UDP + TCP)
# ╚══════════════════════════════════════════════════════════════════════════════

class TestFullStackIntegration:
    def test_discover_then_connect(self):
        image_data = b"\xAB\xCD" * 64

        with MockDevice(
            udp_port=UDP_TEST_PORT,
            tcp_port=TCP_TEST_PORT,
            device_type=0x0001,
            image_data=image_data,
        ):
            time.sleep(0.05)

            # 1. Discover
            with UDPDiscovery(
                listen_port=UDP_TEST_PORT + 1,
                target_port=UDP_TEST_PORT,
                broadcast_addr="127.0.0.1",
            ) as disc:
                responses = disc.scan(timeout=1.0)

            assert len(responses) == 1
            resp = responses[0]
            assert resp.device_type == 0x0001

            # 2. Connect via TCP and acquire image
            with TCPSession("127.0.0.1", resp.tcp_port) as sess:
                sess.connect()
                raw = sess.get_image()

        assert raw == image_data
