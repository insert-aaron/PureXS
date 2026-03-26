#!/usr/bin/env python3
"""
PureXS Orthophos EXPOSE flow — full unit-test suite.

Tests the end-to-end acquisition pipeline:
  Button eligibility → trigger → kV ramp → scanlines → Released → stitch → DICOM

Uses raw hex snippets from the ff.txt Wireshark capture for wire-level replay
and mocks the Tk/CTk layer so the suite runs headless (no display required).

Run:
    pytest tests/test_orthophos_expose.py -v
"""

from __future__ import annotations

import struct
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import numpy as np
import pytest

# ── Imports under test ───────────────────────────────────────────────────────

from hb_decoder import (
    SironaLiveClient,
    KVSample,
    Scanline,
    ScanEvent,
    DecodedCapture,
    _extract_kv_samples,
    _extract_scanlines,
    _extract_events,
    _contains_kv_records,
    reconstruct_image,
    FC_HB_REQUEST,
    FC_HB_RESPONSE,
    FC_EXPOSE_NOTIFY,
    SESSION_HEADER_SIZE,
    SCANLINE_MARKER,
    SCANLINE_PIXELS,
    PIXEL_BYTES,
    KV_RECORD_SIZE,
    EXPOSE_TRIGGER_KV_HI,
    DEVICE_STATUS_READY,
    DEVICE_STATUS_BUSY,
    DEVICE_STATUS_ERROR,
    DEVICE_STATUS_WARMUP,
)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  ff.txt Hex Snippets  (raw bytes captured from Wireshark dump)
# ╚══════════════════════════════════════════════════════════════════════════════

# ── HB pair (request + response, 20 bytes each) ─────────────────────────────
#   Frame 22:  20 0b 07 2d 07 d0 00 01 00 0e 00 00 ...   → HB_REQUEST
#   Frame 23:  20 0c 07 2d 07 d0 00 01 00 0e 00 00 ...   → HB_RESPONSE
HB_REQUEST_RAW = bytes.fromhex(
    "200b072d07d00001000e00000000000000000000"
)
HB_RESPONSE_RAW = bytes.fromhex(
    "200c072d07d00001000e00000000000000000000"
)

# ── kV ramp record (15 bytes, from ff.txt exposure phase) ────────────────────
#   01 02 BC 01 00 EE 01 F4 53 01 00 28 0E 01 00
#   kV_raw=0x02BC=700 → 70.0 kV,  field3=0xF453 (threshold)
KV_RAMP_RECORD = bytes.fromhex("0102bc0100ee01f453010028 0e 01 00".replace(" ", ""))

# ── kV ramp with expose trigger  (field3=0xFF12) ────────────────────────────
#   01 02 BC 01 00 EE 01 FF 12 01 03 42 0E 01 00
KV_TRIGGER_RECORD = bytes.fromhex(
    "0102bc0100ee01ff120103420e0100"
)

# ── OLD EXPOSE_TRIGGER_BYTES (REMOVED — was kV telemetry, not a command) ──────
# The old bytes were a fragment of a kV ramp record the device sends DURING
# exposure.  Sending them TO the device caused immediate connection drop.
# Exposure is now triggered by the physical button on the unit after
# arm_for_expose() sends CAPS + patient DATA_SEND.

# ── One scanline (ID=0x40, 240 px, from ff.txt image data) ──────────────────
#   preamble... 01 40 00 01 00 F0 00 34 [240 × 2 bytes big-endian pixel data]
def _build_scanline(scanline_id: int = 0x40, pixel_count: int = 240) -> bytes:
    """Build a raw wire-format scanline for testing."""
    preamble = b"\x00\x00"                       # 2-byte preamble (varies)
    marker_byte = b"\x01"                         # required 0x01
    sid = bytes([scanline_id])
    # SCANLINE_MARKER = 00 01 00 F0
    marker = SCANLINE_MARKER                      # pixel_count as BE word
    row_param = struct.pack(">H", 0x0034)         # row metadata = 52
    # Deterministic pixel data seeded by scanline ID
    rng = np.random.default_rng(scanline_id)
    pixels = rng.integers(100, 60000, size=pixel_count, dtype=np.uint16)
    pixel_bytes = pixels.astype(">u2").tobytes()  # big-endian
    return preamble + marker_byte + sid + marker + row_param + pixel_bytes


