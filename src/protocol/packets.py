"""
SiNet2 / P2K packet encoding and decoding.

UDP header layout (18 bytes, little-endian):
  +0x00  WORD   magic        = 0x072D
  +0x02  WORD   reserved
  +0x04  WORD   func_code
  +0x06  WORD   reserved
  +0x08  WORD   reserved
  +0x0A  WORD   api_version
  +0x0C  WORD   payload_len  (includes the 14-byte fixed header portion)
  +0x0E  WORD   reserved
  +0x10  WORD   seq_num

TCP header layout (20 bytes, big-endian):
  +0x00  DWORD  magic        = 0x0000072D
  +0x04  WORD   func_code
  +0x06  WORD   api_version
  +0x08  DWORD  session_id
  +0x0C  DWORD  payload_len  (bytes following the header)
  +0x10  DWORD  seq_num

Payload fields are big-endian regardless of transport:
  S   : 4-byte BE DWORD char-count + UTF-16LE chars
  BA  : 2-byte BE WORD  byte-count + raw bytes
  W   : 2-byte BE WORD  scalar
  DW  : 4-byte BE DWORD scalar
  B   : 1-byte unsigned scalar
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from .constants import (
    API_VERSION,
    FUNC_DISCOVER,
    MAGIC,
    TCP_HEADER_SIZE,
    UDP_HEADER_SIZE,
)

# ── UDP header ────────────────────────────────────────────────────────────────

_UDP_HDR_FMT = "<HHHHHHHHH"  # 9 × WORD = 18 bytes, little-endian
assert struct.calcsize(_UDP_HDR_FMT) == UDP_HEADER_SIZE

# The "payload_len" field covers bytes from offset 4 to end-of-frame (i.e. it
# counts func_code … seq_num *plus* the actual payload).  An empty payload
# produces payload_len = 0x000E (14 bytes: words at offsets 4–17).
_UDP_HDR_FIXED_OVERHEAD = 0x000E


def build_udp_header(
    func_code: int,
    payload: bytes = b"",
    *,
    seq_num: int = 0,
    api_version: int = API_VERSION,
) -> bytes:
    """Encode an 18-byte UDP frame header followed by *payload*."""
    payload_len = _UDP_HDR_FIXED_OVERHEAD + len(payload)
    header = struct.pack(
        _UDP_HDR_FMT,
        MAGIC,        # +0x00
        0x0000,       # +0x02 reserved
        func_code,    # +0x04
        0x0000,       # +0x06 reserved
        0x0000,       # +0x08 reserved
        api_version,  # +0x0A
        payload_len,  # +0x0C
        0x0000,       # +0x0E reserved
        seq_num,      # +0x10
    )
    return header + payload


def parse_udp_header(data: bytes) -> dict[str, int]:
    """Decode a UDP frame header.  Raises ValueError on bad magic."""
    if len(data) < UDP_HEADER_SIZE:
        raise ValueError(
            f"Frame too short: {len(data)} bytes, need {UDP_HEADER_SIZE}"
        )
    magic, _r1, func_code, _r2, _r3, api_version, payload_len, _r4, seq_num = (
        struct.unpack_from(_UDP_HDR_FMT, data, 0)
    )
    if magic != MAGIC:
        raise ValueError(f"Bad magic: 0x{magic:04X} (expected 0x{MAGIC:04X})")
    return {
        "magic": magic,
        "func_code": func_code,
        "api_version": api_version,
        "payload_len": payload_len,
        "seq_num": seq_num,
    }


# ── TCP header ────────────────────────────────────────────────────────────────

_TCP_HDR_FMT = ">IHHII I"  # DWORD, WORD, WORD, DWORD, DWORD, DWORD = 20 bytes
# Breakdown: magic(4) func_code(2) api_version(2) session_id(4) payload_len(4) seq_num(4)
assert struct.calcsize(_TCP_HDR_FMT) == TCP_HEADER_SIZE


def build_tcp_header(
    func_code: int,
    payload: bytes = b"",
    *,
    session_id: int = 0,
    seq_num: int = 0,
    api_version: int = API_VERSION,
) -> bytes:
    """Encode a 20-byte TCP frame header followed by *payload*."""
    header = struct.pack(
        _TCP_HDR_FMT,
        MAGIC,          # +0x00  DWORD
        func_code,      # +0x04  WORD
        api_version,    # +0x06  WORD
        session_id,     # +0x08  DWORD
        len(payload),   # +0x0C  DWORD
        seq_num,        # +0x10  DWORD
    )
    return header + payload


def parse_tcp_header(data: bytes) -> dict[str, int]:
    """Decode a TCP frame header. Raises ValueError on bad magic."""
    if len(data) < TCP_HEADER_SIZE:
        raise ValueError(
            f"TCP frame too short: {len(data)} bytes, need {TCP_HEADER_SIZE}"
        )
    magic, func_code, api_version, session_id, payload_len, seq_num = (
        struct.unpack_from(_TCP_HDR_FMT, data, 0)
    )
    if magic != MAGIC:
        raise ValueError(f"Bad TCP magic: 0x{magic:08X} (expected 0x{MAGIC:08X})")
    return {
        "magic": magic,
        "func_code": func_code,
        "api_version": api_version,
        "session_id": session_id,
        "payload_len": payload_len,
        "seq_num": seq_num,
    }


# ── Payload field codecs ──────────────────────────────────────────────────────

def encode_s(value: str) -> bytes:
    """S-type: 4-byte BE DWORD char-count + UTF-16LE characters."""
    encoded = value.encode("utf-16-le")
    char_count = len(value)
    return struct.pack(">I", char_count) + encoded


def decode_s(buf: bytes, off: int) -> tuple[str, int]:
    """Decode S-type field.  Returns (string, bytes_consumed)."""
    if off + 4 > len(buf):
        raise ValueError(f"S field: need 4 header bytes at {off}, only {len(buf)-off} remain")
    char_count = struct.unpack_from(">I", buf, off)[0]
    byte_count = char_count * 2
    off += 4
    if off + byte_count > len(buf):
        raise ValueError(f"S field: need {byte_count} data bytes at {off}")
    text = buf[off : off + byte_count].decode("utf-16-le", errors="replace")
    return text, 4 + byte_count


def encode_ba(value: bytes) -> bytes:
    """BA-type: 2-byte BE WORD byte-count + raw bytes."""
    return struct.pack(">H", len(value)) + value


def decode_ba(buf: bytes, off: int) -> tuple[bytes, int]:
    """Decode BA-type field.  Returns (raw_bytes, bytes_consumed)."""
    if off + 2 > len(buf):
        raise ValueError(f"BA field: need 2 header bytes at {off}")
    count = struct.unpack_from(">H", buf, off)[0]
    off += 2
    if off + count > len(buf):
        raise ValueError(f"BA field: need {count} data bytes at {off}")
    return buf[off : off + count], 2 + count


def encode_w(value: int) -> bytes:
    """W-type: 2-byte BE WORD."""
    return struct.pack(">H", value)


def decode_w(buf: bytes, off: int) -> tuple[int, int]:
    """Decode W-type field.  Returns (int, bytes_consumed=2)."""
    if off + 2 > len(buf):
        raise ValueError(f"W field: need 2 bytes at {off}")
    return struct.unpack_from(">H", buf, off)[0], 2


def encode_dw(value: int) -> bytes:
    """DW-type: 4-byte BE DWORD."""
    return struct.pack(">I", value)


def decode_dw(buf: bytes, off: int) -> tuple[int, int]:
    """Decode DW-type field.  Returns (int, bytes_consumed=4)."""
    if off + 4 > len(buf):
        raise ValueError(f"DW field: need 4 bytes at {off}")
    return struct.unpack_from(">I", buf, off)[0], 4


# ── Discovery probe ───────────────────────────────────────────────────────────

DISCOVERY_PROBE: bytes = build_udp_header(FUNC_DISCOVER, seq_num=0)
"""Ready-to-send 18-byte discovery probe with empty payload."""


# ── Discovery response parser ─────────────────────────────────────────────────

@dataclass
class DiscoveryResponse:
    src_ip: str
    src_port: int
    tcp_port: int
    name_p2k: str
    ip: str
    gateway: str
    netmask: str
    mac: str
    config_time: int
    device_type: int
    api_version: int
    seq_num: int
    raw: bytes = field(repr=False, default=b"")

    @classmethod
    def from_wire(cls, data: bytes, src_ip: str, src_port: int) -> "DiscoveryResponse":
        hdr = parse_udp_header(data)
        payload = data[UDP_HEADER_SIZE:]
        off = 0

        name_str, n = decode_s(payload, off); off += n
        gw_bytes, n = decode_ba(payload, off); off += n
        mask_bytes, n = decode_ba(payload, off); off += n
        ip_bytes, n = decode_ba(payload, off); off += n
        mac_bytes, n = decode_ba(payload, off); off += n
        config_time, n = decode_w(payload, off); off += n
        device_type, n = decode_w(payload, off); off += n

        tcp_port = 1999
        stripped = name_str.strip("\x00").strip()
        try:
            tcp_port = int(stripped)
        except (ValueError, TypeError):
            pass

        def _fmt_ip(b: bytes) -> str:
            return ".".join(str(x) for x in b) if len(b) == 4 else b.hex()

        def _fmt_mac(b: bytes) -> str:
            return ":".join(f"{x:02X}" for x in b)

        return cls(
            src_ip=src_ip,
            src_port=src_port,
            tcp_port=tcp_port,
            name_p2k=stripped,
            ip=_fmt_ip(ip_bytes),
            gateway=_fmt_ip(gw_bytes),
            netmask=_fmt_ip(mask_bytes),
            mac=_fmt_mac(mac_bytes) if mac_bytes else "??:??:??:??:??:??",
            config_time=config_time,
            device_type=device_type,
            api_version=hdr["api_version"],
            seq_num=hdr["seq_num"],
            raw=data,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "src_ip": self.src_ip,
            "src_port": self.src_port,
            "tcp_port": self.tcp_port,
            "name_p2k": self.name_p2k,
            "ip": self.ip,
            "gateway": self.gateway,
            "netmask": self.netmask,
            "mac": self.mac,
            "config_time": self.config_time,
            "device_type": self.device_type,
            "api_version": self.api_version,
            "seq_num": self.seq_num,
        }
