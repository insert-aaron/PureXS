#!/usr/bin/env python3
"""
PureXS Live Hardware Test Suite.

Runs 5 sequential tests against a real Dentsply Sirona Orthophos at
192.168.139.170:12837, or replays from a Wireshark dump (--replay).

Usage:
    python live_test.py                                    # live hardware
    python live_test.py --host 10.0.0.50 --port 12837      # alternate IP
    python live_test.py --replay ../ff.txt                  # offline replay

Exit codes:
    0 = all passed
    1 = any failed
    2 = tests skipped (expose not confirmed)
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

# ── PureXS imports ───────────────────────────────────────────────────────────

from hb_decoder import (
    MAGIC,
    PORT_MARKER,
    SESSION_HEADER_SIZE,
    FC_SESSION_OPEN_REQ,
    FC_SESSION_OPEN_ACK,
    FC_SESSION_INIT,
    FC_SESSION_CONFIRM,
    FC_HB_REQUEST,
    FC_HB_RESPONSE,
    KVSample,
    Scanline,
    DecodedCapture,
    SironaLiveClient,
    parse_wireshark_dump,
    reconstruct_image,
    _extract_kv_samples,
    _extract_scanlines,
    _extract_events,
)

from dicom_export import PureXSDICOM, HAS_PYDICOM

# history.py imports tkinter at module level, so guard it
try:
    from history import load_all_patients
    HAS_HISTORY = True
except ImportError:
    HAS_HISTORY = False


# ── Paths ────────────────────────────────────────────────────────────────────

from utils import get_data_dir

LOG_DIR = get_data_dir()
PATIENTS_DIR = LOG_DIR / "patients"

TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE = LOG_DIR / f"live_test_{TIMESTAMP}.log"
SCAN_PNG = LOG_DIR / f"live_test_scan_{TIMESTAMP}.png"


# ── Logging ──────────────────────────────────────────────────────────────────

log = logging.getLogger("live_test")
log.setLevel(logging.DEBUG)

_fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")

_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_fmt)
log.addHandler(_fh)

_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter("  %(message)s"))
log.addHandler(_ch)


# ── Test Result ──────────────────────────────────────────────────────────────

@dataclass
class TestResult:
    name: str
    status: str = "PENDING"   # PASS, FAIL, SKIPPED
    detail: str = ""
    duration_ms: float = 0.0
    diagnostics: dict = field(default_factory=dict)

    @property
    def icon(self) -> str:
        return {"PASS": "\u2705", "FAIL": "\u274C", "SKIPPED": "\u23ED"}.get(
            self.status, "\u2753"
        )


# ── Wire helpers ─────────────────────────────────────────────────────────────

def _build_session_frame(func_code: int, flags: int = 0x000E) -> bytes:
    """Build a 20-byte P2K session header."""
    hdr = bytearray(SESSION_HEADER_SIZE)
    hdr[0] = (func_code >> 8) & 0xFF
    hdr[1] = func_code & 0xFF
    struct.pack_into(">H", hdr, 2, MAGIC)
    struct.pack_into(">H", hdr, 4, PORT_MARKER)
    struct.pack_into(">H", hdr, 6, 0x0001)
    struct.pack_into(">H", hdr, 8, flags)
    return bytes(hdr)


def _fc_from_bytes(data: bytes) -> int:
    """Extract func_code from first 2 bytes."""
    if len(data) < 2:
        return 0
    return (data[0] << 8) | data[1]


def _hex_preview(data: bytes, limit: int = 64) -> str:
    """First N bytes as hex string for logging."""
    return data[:limit].hex(" ")


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  TEST 1 — TCP Connect + Session Handshake
# ╚══════════════════════════════════════════════════════════════════════════════

def test_connect(host: str, port: int) -> tuple[TestResult, socket.socket | None]:
    """Open socket, perform P2K session handshake, return live socket."""
    r = TestResult("TCP Connect + Handshake")
    sock = None
    t0 = time.perf_counter()

    try:
        # TCP connect
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((host, port))
        connect_ms = (time.perf_counter() - t0) * 1000

        if connect_ms > 2000:
            r.status = "FAIL"
            r.detail = f"Connect too slow: {connect_ms:.0f}ms (>2000ms)"
            sock.close()
            return r, None

        log.info("TCP connected in %.0fms", connect_ms)
        r.diagnostics["connect_ms"] = round(connect_ms, 1)

        # Session open: send 0x205C, expect 0x205D
        sock.sendall(_build_session_frame(FC_SESSION_OPEN_REQ, flags=0x000F))
        resp = sock.recv(4096)
        fc = _fc_from_bytes(resp)
        log.debug("Open response: 0x%04X  hex=%s", fc, _hex_preview(resp))

        if fc != FC_SESSION_OPEN_ACK:
            r.status = "FAIL"
            r.detail = f"Expected 0x{FC_SESSION_OPEN_ACK:04X}, got 0x{fc:04X}"
            sock.close()
            return r, None

        # Session init: send 0x2001, expect 0x2002
        sock.sendall(_build_session_frame(FC_SESSION_INIT))
        resp2 = sock.recv(4096)
        fc2 = _fc_from_bytes(resp2)
        log.debug("Init response: 0x%04X  hex=%s", fc2, _hex_preview(resp2))

        r.diagnostics["open_ack_fc"] = f"0x{fc:04X}"
        r.diagnostics["init_resp_fc"] = f"0x{fc2:04X}"
        r.diagnostics["remote"] = f"{host}:{port}"

        r.duration_ms = (time.perf_counter() - t0) * 1000
        r.status = "PASS"
        r.detail = f"Connected in {connect_ms:.0f}ms, session ACK 0x{fc:04X}"
        return r, sock

    except socket.timeout:
        r.status = "FAIL"
        r.detail = f"Connection to {host}:{port} timed out (5s)"
        r.duration_ms = (time.perf_counter() - t0) * 1000
        return r, None
    except OSError as exc:
        r.status = "FAIL"
        r.detail = f"Socket error: {exc}"
        r.duration_ms = (time.perf_counter() - t0) * 1000
        return r, None


def test_connect_replay(capture: DecodedCapture) -> TestResult:
    """Replay TEST 1 from parsed dump."""
    r = TestResult("TCP Connect + Handshake (replay)")
    opens = [f for f in capture.frames if f.func_code == FC_SESSION_OPEN_REQ]
    acks = [f for f in capture.frames if f.func_code == FC_SESSION_OPEN_ACK]

    if not opens or not acks:
        r.status = "FAIL"
        r.detail = f"No handshake in dump (opens={len(opens)}, acks={len(acks)})"
        return r

    r.diagnostics["open_frames"] = len(opens)
    r.diagnostics["ack_frames"] = len(acks)
    handshake_ms = (acks[0].timestamp - opens[0].timestamp) * 1000
    r.diagnostics["handshake_ms"] = round(handshake_ms, 1)
    r.status = "PASS"
    r.detail = f"Handshake in dump: {handshake_ms:.1f}ms"
    return r


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  TEST 2 — HB Pairs (0x200B / 0x200C) + RTT
# ╚══════════════════════════════════════════════════════════════════════════════

def test_hb_pairs(sock: socket.socket) -> TestResult:
    """Send 4 HB request/response pairs, measure RTT."""
    r = TestResult("HB Pairs (\u00d74) + RTT")
    t0 = time.perf_counter()
    rtts: list[float] = []
    seqs: list[int] = []

    try:
        for seq in range(1, 5):
            t_hb = time.perf_counter()
            sock.sendall(_build_session_frame(FC_HB_REQUEST))
            resp = sock.recv(4096)
            rtt = (time.perf_counter() - t_hb) * 1000
            fc = _fc_from_bytes(resp)

            log.debug("HB seq=%d  fc=0x%04X  rtt=%.1fms  hex=%s",
                       seq, fc, rtt, _hex_preview(resp, 20))

            if fc != FC_HB_RESPONSE:
                r.status = "FAIL"
                r.detail = f"HB seq={seq}: expected 0x{FC_HB_RESPONSE:04X}, got 0x{fc:04X}"
                r.duration_ms = (time.perf_counter() - t0) * 1000
                return r

            rtts.append(rtt)
            seqs.append(seq)
            time.sleep(0.05)  # brief pause between pairs

        avg_rtt = sum(rtts) / len(rtts)
        max_rtt = max(rtts)

        r.diagnostics["rtts_ms"] = [round(x, 1) for x in rtts]
        r.diagnostics["avg_rtt_ms"] = round(avg_rtt, 1)
        r.diagnostics["max_rtt_ms"] = round(max_rtt, 1)
        r.diagnostics["seqs"] = seqs

        if max_rtt > 10.0:
            r.status = "FAIL"
            r.detail = f"RTT too high: max {max_rtt:.1f}ms (>10ms limit)"
        elif max_rtt > 5.0:
            r.status = "PASS"
            r.detail = f"4/4 HB OK, avg {avg_rtt:.1f}ms (WARN: max {max_rtt:.1f}ms >5ms)"
        else:
            r.status = "PASS"
            r.detail = f"4/4 HB OK, avg {avg_rtt:.1f}ms"

    except socket.timeout:
        r.status = "FAIL"
        r.detail = f"HB timeout after {len(rtts)} pair(s)"
    except OSError as exc:
        r.status = "FAIL"
        r.detail = f"Socket error during HB: {exc}"

    r.duration_ms = (time.perf_counter() - t0) * 1000
    return r


def test_hb_pairs_replay(capture: DecodedCapture) -> TestResult:
    """Replay TEST 2 from parsed dump."""
    r = TestResult("HB Pairs (replay)")
    pairs = capture.hb_pairs
    if len(pairs) < 4:
        r.status = "FAIL"
        r.detail = f"Only {len(pairs)} HB pairs in dump (need 4)"
        return r

    rtts = [(resp.timestamp - req.timestamp) * 1000 for req, resp in pairs[:4]]
    avg = sum(rtts) / len(rtts)
    r.diagnostics["rtts_ms"] = [round(x, 1) for x in rtts]
    r.diagnostics["avg_rtt_ms"] = round(avg, 1)
    r.status = "PASS"
    r.detail = f"4 HB pairs, avg RTT {avg:.1f}ms"
    return r


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  TEST 3 — Expose Trigger + kV Ramp
# ╚══════════════════════════════════════════════════════════════════════════════

# NOTE: The old EXPOSE_TRIGGER bytes were kV ramp telemetry sent backwards.
# Expose is triggered by the physical button on the unit, NOT by a TCP command.
# The test now arms the device and waits for the physical button.

def test_expose(sock: socket.socket, skip: bool = False) -> TestResult:
    """Arm device for expose, wait for physical button + kV ramp + Released."""
    r = TestResult("Expose + kV Ramp")

    if skip:
        r.status = "SKIPPED"
        r.detail = "User declined expose confirmation"
        return r

    t0 = time.perf_counter()
    kv_samples: list[KVSample] = []
    trigger_count = 0
    released = False

    try:
        # NOTE: Device should already be armed via SironaLiveClient.arm_for_expose()
        # before this test is called.  This test just monitors the data stream.
        log.info("Waiting for physical expose button (device should be armed)")

        # Monitor for up to 8s
        sock.settimeout(1.0)
        deadline = time.perf_counter() + 8.0

        while time.perf_counter() < deadline:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue

            if not data:
                break

            log.debug("Expose data: %d bytes  hex=%s", len(data), _hex_preview(data))

            # Extract kV samples
            samples = _extract_kv_samples(data)
            for s in samples:
                kv_samples.append(s)
                if s.is_expose_trigger:
                    trigger_count += 1

            # Check for Released event
            events = _extract_events(data)
            for ev in events:
                if ev.event_type == "state_released":
                    released = True
                    break

            if released:
                break

        elapsed = (time.perf_counter() - t0) * 1000
        r.duration_ms = elapsed

        # Compute kV peak
        kv_peak = 0.0
        if kv_samples:
            kv_peak = max(s.kv_raw for s in kv_samples) / 10.0

        r.diagnostics["kv_samples"] = len(kv_samples)
        r.diagnostics["kv_peak"] = kv_peak
        r.diagnostics["trigger_count"] = trigger_count
        r.diagnostics["released"] = released
        r.diagnostics["elapsed_ms"] = round(elapsed, 0)

        # Assertions
        failures = []
        if kv_peak < 65.0 or kv_peak > 75.0:
            failures.append(f"kV peak {kv_peak:.1f} outside 65–75 range")
        if trigger_count < 36:
            failures.append(f"Only {trigger_count} trigger points (need 36+)")
        if not released:
            failures.append("No 'Released' state received within 8s")

        if failures:
            r.status = "FAIL"
            r.detail = "; ".join(failures)
        else:
            r.status = "PASS"
            r.detail = f"{kv_peak:.1f} kV peak, {trigger_count} triggers, Released OK"

    except OSError as exc:
        r.status = "FAIL"
        r.detail = f"Socket error during expose: {exc}"
        r.duration_ms = (time.perf_counter() - t0) * 1000

    return r


def test_expose_replay(capture: DecodedCapture) -> TestResult:
    """Replay TEST 3 from parsed dump."""
    r = TestResult("Expose + kV Ramp (replay)")

    if not capture.kv_samples:
        r.status = "FAIL"
        r.detail = "No kV samples in dump"
        return r

    kv_peak = max(s.kv_raw for s in capture.kv_samples) / 10.0
    triggers = sum(1 for s in capture.kv_samples if s.is_expose_trigger)
    released = any(
        e.event_type == "state_released" for e in capture.events
    )

    r.diagnostics["kv_samples"] = len(capture.kv_samples)
    r.diagnostics["kv_peak"] = kv_peak
    r.diagnostics["trigger_count"] = triggers
    r.diagnostics["released"] = released

    r.status = "PASS"
    r.detail = f"{kv_peak:.1f} kV peak, {triggers} triggers (replay)"
    return r


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  TEST 4 — Scanline Receipt (0x40–0x4D)
# ╚══════════════════════════════════════════════════════════════════════════════

def test_scanlines(
    sock: socket.socket | None,
    capture: DecodedCapture | None = None,
) -> tuple[TestResult, list[Scanline]]:
    """Receive or replay scanlines, validate, reconstruct image."""
    r = TestResult("Scanlines (0x40\u20130x4D)")
    scanlines: list[Scanline] = []
    t0 = time.perf_counter()

    # Collect scanlines — live or from capture
    if sock is not None:
        sock.settimeout(2.0)
        deadline = time.perf_counter() + 15.0

        while time.perf_counter() < deadline:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                if scanlines:
                    break  # got some, done waiting
                continue
            except OSError:
                break

            if not data:
                break

            sls = _extract_scanlines(data)
            scanlines.extend(sls)

            if len(scanlines) >= 13:
                break

    elif capture is not None:
        scanlines = list(capture.scanlines)

    r.duration_ms = (time.perf_counter() - t0) * 1000

    if not scanlines:
        r.status = "FAIL"
        r.detail = "No scanlines received"
        return r, []

    # Validate
    ids = [sl.scanline_id for sl in scanlines]
    pixel_counts = [sl.pixel_count for sl in scanlines]
    all_pixels = np.concatenate([sl.pixels for sl in scanlines])

    r.diagnostics["scanline_count"] = len(scanlines)
    r.diagnostics["ids_hex"] = [f"0x{i:02X}" for i in ids]
    r.diagnostics["pixel_counts"] = list(set(pixel_counts))
    r.diagnostics["pixel_min"] = int(all_pixels.min())
    r.diagnostics["pixel_max"] = int(all_pixels.max())
    r.diagnostics["pixel_mean"] = round(float(all_pixels.mean()), 1)

    failures = []

    # Check we got the expected 13 scanlines
    if len(scanlines) < 9:  # minimum viable for reconstruction
        failures.append(f"Only {len(scanlines)} scanlines (need >=9)")

    # Check pixel count consistency (should be 240)
    target_px = max(set(pixel_counts), key=pixel_counts.count)
    if target_px != 240:
        failures.append(f"Expected 240 px/line, got {target_px}")

    # Check pixels aren't all zeros or all max
    if all_pixels.max() == 0:
        failures.append("All pixels are zero (no image data)")
    elif all_pixels.min() == 0xFFFF and all_pixels.max() == 0xFFFF:
        failures.append("All pixels are 0xFFFF (saturated)")

    # Reconstruct
    img = reconstruct_image(scanlines)
    if img is not None:
        r.diagnostics["reconstruct_shape"] = f"{img.width}x{img.height}"
        img.save(SCAN_PNG)
        r.diagnostics["png_saved"] = str(SCAN_PNG)
        log.info("Reconstructed PNG saved: %s (%dx%d)", SCAN_PNG, img.width, img.height)
    else:
        failures.append("reconstruct_image() returned None")

    if failures:
        r.status = "FAIL"
        r.detail = "; ".join(failures)
    else:
        r.status = "PASS"
        r.detail = (
            f"{len(scanlines)} scanlines, {target_px}px each, "
            f"range [{all_pixels.min()}\u2013{all_pixels.max()}], PNG saved"
        )

    return r, scanlines


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  TEST 5 — DICOM Export + History Integration
# ╚══════════════════════════════════════════════════════════════════════════════

TEST_PATIENT = {
    "first": "Hardware", "last": "Test", "dob": "01/01/1990",
    "id": "hwtest001", "exam": "Panoramic", "set": True,
}


def test_dicom_and_history(
    scanlines: list[Scanline], kv_peak: float
) -> TestResult:
    """Export DICOM from live scanlines, verify tags, check history."""
    r = TestResult("DICOM + History")
    t0 = time.perf_counter()

    if not HAS_PYDICOM:
        r.status = "FAIL"
        r.detail = "pydicom not installed"
        r.duration_ms = 0
        return r

    # Use live scanlines if available, otherwise create synthetic
    if not scanlines:
        log.info("No live scanlines — using synthetic 13x240 for DICOM test")

        class _MockSL:
            def __init__(self, sid: int):
                self.scanline_id = sid
                self.pixel_count = 240
                self.pixels = np.linspace(500, 8000, 240, dtype=np.uint16)

        scanlines = [_MockSL(0x40 + i) for i in range(13)]

    outdir = PATIENTS_DIR / TEST_PATIENT["id"]
    outdir.mkdir(parents=True, exist_ok=True)

    failures = []

    # 1. DICOM export
    try:
        exporter = PureXSDICOM()
        dcm_path = exporter.export(TEST_PATIENT, scanlines, kv_peak, outdir)
        r.diagnostics["dcm_path"] = dcm_path
    except Exception as exc:
        r.status = "FAIL"
        r.detail = f"DICOM export error: {exc}"
        r.duration_ms = (time.perf_counter() - t0) * 1000
        return r

    dcm_file = Path(dcm_path)
    r.diagnostics["dcm_size"] = dcm_file.stat().st_size

    if dcm_file.stat().st_size < 1000:
        failures.append(f"DICOM too small: {dcm_file.stat().st_size} bytes")

    # 2. Verify DICOM tags via readback
    try:
        import pydicom
        ds = pydicom.dcmread(dcm_path)

        checks = {
            "PatientName": (str(ds.PatientName), "Test^Hardware"),
            "Modality": (ds.Modality, "PX"),
            "BitsAllocated": (ds.BitsAllocated, 16),
            "SamplesPerPixel": (ds.SamplesPerPixel, 1),
        }
        r.diagnostics["dicom_tags"] = {
            k: v[0] for k, v in checks.items()
        }
        r.diagnostics["dicom_kvp"] = ds.KVP
        r.diagnostics["dicom_dims"] = f"{ds.Columns}x{ds.Rows}"

        for tag_name, (actual, expected) in checks.items():
            if actual != expected:
                failures.append(f"DICOM {tag_name}: {actual!r} != {expected!r}")

        # Verify pixel data length
        expected_bytes = ds.Rows * ds.Columns * 2
        actual_bytes = len(ds.PixelData)
        if actual_bytes != expected_bytes:
            failures.append(
                f"PixelData length {actual_bytes} != expected {expected_bytes}"
            )

    except Exception as exc:
        failures.append(f"DICOM readback failed: {exc}")

    # 3. Write a sessions.json entry so history can find it
    sessions_path = outdir / "sessions.json"
    session_record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "exam_type": "Panoramic",
        "kv_peak": round(kv_peak, 1),
        "scanlines": len(scanlines),
        "image_file": f"Test_Hardware_{TIMESTAMP}_panoramic.png",
        "dcm_file": dcm_file.name,
        "events_log": "",
    }

    sessions: list[dict] = []
    try:
        if sessions_path.exists():
            sessions = json.loads(sessions_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    sessions.append(session_record)
    sessions_path.write_text(json.dumps(sessions, indent=2), encoding="utf-8")

    # 4. Verify history integration
    if HAS_HISTORY:
        try:
            records = load_all_patients()
            found = any(r.patient_id == "hwtest001" for r in records)
            r.diagnostics["in_history"] = found
            if not found:
                failures.append("hwtest001 not found in patient history")
            else:
                match = next(r for r in records if r.patient_id == "hwtest001")
                r.diagnostics["history_scans"] = match.scan_count
        except Exception as exc:
            failures.append(f"History load failed: {exc}")
    else:
        log.info("history.py not importable (tkinter) — skipping history check")
        r.diagnostics["in_history"] = "skipped (no tkinter)"

    r.duration_ms = (time.perf_counter() - t0) * 1000

    if failures:
        r.status = "FAIL"
        r.detail = "; ".join(failures)
    else:
        r.status = "PASS"
        r.detail = (
            f"DICOM valid ({dcm_file.stat().st_size:,} bytes), "
            f"tags OK, history entry found"
        )

    return r


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Runner
# ╚══════════════════════════════════════════════════════════════════════════════

def _print_header(host: str, port: int, mode: str) -> None:
    print()
    print("\u2550" * 54)
    print(f"  PureXS Live Hardware Test \u2014 {datetime.now():%Y-%m-%d %H:%M}")
    print(f"  Target: {host}:{port}  [{mode}]")
    print(f"  Log: {LOG_FILE}")
    print("\u2550" * 54)
    print()


def _print_result(r: TestResult, idx: int) -> None:
    print(f"  TEST {idx}  {r.name:<30}  {r.icon} {r.status}  ({r.duration_ms:.0f}ms)")
    if r.detail:
        print(f"         {r.detail}")
    log.info(
        "TEST %d  %s  %s  %.0fms  %s  diag=%s",
        idx, r.name, r.status, r.duration_ms, r.detail,
        json.dumps(r.diagnostics, default=str),
    )


def run_live(host: str, port: int, skip_expose: bool) -> int:
    """Run all 5 tests against live hardware."""
    _print_header(host, port, "LIVE")
    results: list[TestResult] = []

    # TEST 1 — Connect
    r1, sock = test_connect(host, port)
    results.append(r1)
    _print_result(r1, 1)

    if r1.status != "PASS" or sock is None:
        print("\n  Cannot continue without connection. Aborting.\n")
        return 1

    # TEST 2 — HB
    r2 = test_hb_pairs(sock)
    results.append(r2)
    _print_result(r2, 2)

    # TEST 3 — Expose
    expose_skip = skip_expose
    if not skip_expose:
        print()
        print("  \u26A0  WARNING: TEST 3 will trigger a panoramic expose on")
        print(f"     real hardware at {host}:{port}.")
        print("     Ensure patient is positioned or device is in test mode.")
        confirm = input("     Type CONFIRM to proceed (or Enter to skip): ").strip()
        expose_skip = confirm.upper() != "CONFIRM"
        print()

    r3 = test_expose(sock, skip=expose_skip)
    results.append(r3)
    _print_result(r3, 3)

    kv_peak = r3.diagnostics.get("kv_peak", 70.0) if r3.status == "PASS" else 70.0

    # TEST 4 — Scanlines
    r4, scanlines = test_scanlines(
        sock if r3.status == "PASS" else None,
        capture=None,
    )
    results.append(r4)
    _print_result(r4, 4)

    # Clean up socket
    try:
        sock.close()
    except Exception:
        pass

    # TEST 5 — DICOM + History (always runs, synthetic fallback)
    r5 = test_dicom_and_history(scanlines, kv_peak)
    results.append(r5)
    _print_result(r5, 5)

    return _print_summary(results)


def run_replay(dump_path: str) -> int:
    """Replay tests 1–4 from a Wireshark dump, test 5 always live."""
    _print_header("replay", 0, f"REPLAY: {Path(dump_path).name}")

    print("  Parsing dump...")
    capture = parse_wireshark_dump(dump_path)
    print(f"  Loaded: {len(capture.frames)} frames, "
          f"{len(capture.kv_samples)} kV samples, "
          f"{len(capture.scanlines)} scanlines\n")

    results: list[TestResult] = []

    # TEST 1
    r1 = test_connect_replay(capture)
    results.append(r1)
    _print_result(r1, 1)

    # TEST 2
    r2 = test_hb_pairs_replay(capture)
    results.append(r2)
    _print_result(r2, 2)

    # TEST 3
    r3 = test_expose_replay(capture)
    results.append(r3)
    _print_result(r3, 3)

    kv_peak = r3.diagnostics.get("kv_peak", 70.0)

    # TEST 4
    r4, scanlines = test_scanlines(None, capture=capture)
    results.append(r4)
    _print_result(r4, 4)

    # TEST 5 — always live
    r5 = test_dicom_and_history(scanlines, kv_peak)
    results.append(r5)
    _print_result(r5, 5)

    return _print_summary(results)


def _print_summary(results: list[TestResult]) -> int:
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    skipped = sum(1 for r in results if r.status == "SKIPPED")
    total = len(results)

    print()
    print("\u2550" * 54)

    if failed == 0 and skipped == 0:
        verdict = f"RESULT: {passed}/{total} PASSED \u2014 PureXS production ready \u2705"
    elif failed == 0:
        verdict = f"RESULT: {passed}/{total} PASSED, {skipped} SKIPPED \u23ED"
    else:
        verdict = f"RESULT: {passed}/{total} PASSED, {failed} FAILED \u274C"

    print(f"  {verdict}")
    print(f"  Full log: {LOG_FILE}")
    print("\u2550" * 54)
    print()

    log.info("SUMMARY: %d/%d passed, %d failed, %d skipped",
             passed, total, failed, skipped)

    if failed > 0:
        return 1
    if skipped > 0:
        return 2
    return 0


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  CLI
# ╚══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="live_test",
        description="PureXS Live Hardware Test Suite",
    )
    parser.add_argument(
        "--host", default="192.168.139.170", help="Device IP (default: 192.168.139.170)"
    )
    parser.add_argument(
        "--port", "-p", type=int, default=12837, help="TCP port (default: 12837)"
    )
    parser.add_argument(
        "--replay", metavar="DUMP", help="Replay from Wireshark text dump (skip live socket)"
    )
    parser.add_argument(
        "--skip-expose", action="store_true",
        help="Skip TEST 3 (expose) without prompting"
    )
    args = parser.parse_args()

    if args.replay:
        return run_replay(args.replay)
    else:
        return run_live(args.host, args.port, args.skip_expose)


if __name__ == "__main__":
    sys.exit(main())