def _build_13_scanlines() -> bytes:
    """Build the standard 13-scanline panoramic payload (IDs 0x40–0x4C)."""
    return b"".join(_build_scanline(0x40 + i) for i in range(13))


# ── ASCII event strings (embedded in TCP payload, from ff.txt) ───────────────
EVENT_RECORDING_START = b"2006-03-21, 12:00:00 Recording started - Value: 1"
EVENT_RECORDING_STOP = b"2006-03-21, 12:00:04 Recording stopped"
EVENT_IMAGE_TRANSFER_START = b"2006-03-21, 12:00:04 Imagetransfer started"
EVENT_IMAGE_TRANSFER_STOP = b"2006-03-21, 12:00:05 Imagetransfer stopped"
EVENT_RELEASED = b"2006-03-21, 12:00:05 Image state switched to Released"
EVENT_E7_ERROR = b"E7 14 02 (ERR_SIDEXIS_API)"


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Wire-Level Tests — Exact Byte Validation
# ╚══════════════════════════════════════════════════════════════════════════════

class TestWireConstants:
    """Validate that wire constants match the ff.txt capture exactly."""

    def test_arm_for_expose_exists(self):
        """arm_for_expose method must exist (replaces old send_expose)."""
        assert hasattr(SironaLiveClient, "arm_for_expose")
        assert callable(getattr(SironaLiveClient, "arm_for_expose"))

    def test_hb_request_func_code(self):
        assert FC_HB_REQUEST == 0x200B
        assert (HB_REQUEST_RAW[0] << 8 | HB_REQUEST_RAW[1]) == FC_HB_REQUEST

    def test_hb_response_func_code(self):
        assert FC_HB_RESPONSE == 0x200C
        assert (HB_RESPONSE_RAW[0] << 8 | HB_RESPONSE_RAW[1]) == FC_HB_RESPONSE

    def test_session_header_size(self):
        assert SESSION_HEADER_SIZE == 20
        assert len(HB_REQUEST_RAW) == SESSION_HEADER_SIZE
        assert len(HB_RESPONSE_RAW) == SESSION_HEADER_SIZE

    def test_status_constants(self):
        assert DEVICE_STATUS_READY == 0x0000
        assert DEVICE_STATUS_BUSY == 0x0001
        assert DEVICE_STATUS_ERROR == 0x0002
        assert DEVICE_STATUS_WARMUP == 0x0003

    def test_scanline_marker(self):
        assert SCANLINE_MARKER == b"\x00\x01\x00\xf0"
        assert SCANLINE_PIXELS == 240
        assert PIXEL_BYTES == 2


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  kV Ramp Extraction Tests
# ╚══════════════════════════════════════════════════════════════════════════════

class TestKVExtraction:
    """Test kV ramp record parsing from raw wire bytes."""

    def _build_kv_payload(self, n: int = 10, trigger_at: int = -1) -> bytes:
        """Build a payload of n kV records, with optional trigger at index."""
        records = []
        for i in range(n):
            kv_raw = 500 + i * 20          # rising kV
            field2 = 0x00EE
            field3 = 0xFF12 if i == trigger_at else 0xF453
            counter = 0x0028 + i
            rec = bytes([
                0x01, (kv_raw >> 8) & 0xFF, kv_raw & 0xFF,
                0x01, (field2 >> 8) & 0xFF, field2 & 0xFF,
                0x01, (field3 >> 8) & 0xFF, field3 & 0xFF,
                0x01, (counter >> 8) & 0xFF, counter & 0xFF,
                0x0E, 0x01, 0x00,
            ])
            records.append(rec)
        return b"".join(records)

    def test_contains_kv_records_true(self):
        payload = self._build_kv_payload(10)
        assert _contains_kv_records(payload) is True

    def test_contains_kv_records_false_too_short(self):
        assert _contains_kv_records(b"\x00" * 10) is False

    def test_contains_kv_records_false_no_markers(self):
        assert _contains_kv_records(b"\x00" * 200) is False

    def test_extract_kv_samples(self):
        payload = self._build_kv_payload(10)
        samples = _extract_kv_samples(payload)
        assert len(samples) >= 8  # some boundary records may be missed

        for s in samples:
            assert isinstance(s, KVSample)
            assert s.kv_raw > 0
            assert s.position > 0

    def test_expose_trigger_detection(self):
        payload = self._build_kv_payload(10, trigger_at=7)
        samples = _extract_kv_samples(payload)
        triggers = [s for s in samples if s.is_expose_trigger]
        assert len(triggers) >= 1
        assert triggers[0].field3 == 0xFF12
        assert (triggers[0].field3 >> 8) == EXPOSE_TRIGGER_KV_HI

    def test_no_false_trigger(self):
        payload = self._build_kv_payload(10, trigger_at=-1)
        samples = _extract_kv_samples(payload)
        triggers = [s for s in samples if s.is_expose_trigger]
        assert len(triggers) == 0

    def test_ff_txt_kv_record(self):
        """Parse the exact kV record from ff.txt."""
        # Need enough records for the heuristic
        payload = KV_RAMP_RECORD * 10
        samples = _extract_kv_samples(payload)
        assert len(samples) > 0
        assert samples[0].kv_raw == 0x02BC  # 700 → 70.0 kV

    def test_ff_txt_trigger_record(self):
        """The ff.txt trigger record must parse with is_expose_trigger=True."""
        payload = KV_TRIGGER_RECORD * 10
        samples = _extract_kv_samples(payload)
        triggers = [s for s in samples if s.is_expose_trigger]
        assert len(triggers) > 0


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Scanline Extraction Tests
# ╚══════════════════════════════════════════════════════════════════════════════

