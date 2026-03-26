#!/usr/bin/env python3
"""
PureXS HB Decoder — Sirona Orthophos P2K wire protocol decoder.

Parses Wireshark text dumps from Sirona ORTHOPHOS (192.168.139.170:12837)
to extract:
  - Session handshake frames  (0x20xx / 0x21xx / 0x10xx func codes)
  - Heartbeat (HB) keep-alive pairs  (0x200B request / 0x200C response)
  - kV ramp-up table during exposure  (f4 53 markers = kV=500 threshold)
  - Exposure trigger point  (ff 12 pattern = kV at max)
  - Scanline image data  (16-bit BE pixel blocks with NN 00 01 00 f0 headers)
  - Event log messages  (Recording started/stopped, Imagetransfer, Released)
  - E7 14 02 ERR_SIDEXIS_API error sequences (treat as post-scan success)

Also provides a LIVE TCP client for real-time device monitoring.

Wire format (confirmed from ff.txt Wireshark capture):
  Session header: 20 bytes, big-endian
    +0x00  BYTE   func_hi        command family (0x20=session, 0x10=data, 0x21=caps)
    +0x01  BYTE   func_lo        sub-command
    +0x02  WORD   magic          0x072D
    +0x04  WORD   port           0x07D0 = 2000
    +0x06  WORD   version        0x0001
    +0x08  WORD   flags          0x000E or 0x000F
    +0x0A  10B    reserved       zeros
  HB pair: func=0x200B (host→device), func=0x200C (device→host), 20B each
  kV ramp data: repeating 15-byte records in 1460B TCP segments:
    +0x00  BYTE   01             record marker
    +0x01  WORD   kV_raw         tube voltage (big-endian)
    +0x03  BYTE   01             separator
    +0x04  WORD   field2         exposure-related counter
    +0x06  BYTE   01             separator
    +0x07  WORD   field3         ramp value (rises to ff 12 = expose trigger)
    +0x09  BYTE   01             separator
    +0x0A  WORD   counter        monotonic position counter (big-endian)
    +0x0C  BYTE   0E             record type marker
    +0x0D  BYTE   01             fixed
    +0x0E  varies zeros/flags
  Scanline header (within image data stream):
    4B     preamble     varies (checksum / metadata)
    BYTE   01           marker
    BYTE   scanline_id  increments 0x40, 0x41, 0x42 ...
    BYTE   00           separator
    BYTE   01           marker
    WORD   pixel_count  0x00F0 = 240 pixels per scanline
    WORD   row_param    0x0034 = 52 (row metadata)
    N×WORD pixels       16-bit big-endian grayscale values

Usage:
    # Parse a Wireshark dump
    python hb_decoder.py parse /path/to/ff.txt --outdir ./decoded

    # Live monitor (connects to Sirona device)
    python hb_decoder.py live --host 192.168.139.170 --port 12837

    # Quick summary of a dump
    python hb_decoder.py summary /path/to/ff.txt
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np
from PIL import Image

# ── Logging ──────────────────────────────────────────────────────────────────

from utils import get_data_dir

LOG_DIR = get_data_dir()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "hb_decoder.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("hb_decoder")


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Protocol Constants (confirmed from ff.txt analysis)
# ╚══════════════════════════════════════════════════════════════════════════════

MAGIC = 0x072D
PORT_MARKER = 0x07D0  # 2000 decimal — appears in every session frame

# Session frame function codes (byte[0] << 8 | byte[1])
FC_SESSION_OPEN_REQ = 0x205C   # SYN-like: host opens session
FC_SESSION_OPEN_ACK = 0x205D   # device acknowledges session
FC_SESSION_INIT = 0x2001       # host sends session params
FC_SESSION_CONFIRM = 0x2002    # device confirms with session_id + flags
FC_HB_REQUEST = 0x200B         # heartbeat request (host → device)
FC_HB_RESPONSE = 0x200C        # heartbeat response (device → host)
FC_HB_STATUS_REQ = 0x200D     # status poll (host → device, required every 5 HBs)
FC_HB_STATUS_RESP = 0x200E    # status poll response (device → host)
FC_CAPS_REQ = 0x2110           # capabilities request
FC_CAPS_RESP = 0x2111          # capabilities response (38 bytes)
FC_DATA_SEND = 0x1000          # host sends patient/exam data (176 bytes)
FC_DATA_ACK = 0x1001           # device acks patient data
FC_DATA_ACK = 0x1001           # device acks patient data
FC_STATUS_RESP = 0x1002        # status response with kV ramp data
FC_EXPOSE_NOTIFY = 0x1005      # device → host: exposure starting (physical button)
FC_IMAGE_ACK = 0x1008          # host → device: image data received
FC_IMAGE_ACK_RESP = 0x1009     # device → host: ack of image ack

# Device readiness status codes (returned in status query response payload)
DEVICE_STATUS_READY  = 0x0000
DEVICE_STATUS_BUSY   = 0x0001
DEVICE_STATUS_ERROR  = 0x0002
DEVICE_STATUS_WARMUP = 0x0003

SESSION_HEADER_SIZE = 20

# kV ramp record structure
KV_RECORD_SIZE = 15            # bytes per kV sample in ramp data
EXPOSE_TRIGGER_KV_HI = 0xFF   # ff XX pattern = tube at full voltage
KV_THRESHOLD_MARKER = 0xF453  # f4 53 = 62547 decimal — seen during ramp-up

# Scanline image structure
SCANLINE_MARKER = b'\x00\x01\x00\xf0'   # pixel_count=240 as BE word
SCANLINE_PIXELS = 0x00F0                 # 240 pixels per row
SCANLINE_ROW_PARAM = 0x0034             # row metadata = 52
PIXEL_BYTES = 2                          # 16-bit big-endian per pixel

# ── Panoramic image extraction constants (from ff.txt Wireshark analysis) ────
#
# The Orthophos XG (DX41) sends the full detector readout as a continuous
# 16-bit big-endian pixel stream, split across 0x1003 continuation frames.
#
# Each 0x1003 frame is 65 586 bytes total:
#   +0x00  20 B   session header   (func=0x1003, magic, port, flags)
#   +0x14  30 B   echo payload     (FC 30 ... 80 00 — patient config echo)
#   +0x32  var    pixel data       (continuous 16-bit BE pixels)
#
# The very first 0x1003 frame has an extra 8-byte padding block between
# the echo and the pixel data:  00 00 00 01 00 00 00 34.
#
# Before the pixel stream, the initial 0x1002 data frame contains:
#   - Patient echo (~350 B)
#   - kV ramp records (~2–5 KB)
#   - Position/status telemetry records
#   - Transition marker: D6 D6 4C 1F + 8 B header
#   - Then continuous pixel data begins
#
# The device 0x00 bytes appear as 0x20 in the TCP stream received by the
# host (observed consistently — echo, padding, and telemetry all show this).
#
# Image dimensions are reported in the post-scan 0x1004 frame:
#   offset +0x0A  WORD  height  (0x0524 = 1316 for ORTHOPHOS XG)
#   offset +0x0C  WORD  width   (0x0A92 = 2706 for ORTHOPHOS XG)
#
# Default panoramic dimensions (DX41 / ORTHOPHOS XG):
PANO_DEFAULT_WIDTH  = 2706
PANO_DEFAULT_HEIGHT = 1316

# Per-0x1003-frame overhead
ECHO_PAYLOAD_SIZE = 30           # FC 30 … 80 00 patient config echo (minimum)
ECHO_PAYLOAD_MAX  = 200          # upper bound — some frames carry extra kV telemetry
FIRST_FRAME_PADDING = 8          # 00 00 00 01 00 00 00 34 (first frame only)

# Pixel stream transition marker (signals end of kV ramp, start of pixels)
PIXEL_TRANSITION_MARKER = b'\xd6\xd6\x4c'

# Inline scanline marker embedded in the pixel stream (8 bytes)
#   01 <scanline_id> 00 01 00 F0 00 34
_INLINE_SCANLINE_HDR = b'\x00\x01\x00\xf0\x00\x34'  # tail 6 bytes of the 8-byte marker

# Event log patterns (ASCII in TCP payload)
RE_RECORDING_START = re.compile(
    rb"Recording started - Value: (\d+)", re.IGNORECASE
)
RE_RECORDING_STOP = re.compile(rb"Recording stopped", re.IGNORECASE)
RE_IMAGE_TRANSFER_START = re.compile(rb"Imagetransfer started", re.IGNORECASE)
RE_IMAGE_TRANSFER_STOP = re.compile(rb"Imagetransfer stopped", re.IGNORECASE)
RE_STATE_RELEASED = re.compile(
    rb"Image state switched to Released", re.IGNORECASE
)
RE_E7_ERROR = re.compile(
    rb"E7 14 02 \(ERR_SIDEXIS_API\)", re.IGNORECASE
)
RE_TIMESTAMP = re.compile(
    rb"(\d{4}-\d{2}-\d{2}, \d{2}:\d{2}:\d{2})"
)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Data Classes
# ╚══════════════════════════════════════════════════════════════════════════════

@dataclass
class SessionFrame:
    """One parsed P2K session-layer frame."""
    frame_no: int
    timestamp: float
    direction: str          # "C2S" (client→server) or "S2C" (server→client)
    func_code: int
    func_name: str
    payload_len: int
    raw_header: bytes
    raw_payload: bytes

    @property
    def is_hb(self) -> bool:
        return self.func_code in (FC_HB_REQUEST, FC_HB_RESPONSE)


@dataclass
class KVSample:
    """One kV ramp sample from the exposure data stream."""
    position: int           # monotonic counter from record
    kv_raw: int             # raw 16-bit kV value
    field2: int             # exposure counter
    field3: int             # ramp value (rises to 0xFF12 at trigger)

    @property
    def is_expose_trigger(self) -> bool:
        """True when field3 reaches ff XX (tube at full voltage)."""
        return (self.field3 >> 8) == EXPOSE_TRIGGER_KV_HI


@dataclass
class Scanline:
    """One decoded image scanline."""
    scanline_id: int
    pixel_count: int
    pixels: np.ndarray      # uint16 array, length = pixel_count

    @property
    def pixels_8bit(self) -> np.ndarray:
        """Normalize to 8-bit for display."""
        if self.pixels.max() == 0:
            return np.zeros(len(self.pixels), dtype=np.uint8)
        norm = self.pixels.astype(np.float32) / self.pixels.max() * 255
        return norm.astype(np.uint8)


@dataclass
class ScanEvent:
    """Timeline event extracted from embedded ASCII log messages."""
    timestamp_str: str
    event_type: str         # "recording_start", "recording_stop", etc.
    detail: str = ""


@dataclass
class DecodedCapture:
    """Complete decoded capture file."""
    frames: list[SessionFrame] = field(default_factory=list)
    hb_pairs: list[tuple[SessionFrame, SessionFrame]] = field(default_factory=list)
    kv_samples: list[KVSample] = field(default_factory=list)
    scanlines: list[Scanline] = field(default_factory=list)
    events: list[ScanEvent] = field(default_factory=list)
    expose_trigger_idx: int = -1


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Wireshark Text Dump Parser
# ╚══════════════════════════════════════════════════════════════════════════════

# Matches hex data lines:  "0000  20 5c 07 2d ..."
_HEX_LINE = re.compile(
    r"^([0-9a-f]{4})  ((?:[0-9a-f]{2} ){1,16})"
)
# Matches frame info line with PSH flag (data-bearing):
_FRAME_INFO = re.compile(
    r"Frame (\d+):.*?(\d+) bytes"
)
_TCP_INFO = re.compile(
    r"Src Port: (\d+), Dst Port: (\d+).*?Seq: (\d+).*?Len: (\d+)"
)
_TIME_INFO = re.compile(
    r"^\s+\d+ ([\d.]+)\s+(\S+)\s+(\S+)"
)


def _parse_hex_block(lines: list[str]) -> bytes:
    """Parse contiguous Wireshark hex dump lines into raw bytes."""
    result = bytearray()
    for line in lines:
        m = _HEX_LINE.match(line.rstrip())
        if m:
            hex_part = m.group(2).strip()
            result.extend(bytes.fromhex(hex_part.replace(" ", "")))
    return bytes(result)


def parse_wireshark_dump(path: str | Path) -> DecodedCapture:
    """Parse a Wireshark text export and extract all protocol elements."""
    path = Path(path)
    log.info("Parsing %s (%s)", path.name, _human_size(path.stat().st_size))

    capture = DecodedCapture()
    current_hex_lines: list[str] = []
    current_frame_no = 0
    current_time = 0.0
    current_src_port = 0
    current_dst_port = 0
    current_data_len = 0
    in_data_section = False

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line_no, raw_line in enumerate(f, 1):
            line = raw_line.rstrip()

            # Frame header
            fm = _FRAME_INFO.match(line)
            if fm:
                # Flush previous hex block
                if current_hex_lines and current_data_len > 0:
                    _process_hex_block(
                        capture, current_hex_lines, current_frame_no,
                        current_time, current_src_port, current_dst_port,
                    )
                current_hex_lines = []
                current_frame_no = int(fm.group(1))
                in_data_section = False
                continue

            # Timestamp + IP info
            tm = _TIME_INFO.match(line)
            if tm:
                try:
                    current_time = float(tm.group(1))
                except ValueError:
                    pass
                continue

            # TCP info
            ti = _TCP_INFO.search(line)
            if ti:
                current_src_port = int(ti.group(1))
                current_dst_port = int(ti.group(2))
                current_data_len = int(ti.group(4))
                continue

            # Data section marker
            if line.startswith("Data ("):
                in_data_section = True
                current_hex_lines = []
                continue

            # Hex data line
            if in_data_section and _HEX_LINE.match(line):
                current_hex_lines.append(line)
                continue

            # Inside data section: skip blank lines (Wireshark puts one
            # between the "Data (N bytes)" header and the hex dump)
            if in_data_section and line.strip() == "":
                # Only end the section if we already collected hex lines
                if current_hex_lines:
                    _process_hex_block(
                        capture, current_hex_lines, current_frame_no,
                        current_time, current_src_port, current_dst_port,
                    )
                    current_hex_lines = []
                    in_data_section = False
                # Otherwise keep waiting for hex lines
                continue

            # Any non-hex, non-blank line ends the data section
            if in_data_section:
                if current_hex_lines:
                    _process_hex_block(
                        capture, current_hex_lines, current_frame_no,
                        current_time, current_src_port, current_dst_port,
                    )
                    current_hex_lines = []
                in_data_section = False

    # Flush final block
    if current_hex_lines:
        _process_hex_block(
            capture, current_hex_lines, current_frame_no,
            current_time, current_src_port, current_dst_port,
        )

    # Post-process: pair HB request/response
    _pair_heartbeats(capture)

    log.info(
        "Parsed: %d frames, %d HB pairs, %d kV samples, "
        "%d scanlines, %d events",
        len(capture.frames), len(capture.hb_pairs),
        len(capture.kv_samples), len(capture.scanlines),
        len(capture.events),
    )
    return capture


def _process_hex_block(
    capture: DecodedCapture,
    hex_lines: list[str],
    frame_no: int,
    timestamp: float,
    src_port: int,
    dst_port: int,
) -> None:
    """Decode one hex payload block and add results to capture."""
    raw = _parse_hex_block(hex_lines)
    if not raw:
        return

    direction = "C2S" if src_port == 50930 else "S2C"

    # ── 1. Session-layer frames (20-byte header with 07 2D magic) ────────
    if (
        len(raw) >= SESSION_HEADER_SIZE
        and len(raw) <= 300
        and raw[2:4] == b'\x07\x2d'
    ):
        func_code = (raw[0] << 8) | raw[1]
        func_name = _fc_name(func_code)
        payload = raw[SESSION_HEADER_SIZE:]

        frame = SessionFrame(
            frame_no=frame_no,
            timestamp=timestamp,
            direction=direction,
            func_code=func_code,
            func_name=func_name,
            payload_len=len(payload),
            raw_header=raw[:SESSION_HEADER_SIZE],
            raw_payload=payload,
        )
        capture.frames.append(frame)
        return

    # For large data payloads, try all extractors (they can coexist)

    # ── 2. kV ramp data (large payloads with repeating 15-byte records) ──
    if len(raw) > 50:
        samples = _extract_kv_samples(raw)
        for s in samples:
            if s.is_expose_trigger and capture.expose_trigger_idx < 0:
                capture.expose_trigger_idx = len(capture.kv_samples)
                log.info(
                    "EXPOSE TRIGGER at position %d (ff %02x)",
                    s.position, s.field3 & 0xFF,
                )
            capture.kv_samples.append(s)

    # ── 3. Scanline image data ───────────────────────────────────────────
    scanlines = _extract_scanlines(raw)
    if scanlines:
        capture.scanlines.extend(scanlines)

    # ── 4. ASCII event log messages ──────────────────────────────────────
    events = _extract_events(raw)
    capture.events.extend(events)


def _fc_name(fc: int) -> str:
    """Human name for a function code."""
    names = {
        FC_SESSION_OPEN_REQ: "SESSION_OPEN_REQ",
        FC_SESSION_OPEN_ACK: "SESSION_OPEN_ACK",
        FC_SESSION_INIT: "SESSION_INIT",
        FC_SESSION_CONFIRM: "SESSION_CONFIRM",
        FC_HB_REQUEST: "HB_REQUEST",
        FC_HB_RESPONSE: "HB_RESPONSE",
        FC_HB_STATUS_REQ: "HB_STATUS_REQ",
        FC_HB_STATUS_RESP: "HB_STATUS_RESP",
        FC_CAPS_REQ: "CAPS_REQ",
        FC_CAPS_RESP: "CAPS_RESP",
        FC_DATA_SEND: "DATA_SEND",
        FC_DATA_ACK: "DATA_ACK",
        FC_STATUS_RESP: "STATUS_RESP",
        FC_EXPOSE_NOTIFY: "EXPOSE_NOTIFY",
        FC_IMAGE_ACK: "IMAGE_ACK",
        FC_IMAGE_ACK_RESP: "IMAGE_ACK_RESP",
    }
    return names.get(fc, f"0x{fc:04X}")


def _contains_kv_records(data: bytes) -> bool:
    """Heuristic: does this payload contain kV ramp sample records?

    kV records have a repeating pattern:
      01 XX XX 01 YY YY 01 ZZ ZZ 01 WW WW 0E 01
    with 0E as a record-type marker appearing every ~15 bytes.
    """
    if len(data) < 30:
        return False
    # Count 0E 01 pairs — kV records have these every ~15 bytes
    marker = b'\x0e\x01'
    count = 0
    idx = 0
    while True:
        idx = data.find(marker, idx)
        if idx < 0:
            break
        count += 1
        idx += 2
    # Need at least 5 records to qualify
    return count >= 5 and count > len(data) // 20


def _extract_kv_samples(data: bytes) -> list[KVSample]:
    """Extract kV ramp samples from a data payload.

    Record pattern (15 bytes):
      01 KV_HI KV_LO 01 F2_HI F2_LO 01 F3_HI F3_LO 01 CNT_HI CNT_LO 0E 01 ...
    """
    samples = []
    # Find all 0E 01 markers and work backward to find record starts
    idx = 0
    while idx < len(data) - KV_RECORD_SIZE:
        # Look for the 0E 01 marker that ends each record
        marker_pos = data.find(b'\x0e\x01', idx)
        if marker_pos < 0 or marker_pos < 12:
            break

        # Record starts 12 bytes before the 0E marker
        rec_start = marker_pos - 12
        if rec_start < 0:
            idx = marker_pos + 2
            continue

        rec = data[rec_start:marker_pos + 2]
        if len(rec) < 14:
            idx = marker_pos + 2
            continue

        # Validate structure: bytes at positions 0, 3, 6, 9 should be 0x01
        if rec[0] == 0x01 and rec[3] == 0x01 and rec[6] == 0x01 and rec[9] == 0x01:
            kv_raw = (rec[1] << 8) | rec[2]
            field2 = (rec[4] << 8) | rec[5]
            field3 = (rec[7] << 8) | rec[8]
            counter = (rec[10] << 8) | rec[11]

            samples.append(KVSample(
                position=counter,
                kv_raw=kv_raw,
                field2=field2,
                field3=field3,
            ))

        idx = marker_pos + 2

    return samples


def _extract_scanlines(data: bytes) -> list[Scanline]:
    """Extract 16-bit scanlines from image data blocks.

    Scanline header pattern:
      [preamble] 01 SCANLINE_ID 00 01 00 F0 00 34 [240 × 2 bytes pixel data]
    """
    scanlines = []
    idx = 0
    while idx < len(data) - 10:
        pos = data.find(SCANLINE_MARKER, idx)
        if pos < 0:
            break

        # The scanline_id is 2 bytes before the marker
        if pos < 2:
            idx = pos + 4
            continue

        scanline_id = data[pos - 1]
        marker_byte = data[pos - 2]

        # Verify the 01 marker byte
        if marker_byte != 0x01:
            idx = pos + 4
            continue

        # Read row parameter (2 bytes after the 00 F0 marker)
        param_offset = pos + 4
        if param_offset + 2 > len(data):
            break
        row_param = (data[param_offset] << 8) | data[param_offset + 1]

        # Pixel data starts after the row parameter
        pixel_start = param_offset + 2
        pixel_byte_count = SCANLINE_PIXELS * PIXEL_BYTES

        if pixel_start + pixel_byte_count > len(data):
            # Partial scanline at end of payload — take what we can
            available = len(data) - pixel_start
            pixel_count = available // PIXEL_BYTES
            if pixel_count < 10:
                idx = pos + 4
                continue
        else:
            pixel_count = SCANLINE_PIXELS

        pixel_data = data[pixel_start:pixel_start + pixel_count * PIXEL_BYTES]
        pixels = np.frombuffer(pixel_data, dtype=">u2")  # big-endian uint16

        scanlines.append(Scanline(
            scanline_id=scanline_id,
            pixel_count=len(pixels),
            pixels=pixels,
        ))

        idx = pixel_start + len(pixel_data)

    return scanlines


# ── Session header signature for stripping embedded headers ───────────
_SESSION_SIG = b'\x07\x2d\x07\xd0'   # MAGIC + PORT at offsets 2-5


def _strip_session_headers(data: bytes) -> bytes:
    """Remove all 20-byte P2K session headers embedded in raw scan data.

    During the data flood the device sends some frames with session
    headers (0x1002, 0x1003, 0x1005) interleaved with raw continuation
    data.  This function finds every occurrence of the 07 2D 07 D0
    signature and removes the 20-byte header so only payload remains.
    """
    result = bytearray()
    idx = 0
    while idx < len(data):
        # Look for next session header signature (at offset +2 from header start)
        pos = data.find(_SESSION_SIG, idx)
        if pos < 0:
            result.extend(data[idx:])
            break
        # The header starts 2 bytes before the signature
        hdr_start = pos - 2
        if hdr_start < idx:
            # Signature is inside data we already consumed — skip it
            result.extend(data[idx:pos + 4])
            idx = pos + 4
            continue
        # Verify it looks like a real header (func_hi in known range)
        func_hi = data[hdr_start] if hdr_start >= 0 else 0
        if func_hi in (0x10, 0x20, 0x21):
            # Append data before this header, then skip the 20-byte header
            result.extend(data[idx:hdr_start])
            idx = hdr_start + SESSION_HEADER_SIZE
        else:
            # False positive — not a real header
            result.extend(data[idx:pos + 4])
            idx = pos + 4
    return bytes(result)


def _detect_echo_end(payload: bytes, min_echo: int = 30,
                     max_scan: int = 200, frame_index: int = -1) -> int:
    """Return the byte offset where pixel data begins inside a 0x1003 payload.

    The device embeds kV telemetry in every 10th 0x1003 frame.  The
    telemetry block follows the 30-byte FC30 echo and always ends with
    the signature ``... 0x0001 XXXX 0x0034`` (row parameter = 52).

    For non-telemetry frames the standard 30-byte echo applies.

    Detection strategy:
      - If frame_index is known and frame is NOT a telemetry frame
        (N%10 != 0), return exactly 30 bytes (deterministic).
      - For telemetry frames (N%10 == 0), search for the ``0x0034``
        row-parameter anchor preceded by ``0x0001``.
      - Fallback: value-based heuristic scan (only when frame_index
        is unknown).
    """
    # ── Deterministic path: non-telemetry frames always have 30-byte echo
    if frame_index >= 0 and frame_index % 10 != 0:
        return 30

    # ── Pass 1: structural anchor (0x0001 … 0x0034) ──────────────────
    #   Only search within a reasonable window (bytes 30-160) to avoid
    #   false positives from the pixel data further into the payload.
    #   Support both plain (00 34) and byte-stuffed (20 34) variants.
    ANCHOR_LIMIT = min(160, max_scan, len(payload) - 1)
    last_anchor = -1
    for off in range(32, ANCHOR_LIMIT):
        if payload[off + 1] != 0x34:
            continue
        if payload[off] not in (0x00, 0x20):
            continue
        if off < 4:
            continue
        if payload[off - 3] != 0x01:
            continue
        if payload[off - 4] not in (0x00, 0x20):
            continue
        last_anchor = off + 2
    if last_anchor > 30:
        # For telemetry frames with known index, validate against expected size
        if frame_index >= 0 and frame_index % 10 == 0:
            expected = 30 + (frame_index // 10 + 1) * 8
            if last_anchor != expected:
                log.debug("Frame %d: anchor echo=%d, formula echo=%d",
                          frame_index, last_anchor, expected)
        return last_anchor

    # ── Telemetry frame but anchor not found — use formula as fallback
    if frame_index >= 0 and frame_index % 10 == 0:
        expected = 30 + (frame_index // 10 + 1) * 8
        log.warning("Frame %d: no anchor found, using formula echo=%d",
                    frame_index, expected)
        return expected

    # ── Pass 2: value-based heuristic (only for unknown frame index) ──
    RUN = 20
    PIXEL_MIN = 0x0600
    ZERO_SCAN = 100
    ZERO_THRESH = 0x0200

    limit = min(max_scan, len(payload) - RUN * 2)
    for off in range(min_echo, limit, 2):
        ok = True
        for j in range(RUN):
            val = (payload[off + j * 2] << 8) | payload[off + j * 2 + 1]
            if val < PIXEL_MIN:
                ok = False
                break
        if not ok:
            continue
        zero_count = 0
        scan_end = min(off + ZERO_SCAN, len(payload) - 1)
        for k in range(off, scan_end, 2):
            v = (payload[k] << 8) | payload[k + 1]
            if v < ZERO_THRESH:
                zero_count += 1
                if zero_count >= 2:
                    break
        if zero_count < 2:
            return off
    return min_echo  # fallback


def _repair_inline_telemetry(segment: bytearray) -> bytearray:
    """Replace inline telemetry blocks with interpolated pixel data.

    The Orthophos XG embeds 72-byte kV/position telemetry records into
    the pixel stream at intervals of exactly 2632 bytes (= 1316 pixels
    = one detector column height).  These records OVERWRITE pixel data
    at fixed row positions — they do NOT add extra bytes.

    Each 72-byte block ends with a 6-byte tail signature.  In the Sidexis
    (de-stuffed) format the tail is ``00 01 XX XX 00 34``.  In a direct
    network capture the protocol uses 0x20 byte-stuffing, so the same
    tail appears as ``20 01 XX XX 20 34``.  Both variants are detected.

    This function finds each block and replaces it with linearly
    interpolated pixel values from the surrounding rows, preserving
    the total byte count.
    """
    TELEM_SIZE = 72
    TELEM_PIXELS = TELEM_SIZE // 2  # 36 pixels overwritten per block

    # Find all telemetry blocks by tail pattern.
    # Support both plain (00 01 XX XX 00 34) and byte-stuffed (20 01 XX XX 20 34).
    block_starts: list[int] = []
    for off in range(TELEM_SIZE - 2, len(segment) - 1):
        if segment[off + 1] != 0x34:
            continue
        # Check for tail byte before 0x34: either 0x00 or 0x20 (byte-stuffed)
        if segment[off] not in (0x00, 0x20):
            continue
        # Check for 0x01 marker 4 bytes before 0x34
        if off < 4:
            continue
        if segment[off - 3] != 0x01:
            continue
        if segment[off - 4] not in (0x00, 0x20):
            continue
        blk_start = off + 2 - TELEM_SIZE
        if blk_start < 0:
            continue
        # Validate: the 72-byte block should contain multiple 0x20 or
        # 0x00 escape/marker bytes (telemetry has many low-value or
        # 0x20-stuffed fields, unlike normal 0x08xx pixel data).
        marker_count = 0
        for j in range(0, TELEM_SIZE, 2):
            hi = segment[blk_start + j]
            if hi == 0x20 or hi == 0x00:
                marker_count += 1
        if marker_count >= 3:
            block_starts.append(blk_start)

    if not block_starts:
        return segment

    result = bytearray(segment)  # copy — we'll overwrite in place

    # Repair 72-byte telemetry blocks
    for bs in block_starts:
        be = bs + TELEM_SIZE
        if bs >= 2 and be + 1 < len(result):
            val_before = (result[bs - 2] << 8) | result[bs - 1]
            val_after = (result[be] << 8) | result[be + 1]
        elif bs >= 2:
            val_before = val_after = (result[bs - 2] << 8) | result[bs - 1]
        else:
            val_before = val_after = (result[be] << 8) | result[be + 1]
        for j in range(TELEM_PIXELS):
            t = (j + 1) / (TELEM_PIXELS + 1)
            val = int(val_before * (1 - t) + val_after * t)
            pos = bs + j * 2
            result[pos] = (val >> 8) & 0xFF
            result[pos + 1] = val & 0xFF

    return result


def _find_pixel_start(data: bytes, search_start: int = 60000,
                      search_end: int = 90000) -> int:
    """Find where actual pixel data begins using column-correlation.

    Scans byte offsets in the raw buffer looking for the position where
    adjacent 1316-pixel columns are most strongly correlated, indicating
    a genuine detector readout rather than kV telemetry / protocol data.
    """
    H = PANO_DEFAULT_HEIGHT
    best_off = search_start
    best_corr = -1.0

    for byte_off in range(search_start, min(search_end, len(data) - H * 4), 2):
        c1 = np.frombuffer(data[byte_off:byte_off + H * 2], dtype=">u2").astype(np.float32)
        c2 = np.frombuffer(data[byte_off + H * 2:byte_off + H * 4], dtype=">u2").astype(np.float32)
        c1z = c1 - c1.mean()
        c2z = c2 - c2.mean()
        d = np.sqrt(np.dot(c1z, c1z) * np.dot(c2z, c2z))
        ncc = np.dot(c1z, c2z) / (d + 1e-10) if d > 0 else 0.0
        if ncc > best_corr:
            best_corr = ncc
            best_off = byte_off

    log.info("Pixel start scan: best offset %d (corr=%.4f)", best_off, best_corr)
    return best_off


def _extract_panoramic(data: bytes, detector_height: int = 0) -> list[Scanline]:
    """Extract a full panoramic image from the raw scan data stream.

    The Orthophos XG (DX41) sends the full detector readout as a continuous
    stream of 16-bit big-endian pixels, split across 0x1003 continuation
    frames.  Each frame has a 20-byte session header and a *variable-length*
    echo payload (30-120 bytes, depending on session config) before pixels.

    The echo size depends on a 2-byte field in the DATA_SEND payload
    (offset 18-19: 0xDB04 for Sidexis, was 0xE300 for PureXS).  With
    the Sidexis value, all frames get clean 30-byte echoes.  With the
    old PureXS value, some frames got 30-120 byte echoes with embedded
    kV telemetry that caused image artifacts.

    Processing steps:
      1. Locate 0x1003 session headers in the raw buffer.
      2. Auto-detect where pixel data begins (correlation-based scan).
      3. For each 0x1003 frame, dynamically detect the echo payload end.
      4. Strip inline scanline markers embedded in the pixel stream.
      5. Reshape the clean pixel stream at the correct detector height.
      6. Repair any remaining artifact rows via neighbour interpolation.

    Returns a list of Scanline objects, one per image column.
    """
    if len(data) < 10000:
        return []

    # ── 1. Locate all 0x1003 session headers ──────────────────────────────
    headers_1003: list[int] = []
    idx = 0
    while idx < len(data) - 6:
        pos = data.find(_SESSION_SIG, idx)
        if pos < 0:
            break
        hdr_start = pos - 2
        if hdr_start < idx:
            idx = pos + 4
            continue
        func_hi = data[hdr_start] if hdr_start >= 0 else 0
        if func_hi == 0x10 and hdr_start + 1 < len(data) and data[hdr_start + 1] == 0x03:
            headers_1003.append(hdr_start)
        idx = pos + 4

    if not headers_1003:
        log.warning("Panoramic: no 0x1003 frames found in %d bytes", len(data))
        return []

    log.info("Panoramic: found %d 0x1003 frames", len(headers_1003))

    # ── 2. Find where pixel data starts ───────────────────────────────────
    first_frame = headers_1003[0]
    scan_lo = first_frame + SESSION_HEADER_SIZE + ECHO_PAYLOAD_SIZE
    scan_hi = min(scan_lo + 30000, len(data) - PANO_DEFAULT_HEIGHT * 4)
    pixel_start = _find_pixel_start(data, scan_lo, scan_hi)

    # ── 3. Build clean pixel stream with echo + inline telemetry stripping ─
    clean = bytearray()
    read_pos = pixel_start

    echo_sizes_log: list[int] = []
    telem_blocks_repaired = 0

    for i, hdr_pos in enumerate(headers_1003):
        if hdr_pos < pixel_start:
            continue

        # Pixels between the previous frame's echo end and this header
        if hdr_pos > read_pos:
            segment = bytearray(data[read_pos:hdr_pos])
            repaired = _repair_inline_telemetry(segment)
            telem_blocks_repaired += (len(segment) - len(repaired) == 0)  # count frames processed
            clean.extend(repaired)

        after_hdr = hdr_pos + SESSION_HEADER_SIZE
        payload = data[after_hdr:after_hdr + ECHO_PAYLOAD_MAX]
        echo_end = _detect_echo_end(payload, frame_index=i)
        echo_sizes_log.append(echo_end)

        read_pos = after_hdr + echo_end

    # Tail: stop at 0x1004/0x1005 end markers (post-scan report is not pixels)
    tail_limit = len(data)
    for end_sig in [b'\x10\x04\x07\x2d\x07\xd0', b'\x10\x05\x07\x2d\x07\xd0']:
        pos = data.find(end_sig, read_pos)
        if 0 < pos < tail_limit:
            tail_limit = pos
    if tail_limit > read_pos:
        segment = bytearray(data[read_pos:tail_limit])
        clean.extend(_repair_inline_telemetry(segment))

    if echo_sizes_log:
        from collections import Counter
        dist = Counter(echo_sizes_log)
        log.info("Echo sizes: %s", dict(sorted(dist.items())))

    log.info(
        "Panoramic extract: %d raw -> %d clean bytes (from offset %d)",
        len(data), len(clean), pixel_start,
    )

    pixel_data = bytes(clean)
    total_pixels = len(pixel_data) // 2

    if total_pixels < 100000:
        log.warning("Panoramic: too few pixels (%d)", total_pixels)
        return []

    # ── 5. Determine image dimensions ─────────────────────────────────────
    img_height = detector_height if detector_height > 0 else PANO_DEFAULT_HEIGHT
    img_width = total_pixels // img_height

    expected_width = PANO_DEFAULT_WIDTH
    if abs(img_width - expected_width) < 100:
        img_width = expected_width

    usable_bytes = img_width * img_height * 2
    if usable_bytes > len(pixel_data):
        img_width = len(pixel_data) // (img_height * 2)
        usable_bytes = img_width * img_height * 2

    pixels = np.frombuffer(pixel_data[:usable_bytes], dtype=">u2")
    img_array = pixels.reshape(img_width, img_height)  # (cols, rows)

    log.info(
        "Panoramic: %d columns x %d rows, pixel range %d-%d, mean %.0f",
        img_width, img_height,
        img_array.min(), img_array.max(), img_array.mean(),
    )

    # ── 6. Repair remaining artifact rows ─────────────────────────────────
    img_2d = img_array.T.astype(np.float32)  # (height, width)
    repaired_rows: list[int] = []

    row_means = np.mean(img_2d, axis=1)
    row_diffs = np.abs(np.diff(row_means))
    diff_median = np.median(row_diffs)
    diff_std = np.std(row_diffs)
    threshold = diff_median + 6 * diff_std

    for r in range(1, img_height - 1):
        if r < len(row_diffs) and row_diffs[r] > threshold:
            repaired_rows.append(r)
            if r + 1 < img_height - 1:
                repaired_rows.append(r + 1)

    repaired_rows = sorted(set(repaired_rows))

    if repaired_rows:
        repaired_set = set(repaired_rows)
        for r in repaired_rows:
            above = r - 1
            while above in repaired_set and above > 0:
                above -= 1
            below = r + 1
            while below in repaired_set and below < img_height - 1:
                below += 1
            if above >= 0 and below < img_height:
                t = (r - above) / max(below - above, 1)
                img_2d[r] = img_2d[above] * (1 - t) + img_2d[below] * t

        img_array = img_2d.T.astype(np.uint16)
        log.info("Panoramic: repaired %d artifact rows", len(repaired_rows))

    # ── 7. Column realignment ────────────────────────────────────────────
    #   With inline telemetry properly repaired (including 0x20 byte-
    #   stuffed variants), real vertical shifts should be rare.  Any
    #   remaining shifts come from unrepaired telemetry residue.
    #
    #   Guard against false positives from:
    #     - Noisy early columns (scan start, before X-ray exposure)
    #     - Telemetry frame boundaries where repaired blocks decorrelate
    #       adjacent columns without any actual vertical shift
    #
    #   A shift is only accepted when the improved correlation exceeds an
    #   absolute minimum (0.85), not just a relative improvement.  The
    #   first 5% of columns are skipped to avoid scan-start noise.
    img_f = img_array.T.astype(np.float32) if img_array.dtype != np.float32 else img_array.T.copy()
    # img_f is (height, width)

    col_corrs = np.zeros(img_width)
    for c in range(1, img_width):
        c1 = img_f[:, c - 1]; c2 = img_f[:, c]
        c1z = c1 - c1.mean(); c2z = c2 - c2.mean()
        d = np.sqrt(np.dot(c1z, c1z) * np.dot(c2z, c2z))
        col_corrs[c] = np.dot(c1z, c2z) / (d + 1e-10) if d > 0 else 0

    median_corr = np.median(col_corrs[1:])
    shift_thresh = median_corr - 0.20
    MAX_SHIFT = 15
    MIN_SHIFTED_CORR = 0.85   # absolute minimum after shifting
    SKIP_COLS = max(img_width // 20, 50)  # skip noisy scan start

    cumshift = 0
    col_shifts = np.zeros(img_width, dtype=int)
    realigned_count = 0

    for c in range(1, img_width):
        if c < SKIP_COLS or col_corrs[c] >= shift_thresh:
            col_shifts[c] = cumshift
            continue
        c_prev = img_f[:, c - 1]
        c_curr = img_f[:, c]
        best_s = 0
        best_nc = col_corrs[c]
        for s in range(-MAX_SHIFT, MAX_SHIFT + 1):
            if s == 0:
                continue
            if s > 0:
                a = c_prev[s:]; b = c_curr[:img_height - s]
            else:
                a = c_prev[:img_height + s]; b = c_curr[-s:]
            if len(a) < 200:
                continue
            az = a - a.mean(); bz = b - b.mean()
            dd = np.sqrt(np.dot(az, az) * np.dot(bz, bz))
            nc = np.dot(az, bz) / (dd + 1e-10)
            if nc > best_nc + 0.10:
                best_nc = nc
                best_s = s
        if best_s != 0 and best_nc >= MIN_SHIFTED_CORR:
            cumshift += best_s
            realigned_count += 1
        col_shifts[c] = cumshift

    if realigned_count:
        aligned = np.zeros_like(img_f)
        for c in range(img_width):
            s = int(col_shifts[c])
            if s == 0:
                aligned[:, c] = img_f[:, c]
            elif s > 0:
                aligned[s:, c] = img_f[:img_height - s, c]
                aligned[:s, c] = img_f[s, c]
            else:
                aligned[:img_height + s, c] = img_f[-s:, c]
                aligned[img_height + s:, c] = img_f[img_height + s - 1, c]
        img_array = aligned.T.astype(np.uint16)
        log.info("Column realignment: %d shift points corrected "
                 "(range %d to %d pixels)",
                 realigned_count, col_shifts.min(), col_shifts.max())
    else:
        log.info("Column realignment: no shifts needed")

    # ── 8. Build Scanline objects (one per column) ────────────────────────
    scanlines = []
    for col_idx in range(img_width):
        scanlines.append(Scanline(
            scanline_id=col_idx & 0xFF,
            pixel_count=img_height,
            pixels=img_array[col_idx] if img_array.dtype == np.uint16
            else img_array[col_idx].astype(np.uint16),
        ))

    return scanlines


def _extract_events(data: bytes) -> list[ScanEvent]:
    """Extract ASCII log events embedded in TCP payloads."""
    events = []

    for pattern, event_type in [
        (RE_RECORDING_START, "recording_start"),
        (RE_RECORDING_STOP, "recording_stop"),
        (RE_IMAGE_TRANSFER_START, "imagetransfer_start"),
        (RE_IMAGE_TRANSFER_STOP, "imagetransfer_stop"),
        (RE_STATE_RELEASED, "state_released"),
        (RE_E7_ERROR, "e7_error"),
    ]:
        for m in pattern.finditer(data):
            # Try to find a preceding timestamp
            ts_str = ""
            search_start = max(0, m.start() - 80)
            ts_match = RE_TIMESTAMP.search(data[search_start:m.start()])
            if ts_match:
                ts_str = ts_match.group(1).decode("ascii", errors="replace")

            detail = ""
            if event_type == "recording_start" and m.lastindex:
                detail = f"Value: {m.group(1).decode()}"
            elif event_type == "e7_error":
                detail = "ERR_SIDEXIS_API (treat as post-scan success)"

            events.append(ScanEvent(
                timestamp_str=ts_str,
                event_type=event_type,
                detail=detail,
            ))

    return events


def _pair_heartbeats(capture: DecodedCapture) -> None:
    """Match HB_REQUEST frames with their HB_RESPONSE partners."""
    requests = [f for f in capture.frames if f.func_code == FC_HB_REQUEST]
    responses = [f for f in capture.frames if f.func_code == FC_HB_RESPONSE]

    for req in requests:
        # Find the closest response after this request
        best = None
        for resp in responses:
            if resp.timestamp >= req.timestamp:
                if best is None or resp.timestamp < best.timestamp:
                    best = resp
        if best:
            capture.hb_pairs.append((req, best))
            responses.remove(best)  # don't reuse


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Image Reconstruction
# ╚══════════════════════════════════════════════════════════════════════════════

def reconstruct_image(
    scanlines: list[Scanline],
    invert: bool = True,
) -> Image.Image | None:
    """Reconstruct a panoramic image from decoded scanlines.

    Each scanline contributes one column of the panoramic image.  The
    Orthophos detector sweeps horizontally, so each scanline is a
    vertical strip of *height* pixels.

    Args:
        scanlines: List of Scanline objects (one per column).
        invert: If True (default), invert for dental convention
                (MONOCHROME1 — bone/tooth = white, air = black).

    Returns:
        PIL Image (8-bit grayscale) or None.
    """
    if not scanlines:
        return None

    # Determine consistent pixel count (most common)
    counts: dict[int, int] = {}
    for sl in scanlines:
        counts[sl.pixel_count] = counts.get(sl.pixel_count, 0) + 1
    target_count = max(counts, key=counts.get)

    # Filter to scanlines with the expected pixel count
    valid = [sl for sl in scanlines if sl.pixel_count == target_count]
    if not valid:
        return None

    log.info(
        "Reconstructing image: %d scanlines x %d pixels",
        len(valid), target_count,
    )

    # Build the image array: each scanline becomes one column
    width = len(valid)
    height = target_count

    img_array = np.zeros((height, width), dtype=np.uint16)
    for col, sl in enumerate(valid):
        img_array[:, col] = sl.pixels[:height]

    img_f = img_array.astype(np.float32)

    # ── Dark current correction ──────────────────────────────────────
    #   The first ~100 columns are pre-exposure dark frames (before the
    #   X-ray turns on) and the last ~25 columns are post-exposure.
    #   Compute the per-row dark baseline from these regions and subtract
    #   it, interpolating linearly across the scan to account for thermal
    #   drift during the sweep.
    DARK_PRE_COLS = min(100, width // 10)
    DARK_POST_COLS = min(25, width // 20)
    if DARK_PRE_COLS >= 10 and DARK_POST_COLS >= 5:
        from scipy.ndimage import uniform_filter1d as _uf1d
        dark_pre = np.median(img_f[:, :DARK_PRE_COLS], axis=1)
        dark_post = np.median(img_f[:, -DARK_POST_COLS:], axis=1)
        # Filter out anomalous rows (telemetry spikes) in dark profiles
        dp_med = np.median(dark_pre)
        dq_med = np.median(dark_post)
        for r in range(height):
            if abs(dark_pre[r] - dp_med) > 500:
                dark_pre[r] = dp_med
            if abs(dark_post[r] - dq_med) > 500:
                dark_post[r] = dq_med
        dark_pre = _uf1d(dark_pre, size=11)
        dark_post = _uf1d(dark_post, size=11)
        # Subtract with linear interpolation across scan
        for c in range(width):
            t = c / max(width - 1, 1)
            img_f[:, c] -= dark_pre * (1 - t) + dark_post * t
        img_f = np.maximum(img_f, 0)
        log.info("Dark correction: pre=%.0f, post=%.0f, drift=%.0f",
                 dp_med, dq_med, dq_med - dp_med)

    # ── Row repair ─────────────────────────────────────────────────────
    #   1. Telemetry-repair spike rows: the 36-pixel interpolated blocks
    #      drift across columns, creating single-row brightness spikes.
    #   2. Dead/anomalous rows from die gaps and center junction.
    row_means = np.mean(img_f, axis=1)
    global_med = np.median(row_means[row_means > 0]) if np.any(row_means > 0) else 1.0
    spike_thresh = max(global_med * 0.05, 20)  # 5% of signal or minimum 20
    spike_rows: set[int] = set()
    for r in range(1, height - 1):
        baseline = (row_means[r - 1] + row_means[r + 1]) / 2.0
        spike = abs(row_means[r] - baseline)
        if spike > spike_thresh and spike > abs(row_means[r - 1] - row_means[r + 1]) * 2 + 1:
            spike_rows.add(r)

    row_std = np.std(img_f, axis=1)
    for r in range(height):
        if row_std[r] < 5:
            spike_rows.add(r)
        elif row_means[r] > global_med * 5:
            spike_rows.add(r)

    for r in sorted(spike_rows):
        above = r - 1
        while above in spike_rows and above > 0:
            above -= 1
        below = r + 1
        while below in spike_rows and below < height - 1:
            below += 1
        if above not in spike_rows and below not in spike_rows:
            t = (r - above) / max(below - above, 1)
            img_f[r] = img_f[above] * (1 - t) + img_f[below] * t
    if spike_rows:
        log.info("Row repair: %d rows interpolated", len(spike_rows))

    # ── Frame gain equalization ─────────────────────────────────────────
    #   Each TCP frame (~24.9 columns) has slightly different detector
    #   gain.  Compute the median gain per frame, smooth across frames
    #   to get the expected exposure trend, then correct each frame so
    #   frame-to-frame steps vanish while the slow trend is preserved.
    from scipy.ndimage import uniform_filter1d
    stable_lo = height // 6
    stable_hi = height * 5 // 8
    col_profile = np.median(img_f[stable_lo:stable_hi, :], axis=0)

    COLS_PER_FRAME = 32768.0 / height  # pixels-per-frame / detector-height
    num_frames = int(width / COLS_PER_FRAME) + 1

    frame_gains = np.zeros(num_frames)
    for fi in range(num_frames):
        c0 = int(fi * COLS_PER_FRAME)
        c1 = min(int((fi + 1) * COLS_PER_FRAME), width)
        if c0 < width and c1 > c0:
            frame_gains[fi] = np.median(col_profile[c0:c1])

    # Replace zero/near-zero gains (dark frames) with NaN so they don't
    # distort the smoothed trend, then interpolate across them.
    min_signal = np.max(frame_gains) * 0.05
    active_gains = frame_gains.copy()
    active_gains[active_gains < min_signal] = np.nan
    # Fill NaN with nearest valid value for smoothing
    valid_mask = ~np.isnan(active_gains)
    if np.any(valid_mask):
        first_valid = np.argmax(valid_mask)
        last_valid = len(active_gains) - 1 - np.argmax(valid_mask[::-1])
        for i in range(first_valid):
            active_gains[i] = active_gains[first_valid]
        for i in range(last_valid + 1, len(active_gains)):
            active_gains[i] = active_gains[last_valid]
        # Linear interpolation for interior NaN
        nans = np.isnan(active_gains)
        if np.any(nans):
            active_gains[nans] = np.interp(
                np.where(nans)[0], np.where(~nans)[0], active_gains[~nans]
            )

    def _apply_frame_eq(gains_arr, smooth_sz, clip_lo, clip_hi):
        """Apply one pass of frame gain equalization."""
        ag = gains_arr.copy()
        ag[ag < min_signal] = np.nan
        vm = ~np.isnan(ag)
        if not np.any(vm):
            return
        fv = int(np.argmax(vm))
        lv = len(ag) - 1 - int(np.argmax(vm[::-1]))
        ag[:fv] = ag[fv]
        ag[lv + 1:] = ag[lv]
        nans = np.isnan(ag)
        if np.any(nans):
            ag[nans] = np.interp(
                np.where(nans)[0], np.where(~nans)[0], ag[~nans]
            )
        trend = uniform_filter1d(ag, size=smooth_sz)
        with np.errstate(divide='ignore', invalid='ignore'):
            fc = np.where(gains_arr > min_signal, trend / gains_arr, 1.0)
        fc = np.clip(fc, clip_lo, clip_hi)
        for c in range(width):
            ff = c / COLS_PER_FRAME
            f0 = int(ff)
            f1 = min(f0 + 1, num_frames - 1)
            t = ff - f0
            img_f[:, c] *= fc[f0] * (1 - t) + fc[f1] * t

    # Pass 1: broad equalization (11-frame smooth) — removes large-scale steps
    _apply_frame_eq(frame_gains, smooth_sz=11, clip_lo=0.80, clip_hi=1.25)

    # Pass 2: narrow equalization (3-frame smooth) — catches remaining outliers
    col_profile2 = np.median(img_f[stable_lo:stable_hi, :], axis=0)
    frame_gains2 = np.zeros(num_frames)
    for fi in range(num_frames):
        c0 = int(fi * COLS_PER_FRAME)
        c1 = min(int((fi + 1) * COLS_PER_FRAME), width)
        if c0 < width and c1 > c0:
            frame_gains2[fi] = np.median(col_profile2[c0:c1])
    _apply_frame_eq(frame_gains2, smooth_sz=3, clip_lo=0.92, clip_hi=1.08)

    log.info("Frame equalization: %d frames (two-pass)", num_frames)

    # ── Per-column flat-field (residual) ─────────────────────────────
    #   After frame equalization, remove any remaining per-column gain
    #   variation by dividing each column by its median and restoring
    #   the slowly-varying exposure trend.
    col_meds = np.median(img_f[stable_lo:stable_hi, :], axis=0)
    col_meds[col_meds == 0] = 1
    col_trend = uniform_filter1d(col_meds, size=101)
    img_f *= (col_trend / col_meds)[np.newaxis, :]

    # ── WWE tone mapping + CLAHE ───────────────────────────────────
    #   Window Width Equalization inspired by Sidexis WWEXP=1,7000,0,750,20.
    #   1. Percentile-normalise to [0,1]
    #   2. Apply gamma correction (gamma ≈ 0.4): expands tissue/bone detail,
    #      compresses air/background — matches Sidexis output histogram
    #   3. Invert for dental display convention (bone = bright)
    #   4. CLAHE for local contrast enhancement
    WWE_GAMMA = 0.4
    p_lo, p_hi = np.percentile(img_f, [0.5, 99.5])
    if p_hi <= p_lo:
        p_hi = p_lo + 1.0

    img_norm = np.clip((img_f - p_lo) / (p_hi - p_lo), 0, 1)

    if invert:
        # display = 1 - raw^gamma  (bone bright, air dark)
        img_norm = 1.0 - np.power(img_norm, WWE_GAMMA)
    else:
        img_norm = np.power(img_norm, WWE_GAMMA)

    img_16 = (img_norm * 65535).astype(np.uint16)

    try:
        import cv2
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 16))
        img_16 = clahe.apply(img_16)
        log.info("Applied WWE gamma=%.1f + CLAHE", WWE_GAMMA)
    except ImportError:
        pass  # cv2 not available — skip CLAHE

    img_8bit = (img_16 >> 8).astype(np.uint8)

    # ── Crop to standard output size ───────────────────────────────
    #   Sidexis outputs 2440×1280 from the raw 2706×1316.
    #   Remove: 18 rows top (dark reference / border)
    #           18 rows bottom (dark reference / die junction tail)
    #           ~133 columns left (pre-exposure dead + scan start)
    #           ~133 columns right (scan end + post-exposure)
    #   Adjust dynamically if the image is smaller than expected.
    CROP_H, CROP_W = 1280, 2440
    if height > CROP_H:
        row_top = min(18, (height - CROP_H) // 2)
        row_bot = row_top + CROP_H
        img_8bit = img_8bit[row_top:row_bot, :]
    if width > CROP_W:
        # Center the crop on the active scan region (skip pre-exposure left)
        col_left = min((width - CROP_W), max(20, (width - CROP_W) // 2))
        col_right = col_left + CROP_W
        img_8bit = img_8bit[:, col_left:col_right]
    crop_h, crop_w = img_8bit.shape
    log.info("Cropped: %dx%d -> %dx%d", width, height, crop_w, crop_h)

    # ── Sharpen ──────────────────────────────────────────────────────
    img_pil = Image.fromarray(img_8bit, mode="L")
    try:
        from PIL import ImageFilter
        img_pil = img_pil.filter(
            ImageFilter.UnsharpMask(radius=2, percent=80, threshold=3)
        )
    except Exception:
        pass

    log.info(
        "Panoramic stitched: %dx%d%s",
        crop_w, crop_h, " (inverted)" if invert else "",
    )

    return img_pil


def save_scanline_pngs(
    scanlines: list[Scanline], outdir: Path
) -> list[Path]:
    """Save each scanline as an individual PNG strip."""
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []

    for idx, sl in enumerate(scanlines):
        # Create a 1-pixel-wide vertical strip image
        arr = sl.pixels_8bit.reshape(-1, 1)  # Nx1 column
        img = Image.fromarray(arr, mode="L")

        filename = f"HB_{idx + 1:04d}_sl{sl.scanline_id:02X}.png"
        path = outdir / filename
        img.save(path)
        paths.append(path)

    return paths


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Live TCP Client
# ╚══════════════════════════════════════════════════════════════════════════════

class SironaLiveClient:
    """Live TCP client for Sirona Orthophos direct connection.

    Implements the P2K session handshake, heartbeat loop, and scan
    data capture as observed in the ff.txt Wireshark dump.
    """

    def __init__(
        self,
        host: str = "192.168.139.170",
        port: int = 12837,
        hb_interval: float = 0.4,
        timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.hb_interval = hb_interval
        self.timeout = timeout

        self._sock: socket.socket | None = None
        self._hb_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._hb_seq = 0
        self._connected = False
        self._device_status_code: int = -1  # last polled device status
        self._armed = False          # patient data sent, waiting for button
        self._exposing_active = False  # device actively exposing (got 0x1005)

        # Diagnostic ring buffer — last N HB/status entries for failure dumps
        self._diag_ring: list[str] = []
        self._diag_ring_max: int = 20
        self._last_recv_frame: bytes = b""  # last raw frame for disconnect diagnosis

        # Callbacks
        self.on_hb: list = []           # (seq, rtt_ms) → None
        self.on_status: list = []       # (status_str) → None
        self.on_device_status: list = []  # (status_code: int) → None
        self.on_kv_sample: list = []    # (KVSample) → None
        self.on_scanline: list = []     # (Scanline) → None
        self.on_event: list = []        # (str) → None
        self.on_error: list = []        # (Exception) → None

    # ── Connection lifecycle ─────────────────────────────────────────────

    def connect(self) -> None:
        """Open TCP connection and perform P2K session handshake.

        The handshake sends SESSION_OPEN_REQ (0x205C) and waits up to 1 s
        for SESSION_OPEN_ACK (0x205D).  Some firmware variants (notably on
        Windows-attached units) silently ignore the OPEN request but do
        respond to SESSION_INIT (0x2001) and HB_REQUEST (0x200B).  When
        no ACK arrives within 1 s the OPEN step is skipped and the method
        proceeds directly to INIT — this is not an error.
        """
        log.info("Connecting to %s:%d ...", self.host, self.port)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect((self.host, self.port))
        self._sock = sock
        log.info("TCP connected")

        # ── 1. SESSION_OPEN  (optional — may be ignored by some firmware) ─
        self._send_session_frame(FC_SESSION_OPEN_REQ, flags=0x000F)

        saved_timeout = sock.gettimeout()
        sock.settimeout(1.0)                  # 1 s window for ACK
        try:
            resp = self._recv_frame()
            fc = (resp[0] << 8) | resp[1] if len(resp) >= 2 else 0
            if fc == FC_SESSION_OPEN_ACK:
                log.info("Session opened (0x205D ACK)")
            else:
                # Got *something* but not the ACK — log and carry on.
                log.info(
                    "SESSION_OPEN response was 0x%04X (not ACK) "
                    "\u2014 proceeding to INIT",
                    fc,
                )
        except socket.timeout:
            log.info(
                "SESSION_OPEN ACK skipped (device silent) "
                "\u2014 proceeding to INIT"
            )
        finally:
            sock.settimeout(saved_timeout)    # restore original timeout

        # ── 2. SESSION_INIT  (always sent) ────────────────────────────────
        self._send_session_frame(FC_SESSION_INIT)
        resp = self._recv_frame()
        fc = (resp[0] << 8) | resp[1] if len(resp) >= 2 else 0
        log.info("Session init response: 0x%04X (%d bytes)", fc, len(resp))

        self._connected = True
        self._session_start = time.perf_counter()
        self._hb_responses_received = 0
        self._fire(self.on_status, "CONNECTED")

    def disconnect(self) -> None:
        """Stop HB thread and close the TCP socket."""
        self._stop.set()
        if self._hb_thread:
            self._hb_thread.join(timeout=3.0)
            self._hb_thread = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._connected = False
        self._armed = False
        self._exposing_active = False
        log.info("Disconnected")

    # ── Heartbeat loop ───────────────────────────────────────────────────

    def start_hb_loop(self) -> None:
        """Start the background heartbeat thread."""
        self._stop.clear()
        self._hb_thread = threading.Thread(
            target=self._hb_loop, name="sirona-hb", daemon=True
        )
        self._hb_thread.start()
        log.info("HB loop started (interval=%.1fs)", self.hb_interval)

    # Maximum session age before proactive refresh (device hard limit ~2 s)
    SESSION_REFRESH_S = 1.5

    def _hb_loop(self) -> None:
        """Send HB_REQUEST, wait for HB_RESPONSE, repeat.

        Operates in three modes:
          NORMAL:   Session refresh every 1.5s, send HB, recv response.
          ARMED:    No session refresh (session must survive until scan).
                    Send HB, recv — watch for EXPOSE_NOTIFY (0x1005).
          EXPOSING: Device is flooding data.  Recv in tight loop (no HB
                    needed).  Detect end-of-data → send IMAGE_ACK.
        """
        while not self._stop.is_set():
            try:
                # ── EXPOSING mode: tight recv loop ────────────────────
                if self._exposing_active:
                    self._recv_scan_data()
                    continue

                # ── Session refresh (only in NORMAL mode) ─────────────
                if not self._armed:
                    session_age = time.perf_counter() - self._session_start
                    if session_age >= self.SESSION_REFRESH_S:
                        self._session_refresh()
                        continue

                # ── Send HB (NORMAL and ARMED modes) ──────────────────
                self._hb_seq += 1
                t0 = time.perf_counter()

                with self._lock:
                    self._send_session_frame(FC_HB_REQUEST)
                    resp = self._recv_frame()

                rtt_ms = (time.perf_counter() - t0) * 1000
                fc = (resp[0] << 8) | resp[1] if len(resp) >= 2 else 0

                if fc == FC_HB_RESPONSE:
                    self._hb_responses_received += 1
                    self._diag_push(
                        f"HB seq={self._hb_seq} rtt={rtt_ms:.0f}ms "
                        f"armed={self._armed}"
                    )
                    self._fire(self.on_hb, self._hb_seq, rtt_ms)

                elif fc == FC_EXPOSE_NOTIFY:
                    # Physical button pressed — device starting exposure.
                    # The 0x1005 frame often carries a large payload that
                    # includes the first scan data (embedded 0x1002 header
                    # + kV ramp / patient echo).  Stash it so _recv_scan_data
                    # can prepend it to the scan buffer.
                    self._exposing_active = True
                    payload = resp[SESSION_HEADER_SIZE:] if len(resp) > SESSION_HEADER_SIZE else b""
                    self._expose_initial_data = payload
                    self._diag_push(
                        f"EXPOSE_NOTIFY (0x1005) {len(payload)}B payload"
                    )
                    log.info(
                        "EXPOSE_NOTIFY received — exposure starting! "
                        "%d bytes initial data", len(payload),
                    )
                    self._fire(self.on_event, "EXPOSE_STARTED")
                    self._fire(self.on_status, "EXPOSING")
                    continue  # switch to recv loop immediately

                elif fc == FC_SESSION_OPEN_ACK:
                    # Unsolicited device session refresh — just log it
                    self._diag_push("Device SESSION_REFRESH (0x205D)")

                elif fc == FC_SESSION_CONFIRM:
                    # Unsolicited confirm — device cycling session
                    self._diag_push("Device SESSION_CONFIRM (0x2002)")

                else:
                    # Might be scan data or status — process it
                    self._diag_push(
                        f"DATA fc=0x{fc:04X} len={len(resp)}"
                    )
                    self._process_live_data(resp)

            except socket.timeout:
                self._diag_push("HB_TIMEOUT (no response)")
                self._fire(self.on_status, "HB_TIMEOUT")
            except OSError as exc:
                # Log the LAST frame received before the connection dropped
                last = self._last_recv_frame
                if last:
                    last_fc = (last[0] << 8 | last[1]) if len(last) >= 2 else 0
                    last_payload = last[SESSION_HEADER_SIZE:] if len(last) > SESSION_HEADER_SIZE else b""
                    log.warning(
                        "LAST FRAME before drop: fc=0x%04X len=%d first20=%s",
                        last_fc, len(last),
                        last_payload[:20].hex() if last_payload else "(empty)",
                    )

                session_age = time.perf_counter() - self._session_start
                early_reject = (
                    session_age < 2.0
                    and self._hb_responses_received == 0
                )

                if early_reject:
                    msg = (
                        "Device rejected session \u2014 another client "
                        "may be connected (close Sidexis)"
                    )
                    self._diag_push(f"SESSION_REJECTED: {exc} "
                                    f"(age={session_age:.1f}s, 0 HB)")
                    log.warning("%s  (%s)", msg, exc)
                    self._fire(self.on_event, msg)
                    self._fire(self.on_status, "SESSION_REJECTED")
                    self._attempt_reconnect(backoff_s=10.0)
                else:
                    self._diag_push(f"HB_ERROR: {exc}")
                    log.error("HB loop error: %s", exc)
                    self._fire(self.on_error, exc)
                    self._fire(self.on_status, "RECONNECTING")
                    self._attempt_reconnect(backoff_s=2.0)
                break

            self._stop.wait(self.hb_interval)

    def _recv_scan_data(self) -> None:
        """Receive scan data in a tight loop during active exposure.

        The device sends data as a continuous TCP byte stream:
          1. EXPOSE_NOTIFY (0x1005) with embedded 0x1002 header + data
          2. Raw data chunks (no per-chunk session headers)
          3. Stream ends when no data arrives for 2s

        We accumulate all bytes into a single buffer, then parse kV
        samples and scanlines from the complete buffer.
        """
        if self._sock is None:
            return

        saved_timeout = self._sock.gettimeout()
        self._sock.settimeout(2.0)

        scan_buffer = bytearray()
        chunk_count = 0

        # Seed buffer with data from the EXPOSE_NOTIFY payload
        initial = getattr(self, '_expose_initial_data', b'')
        if initial:
            scan_buffer.extend(initial)
            self._expose_initial_data = b''
            log.info("Seeded scan buffer with %d bytes from EXPOSE_NOTIFY", len(initial))

        try:
            while not self._stop.is_set() and self._exposing_active:
                try:
                    with self._lock:
                        data = self._sock.recv(65536)

                    if not data:
                        raise ConnectionError("Connection closed during scan")

                    chunk_count += 1
                    self._last_recv_frame = data

                    # The first chunk after EXPOSE_NOTIFY may contain an
                    # embedded 0x1002 session header.  Find and skip it so
                    # the buffer contains only raw scan data.
                    if chunk_count <= 2:
                        # Look for 0x1002 header signature in first chunks.
                        # This frame contains calibration data (SGFHeader,
                        # DieWidthPixel, DarkCurrentRows, etc.) — save it
                        # before stripping.
                        sig_1002 = b'\x10\x02\x07\x2d\x07\xd0'
                        pos = data.find(sig_1002)
                        if pos >= 0:
                            # Save the full 0x1002 frame for calibration
                            calib_data = data[pos:]
                            try:
                                calib_path = LOG_DIR / "last_scan_calibration.bin"
                                with open(calib_path, "wb") as cf:
                                    cf.write(calib_data)
                                log.info(
                                    "Saved 0x1002 calibration frame: %s (%d bytes)",
                                    calib_path, len(calib_data),
                                )
                            except Exception as exc:
                                log.warning("Failed to save calibration: %s", exc)

                            # Strip the 20-byte session header for pixel stream
                            data = data[pos + SESSION_HEADER_SIZE:]
                            log.info(
                                "Scan chunk %d: stripped 0x1002 header at "
                                "offset %d, %d bytes remain",
                                chunk_count, pos, len(data),
                            )

                    scan_buffer.extend(data)

                    if chunk_count % 50 == 0:
                        log.info(
                            "Scan progress: %d chunks, %.1f KB buffered",
                            chunk_count, len(scan_buffer) / 1024,
                        )

                except socket.timeout:
                    log.info(
                        "Scan data stream ended (timeout) — "
                        "%d chunks, %.1f KB total",
                        chunk_count, len(scan_buffer) / 1024,
                    )
                    break

        finally:
            self._sock.settimeout(saved_timeout)

        # ── Save raw buffer for offline analysis ──────────────────────
        log.info(
            "Parsing scan buffer: %d bytes from %d chunks",
            len(scan_buffer), chunk_count,
        )
        try:
            raw_path = LOG_DIR / "last_scan_raw.bin"
            with open(raw_path, "wb") as f:
                f.write(scan_buffer)
            log.info("Raw scan buffer saved: %s (%d bytes)", raw_path, len(scan_buffer))
        except Exception as exc:
            log.warning("Failed to save raw buffer: %s", exc)

        raw = bytes(scan_buffer)

        # Extract kV ramp samples
        kv_samples = _extract_kv_samples(raw)
        kv_peak = 0
        if kv_samples:
            kv_peak = max(s.kv_raw for s in kv_samples)
            log.info(
                "kV ramp: %d samples, peak raw=0x%04X (%.1f kV)",
                len(kv_samples), kv_peak, kv_peak / 10.0,
            )
            peak_sample = max(kv_samples, key=lambda s: s.kv_raw)
            self._fire(self.on_kv_sample, peak_sample)
        # Store peak kV for direct retrieval by the GUI (avoids
        # race with after(0,...) callback ordering).
        self._scan_kv_peak = kv_peak / 10.0

        # Extract full panoramic image from continuous pixel stream
        scanlines = _extract_panoramic(raw)
        if not scanlines:
            # Fallback to marker-based extraction
            scanlines = _extract_scanlines(raw)
            if scanlines:
                log.info(
                    "Marker scanlines: %d (IDs 0x%02X-0x%02X)",
                    len(scanlines),
                    scanlines[0].scanline_id,
                    scanlines[-1].scanline_id,
                )

        if scanlines:
            log.info(
                "Image: %d columns x %d px = %dx%d panoramic",
                len(scanlines), scanlines[0].pixel_count,
                len(scanlines), scanlines[0].pixel_count,
            )
            # Store scanlines for batch retrieval by the GUI.
            self._scan_scanlines = scanlines
            # Fire first and last to notify GUI without flooding Tk.
            self._fire(self.on_scanline, scanlines[0])
            if len(scanlines) > 1:
                self._fire(self.on_scanline, scanlines[-1])

        # Extract ASCII events (only fire unique event types)
        seen_types = set()
        for ev in _extract_events(raw):
            if ev.event_type not in seen_types:
                seen_types.add(ev.event_type)
                self._fire(self.on_event, f"{ev.event_type}: {ev.detail}")

        # ── Scan complete — send IMAGE_ACK ────────────────────────────
        self._exposing_active = False
        self._armed = False
        log.info("Scan data reception complete — sending IMAGE_ACK")
        self._diag_push(
            f"SCAN_COMPLETE — {len(scanlines)} scanlines, "
            f"{len(kv_samples)} kV samples"
        )

        try:
            self.send_image_ack()
        except Exception as exc:
            log.warning("IMAGE_ACK failed: %s (non-fatal)", exc)

        self._fire(self.on_event, "SCAN_COMPLETE")
        self._fire(self.on_status, "SCAN_COMPLETE")

    def _session_refresh(self) -> None:
        """Silently close and reopen the TCP session.

        The device enforces a hard ~2 s session limit.  This method
        cycles the connection without triggering error callbacks or
        backoff — the GUI stays CONNECTED throughout.

        MUST NOT be called when armed — the session must stay alive
        for the physical button press and subsequent data flood.
        """
        if self._armed or self._exposing_active:
            return  # never refresh during armed/exposing state
        log.debug("Session refresh (device 2s limit)")
        self._diag_push("SESSION_REFRESH")

        # Close the old socket
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

        # Reconnect: new TCP + SESSION_OPEN + SESSION_INIT
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect((self.host, self.port))
        self._sock = sock

        # SESSION_OPEN (optional ACK)
        self._send_session_frame(FC_SESSION_OPEN_REQ, flags=0x000F)
        saved_timeout = sock.gettimeout()
        sock.settimeout(1.0)
        try:
            self._recv_frame()
        except socket.timeout:
            pass
        finally:
            sock.settimeout(saved_timeout)

        # SESSION_INIT
        self._send_session_frame(FC_SESSION_INIT)
        self._recv_frame()

        self._session_start = time.perf_counter()

    def _attempt_reconnect(self, backoff_s: float = 2.0) -> None:
        """Reconnect after connection loss.

        Args:
            backoff_s: Seconds to wait between retry attempts.  Use 10.0
                       for session-rejected (another client) scenarios,
                       2.0 (default) for normal post-scan E7 recovery.
        """
        log.info("Attempting reconnect (backoff=%.0fs)...", backoff_s)
        self._fire(self.on_event,
                   f"Reconnecting (backoff={backoff_s:.0f}s)")

        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

        for attempt in range(1, 6):
            if self._stop.is_set():
                return
            time.sleep(backoff_s)
            try:
                self.connect()
                self.start_hb_loop()
                self._fire(self.on_event, f"Reconnected after {attempt} attempt(s)")
                return
            except Exception as exc:
                log.warning("Reconnect attempt %d failed: %s", attempt, exc)

        self._fire(self.on_error, ConnectionError("Reconnect failed after 5 attempts"))

    # ── Device status query ─────────────────────────────────────────────

    def query_status(self) -> int:
        """Return the last known device status code.

        NOTE: Active status polling via FC 0x1005 is not supported —
        that function code is EXPOSE_NOTIFY (device → host only).
        Status is inferred from HB responses and device events.
        Returns -1 if unknown.
        """
        return self._device_status_code

    @property
    def device_status_code(self) -> int:
        """Last known device status code, or -1 if unknown."""
        return self._device_status_code

    # ── Diagnostic ring buffer ────────────────────────────────────────

    def _diag_push(self, entry: str) -> None:
        """Append a timestamped diagnostic entry to the ring buffer."""
        ts = time.strftime("%H:%M:%S")
        self._diag_ring.append(f"[{ts}] {entry}")
        if len(self._diag_ring) > self._diag_ring_max:
            self._diag_ring = self._diag_ring[-self._diag_ring_max:]

    def dump_diagnostics(self, last_n: int = 10) -> list[str]:
        """Return the most recent *last_n* HB/status diagnostic entries."""
        return list(self._diag_ring[-last_n:])

    # ── Expose: arm + wait-for-button protocol ───────────────────────

    # Continuation data sent immediately after DATA_SEND (102 bytes).
    # This is the program/parameter table from ff.txt Sidexis capture.
    _DATA_CONTINUATION = bytes([
        0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x2c, 0x00, 0x02, 0x00, 0x01, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x2c, 0x00, 0x03, 0x00, 0x01,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x2c, 0x00, 0x01,
        0x00, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x2c,
        0x00, 0x02, 0x00, 0x02, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x2c, 0x00, 0x00, 0x00, 0x00, 0x00, 0x04,
        0x00, 0x08, 0x00, 0x01, 0x00, 0x0a, 0x00, 0x03,
        0xff, 0xff, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x05, 0x00, 0x00, 0x00, 0x02, 0xff, 0xff,
        0x00, 0x03, 0x00, 0x03, 0x00, 0x00, 0x00, 0x05,
        0xff, 0xff, 0x00, 0x00, 0x00, 0x00, 0x00, 0x05,
        0xff, 0xff, 0x00, 0x05, 0xff, 0xff,
    ])

    @staticmethod
    def _encode_utf16le_field(text: str) -> bytes:
        """Encode a string as [LE-uint16 length][UTF-16LE data]."""
        encoded = text.encode("utf-16-le")
        length = len(text)  # char count, not byte count
        return struct.pack("<H", length) + encoded

    # Exact 156-byte payload from ff.txt frame 750 DATA_SEND.
    # Packet bytes 0x14-0xAF (everything after the 20-byte session header).
    # Patient "test test", Doctor "Dr. Demo", Station "DESKTOP-NK6UFML".
    # Confirmed working against live device 2026-03-23.
    _DATA_SEND_TEMPLATE = bytes([
        0xfc,0x30,0x00,0x00,0x1f,0x00,0x05,0x00,0xe6,0x07,0x11,0x00,
        0x0f,0x00,0x29,0x00,0xfa,0x00,0xdb,0x04,0x9b,0x08,0x00,0x04,
        0x00,0x74,0x00,0x65,0x00,0x73,0x00,0x74,0x00,0x04,0x00,0x74,
        0x00,0x65,0x00,0x73,0x00,0x74,0x00,0x01,0x00,0x01,0x07,0xd1,
        0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
        0x00,0x08,0x00,0x44,0x00,0x72,0x00,0x2e,0x00,0x20,0x00,0x44,
        0x00,0x65,0x00,0x6d,0x00,0x6f,0x00,0x00,0x00,0x14,0x00,0x30,
        0x00,0x30,0x00,0x33,0x00,0x31,0x00,0x30,0x00,0x35,0x00,0x32,
        0x00,0x30,0x00,0x32,0x00,0x32,0x00,0x31,0x00,0x37,0x00,0x31,
        0x00,0x35,0x00,0x34,0x00,0x31,0x00,0x30,0x00,0x32,0x00,0x35,
        0x00,0x30,0x00,0x0f,0x00,0x44,0x00,0x45,0x00,0x53,0x00,0x4b,
        0x00,0x54,0x00,0x4f,0x00,0x50,0x00,0x2d,0x00,0x4e,0x00,0x4b,
        0x00,0x36,0x00,0x55,0x00,0x46,0x00,0x4d,0x00,0x4c,0x00,0x05,
    ])

    def _build_patient_payload(
        self,
        last_name: str = "test",
        first_name: str = "test",
        doctor: str = "Dr. Demo",
        study_id: str = "",
        workstation: str = "PUREXS",
    ) -> bytes:
        """Return the DATA_SEND payload for arming the device.

        Uses the exact 156-byte payload captured from ff.txt which is
        known to be accepted by the Orthophos XG.  Patient name fields
        in the payload are "test test" — the device does not validate
        these for exposure (they are for DICOM metadata only).
        """
        return self._DATA_SEND_TEMPLATE

    def arm_for_expose(
        self,
        last_name: str = "test",
        first_name: str = "test",
        doctor: str = "Dr. Demo",
        study_id: str = "",
        workstation: str = "PUREXS",
    ) -> None:
        """Arm the device for exposure: CAPS exchange + patient DATA_SEND.

        After this call the device is armed and waiting for the physical
        expose button to be pressed.  The HB loop continues but session
        refresh is disabled (the session must stay alive until the scan
        completes).

        The device will send FC_EXPOSE_NOTIFY (0x1005) when the operator
        presses the button, followed by kV ramp data and scanline images.

        Raises:
            ConnectionError: Socket not connected.
            RuntimeError: Unexpected device response.
        """
        if self._sock is None:
            raise ConnectionError("Not connected — cannot arm")

        # Force a fresh session so that CAPS_REQ is the FIRST command
        # after SESSION_INIT (no prior HB).  This matches the Sidexis
        # sequence and prevents the device from embedding extra kV
        # telemetry in the scan data echo payloads.
        if not self._armed:
            try:
                self._session_refresh()
                log.info("Fresh session for arm (no prior HB)")
            except Exception as exc:
                log.warning("Pre-arm session refresh failed: %s", exc)

        with self._lock:
            # 1. Capabilities exchange
            self._send_session_frame(FC_CAPS_REQ)
            caps_resp = self._recv_frame()
            caps_fc = (caps_resp[0] << 8) | caps_resp[1] if len(caps_resp) >= 2 else 0
            if caps_fc != FC_CAPS_RESP:
                log.warning(
                    "Expected CAPS_RESP (0x2111), got 0x%04X — continuing",
                    caps_fc,
                )
            else:
                log.info(
                    "CAPS_RESP received (%d bytes payload)",
                    len(caps_resp) - SESSION_HEADER_SIZE,
                )

            # 2. DATA_SEND (patient + exam info)
            payload = self._build_patient_payload(
                last_name, first_name, doctor, study_id, workstation,
            )
            # Header payload_length must cover BOTH the payload AND the
            # continuation data that follows in a separate TCP segment.
            total_len = len(payload) + len(self._DATA_CONTINUATION)
            self._send_data_frame(
                FC_DATA_SEND, payload, total_payload_length=total_len,
            )
            log.info(
                "DATA_SEND: %d bytes payload (total_len=%d incl continuation)",
                len(payload), total_len,
            )

            # 3. Continuation data (program parameters)
            self._sock.sendall(self._DATA_CONTINUATION)
            log.info("DATA continuation: %d bytes", len(self._DATA_CONTINUATION))

            # 4. Wait for DATA_ACK (0x1001)
            ack_resp = self._recv_frame()
            ack_fc = (ack_resp[0] << 8) | ack_resp[1] if len(ack_resp) >= 2 else 0
            if ack_fc != FC_DATA_ACK:
                log.warning(
                    "Expected DATA_ACK (0x1001), got 0x%04X", ack_fc,
                )
            else:
                log.info("DATA_ACK received — device armed")

        self._armed = True
        self._exposing_active = False
        self._diag_push("ARMED — waiting for physical expose button")
        self._fire(self.on_event, "ARMED")
        self._fire(self.on_status, "ARMED")
        log.info("Device armed — press the physical expose button on the unit")

    def send_expose(self) -> None:
        """DEPRECATED: Use arm_for_expose() instead.

        The old approach of sending raw kV ramp bytes as a 'trigger' was
        incorrect — those bytes are device telemetry, not a command.
        The Orthophos expose is triggered by the physical button on the
        unit.  arm_for_expose() sets up the device to accept exposure.
        """
        log.warning(
            "send_expose() is DEPRECATED — the Orthophos expose is "
            "triggered by the physical button. Calling arm_for_expose() "
            "with defaults instead."
        )
        self.arm_for_expose()

    def send_image_ack(self) -> None:
        """Send IMAGE_ACK (0x1008) after receiving all scan data.

        This tells the device we received the image data.  The device
        responds with IMAGE_ACK_RESP (0x1009) and the session can then
        be closed cleanly.
        """
        if self._sock is None:
            raise ConnectionError("Not connected")
        with self._lock:
            self._send_session_frame(FC_IMAGE_ACK)
            log.info("IMAGE_ACK (0x1008) sent")
            try:
                resp = self._recv_frame()
                fc = (resp[0] << 8) | resp[1] if len(resp) >= 2 else 0
                if fc == FC_IMAGE_ACK_RESP:
                    log.info("IMAGE_ACK_RESP (0x1009) received — scan complete")
                else:
                    log.info("Post-IMAGE_ACK response: 0x%04X", fc)
            except socket.timeout:
                log.info("No response to IMAGE_ACK (timeout) — OK")

    def send_raw(self, data: bytes) -> None:
        """Send arbitrary bytes on the session socket (for protocol research)."""
        if self._sock is None:
            raise ConnectionError("Not connected")
        with self._lock:
            self._sock.sendall(data)

    # ── Wire I/O ─────────────────────────────────────────────────────────

    def _build_session_header(
        self, func_code: int, flags: int = 0x000E,
        payload_length: int = 0,
    ) -> bytearray:
        """Build a 20-byte P2K session header (no send).

        Header layout (confirmed from ff.txt):
          +0x00  WORD   func_code      command family + sub-command
          +0x02  WORD   magic          0x072D
          +0x04  WORD   port           0x07D0
          +0x06  WORD   version        0x0001
          +0x08  WORD   flags          0x000E or 0x000F
          +0x0A  8B     reserved       zeros
          +0x12  WORD   payload_len    total bytes following this header (BE)
        """
        header = bytearray(SESSION_HEADER_SIZE)
        header[0] = (func_code >> 8) & 0xFF    # func_hi
        header[1] = func_code & 0xFF            # func_lo
        struct.pack_into(">H", header, 2, MAGIC)
        struct.pack_into(">H", header, 4, PORT_MARKER)
        struct.pack_into(">H", header, 6, 0x0001)  # version (always 1)
        struct.pack_into(">H", header, 8, flags)
        # bytes 10-17 are zeros (reserved)
        struct.pack_into(">H", header, 18, payload_length)
        return header

    def _send_session_frame(self, func_code: int, flags: int = 0x000E) -> None:
        """Build and send a 20-byte P2K session frame."""
        header = self._build_session_header(func_code, flags)
        if self._sock is None:
            raise ConnectionError("Not connected")
        self._sock.sendall(header)

    def _send_data_frame(
        self, func_code: int, payload: bytes, flags: int = 0x000E,
        total_payload_length: int | None = None,
    ) -> None:
        """Build session header + payload and send as one frame.

        Args:
            total_payload_length: If set, overrides the auto-computed
                payload_length in the header.  Use this when additional
                data (e.g. continuation bytes) will follow in a separate
                TCP segment — the header length field must cover ALL data.
        """
        plen = total_payload_length if total_payload_length is not None else len(payload)
        header = self._build_session_header(func_code, flags, payload_length=plen)
        if self._sock is None:
            raise ConnectionError("Not connected")
        self._sock.sendall(header + payload)

    def _recv_frame(self) -> bytes:
        """Receive data from the device. Returns at least the header."""
        if self._sock is None:
            raise ConnectionError("Not connected")
        # Read whatever is available (device sends variable-length frames)
        data = self._sock.recv(4096)
        if not data:
            raise ConnectionError("Connection closed by device")
        # Raw frame logging: func_code (hex) + payload len + first 20 bytes
        fc = (data[0] << 8 | data[1]) if len(data) >= 2 else 0
        payload = data[SESSION_HEADER_SIZE:] if len(data) > SESSION_HEADER_SIZE else b""
        preview = payload[:20].hex() if payload else "(empty)"
        log.info(
            "RECV fc=0x%04X payload_len=%d first20=%s",
            fc, len(payload), preview,
        )
        self._last_recv_frame = data
        return data

    def _process_live_data(self, data: bytes) -> None:
        """Process non-HB data received during the live loop."""
        # Check for kV ramp data
        if _contains_kv_records(data):
            for sample in _extract_kv_samples(data):
                self._fire(self.on_kv_sample, sample)
                if sample.is_expose_trigger:
                    self._fire(self.on_event, "EXPOSE TRIGGER DETECTED")

        # Check for scanlines
        for sl in _extract_scanlines(data):
            self._fire(self.on_scanline, sl)

        # Check for ASCII events
        for ev in _extract_events(data):
            self._fire(self.on_event, f"{ev.event_type}: {ev.detail}")

    def _fire(self, callbacks: list, *args) -> None:
        for cb in callbacks:
            try:
                cb(*args)
            except Exception as exc:
                log.debug("Callback error: %s", exc)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Output / Reporting
# ╚══════════════════════════════════════════════════════════════════════════════

def print_summary(capture: DecodedCapture) -> None:
    """Print a human-readable summary of a decoded capture."""
    print("=" * 70)
    print("PureXS HB Decoder — Capture Summary")
    print("=" * 70)

    print(f"\n  Session frames:     {len(capture.frames)}")
    print(f"  Heartbeat pairs:    {len(capture.hb_pairs)}")
    print(f"  kV ramp samples:    {len(capture.kv_samples)}")
    print(f"  Image scanlines:    {len(capture.scanlines)}")
    print(f"  Log events:         {len(capture.events)}")

    if capture.hb_pairs:
        print("\n  HB Pairs:")
        for i, (req, resp) in enumerate(capture.hb_pairs):
            rtt = (resp.timestamp - req.timestamp) * 1000
            print(f"    [{i+1}] t={req.timestamp:.3f}  RTT={rtt:.1f}ms")

    if capture.kv_samples:
        print(f"\n  kV Ramp: {len(capture.kv_samples)} samples")
        trigger = [s for s in capture.kv_samples if s.is_expose_trigger]
        print(f"  Expose triggers:    {len(trigger)}")
        if trigger:
            t = trigger[0]
            print(
                f"  First trigger:      pos={t.position} "
                f"kV=0x{t.kv_raw:04X} ramp=0x{t.field3:04X}"
            )

    if capture.scanlines:
        ids = [sl.scanline_id for sl in capture.scanlines]
        print(f"\n  Scanlines: {len(capture.scanlines)}")
        print(f"  ID range:           0x{min(ids):02X} — 0x{max(ids):02X}")
        pixels = capture.scanlines[0].pixel_count
        print(f"  Pixels per line:    {pixels}")

    if capture.events:
        print("\n  Events:")
        rec_starts = [e for e in capture.events if e.event_type == "recording_start"]
        rec_stops = [e for e in capture.events if e.event_type == "recording_stop"]
        releases = [e for e in capture.events if e.event_type == "state_released"]
        e7_errors = [e for e in capture.events if e.event_type == "e7_error"]

        print(f"    Recording start:  {len(rec_starts)}")
        print(f"    Recording stop:   {len(rec_stops)}")
        print(f"    Released:         {len(releases)}")
        print(f"    E7 14 02 errors:  {len(e7_errors)}")

        for ev in rec_starts[:5]:
            print(f"      {ev.timestamp_str}  {ev.detail}")

    print("\n" + "=" * 70)


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  CLI
# ╚══════════════════════════════════════════════════════════════════════════════

def cmd_parse(args: argparse.Namespace) -> int:
    """Parse a Wireshark dump and extract all protocol elements."""
    capture = parse_wireshark_dump(args.dump_file)
    print_summary(capture)

    outdir = Path(args.outdir)

    # Save kV ramp as CSV
    if capture.kv_samples:
        csv_path = outdir / "kv_ramp.csv"
        outdir.mkdir(parents=True, exist_ok=True)
        with open(csv_path, "w") as f:
            f.write("position,kv_raw,field2,field3,is_trigger\n")
            for s in capture.kv_samples:
                f.write(
                    f"{s.position},{s.kv_raw},{s.field2},"
                    f"{s.field3},{int(s.is_expose_trigger)}\n"
                )
        log.info("kV ramp saved: %s (%d samples)", csv_path, len(capture.kv_samples))

    # Save scanline PNGs
    if capture.scanlines:
        sl_dir = outdir / "scanlines"
        paths = save_scanline_pngs(capture.scanlines, sl_dir)
        log.info("Scanline PNGs saved: %s (%d files)", sl_dir, len(paths))

        # Reconstruct composite image
        img = reconstruct_image(capture.scanlines)
        if img:
            composite_path = outdir / "panoramic_reconstructed.png"
            img.save(composite_path)
            log.info("Composite image: %s (%dx%d)", composite_path, img.width, img.height)

    # Save event log
    if capture.events:
        log_path = outdir / "events.log"
        outdir.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w") as f:
            for ev in capture.events:
                f.write(f"{ev.timestamp_str}  {ev.event_type}  {ev.detail}\n")
        log.info("Event log: %s (%d events)", log_path, len(capture.events))

    # Save session frame summary
    if capture.frames:
        frames_path = outdir / "frames.log"
        outdir.mkdir(parents=True, exist_ok=True)
        with open(frames_path, "w") as f:
            f.write(f"{'#':>5}  {'Time':>12}  {'Dir':>3}  {'FuncCode':>10}  "
                    f"{'Name':<20}  {'PayloadLen':>10}\n")
            f.write("-" * 70 + "\n")
            for fr in capture.frames:
                f.write(
                    f"{fr.frame_no:>5}  {fr.timestamp:>12.3f}  {fr.direction:>3}  "
                    f"0x{fr.func_code:04X}      {fr.func_name:<20}  "
                    f"{fr.payload_len:>10}\n"
                )
        log.info("Frame log: %s (%d frames)", frames_path, len(capture.frames))

    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    """Print a quick summary without writing output files."""
    capture = parse_wireshark_dump(args.dump_file)
    print_summary(capture)
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    """Connect to device and run live HB monitor."""
    client = SironaLiveClient(
        host=args.host,
        port=args.port,
        hb_interval=args.interval,
    )

    # Wire up console output
    client.on_hb.append(
        lambda seq, rtt: print(f"  HB seq={seq:>4}  RTT={rtt:.1f}ms")
    )
    client.on_status.append(lambda s: print(f"  STATUS: {s}"))
    client.on_event.append(lambda e: print(f"  EVENT: {e}"))
    client.on_kv_sample.append(
        lambda s: print(
            f"  kV pos={s.position} raw=0x{s.kv_raw:04X} "
            f"ramp=0x{s.field3:04X}"
            f"{'  ** TRIGGER **' if s.is_expose_trigger else ''}"
        )
    )
    client.on_error.append(lambda e: print(f"  ERROR: {e}"))

    try:
        client.connect()
        client.start_hb_loop()
        print(f"\nLive monitoring {args.host}:{args.port}")
        print("Press Ctrl+C to stop.\n")

        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nStopping...")
    except Exception as exc:
        log.error("Live monitor failed: %s", exc)
        print(f"ERROR: {exc}")
        return 1
    finally:
        client.disconnect()

    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hb_decoder",
        description="PureXS HB Decoder — Sirona Orthophos protocol decoder",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # parse
    p_parse = sub.add_parser(
        "parse", help="Parse a Wireshark text dump",
    )
    p_parse.add_argument("dump_file", help="Path to Wireshark text export")
    p_parse.add_argument(
        "--outdir", "-o", default="./decoded",
        help="Output directory (default: ./decoded)",
    )
    p_parse.set_defaults(func=cmd_parse)

    # summary
    p_sum = sub.add_parser(
        "summary", help="Quick summary of a dump (no file output)",
    )
    p_sum.add_argument("dump_file", help="Path to Wireshark text export")
    p_sum.set_defaults(func=cmd_summary)

    # live
    p_live = sub.add_parser(
        "live", help="Live TCP monitor (connects to device)",
    )
    p_live.add_argument(
        "--host", default="192.168.139.170", help="Device IP",
    )
    p_live.add_argument(
        "--port", "-p", type=int, default=12837, help="TCP port",
    )
    p_live.add_argument(
        "--interval", "-i", type=float, default=0.1,
        help="HB poll interval in seconds (default: 0.1)",
    )
    p_live.set_defaults(func=cmd_live)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