class TestScanlineExtraction:
    """Test scanline parsing from raw wire bytes."""

    def test_single_scanline(self):
        data = _build_scanline(0x40, 240)
        scanlines = _extract_scanlines(data)
        assert len(scanlines) == 1
        sl = scanlines[0]
        assert sl.scanline_id == 0x40
        assert sl.pixel_count == 240
        assert len(sl.pixels) == 240
        assert sl.pixels.dtype == np.dtype(">u2")

    def test_13_scanlines(self):
        data = _build_13_scanlines()
        scanlines = _extract_scanlines(data)
        assert len(scanlines) == 13
        ids = [sl.scanline_id for sl in scanlines]
        assert ids == list(range(0x40, 0x4D))
        for sl in scanlines:
            assert sl.pixel_count == 240

    def test_pixels_8bit_normalization(self):
        data = _build_scanline(0x40)
        sl = _extract_scanlines(data)[0]
        p8 = sl.pixels_8bit
        assert p8.dtype == np.uint8
        assert p8.max() == 255  # normalized
        assert len(p8) == 240

    def test_empty_payload(self):
        assert _extract_scanlines(b"") == []
        assert _extract_scanlines(b"\x00" * 5) == []


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Event Extraction Tests
# ╚══════════════════════════════════════════════════════════════════════════════

class TestEventExtraction:
    """Test ASCII event parsing from TCP payloads."""

    def test_recording_start(self):
        events = _extract_events(EVENT_RECORDING_START)
        assert len(events) == 1
        assert events[0].event_type == "recording_start"
        assert "Value: 1" in events[0].detail

    def test_recording_stop(self):
        events = _extract_events(EVENT_RECORDING_STOP)
        assert len(events) == 1
        assert events[0].event_type == "recording_stop"

    def test_released(self):
        events = _extract_events(EVENT_RELEASED)
        assert len(events) == 1
        assert events[0].event_type == "state_released"

    def test_e7_error(self):
        events = _extract_events(EVENT_E7_ERROR)
        assert len(events) == 1
        assert events[0].event_type == "e7_error"

    def test_image_transfer_start_stop(self):
        payload = EVENT_IMAGE_TRANSFER_START + b"\n" + EVENT_IMAGE_TRANSFER_STOP
        events = _extract_events(payload)
        types = {e.event_type for e in events}
        assert "imagetransfer_start" in types
        assert "imagetransfer_stop" in types

    def test_full_event_sequence(self):
        """Replay the full ff.txt event sequence in order."""
        payload = (
            EVENT_RECORDING_START + b"\r\n"
            + EVENT_RECORDING_STOP + b"\r\n"
            + EVENT_IMAGE_TRANSFER_START + b"\r\n"
            + EVENT_IMAGE_TRANSFER_STOP + b"\r\n"
            + EVENT_RELEASED
        )
        events = _extract_events(payload)
        types = [e.event_type for e in events]
        assert "recording_start" in types
        assert "recording_stop" in types
        assert "imagetransfer_start" in types
        assert "imagetransfer_stop" in types
        assert "state_released" in types


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Image Reconstruction Tests
# ╚══════════════════════════════════════════════════════════════════════════════

class TestImageReconstruction:
    """Test panoramic image stitching from scanlines."""

    def test_reconstruct_13_scanlines(self):
        data = _build_13_scanlines()
        scanlines = _extract_scanlines(data)
        img = reconstruct_image(scanlines)
        assert img is not None
        assert img.mode == "L"
        assert img.width == 13
        assert img.height == 240

    def test_reconstruct_empty(self):
        assert reconstruct_image([]) is None

    def test_reconstruct_single_scanline(self):
        data = _build_scanline(0x40)
        scanlines = _extract_scanlines(data)
        img = reconstruct_image(scanlines)
        assert img is not None
        assert img.width == 1
        assert img.height == 240

    def test_partial_scanlines(self):
        """A timeout capture with < 13 scanlines should still reconstruct."""
        data = b"".join(_build_scanline(0x40 + i) for i in range(5))
        scanlines = _extract_scanlines(data)
        img = reconstruct_image(scanlines)
        assert img is not None
        assert img.width == 5


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  SironaLiveClient Unit Tests
# ╚══════════════════════════════════════════════════════════════════════════════

class TestSironaLiveClient:
    """Test SironaLiveClient internals without a real TCP connection."""

    def test_initial_state(self):
        client = SironaLiveClient(host="0.0.0.0", port=1)
        assert client.device_status_code == -1
        assert client._connected is False
        assert client._hb_seq == 0

    def test_diag_ring_buffer(self):
        client = SironaLiveClient(host="0.0.0.0", port=1)
        for i in range(25):
            client._diag_push(f"entry-{i}")
        entries = client.dump_diagnostics(10)
        assert len(entries) == 10
        # Most recent should be entry-24
        assert "entry-24" in entries[-1]
        assert "entry-15" in entries[0]

    def test_diag_ring_overflow(self):
        client = SironaLiveClient(host="0.0.0.0", port=1)
        for i in range(50):
            client._diag_push(f"e{i}")
        assert len(client._diag_ring) == 20  # max size

    def test_callbacks_initialized(self):
        client = SironaLiveClient(host="0.0.0.0", port=1)
        assert isinstance(client.on_hb, list)
        assert isinstance(client.on_device_status, list)
        assert isinstance(client.on_kv_sample, list)
        assert isinstance(client.on_scanline, list)
        assert isinstance(client.on_event, list)

    def test_process_live_data_kv(self):
        """Verify _process_live_data dispatches kV samples to callbacks."""
        client = SironaLiveClient(host="0.0.0.0", port=1)
        received = []
        client.on_kv_sample.append(lambda s: received.append(s))

        # Build a payload with enough kV records
        payload = KV_RAMP_RECORD * 10
        client._process_live_data(payload)
        assert len(received) > 0
        assert all(isinstance(s, KVSample) for s in received)

    def test_process_live_data_scanlines(self):
        """Verify _process_live_data dispatches scanlines to callbacks."""
        client = SironaLiveClient(host="0.0.0.0", port=1)
        received = []
        client.on_scanline.append(lambda sl: received.append(sl))

        payload = _build_13_scanlines()
        client._process_live_data(payload)
        assert len(received) == 13

    def test_process_live_data_events(self):
        """Verify _process_live_data dispatches ASCII events to callbacks."""
        client = SironaLiveClient(host="0.0.0.0", port=1)
        received = []
        client.on_event.append(lambda e: received.append(e))

        client._process_live_data(EVENT_RELEASED)
        assert len(received) == 1
        assert "state_released" in received[0]

    def test_data_continuation_immutable(self):
        """Continuation data template must not be accidentally mutated."""
        original = bytes(SironaLiveClient._DATA_CONTINUATION)
        _ = SironaLiveClient._DATA_CONTINUATION  # access
        assert SironaLiveClient._DATA_CONTINUATION == original


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  GUI State Machine Tests  (mocked Tk — no display needed)
# ╚══════════════════════════════════════════════════════════════════════════════

@dataclass
class _MockWidget:
    """Minimal mock for CTk widgets that tracks .configure() calls."""
    _config: dict

    def __init__(self, **kwargs: Any):
        self._config = dict(kwargs)

    def configure(self, **kwargs: Any) -> None:
        self._config.update(kwargs)

    def __getitem__(self, key: str) -> Any:
        return self._config.get(key)

    def pack(self, **_kw: Any) -> None: ...  # no-op stub
    def grid(self, **_kw: Any) -> None: ...  # no-op stub


class _StubApp:
    """Minimal stub simulating PureXSApp state without Tk mainloop.

    Replicates the state fields and key methods used by the expose flow
    so we can test the state machine logic in isolation.
    """

    def __init__(self) -> None:
        # ── State (mirrors purexs_gui.py PureXSApp.__init__) ──────────
        self._last_status: str = "OFFLINE"
        self._connected = False
        self._direct_connected = False
        self._device_ready = False
        self._exposing = False
        self._expose_scanlines: list = []
        self._expose_kv_peak: float = 0.0
        self._expose_start_time: float = 0.0
        self._got_kv_or_scanline = False
        self._patient: dict = {
            "first": "", "last": "", "dob": "", "id": "",
            "exam": "Panoramic", "set": False,
        }

        # ── Mock widgets ──────────────────────────────────────────────
        self._status_label = _MockWidget(text="OFFLINE", text_color="#616161")
        self._phase_label = _MockWidget(text="", text_color="#757575")
        self._direct_expose_btn = _MockWidget(
            state="disabled", fg_color="#616161", text_color="#9E9E9E"
        )
        self._expose_btn = _MockWidget(state="disabled")

        # ── Sirona client mock ────────────────────────────────────────
        self._sirona_client = MagicMock(spec=SironaLiveClient)
        self._sirona_client.device_status_code = -1

    # ── Replicate _set_status ─────────────────────────────────────────
    def _set_status(self, text: str, color: str, phase: str = "") -> None:
        self._last_status = text
        self._status_label.configure(text=text, text_color=color)
        self._phase_label.configure(text=phase)

    # ── Replicate _update_expose_eligibility ──────────────────────────
    def _update_expose_eligibility(self) -> None:
        patient_set = self._patient.get("set", False)
        hb_active = self._direct_connected

        if patient_set and hb_active and self._device_ready:
            self._direct_expose_btn.configure(
                state="normal", fg_color="#FF3B30",
            )
        else:
            self._direct_expose_btn.configure(
                state="disabled", fg_color="#616161",
            )

        api_ready = self._last_status in ("READY", "CONNECTED")
        if patient_set and self._connected and api_ready:
            self._expose_btn.configure(state="normal")
        else:
            self._expose_btn.configure(state="disabled")


class TestExposeEligibility:
    """Test the EXPOSE button enable/disable gate logic."""

    def test_all_conditions_met(self):
        app = _StubApp()
        app._patient["set"] = True
        app._direct_connected = True
        app._device_ready = True
        app._update_expose_eligibility()
        assert app._direct_expose_btn._config["state"] == "normal"
        assert app._direct_expose_btn._config["fg_color"] == "#FF3B30"

    def test_no_patient_disables(self):
        app = _StubApp()
        app._patient["set"] = False
        app._direct_connected = True
        app._device_ready = True
        app._update_expose_eligibility()
        assert app._direct_expose_btn._config["state"] == "disabled"

    def test_warmup_disables(self):
        """WARMUP status (0x0003) → device_ready=False → button disabled."""
        app = _StubApp()
        app._patient["set"] = True
        app._direct_connected = True
        app._device_ready = False  # WARMUP
        app._update_expose_eligibility()
        assert app._direct_expose_btn._config["state"] == "disabled"
        assert app._direct_expose_btn._config["fg_color"] == "#616161"

    def test_no_hb_disables(self):
        app = _StubApp()
        app._patient["set"] = True
        app._direct_connected = False
        app._device_ready = True
        app._update_expose_eligibility()
        assert app._direct_expose_btn._config["state"] == "disabled"

    def test_api_button_needs_ready_status(self):
        app = _StubApp()
        app._patient["set"] = True
        app._connected = True
        app._last_status = "READY"
        app._update_expose_eligibility()
        assert app._expose_btn._config["state"] == "normal"

    def test_api_button_warmup_disables(self):
        app = _StubApp()
        app._patient["set"] = True
        app._connected = True
        app._last_status = "WARMUP"
        app._update_expose_eligibility()
        assert app._expose_btn._config["state"] == "disabled"

    def test_status_label_colors(self):
        """Verify _set_status drives the correct label text and color."""
        app = _StubApp()

        app._set_status("READY", "#4CAF50")
        assert app._status_label._config["text"] == "READY"
        assert app._status_label._config["text_color"] == "#4CAF50"

        app._set_status("WARMUP", "#FFC107")
        assert app._status_label._config["text"] == "WARMUP"
        assert app._status_label._config["text_color"] == "#FFC107"

        app._set_status("BUSY", "#FFA726")
        assert app._status_label._config["text"] == "BUSY"
        assert app._status_label._config["text_color"] == "#FFA726"

    def test_phase_label_set(self):
        app = _StubApp()
        app._set_status("WARMUP", "#FFC107", phase="Gantry positioning")
        assert app._phase_label._config["text"] == "Gantry positioning"

    def test_transition_warmup_to_ready(self):
        """Simulate device going from WARMUP → READY, button must enable."""
        app = _StubApp()
        app._patient["set"] = True
        app._direct_connected = True

        # WARMUP phase
        app._device_ready = False
        app._update_expose_eligibility()
        assert app._direct_expose_btn._config["state"] == "disabled"

        # Device becomes READY
        app._device_ready = True
        app._update_expose_eligibility()
        assert app._direct_expose_btn._config["state"] == "normal"


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Full Expose Flow — Replay ff.txt Success
# ╚══════════════════════════════════════════════════════════════════════════════

class TestExposeFlowSuccess:
    """End-to-end replay: READY → trigger → kV → scanlines → Released → stitch."""

    def test_full_flow_replay(self):
        """Simulate the complete ff.txt capture sequence through the parsers."""
        # 1. Device is READY
        assert DEVICE_STATUS_READY == 0x0000

        # 2. arm_for_expose method exists (replaces old trigger bytes)
        assert hasattr(SironaLiveClient, "arm_for_expose")

        # 3. kV ramp arrives → extract samples
        kv_payload = KV_RAMP_RECORD * 10 + KV_TRIGGER_RECORD * 5
        samples = _extract_kv_samples(kv_payload)
        assert len(samples) > 0
        kv_peak = max(s.kv_raw for s in samples) / 10.0
        assert kv_peak >= 70.0  # ff.txt shows ~70 kV

        # Trigger detected
        triggers = [s for s in samples if s.is_expose_trigger]
        assert len(triggers) > 0

        # 4. Scanlines arrive → extract
        scanline_payload = _build_13_scanlines()
        scanlines = _extract_scanlines(scanline_payload)
        assert len(scanlines) == 13
        assert scanlines[0].scanline_id == 0x40
        assert scanlines[-1].scanline_id == 0x4C

        # 5. Events: Recording started → stopped → Released
        event_payload = (
            EVENT_RECORDING_START + b"\r\n"
            + EVENT_RECORDING_STOP + b"\r\n"
            + EVENT_RELEASED
        )
        events = _extract_events(event_payload)
        types = [e.event_type for e in events]
        assert types == ["recording_start", "recording_stop", "state_released"]

        # 6. Stitch panoramic
        img = reconstruct_image(scanlines)
        assert img is not None
        assert img.width == 13
        assert img.height == 240
        assert img.mode == "L"

    def test_e7_as_released(self):
        """E7 14 02 must parse as e7_error (treated as Released by GUI)."""
        events = _extract_events(EVENT_E7_ERROR)
        assert len(events) == 1
        assert events[0].event_type == "e7_error"
        assert "ERR_SIDEXIS_API" in events[0].detail


class TestExposeFlowTimeout:
    """Timeout scenario: partial scanlines → force-complete → partial stitch."""

    def test_partial_stitch_on_timeout(self):
        """5 out of 13 scanlines received before timeout → valid partial image."""
        data = b"".join(_build_scanline(0x40 + i) for i in range(5))
        scanlines = _extract_scanlines(data)
        assert len(scanlines) == 5

        img = reconstruct_image(scanlines)
        assert img is not None
        assert img.width == 5
        assert img.height == 240

    def test_zero_scanlines_on_timeout(self):
        """No scanlines at all → reconstruct_image returns None."""
        img = reconstruct_image([])
        assert img is None

    def test_kv_only_no_scanlines(self):
        """kV ramp arrived but no scanlines (tube fired but gantry didn't move)."""
        kv_payload = KV_TRIGGER_RECORD * 10
        samples = _extract_kv_samples(kv_payload)
        assert len(samples) > 0

        # No scanlines → stitch fails gracefully
        img = reconstruct_image([])
        assert img is None


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Diagnostic Ring Buffer Integration
# ╚══════════════════════════════════════════════════════════════════════════════

class TestDiagnostics:
    """Test the HB diagnostic ring buffer used for no-response debugging."""

    def test_dump_after_hb_sequence(self):
        """Simulate a sequence of HB + status polls and verify dump content."""
        client = SironaLiveClient(host="0.0.0.0", port=1)

        # Simulate 5 HB cycles + 1 status poll
        for i in range(5):
            client._diag_push(f"HB seq={i+1} rtt=2ms status=0x0000")
        client._diag_push("STATUS poll → 0x0000 (READY)")

        entries = client.dump_diagnostics(10)
        assert len(entries) == 6
        assert "READY" in entries[-1]
        assert "HB seq=1" in entries[0]

    def test_dump_captures_timeout(self):
        client = SironaLiveClient(host="0.0.0.0", port=1)
        client._diag_push("HB_TIMEOUT (no response)")
        entries = client.dump_diagnostics(5)
        assert len(entries) == 1
        assert "TIMEOUT" in entries[0]

    def test_dump_captures_error(self):
        client = SironaLiveClient(host="0.0.0.0", port=1)
        client._diag_push("HB_ERROR: Connection reset by peer")
        entries = client.dump_diagnostics(5)
        assert "Connection reset" in entries[0]

    def test_dump_warmup_blocks_expose(self):
        """If last status was WARMUP, diagnostics should show it."""
        client = SironaLiveClient(host="0.0.0.0", port=1)
        for i in range(3):
            client._diag_push(f"HB seq={i} rtt=1ms status=0x0003")
        client._diag_push("STATUS poll → 0x0003 (WARMUP)")

        entries = client.dump_diagnostics(10)
        warmup_entries = [e for e in entries if "WARMUP" in e or "0x0003" in e]
        assert len(warmup_entries) >= 1


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  DICOM Export Smoke Test
# ╚══════════════════════════════════════════════════════════════════════════════

class TestDICOMExport:
    """Verify DICOM export from scanline data (smoke test, not full DICOM validation)."""

    @pytest.fixture
    def scanlines(self) -> list[Scanline]:
        data = _build_13_scanlines()
        return _extract_scanlines(data)

    def test_dicom_export_roundtrip(self, scanlines, tmp_path):
        """Export scanlines → DICOM file on disk → verify it exists and is non-empty."""
        try:
            from dicom_export import PureXSDICOM
        except ImportError:
            pytest.skip("pydicom not installed")

        patient = {
            "first": "Test", "last": "Patient",
            "dob": "01/01/1990", "id": "TEST-001",
            "exam": "Panoramic", "set": True,
        }

        exporter = PureXSDICOM()
        dcm_path = exporter.export(patient, scanlines, 70.0, tmp_path)

        assert Path(dcm_path).exists()
        assert Path(dcm_path).stat().st_size > 1000  # non-trivial file

    def test_dicom_export_empty_scanlines(self):
        """Empty scanline buffer must raise, not produce a corrupt file."""
        try:
            from dicom_export import PureXSDICOM
        except ImportError:
            pytest.skip("pydicom not installed")

        patient = {
            "first": "T", "last": "P", "dob": "01/01/1990",
            "id": "X", "exam": "Panoramic", "set": True,
        }
        with pytest.raises(RuntimeError):
            PureXSDICOM().export(patient, [], 0.0, "/tmp")


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Tooltip Class Test
# ╚══════════════════════════════════════════════════════════════════════════════

class TestToolTip:
    """Verify the _ToolTip class doesn't crash on construction."""

    def test_import_and_construct(self):
        """_ToolTip should be importable and constructable with a mock widget."""
        try:
            import tkinter  # noqa: F401
        except ImportError:
            pytest.skip("tkinter not available (headless environment)")

        import importlib
        mod = importlib.import_module("purexs_gui")
        assert hasattr(mod, "_ToolTip")
        assert hasattr(mod._ToolTip, "_show")
        assert hasattr(mod._ToolTip, "_hide")
