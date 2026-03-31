#!/usr/bin/env python3
"""
PureXS Expose GUI — CustomTkinter native desktop client for PureXS headless API.

Replaces Sidexis desktop UI with a modern dark-mode interface for direct
Sirona ORTHOPHOS XG / SL / GALILEOS control via the PureXS REST API.

Dependencies:
    pip install customtkinter pillow requests websocket-client

PyInstaller:
    pyinstaller --onefile --windowed --name PureXS purexs_gui.py
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import struct
import sys
import textwrap
import threading
import time
import tkinter as tk
import uuid
from datetime import datetime, date
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any

import customtkinter as ctk
from PIL import Image, ImageTk

# Optional: requests is only needed for the PureXS REST API path.
# On dental PCs that use direct TCP (SironaLiveClient), it's not required.
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# Optional: direct TCP decoder for raw Sirona HB monitoring
try:
    from hb_decoder import SironaLiveClient, KVSample, Scanline, reconstruct_image
    HAS_HB_DECODER = True
except ImportError:
    HAS_HB_DECODER = False

# Optional: SiNet2 UDP device discovery
try:
    from src.protocol.udp import UDPDiscovery
    HAS_UDP_DISCOVERY = True
except ImportError:
    HAS_UDP_DISCOVERY = False

import numpy as np

# Optional: DICOM export
try:
    from dicom_export import PureXSDICOM
    HAS_DICOM = True
except ImportError:
    HAS_DICOM = False

# Optional: Patient history view
try:
    from history import PatientHistoryWindow
    HAS_HISTORY = True
except ImportError:
    HAS_HISTORY = False

# PHASE 1-3 — PureChart patient search + upload integration
try:
    from purechart import (
        PureChartPatientLoader, PureChartPatient,
        PureChartUploader, UploadResult, EXAM_TYPE_MAP,
    )
    HAS_PURECHART = True
except ImportError:
    HAS_PURECHART = False

from utils import get_data_dir, open_path

# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Configuration
# ╚══════════════════════════════════════════════════════════════════════════════

_DATA_DIR = get_data_dir()

# API server URL — only used if PureXS FastAPI server is running (optional).
# Direct TCP mode (Monitor HB + direct EXPOSE) does NOT need this.
API_BASE = os.environ.get("PUREXS_API", "http://localhost:8000")

SIRONA_IP = os.environ.get("SIRONA_IP", "192.168.139.170")
SIRONA_PORT = int(os.environ.get("SIRONA_PORT", "12837"))

HB_POLL_INTERVAL_S = 0.5          # Heartbeat poll every 500 ms
SCAN_TIMEOUT_S = 5.0              # UDP discovery timeout
ACQUIRE_TIMEOUT_S = 120.0         # Max wait for image acquisition
LOG_MAX_LINES = 500               # Ring-buffer log limit

# Direct expose constants
EXPOSE_TIMEOUT_S = 120.0          # Hard timeout — generous for physical button press + scan
KV_MAX_DISPLAY = 90.0             # Max kV for progress bar scaling
EXPOSE_EVENT_LOG = _DATA_DIR / "events.log"

# Patient workflow
PATIENTS_DIR = _DATA_DIR / "patients"
RECENT_PATIENTS_FILE = _DATA_DIR / "recent_patients.json"
RECENT_PATIENTS_MAX = 10
EXAM_TYPES = ["Panoramic", "Bitewing", "Periapical"]

# Exposure parameter IDs (from constants.py / orthophos_xg.py)
PARAM_KV = 0x0010
PARAM_MA = 0x0011
PARAM_PROGRAM = 0x0020
PARAM_PATIENT_SIZE = 0x0021

# Exposure programs
PROGRAMS = {
    "Panoramic":            0x01,
    "Ceph Lateral":         0x02,
    "Ceph Frontal":         0x03,
    "Bitewing Left":        0x10,
    "Bitewing Right":       0x11,
    "Bitewing Bilateral":   0x12,
}

# Patient size presets
PATIENT_SIZES = {
    "Child":    0x00,
    "Adult S":  0x01,
    "Adult M":  0x02,
    "Adult L":  0x03,
}

# kV range for ORTHOPHOS XG
KV_OPTIONS = [str(v) for v in range(60, 91)]
# mA × 10 common values (display as mA)
MA_OPTIONS = ["4.0", "5.0", "6.0", "7.0", "8.0", "10.0", "12.0", "14.0", "16.0"]

# ── Logging setup ────────────────────────────────────────────────────────────

LOG_DIR = _DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"purexs_{datetime.now():%Y%m%d_%H%M%S}.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("purexs.gui")


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  API Client (optional — only works if `requests` is installed and
# ║  the PureXS FastAPI server is running.  Direct TCP mode skips this.)
# ╚══════════════════════════════════════════════════════════════════════════════

class PureXSAPI:
    """Thin wrapper around PureXS REST endpoints. All methods are blocking.

    Only functional when the `requests` library is installed.  On dental PCs
    that use direct TCP (Monitor HB + direct EXPOSE), this class is
    instantiated but every method is a no-op that raises.
    """

    def __init__(self, base_url: str = API_BASE) -> None:
        self.base = base_url.rstrip("/")
        self.session = None
        if HAS_REQUESTS:
            self.session = requests.Session()
            self.session.headers["Accept"] = "application/json"

    # ── Discovery ─────────────────────────────────────────────────────────────

    def scan(self, timeout: float = SCAN_TIMEOUT_S) -> list[dict]:
        """POST /devices/scan → list of device summaries."""
        r = self.session.post(
            f"{self.base}/devices/scan",
            json={"timeout": timeout},
            timeout=timeout + 5,
        )
        r.raise_for_status()
        return r.json()

    def list_devices(self) -> list[dict]:
        """GET /devices → list of known devices."""
        r = self.session.get(f"{self.base}/devices", timeout=5)
        r.raise_for_status()
        return r.json()

    # ── Device lifecycle ──────────────────────────────────────────────────────

    def connect(self, mac: str) -> dict:
        """POST /devices/{mac}/connect → device summary with state."""
        r = self.session.post(f"{self.base}/devices/{mac}/connect", timeout=15)
        r.raise_for_status()
        return r.json()

    def disconnect(self, mac: str) -> dict:
        """POST /devices/{mac}/disconnect."""
        r = self.session.post(f"{self.base}/devices/{mac}/disconnect", timeout=5)
        r.raise_for_status()
        return r.json()

    def status(self, mac: str) -> dict:
        """GET /devices/{mac}/status → {"status_code": int}."""
        r = self.session.get(f"{self.base}/devices/{mac}/status", timeout=3)
        r.raise_for_status()
        return r.json()

    def device_info(self, mac: str) -> dict:
        """GET /devices/{mac} → device summary."""
        r = self.session.get(f"{self.base}/devices/{mac}", timeout=5)
        r.raise_for_status()
        return r.json()

    # ── Parameters ────────────────────────────────────────────────────────────

    def get_param(self, mac: str, param_id: int) -> str:
        """GET /devices/{mac}/param/{id} → hex value string."""
        r = self.session.get(
            f"{self.base}/devices/{mac}/param/{param_id}", timeout=5
        )
        r.raise_for_status()
        return r.json().get("value_hex", "")

    def set_param(self, mac: str, param_id: int, value_hex: str) -> None:
        """PUT /devices/{mac}/param/{id} with hex value."""
        r = self.session.put(
            f"{self.base}/devices/{mac}/param/{param_id}",
            json={"value_hex": value_hex},
            timeout=5,
        )
        r.raise_for_status()

    def set_param_word(self, mac: str, param_id: int, value: int) -> None:
        """Write a 2-byte big-endian WORD parameter."""
        self.set_param(mac, param_id, f"{value:04x}")

    # ── Acquisition ───────────────────────────────────────────────────────────

    def acquire(self, mac: str) -> dict:
        """POST /devices/{mac}/acquire → {mac, ip, size_bytes, data_b64}."""
        r = self.session.post(
            f"{self.base}/devices/{mac}/acquire",
            timeout=ACQUIRE_TIMEOUT_S,
        )
        r.raise_for_status()
        return r.json()

    # ── Health ────────────────────────────────────────────────────────────────

    def health(self) -> dict:
        """GET /health → {"status": "ok", ...}."""
        r = self.session.get(f"{self.base}/health", timeout=3)
        r.raise_for_status()
        return r.json()


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Toast Notification
# ╚══════════════════════════════════════════════════════════════════════════════

class Toast(ctk.CTkToplevel):
    """Non-blocking toast notification that auto-dismisses."""

    def __init__(
        self,
        parent: ctk.CTk,
        message: str,
        duration_ms: int = 3000,
        level: str = "info",
    ) -> None:
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        colours = {
            "info":    ("#2196F3", "#FFFFFF"),
            "success": ("#4CAF50", "#FFFFFF"),
            "warning": ("#FF9800", "#000000"),
            "error":   ("#F44336", "#FFFFFF"),
        }
        bg, fg = colours.get(level, colours["info"])

        self.configure(fg_color=bg)

        label = ctk.CTkLabel(
            self,
            text=message,
            text_color=fg,
            font=ctk.CTkFont(size=13, weight="bold"),
            wraplength=400,
            padx=20,
            pady=12,
        )
        label.pack()

        # Position: bottom-right of parent
        self.update_idletasks()
        px = parent.winfo_x() + parent.winfo_width() - self.winfo_width() - 20
        py = parent.winfo_y() + parent.winfo_height() - self.winfo_height() - 40
        self.geometry(f"+{px}+{py}")

        self.after(duration_ms, self.destroy)


class _ToolTip:
    """Hover tooltip for any Tk/CTk widget."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self._widget = widget
        self.text = text
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event: tk.Event) -> None:
        if self._tip or not self.text:
            return
        x = self._widget.winfo_rootx() + 20
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tip = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            tw, text=self.text, justify="left",
            background="#37474F", foreground="#ECEFF1",
            relief="solid", borderwidth=1,
            font=("Consolas", 10), padx=8, pady=4,
        )
        label.pack()

    def _hide(self, _event: tk.Event) -> None:
        if self._tip:
            self._tip.destroy()
            self._tip = None


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Main Application
# ╚══════════════════════════════════════════════════════════════════════════════

class PureXSApp(ctk.CTk):
    """PureXS Expose GUI — Sidexis-killer desktop client."""

    def __init__(self) -> None:
        super().__init__()

        # ── Window setup ─────────────────────────────────────────────────────
        self.title("PureXS — Sirona Direct Control")
        self.geometry("1280x820")
        self.minsize(960, 700)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # Win11 acrylic / DWM transparency (best-effort)
        try:
            if sys.platform == "win32":
                self.attributes("-alpha", 0.97)
        except Exception:
            pass

        # ── State ────────────────────────────────────────────────────────────
        self.api = PureXSAPI()
        self._mac: str = ""
        self._connected = False
        self._hb_thread: threading.Thread | None = None
        self._hb_stop = threading.Event()
        self._acquire_lock = threading.Lock()
        self._last_status: str = "OFFLINE"
        self._expose_count: int = 0
        self._photo_image: ImageTk.PhotoImage | None = None  # prevent GC
        self._last_pil_image: Image.Image | None = None     # for resize re-render
        self._last_raw_image: bytes = b""

        # ── Patient workflow state ─────────────────────────────────────────────
        self._patient: dict = {
            "first": "", "last": "", "dob": "", "id": "",
            "exam": "Panoramic", "set": False,
        }

        # PHASE 1-3 — PureChart patient list, search & upload
        self._purechart_patients: list = []       # List[PureChartPatient]
        self._selected_purechart: object = None   # currently selected PureChartPatient
        self._facility_token: str = os.environ.get("PURECHART_FACILITY_TOKEN", "43bd5ee3a662f5cbf468bfc6402eb56ec685fb315461114275b4204402a2cf17")
        self._purechart_loader: object = None
        self._purechart_uploader: object = None   # PHASE 3
        self._last_pano_path: str = ""            # PHASE 3 — path to last saved panoramic PNG
        self._last_upload_args: tuple = ()        # PHASE 5 — (patient_id, file_path, type, title)
        self._purechart_searching: bool = False   # PHASE 2 — True while bg search in flight
        self._profile_photo: ImageTk.PhotoImage | None = None  # prevent GC for profile pic
        if HAS_PURECHART:
            self._purechart_loader = PureChartPatientLoader(self._facility_token)
            self._purechart_uploader = PureChartUploader(self._facility_token)

        # ── Direct TCP monitor state ─────────────────────────────────────────
        self._sirona_client: object | None = None  # SironaLiveClient when connected
        self._direct_connected = False
        self._device_ready = False                  # True only when status == READY (0x0000)

        # ── Direct expose state ──────────────────────────────────────────────
        self._exposing = False              # True while expose in flight
        self._expose_timer_id: str | None = None  # after() ID for timeout
        self._expose_scanlines: list = []   # Scanline objects from current expose
        self._expose_kv_peak: float = 0.0   # peak kV seen this expose
        self._expose_start_time: float = 0.0
        self._pano_photo: ImageTk.PhotoImage | None = None  # prevent GC
        self._last_dcm_path: str = ""     # path to last exported .dcm
        self._no_response_timer_id: str | None = None  # 2s no-response watchdog
        self._got_kv_or_scanline: bool = False          # cleared each expose
        self._history_window: object | None = None  # PatientHistoryWindow singleton

        # ── Build UI ─────────────────────────────────────────────────────────
        self._build_toolbar()
        self._build_main_area()
        self._build_status_bar()

        # ── Keybindings ──────────────────────────────────────────────────────
        self.bind("<Escape>", lambda _: self._on_disconnect())
        self.bind("<F5>", lambda _: self._on_scan())
        self.bind("<Return>", lambda _: self._on_expose())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── Initial log ──────────────────────────────────────────────────────
        self._log("PureXS GUI started", "info")
        self._log(f"API: {API_BASE}", "info")
        self._log(f"Log file: {LOG_FILE}", "info")

        # ── Auto-check API health on startup ─────────────────────────────────
        self.after(200, self._check_api_health)

        # PHASE 1 — Load PureChart patients on startup (non-blocking)
        if HAS_PURECHART and self._purechart_loader:
            self.after(300, self._phase1_load_patients)

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  UI Construction
    # ╚════════════════════════════════════════════════════════════════════════

    def _build_toolbar(self) -> None:
        """Top toolbar: device selector, connect/disconnect, scan."""
        toolbar = ctk.CTkFrame(self, height=50, corner_radius=0)
        toolbar.pack(fill="x", padx=0, pady=0)
        toolbar.pack_propagate(False)

        # ── Logo / title ─────────────────────────────────────────────────────
        logo_label = ctk.CTkLabel(
            toolbar,
            text="  PureXS",
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color="#4FC3F7",
        )
        logo_label.pack(side="left", padx=(12, 20))

        # Hidden device state (no UI — direct TCP auto-connects)
        self._mac_var = ctk.StringVar(value="")

        # ── Direct TCP HB monitor button ─────────────────────────────────────
        self._hb_monitor_btn = ctk.CTkButton(
            toolbar,
            text="Connect to Device",
            width=120,
            command=self._on_toggle_hb_monitor,
            fg_color="#4A148C",
            hover_color="#6A1B9A",
            state="normal" if HAS_HB_DECODER else "disabled",
        )
        self._hb_monitor_btn.pack(side="left", padx=(16, 4))

        # ── Direct TCP EXPOSE button ─────────────────────────────────────────
        self._direct_expose_btn = ctk.CTkButton(
            toolbar,
            text="\u2622 EXPOSE",
            width=120,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._on_direct_expose,
            fg_color="#616161",
            hover_color="#757575",
            text_color="#9E9E9E",
            state="disabled",
        )
        self._direct_expose_btn.pack(side="left", padx=4)
        _EXPOSE_TOOLTIP = (
            "Requires: Patient set + HB pulse\n"
            "Press R on unit keypad to position gantry first."
        )
        _ToolTip(self._direct_expose_btn, _EXPOSE_TOOLTIP)

        # ── History button ───────────────────────────────────────────────────
        self._history_btn = ctk.CTkButton(
            toolbar,
            text="\U0001F5C2 History",
            width=90,
            command=self._on_open_history,
            fg_color="#37474F",
            hover_color="#455A64",
            state="normal" if HAS_HISTORY else "disabled",
        )
        self._history_btn.pack(side="left", padx=(12, 4))

        # ── API status indicator ─────────────────────────────────────────────
        self._api_dot = ctk.CTkLabel(
            toolbar,
            text="\u2B24",
            font=ctk.CTkFont(size=10),
            text_color="#616161",
        )
        self._api_dot.pack(side="right", padx=(0, 12))

        self._api_label = ctk.CTkLabel(
            toolbar,
            text="API: checking...",
            font=ctk.CTkFont(size=11),
            text_color="#9E9E9E",
        )
        self._api_label.pack(side="right", padx=0)

    def _build_main_area(self) -> None:
        """Middle area: left panel (controls + log), right panel (image)."""
        self._main_grid = ctk.CTkFrame(self, fg_color="transparent")
        main = self._main_grid
        main.pack(fill="both", expand=True, padx=8, pady=(4, 0))
        main.columnconfigure(0, weight=0, minsize=90)   # patient dock
        main.columnconfigure(1, weight=1, minsize=300)   # controls
        main.columnconfigure(2, weight=2, minsize=400)   # canvas
        main.rowconfigure(0, weight=1)

        # ═══════════════════════════════════════════════════════════════════
        # PATIENT DOCK (far left — vertical scrollable avatar strip)
        # ═══════════════════════════════════════════════════════════════════
        self._dock_frame = ctk.CTkFrame(main, corner_radius=10, fg_color="#0D1117", width=88)
        self._dock_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=0)
        self._dock_frame.grid_propagate(False)
        self._dock_visible = True

        # Hidden refs for search compatibility
        self._purechart_search_var = ctk.StringVar(value="")
        self._purechart_debounce_id: str | None = None
        self._purechart_status = ctk.CTkLabel(
            self._dock_frame, text="", font=ctk.CTkFont(size=8),
            text_color="#757575", wraplength=76,
        )
        self._purechart_status.pack(padx=4, pady=(6, 2))

        # Scrollable avatar column (single column, vertical)
        self._avatar_dock_frame = ctk.CTkScrollableFrame(
            self._dock_frame, fg_color="#0D1117", corner_radius=0,
            width=76, label_text="",
        )
        self._avatar_dock_frame.pack(fill="both", expand=True, padx=2, pady=(0, 4))
        self._avatar_tiles: list[dict] = []
        self._avatar_photos: dict = {}   # (patient_id, size) → PhotoImage
        self._avatar_raw_bytes: dict[str, bytes] = {}  # patient_id → raw image bytes

        # ═══════════════════════════════════════════════════════════════════
        # LEFT PANEL: status, patient card, expose button, log
        # ═══════════════════════════════════════════════════════════════════
        left = ctk.CTkFrame(main, corner_radius=8)
        left.grid(row=0, column=1, sticky="nsew", padx=(0, 4), pady=0)
        left.rowconfigure(4, weight=1)  # log expands

        # ── Status section ───────────────────────────────────────────────
        status_frame = ctk.CTkFrame(left, fg_color="#1A1A2E", corner_radius=8)
        status_frame.pack(fill="x", padx=8, pady=(8, 4))

        self._status_label = ctk.CTkLabel(
            status_frame,
            text="OFFLINE",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#616161",
            wraplength=260,
        )
        self._status_label.pack(pady=(8, 2))

        self._device_info_label = ctk.CTkLabel(
            status_frame,
            text="No device connected",
            font=ctk.CTkFont(size=10),
            text_color="#757575",
        )
        self._device_info_label.pack(pady=(0, 2))

        # ── Progress bar (indeterminate during scan/acquire) ─────────────
        self._progress = ctk.CTkProgressBar(
            status_frame, mode="indeterminate", width=260
        )
        self._progress.pack(pady=(0, 2), padx=12)
        self._progress.set(0)

        # ── Gantry / acquisition phase label ──────────────────────────
        self._phase_label = ctk.CTkLabel(
            status_frame,
            text="",
            font=ctk.CTkFont(size=9),
            text_color="#757575",
            wraplength=260,
        )
        self._phase_label.pack(pady=(0, 6))

        # ── Patient panel ───────────────────────────────────────────────
        patient_frame = ctk.CTkFrame(left, corner_radius=8, fg_color="#1B2631")
        patient_frame.pack(fill="x", padx=8, pady=4)

        ctk.CTkLabel(
            patient_frame, text="Patient",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#81D4FA",
        ).pack(pady=(8, 2))

        # Hidden refs for compatibility
        self._recent_var = ctk.StringVar(value="")
        self._purechart_row = patient_frame  # reference for pack_configure

        # ── Selected patient profile card ───────────────────────────────
        self._profile_card = ctk.CTkFrame(patient_frame, fg_color="#162029", corner_radius=8)
        self._profile_card_visible = False

        profile_top = ctk.CTkFrame(self._profile_card, fg_color="transparent")
        profile_top.pack(fill="x", padx=10, pady=(8, 4))

        self._profile_avatar_canvas = tk.Canvas(
            profile_top, width=64, height=64,
            bg="#162029", highlightthickness=0,
        )
        self._profile_avatar_canvas.pack(side="left", padx=(0, 10))
        self._profile_avatar_canvas.create_oval(2, 2, 62, 62, fill="#37474F", outline="#546E7A", width=2)
        self._profile_initials = self._profile_avatar_canvas.create_text(
            32, 32, text="?", fill="#B0BEC5",
            font=("Helvetica", 18, "bold"),
        )
        self._profile_photo = None

        profile_info = ctk.CTkFrame(profile_top, fg_color="transparent")
        profile_info.pack(side="left", fill="both", expand=True)

        self._profile_name_label = ctk.CTkLabel(
            profile_info, text="",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#E0E0E0", anchor="w",
        )
        self._profile_name_label.pack(fill="x")

        self._profile_mrn_label = ctk.CTkLabel(
            profile_info, text="",
            font=ctk.CTkFont(size=10),
            text_color="#90A4AE", anchor="w",
        )
        self._profile_mrn_label.pack(fill="x")

        self._profile_dob_label = ctk.CTkLabel(
            profile_info, text="",
            font=ctk.CTkFont(size=10),
            text_color="#90A4AE", anchor="w",
        )
        self._profile_dob_label.pack(fill="x")

        self._profile_phone_label = ctk.CTkLabel(
            profile_info, text="",
            font=ctk.CTkFont(size=10),
            text_color="#90A4AE", anchor="w",
        )
        self._profile_phone_label.pack(fill="x")

        # Change Patient button (re-shows dock)
        self._change_patient_btn = ctk.CTkButton(
            self._profile_card, text="Change Patient",
            width=120, height=24, font=ctk.CTkFont(size=10),
            fg_color="#37474F", hover_color="#455A64",
            command=self._on_change_patient,
        )
        self._change_patient_btn.pack(pady=(2, 8))

        # PHASE 5 — Upload status bar with progress + retry
        upload_row = ctk.CTkFrame(patient_frame, fg_color="#162029", corner_radius=6)
        upload_row.pack(fill="x", padx=12, pady=(0, 4))
        self._upload_frame = upload_row
        self._upload_frame.pack_forget()  # hidden until first upload

        self._upload_progress = ctk.CTkProgressBar(
            upload_row, mode="indeterminate", width=180, height=8,
        )
        self._upload_progress.pack(fill="x", padx=8, pady=(6, 2))
        self._upload_progress.set(0)

        upload_bottom = ctk.CTkFrame(upload_row, fg_color="transparent")
        upload_bottom.pack(fill="x", padx=8, pady=(0, 6))

        self._upload_status_label = ctk.CTkLabel(
            upload_bottom, text="", font=ctk.CTkFont(size=10),
            text_color="#757575", anchor="w",
        )
        self._upload_status_label.pack(side="left", fill="x", expand=True)

        self._upload_retry_btn = ctk.CTkButton(
            upload_bottom, text="Retry", width=50, height=22,
            font=ctk.CTkFont(size=10),
            fg_color="#B71C1C", hover_color="#D32F2F",
            command=self._on_upload_retry,
        )
        self._upload_retry_btn.pack(side="right", padx=(4, 0))
        self._upload_retry_btn.pack_forget()  # hidden until failure

        # Patient fields grid
        pg = ctk.CTkFrame(patient_frame, fg_color="transparent")
        pg.pack(fill="x", padx=12, pady=(0, 4))

        ctk.CTkLabel(pg, text="First:", width=65, anchor="e").grid(
            row=0, column=0, padx=(0, 4), pady=2, sticky="e")
        self._pt_first = ctk.CTkEntry(pg, width=195, placeholder_text="First name")
        self._pt_first.grid(row=0, column=1, pady=2, sticky="w")

        ctk.CTkLabel(pg, text="Last:", width=65, anchor="e").grid(
            row=1, column=0, padx=(0, 4), pady=2, sticky="e")
        self._pt_last = ctk.CTkEntry(pg, width=195, placeholder_text="Last name")
        self._pt_last.grid(row=1, column=1, pady=2, sticky="w")

        ctk.CTkLabel(pg, text="DOB:", width=65, anchor="e").grid(
            row=2, column=0, padx=(0, 4), pady=2, sticky="e")
        self._pt_dob = ctk.CTkEntry(pg, width=195, placeholder_text="MM/DD/YYYY")
        self._pt_dob.grid(row=2, column=1, pady=2, sticky="w")

        ctk.CTkLabel(pg, text="ID:", width=65, anchor="e").grid(
            row=3, column=0, padx=(0, 4), pady=2, sticky="e")
        self._pt_id = ctk.CTkEntry(pg, width=195, placeholder_text="(auto if blank)")
        self._pt_id.grid(row=3, column=1, pady=2, sticky="w")

        self._pt_exam_var = ctk.StringVar(value="Panoramic")

        # Set / Clear buttons
        btn_row = ctk.CTkFrame(patient_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(2, 8))

        self._pt_set_btn = ctk.CTkButton(
            btn_row, text="\u2713 Set Patient", width=120, height=28,
            fg_color="#1B5E20", hover_color="#2E7D32",
            command=self._on_set_patient,
        )
        self._pt_set_btn.pack(side="left", padx=(0, 4))

        self._pt_clear_btn = ctk.CTkButton(
            btn_row, text="\u2717 Clear", width=80, height=28,
            fg_color="#37474F", hover_color="#455A64",
            command=self._on_clear_patient,
        )
        self._pt_clear_btn.pack(side="left")

        self._pt_status_label = ctk.CTkLabel(
            btn_row, text="", font=ctk.CTkFont(size=10),
            text_color="#EF5350",
        )
        self._pt_status_label.pack(side="left", padx=8)

        # Load recent patients on startup
        self._load_recent_patients()

        # Exposure parameter defaults (no UI — device uses fixed values)
        self._program_var = ctk.StringVar(value="Panoramic")
        self._patient_var = ctk.StringVar(value="Adult M")
        self._kv_var = ctk.StringVar(value="73")
        self._ma_var = ctk.StringVar(value="8.0")

        # ── EXPOSE BUTTON ────────────────────────────────────────────────
        expose_frame = ctk.CTkFrame(left, fg_color="transparent")
        expose_frame.pack(fill="x", padx=8, pady=6)

        self._expose_btn = ctk.CTkButton(
            expose_frame,
            text="\u2622  EXPOSE",
            font=ctk.CTkFont(family="Segoe UI", size=24, weight="bold"),
            height=72,
            corner_radius=12,
            fg_color="#B71C1C",
            hover_color="#D32F2F",
            text_color="#FFFFFF",
            command=self._on_expose,
            state="disabled",
        )
        self._expose_btn.pack(fill="x", padx=4, pady=4)
        _ToolTip(self._expose_btn, (
            "Requires: Patient set + READY (green) + HB pulse\n"
            "API server must be running."
        ))

        self._expose_count_label = ctk.CTkLabel(
            expose_frame,
            text="Exposures this session: 0",
            font=ctk.CTkFont(size=11),
            text_color="#757575",
        )
        self._expose_count_label.pack()

        # ── kV gauge (direct TCP expose) — hidden until expose ────────
        self._kv_frame = ctk.CTkFrame(left, corner_radius=8, fg_color="#1A1A2E")
        # Not packed — shown during expose

        kv_header = ctk.CTkFrame(self._kv_frame, fg_color="transparent")
        kv_header.pack(fill="x", padx=8, pady=(6, 0))

        ctk.CTkLabel(
            kv_header, text="Tube Voltage",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#78909C",
        ).pack(side="left")

        self._kv_value_label = ctk.CTkLabel(
            kv_header, text="kV: --",
            font=ctk.CTkFont(family="Consolas", size=12, weight="bold"),
            text_color="#546E7A",
        )
        self._kv_value_label.pack(side="right")

        self._kv_progress = ctk.CTkProgressBar(
            self._kv_frame, mode="determinate", width=280,
            progress_color="#FF6F00", fg_color="#263238",
        )
        self._kv_progress.pack(pady=(4, 8), padx=16)
        self._kv_progress.set(0)

        # ── Scanline preview (direct TCP) — hidden until expose ──────
        self._scan_preview_frame = ctk.CTkFrame(left, corner_radius=8)
        # Not packed — shown during expose

        scan_preview_header = ctk.CTkFrame(self._scan_preview_frame, fg_color="transparent")
        scan_preview_header.pack(fill="x", padx=8, pady=(6, 0))

        ctk.CTkLabel(
            scan_preview_header, text="Scanline Preview",
            font=ctk.CTkFont(size=11, weight="bold"),
        ).pack(side="left")

        self._scanline_count_label = ctk.CTkLabel(
            scan_preview_header, text="0 lines",
            font=ctk.CTkFont(family="Consolas", size=10),
            text_color="#757575",
        )
        self._scanline_count_label.pack(side="right")

        self._scanline_canvas = tk.Canvas(
            self._scan_preview_frame, bg="#0A0A0A", height=200,
            highlightthickness=0,
        )
        self._scanline_canvas.pack(fill="both", expand=True, padx=8, pady=(4, 4))

        self._save_pano_btn = ctk.CTkButton(
            self._scan_preview_frame,
            text="\U0001F4BE Save Panoramic",
            width=140, height=26,
            font=ctk.CTkFont(size=10),
            fg_color="#37474F", hover_color="#455A64",
            command=self._on_save_panoramic,
            state="disabled",
        )
        self._save_pano_btn.pack(pady=(0, 6))

        # Hidden log frame ref (for kV/scanline pack ordering)
        self._log_frame = ctk.CTkFrame(left, height=0, fg_color="transparent")
        self._log_frame.pack(side="bottom")

        # Hidden log textbox (methods still write to it)
        self._log_text = ctk.CTkTextbox(
            self._log_frame, height=0, state="disabled",
        )

        # ═══════════════════════════════════════════════════════════════════
        # RIGHT PANEL: image viewer + save button
        # ═══════════════════════════════════════════════════════════════════
        right = ctk.CTkFrame(main, corner_radius=8)
        right.grid(row=0, column=2, sticky="nsew", padx=(4, 0), pady=0)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        # ── Image canvas ─────────────────────────────────────────────────
        self._canvas = tk.Canvas(
            right,
            bg="#0A0A0A",
            highlightthickness=0,
            cursor="crosshair",
        )
        self._canvas.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 4))

        # Placeholder text
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        self._canvas_text_id = self._canvas.create_text(
            0, 0,
            text="No Image\n\nConnect a device and press EXPOSE",
            fill="#3A3A3A",
            font=("Segoe UI", 16),
            justify="center",
        )

        # ── Post-display toolbar ─────────────────────────────────────────
        self._toolbar_frame = ctk.CTkFrame(right, fg_color="#111827", corner_radius=8)
        self._toolbar_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 4))

        # Row 1: Image info + brightness/contrast sliders
        toolbar_top = ctk.CTkFrame(self._toolbar_frame, fg_color="transparent")
        toolbar_top.pack(fill="x", padx=8, pady=(6, 2))

        self._img_info_label = ctk.CTkLabel(
            toolbar_top, text="No image",
            font=ctk.CTkFont(size=10), text_color="#9E9E9E",
        )
        self._img_info_label.pack(side="left")

        # Contrast slider
        ctk.CTkLabel(toolbar_top, text="Contrast", font=ctk.CTkFont(size=9),
                     text_color="#78909C").pack(side="right", padx=(8, 2))
        self._contrast_var = tk.DoubleVar(value=1.0)
        self._contrast_slider = ctk.CTkSlider(
            toolbar_top, from_=0.3, to=3.0, variable=self._contrast_var,
            width=100, height=14, command=self._on_adjust_display,
        )
        self._contrast_slider.pack(side="right")
        self._contrast_slider.configure(state="disabled")

        # Brightness slider
        ctk.CTkLabel(toolbar_top, text="Brightness", font=ctk.CTkFont(size=9),
                     text_color="#78909C").pack(side="right", padx=(8, 2))
        self._brightness_var = tk.DoubleVar(value=0.0)
        self._brightness_slider = ctk.CTkSlider(
            toolbar_top, from_=-80, to=80, variable=self._brightness_var,
            width=100, height=14, command=self._on_adjust_display,
        )
        self._brightness_slider.pack(side="right")
        self._brightness_slider.configure(state="disabled")

        # Row 2: Action buttons
        toolbar_btns = ctk.CTkFrame(self._toolbar_frame, fg_color="transparent")
        toolbar_btns.pack(fill="x", padx=8, pady=(2, 6))

        self._save_btn = ctk.CTkButton(
            toolbar_btns, text="Save PNG", width=90, height=28,
            font=ctk.CTkFont(size=10),
            fg_color="#1565C0", hover_color="#1976D2",
            command=self._on_save_image, state="disabled",
        )
        self._save_btn.pack(side="left", padx=(0, 4))

        # Hidden ref to avoid crashes on existing .configure() calls
        self._save_raw_btn = ctk.CTkButton(toolbar_btns, text="", width=0, height=0)


        self._open_dcm_btn = ctk.CTkButton(
            toolbar_btns, text="DICOM Folder", width=100, height=28,
            font=ctk.CTkFont(size=10),
            fg_color="#37474F", hover_color="#455A64",
            command=self._on_open_dicom_folder, state="disabled",
        )
        self._open_dcm_btn.pack(side="left", padx=(0, 4))

        self._view_dcm_btn = ctk.CTkButton(
            toolbar_btns, text="View DICOM", width=90, height=28,
            font=ctk.CTkFont(size=10),
            fg_color="#6A1B9A", hover_color="#7B1FA2",
            command=self._on_view_dicom, state="disabled",
        )
        self._view_dcm_btn.pack(side="left", padx=(0, 4))

        # Reset adjustments button
        self._reset_adj_btn = ctk.CTkButton(
            toolbar_btns, text="Reset", width=60, height=28,
            font=ctk.CTkFont(size=10),
            fg_color="#37474F", hover_color="#455A64",
            command=self._on_reset_adjustments, state="disabled",
        )
        self._reset_adj_btn.pack(side="right")

        # New Patient button (visible after scan complete)
        self._new_patient_btn = ctk.CTkButton(
            toolbar_btns, text="New Patient", width=100, height=28,
            font=ctk.CTkFont(size=10, weight="bold"),
            fg_color="#1B5E20", hover_color="#2E7D32",
            command=self._on_new_patient, state="disabled",
        )
        self._new_patient_btn.pack(side="right", padx=(0, 8))

    def _build_status_bar(self) -> None:
        """Bottom status bar with animated HB heart, status, and session info."""
        bar = ctk.CTkFrame(self, height=28, corner_radius=0, fg_color="#1A1A1A")
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        # ── Animated heart canvas ─────────────────────────────────────────
        self._heart_canvas = tk.Canvas(
            bar, width=20, height=20, bg="#1A1A1A",
            highlightthickness=0, bd=0,
        )
        self._heart_canvas.pack(side="left", padx=(12, 2))
        self._heart_item = self._heart_canvas.create_polygon(
            *self._heart_polygon(10, 10, 7),
            fill="#424242", outline="", smooth=True,
        )
        self._heart_bright = "#F44336"    # systole colour
        self._heart_dim = "#880E4F"       # diastole colour
        self._heart_off = "#424242"       # no-connection colour
        self._heart_pulse_id: str | None = None

        # Text-label fallback kept for code that still references it
        self._hb_indicator = ctk.CTkLabel(
            bar, text="", font=ctk.CTkFont(size=1), width=0,
        )

        self._hb_label = ctk.CTkLabel(
            bar,
            text="HB: --",
            font=ctk.CTkFont(family="Consolas", size=10),
            text_color="#616161",
        )
        self._hb_label.pack(side="left")

        self._patient_banner = ctk.CTkLabel(
            bar,
            text="",
            font=ctk.CTkFont(family="Consolas", size=10),
            text_color="#4FC3F7",
        )
        self._patient_banner.pack(side="left", padx=(16, 0))

        self._session_label = ctk.CTkLabel(
            bar,
            text="",
            font=ctk.CTkFont(family="Consolas", size=10),
            text_color="#616161",
        )
        self._session_label.pack(side="right", padx=12)

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  Heart Animation
    # ╚════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _heart_polygon(cx: int, cy: int, r: int) -> list[int]:
        """Return (x, y, ...) coords for a heart shape centred at *cx*, *cy*."""
        import math
        pts: list[int] = []
        for deg in range(0, 360, 10):
            t = math.radians(deg)
            x = r * 16 * math.sin(t) ** 3
            y = -r * (
                13 * math.cos(t) - 5 * math.cos(2 * t)
                - 2 * math.cos(3 * t) - math.cos(4 * t)
            )
            pts.extend([int(cx + x / 17), int(cy + y / 17)])
        return pts

    def _pulse_heart(self) -> None:
        """Single systole-diastole blink cycle (~200 ms)."""
        if self._heart_pulse_id is not None:
            self.after_cancel(self._heart_pulse_id)
        self._heart_canvas.itemconfigure(self._heart_item, fill=self._heart_bright)
        self._heart_pulse_id = self.after(
            120, self._heart_diastole,
        )

    def _heart_diastole(self) -> None:
        """Dim phase of the pulse — called 120 ms after systole."""
        self._heart_canvas.itemconfigure(self._heart_item, fill=self._heart_dim)
        self._heart_pulse_id = None

    def _heart_off_state(self) -> None:
        """Grey-out the heart (no connection)."""
        if self._heart_pulse_id is not None:
            self.after_cancel(self._heart_pulse_id)
            self._heart_pulse_id = None
        self._heart_canvas.itemconfigure(self._heart_item, fill=self._heart_off)

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  Canvas Helpers
    # ╚════════════════════════════════════════════════════════════════════════

    _resize_debounce_id: str | None = None

    def _on_canvas_resize(self, event: tk.Event) -> None:
        """Debounced resize — re-render image after 150ms of no resize events."""
        # Move placeholder text if it still exists
        try:
            self._canvas.coords(
                self._canvas_text_id, event.width // 2, event.height // 2
            )
        except tk.TclError:
            pass  # text was deleted (image is displayed)

        # Debounce: cancel pending re-render, schedule new one
        if self._resize_debounce_id is not None:
            self.after_cancel(self._resize_debounce_id)
        self._resize_debounce_id = self.after(
            150, self._rerender_on_resize
        )

    def _rerender_on_resize(self) -> None:
        """Re-render stored image at new canvas size."""
        self._resize_debounce_id = None
        if self._last_pil_image is not None:
            self._display_pil_image(self._last_pil_image)
        elif self._last_raw_image:
            try:
                self._display_image_bytes(self._last_raw_image, refit=True)
            except Exception:
                pass

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  API Health Check
    # ╚════════════════════════════════════════════════════════════════════════

    def _check_api_health(self) -> None:
        """Background check that the PureXS API is reachable.

        Skipped entirely when `requests` is not installed — the GUI runs
        in direct TCP mode only (Monitor HB + direct EXPOSE).
        """
        if not HAS_REQUESTS or self.api.session is None:
            # No API server path — show as N/A, don't poll
            self._api_dot.configure(text_color="#616161")
            self._api_label.configure(
                text="API: N/A (direct TCP)", text_color="#757575"
            )
            return

        def _check():
            try:
                h = self.api.health()
                devices = h.get("devices_known", 0)
                self.after(0, self._update_api_status, True, devices)
            except Exception as exc:
                self.after(0, self._update_api_status, False, 0)
                log.debug("API health check: %s", exc)

        threading.Thread(target=_check, daemon=True).start()

    def _update_api_status(self, online: bool, device_count: int) -> None:
        if online:
            self._api_dot.configure(text_color="#4CAF50")
            self._api_label.configure(
                text=f"API: online ({device_count} dev)",
                text_color="#81C784",
            )
        else:
            self._api_dot.configure(text_color="#424242")
            self._api_label.configure(
                text="API: offline", text_color="#616161"
            )
        # Re-check every 30 seconds (non-critical)
        self.after(30_000, self._check_api_health)

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  Scan / Discovery
    # ╚════════════════════════════════════════════════════════════════════════

    def _on_scan(self) -> None:
        """Trigger a UDP discovery scan in a background thread."""
        pass  # scan btn removed
        self._log("Scanning for P2K devices...", "info")
        self._progress.start()

        def _do_scan():
            try:
                devices = self.api.scan(timeout=SCAN_TIMEOUT_S)
                self.after(0, self._on_scan_done, devices, None)
            except Exception as exc:
                self.after(0, self._on_scan_done, [], exc)

        threading.Thread(target=_do_scan, daemon=True).start()

    def _on_scan_done(
        self, devices: list[dict], error: Exception | None
    ) -> None:
        self._progress.stop()
        self._progress.set(0)
        pass  # scan btn removed

        if error:
            self._log(f"Scan failed: {error}", "error")
            Toast(self, f"Scan failed: {error}", level="error")
            return

        if not devices:
            self._log("No devices found", "warning")
            Toast(self, "No P2K devices found on network", level="warning")
            return

        # Populate combo box
        macs = []
        for d in devices:
            mac = d.get("mac", "")
            name = d.get("device_type_name", "Unknown")
            ip = d.get("ip", "?")
            entry = f"{mac}  ({name} @ {ip})"
            macs.append(entry)
            self._log(
                f"Found: {mac}  {name}  IP={ip}  type=0x{d.get('device_type', 0):04X}",
                "info",
            )

        pass  # mac combo removed
        if macs:
            self._mac_var.set(macs[0])
        Toast(
            self,
            f"Found {len(devices)} device(s)",
            level="success",
        )

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  Connect / Disconnect
    # ╚════════════════════════════════════════════════════════════════════════

    def _get_selected_mac(self) -> str:
        """Extract raw MAC from combo box selection."""
        raw = self._mac_var.get().strip()
        if not raw or raw.startswith("("):
            return ""
        # Format: "AA:BB:CC:DD:EE:FF  (ORTHOPHOS XG @ 192.168.1.50)"
        return raw.split()[0].strip()

    def _on_connect(self) -> None:
        mac = self._get_selected_mac()
        if not mac:
            Toast(self, "Select a device first (press Scan)", level="warning")
            return

        pass  # connect btn removed
        self._log(f"Connecting to {mac}...", "info")
        self._progress.start()
        self._set_status("CONNECTING", "#FFA726")

        def _do():
            try:
                result = self.api.connect(mac)
                self.after(0, self._on_connect_done, mac, result, None)
            except Exception as exc:
                self.after(0, self._on_connect_done, mac, {}, exc)

        threading.Thread(target=_do, daemon=True).start()

    def _on_connect_done(
        self, mac: str, result: dict, error: Exception | None
    ) -> None:
        self._progress.stop()
        self._progress.set(0)

        if error:
            pass  # connect btn removed
            self._set_status("ERROR", "#F44336")
            self._log(f"Connect failed: {error}", "error")
            Toast(self, f"Connection failed: {error}", level="error")
            return

        self._mac = mac
        self._connected = True

        # Update UI
        state = result.get("state", "CONNECTED")
        self._set_status(state, "#4CAF50")
        self._device_info_label.configure(
            text=f"{result.get('device_type_name', '?')}  |  {result.get('ip', '?')}:{result.get('tcp_port', 1999)}",
            text_color="#B0BEC5",
        )

        pass  # connect btn removed
        pass  # disconnect btn removed
        self._session_label.configure(text=f"MAC: {mac}")
        self._update_expose_eligibility()

        self._log(f"Connected to {mac} — state={state}", "info")
        Toast(self, f"Connected to {mac}", level="success")

        # Start heartbeat monitor thread
        self._start_hb_monitor()

    def _on_disconnect(self) -> None:
        if not self._connected:
            return

        self._stop_hb_monitor()

        mac = self._mac

        def _do():
            try:
                self.api.disconnect(mac)
            except Exception as exc:
                log.warning("Disconnect error (non-fatal): %s", exc)
            self.after(0, self._on_disconnect_done, mac)

        threading.Thread(target=_do, daemon=True).start()

    def _on_disconnect_done(self, mac: str) -> None:
        self._connected = False
        self._mac = ""
        self._set_status("DISCONNECTED", "#9E9E9E")
        self._device_info_label.configure(
            text="No device connected", text_color="#757575"
        )
        pass  # connect btn removed
        pass  # disconnect btn removed
        self._hb_label.configure(text="HB: --", text_color="#616161")
        self._heart_off_state()
        self._session_label.configure(text="")
        self._update_expose_eligibility()
        self._log(f"Disconnected from {mac}", "info")

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  Heartbeat Monitor Thread
    # ╚════════════════════════════════════════════════════════════════════════

    def _start_hb_monitor(self) -> None:
        """Start background thread that polls /status every HB_POLL_INTERVAL_S."""
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(
            target=self._hb_loop, name="hb-monitor", daemon=True
        )
        self._hb_thread.start()
        self._log("HB monitor started", "info")

    def _stop_hb_monitor(self) -> None:
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=2.0)
            self._hb_thread = None

    def _hb_loop(self) -> None:
        """Poll device status in a loop. Runs in daemon thread."""
        seq = 0
        while not self._hb_stop.is_set():
            mac = self._mac
            if not mac:
                break

            t0 = time.perf_counter()
            try:
                result = self.api.status(mac)
                rtt_ms = (time.perf_counter() - t0) * 1000
                status_code = result.get("status_code", -1)
                seq += 1

                # Map status code to human string
                status_map = {
                    0x0000: "READY",
                    0x0001: "BUSY",
                    0x0002: "ERROR",
                    0x0003: "WARMUP",
                }
                status_str = status_map.get(status_code, f"0x{status_code:04X}")

                self.after(
                    0,
                    self._update_hb,
                    True,
                    seq,
                    rtt_ms,
                    status_str,
                    status_code,
                )

            except Exception as exc:
                # requests.HTTPError 409 = not connected (expected after scan)
                resp = getattr(exc, "response", None)
                if resp is not None and getattr(resp, "status_code", 0) == 409:
                    self.after(0, self._update_hb, True, seq, 0.0, "NOT_CONN", -1)
                else:
                    self.after(0, self._update_hb, False, seq, 0.0, "OFFLINE", -1)

            self._hb_stop.wait(HB_POLL_INTERVAL_S)

    def _update_hb(
        self,
        alive: bool,
        seq: int,
        rtt_ms: float,
        status_str: str,
        status_code: int,
    ) -> None:
        """Update HB indicator on the main thread (API path)."""
        prev_status = self._last_status
        if alive:
            self._pulse_heart()
            self._hb_label.configure(
                text=f"HB: seq={seq}  {rtt_ms:.0f}ms  [{status_str}]",
                text_color="#81C784",
            )

            if status_code == 0x0000 and self._connected:
                self._set_status("READY", "#4CAF50",
                                 phase="Device idle \u2014 ready for exposure")
            elif status_code == 0x0001:
                self._set_status("BUSY", "#FFA726",
                                 phase="Acquisition in progress")
            elif status_code == 0x0003:
                self._set_status("WARMUP", "#FFC107",
                                 phase="Gantry positioning \u2014 align lasers")
            elif status_code == 0x0002:
                self._set_status("ERROR", "#F44336",
                                 phase="Device error \u2014 check unit")
        else:
            self._heart_off_state()
            self._hb_label.configure(
                text=f"HB: seq={seq}  TIMEOUT", text_color="#EF5350"
            )

        # Re-evaluate expose eligibility when API-path status changes
        if self._last_status != prev_status:
            self._update_expose_eligibility()

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  Expose / Acquire
    # ╚════════════════════════════════════════════════════════════════════════

    def _on_expose(self) -> None:
        """Trigger an X-ray exposure via PureXS API. Thread-safe.

        This is the API-server path (requires `requests` + running server).
        For direct TCP expose, use the direct EXPOSE button instead.
        """
        if not HAS_REQUESTS or self.api.session is None:
            Toast(self, "API mode requires `requests` library", level="warning")
            return
        if not self._connected or not self._mac:
            return
        if self._last_status not in ("READY", "CONNECTED"):
            Toast(
                self,
                "Rotate gantry to patient position "
                "(R/Return, lasers align). Wait READY green + HB pulse.",
                level="warning",
                duration_ms=6000,
            )
            self._log(
                f"EXPOSE blocked: device not READY (status={self._last_status})",
                "warning",
            )
            return
        if not self._acquire_lock.acquire(blocking=False):
            Toast(self, "Acquisition already in progress", level="warning")
            return

        self._expose_btn.configure(state="disabled", fg_color="#5D4037")
        self._progress.start()
        self._set_status("EXPOSING", "#FF6F00")
        self._log("=" * 50, "info")
        self._log("EXPOSE triggered", "info")

        mac = self._mac
        program_name = self._program_var.get()
        patient_name = self._patient_var.get()
        kv = int(self._kv_var.get())
        ma_tenths = int(float(self._ma_var.get()) * 10)

        def _do():
            try:
                # 1. Set exposure parameters
                self.after(0, self._log, f"Setting kV={kv}", "info")
                self.api.set_param_word(mac, PARAM_KV, kv)

                self.after(0, self._log, f"Setting mA={ma_tenths/10:.1f}", "info")
                self.api.set_param_word(mac, PARAM_MA, ma_tenths)

                program_code = PROGRAMS.get(program_name, 0x01)
                self.after(
                    0,
                    self._log,
                    f"Setting program={program_name} (0x{program_code:02X})",
                    "info",
                )
                self.api.set_param_word(mac, PARAM_PROGRAM, program_code)

                patient_code = PATIENT_SIZES.get(patient_name, 0x02)
                self.after(
                    0,
                    self._log,
                    f"Setting patient={patient_name} (0x{patient_code:02X})",
                    "info",
                )
                self.api.set_param_word(mac, PARAM_PATIENT_SIZE, patient_code)

                # 2. Trigger acquisition
                self.after(0, self._log, "Sending EXPOSE trigger...", "info")
                self.after(0, self._set_status, "SCANNING", "#FF6F00")
                t0 = time.perf_counter()

                result = self.api.acquire(mac)

                elapsed = time.perf_counter() - t0
                self.after(0, self._on_acquire_done, result, elapsed, None)

            except Exception as exc:
                self.after(0, self._on_acquire_done, {}, 0.0, exc)

        threading.Thread(target=_do, name="expose-worker", daemon=True).start()

    def _on_acquire_done(
        self, result: dict, elapsed: float, error: Exception | None
    ) -> None:
        """Handle acquisition result on the main thread."""
        self._acquire_lock.release()
        self._progress.stop()
        self._progress.set(0)

        self._update_expose_eligibility()

        if error:
            # Handle E7 14 02 as success (Sirona quirk: device drops + image is valid)
            err_str = str(error)
            if "E7" in err_str or "0x00E7" in err_str or "502" in err_str:
                self._log(
                    "Device reported E7 (post-scan disconnect) — treating as SUCCESS",
                    "warning",
                )
                Toast(
                    self,
                    "Scan complete (E7 handled as success)",
                    level="success",
                    duration_ms=4000,
                )
                self._set_status("READY", "#4CAF50")
            else:
                self._set_status("ERROR", "#F44336")
                self._log(f"Acquire FAILED: {error}", "error")
                Toast(self, f"Acquisition failed: {error}", level="error")
            return

        # ── Success ──────────────────────────────────────────────────────
        size = result.get("size_bytes", 0)
        b64_data = result.get("data_b64", "")

        self._expose_count += 1
        self._expose_count_label.configure(
            text=f"Exposures this session: {self._expose_count}"
        )
        self._set_status("READY", "#4CAF50")
        self._log(
            f"Image received: {size:,} bytes in {elapsed:.1f}s",
            "info",
        )
        Toast(
            self,
            f"Image acquired ({size:,} bytes, {elapsed:.1f}s)",
            level="success",
        )

        # ── Decode and display ───────────────────────────────────────────
        if b64_data:
            try:
                raw = base64.b64decode(b64_data)
                self._last_raw_image = raw
                self._display_image_bytes(raw)
                self._save_btn.configure(state="normal")
                self._save_raw_btn.configure(state="normal")
                self._log(
                    f"Image decoded and displayed ({len(raw):,} raw bytes)",
                    "info",
                )
            except Exception as exc:
                self._log(f"Image decode error: {exc}", "error")
                Toast(self, f"Image decode failed: {exc}", level="error")

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  Image Display
    # ╚════════════════════════════════════════════════════════════════════════

    def _display_image_bytes(
        self, raw: bytes, refit: bool = False
    ) -> None:
        """Decode raw image bytes and display on canvas.

        Attempts to interpret as:
          1. Standard image format (JPEG, PNG, TIFF) via Pillow
          2. Raw 16-bit grayscale (2 bytes/pixel, big-endian)
        """
        img: Image.Image | None = None

        # Try standard image formats first
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception:
            img = None

        # Fallback: interpret as raw 16-bit grayscale
        if img is None and len(raw) >= 1000:
            img = self._decode_raw_16bit(raw)

        if img is None:
            self._log("Could not decode image data", "warning")
            return

        # Convert to 8-bit grayscale for display
        if img.mode == "I;16" or img.mode == "I;16B":
            # Normalize 16-bit to 8-bit
            import numpy as np
            arr = np.array(img, dtype=np.float32)
            if arr.max() > 0:
                arr = (arr / arr.max() * 255).astype(np.uint8)
            else:
                arr = arr.astype(np.uint8)
            img = Image.fromarray(arr, mode="L")
        elif img.mode not in ("L", "RGB", "RGBA"):
            img = img.convert("L")

        # Scale to fit canvas
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw < 10 or ch < 10:
            cw, ch = 600, 400

        img_w, img_h = img.size
        scale = min(cw / img_w, ch / img_h, 1.0)
        new_w = max(int(img_w * scale), 1)
        new_h = max(int(img_h * scale), 1)
        display_img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Render to canvas
        self._photo_image = ImageTk.PhotoImage(display_img)
        self._canvas.delete("all")
        self._canvas.create_image(
            cw // 2, ch // 2, image=self._photo_image, anchor="center"
        )

        # Update info
        self._img_info_label.configure(
            text=f"{img_w}\u00D7{img_h}  {img.mode}  |  {len(raw):,} bytes  |  Exposure #{self._expose_count}"
        )

    def _decode_raw_16bit(self, raw: bytes) -> Image.Image | None:
        """Attempt to decode raw bytes as 16-bit big-endian grayscale.

        Tries common ORTHOPHOS XG panoramic dimensions:
          - 2440 x 1292 (standard pan mode, 127 µm pitch)
          - 2440 x 1280
          - 2048 x 1024
        Falls back to a square-ish guess.
        """
        nbytes = len(raw)
        npixels = nbytes // 2

        if npixels == 0:
            return None

        # Common known sizes
        known_dims = [
            (2440, 1292),
            (2440, 1280),
            (2048, 1024),
            (1944, 1380),  # HELIODENT DS
            (3072, 2048),
        ]

        width, height = 0, 0
        for w, h in known_dims:
            if w * h == npixels:
                width, height = w, h
                break

        if width == 0:
            # Guess: find the closest 4:3 or 2:1 aspect
            import math
            sqrt = int(math.isqrt(npixels))
            # Try to find a width that divides evenly
            for w in range(sqrt + 100, sqrt - 100, -1):
                if w > 0 and npixels % w == 0:
                    width = w
                    height = npixels // w
                    if 0.3 < height / width < 3.0:
                        break
            if width == 0:
                width = sqrt or 1
                height = npixels // width if width else 1

        if width * height * 2 > nbytes:
            # Not enough data
            actual_pixels = nbytes // 2
            width = min(width, actual_pixels)
            height = actual_pixels // width if width else 1

        try:
            # Unpack as big-endian unsigned 16-bit
            pixel_count = width * height
            pixels = struct.unpack(f">{pixel_count}H", raw[: pixel_count * 2])
            img = Image.new("I;16", (width, height))
            img.putdata(pixels)  # type: ignore[arg-type]
            self._log(f"Raw 16-bit decode: {width}\u00D7{height}", "info")
            return img
        except Exception as exc:
            self._log(f"Raw 16-bit decode failed: {exc}", "warning")
            return None

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  Save Image
    # ╚════════════════════════════════════════════════════════════════════════

    def _on_save_image(self) -> None:
        """Save the currently displayed image as PNG."""
        if self._photo_image is None:
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"purexs_scan_{ts}.png"

        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save Image",
            defaultextension=".png",
            initialfile=default_name,
            filetypes=[
                ("PNG Image", "*.png"),
                ("JPEG Image", "*.jpg"),
                ("TIFF Image", "*.tif"),
                ("All Files", "*.*"),
            ],
        )
        if not path:
            return

        try:
            # Re-decode from raw to get full resolution
            if self._last_raw_image:
                img = None
                try:
                    img = Image.open(io.BytesIO(self._last_raw_image))
                    img.load()
                except Exception:
                    img = self._decode_raw_16bit(self._last_raw_image)

                if img is not None:
                    # Convert 16-bit to 8-bit for PNG
                    if img.mode in ("I;16", "I;16B"):
                        import numpy as np
                        arr = np.array(img, dtype=np.float32)
                        if arr.max() > 0:
                            arr = (arr / arr.max() * 255).astype(np.uint8)
                        else:
                            arr = arr.astype(np.uint8)
                        img = Image.fromarray(arr, mode="L")
                    img.save(path)
                    self._log(f"Image saved: {path}", "info")
                    Toast(self, f"Saved: {Path(path).name}", level="success")
                    return

            Toast(self, "No image data to save", level="warning")
        except Exception as exc:
            self._log(f"Save failed: {exc}", "error")
            Toast(self, f"Save failed: {exc}", level="error")

    def _on_save_raw(self) -> None:
        """Save raw image bytes to a .bin file for external processing."""
        if not self._last_raw_image:
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"purexs_raw_{ts}.bin"

        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save Raw Image Data",
            defaultextension=".bin",
            initialfile=default_name,
            filetypes=[
                ("Binary Data", "*.bin"),
                ("All Files", "*.*"),
            ],
        )
        if not path:
            return

        try:
            with open(path, "wb") as f:
                f.write(self._last_raw_image)
            self._log(
                f"Raw data saved: {path} ({len(self._last_raw_image):,} bytes)",
                "info",
            )
            Toast(self, f"Raw saved: {Path(path).name}", level="success")
        except Exception as exc:
            self._log(f"Raw save failed: {exc}", "error")
            Toast(self, f"Save failed: {exc}", level="error")

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  Direct TCP HB Monitor (raw Sirona wire protocol)
    # ╚════════════════════════════════════════════════════════════════════════

    def _on_toggle_hb_monitor(self) -> None:
        """Toggle the direct TCP heartbeat monitor on/off."""
        if not HAS_HB_DECODER:
            Toast(self, "hb_decoder module not found", level="error")
            return

        if self._direct_connected:
            self._stop_hb_monitor_direct()
        else:
            self._start_hb_monitor_direct()

    def _discover_sirona_tcp(self) -> str:
        """Scan the local subnet for a Sirona device on port 12837 (or 1999).

        Runs on a background thread.  Returns the IP address of the first
        device that accepts a TCP connection, or empty string if none found.
        """
        import socket as _sock
        import concurrent.futures

        # Determine local subnet from this machine's IP
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = "192.168.1.1"

        # Build subnet prefix (e.g. "192.168.139.")
        parts = local_ip.split(".")
        subnet = ".".join(parts[:3]) + "."

        self.after(
            0, self._log,
            f"TCP discovery: scanning {subnet}1-254 on port {SIRONA_PORT}...",
            "info",
        )

        ports_to_try = [SIRONA_PORT]
        if SIRONA_PORT != 1999:
            ports_to_try.append(1999)

        def _try_connect(ip: str) -> str | None:
            for port in ports_to_try:
                try:
                    s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                    s.settimeout(0.3)
                    s.connect((ip, port))
                    s.close()
                    return ip
                except (_sock.timeout, OSError):
                    pass
            return None

        # Parallel scan — 50 threads, fast timeout
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            futures = {
                pool.submit(_try_connect, f"{subnet}{i}"): i
                for i in range(1, 255)
                if f"{subnet}{i}" != local_ip
            }
            for future in concurrent.futures.as_completed(futures, timeout=15):
                result = future.result()
                if result:
                    self.after(
                        0, self._log,
                        f"TCP discovery: found Sirona at {result}:{SIRONA_PORT}",
                        "info",
                    )
                    # Cancel remaining futures
                    for f in futures:
                        f.cancel()
                    return result

        return ""

    def _start_hb_monitor_direct(self) -> None:
        """Auto-discover Sirona device on network, then connect via TCP."""
        self._hb_monitor_btn.configure(state="disabled", text="Discovering...")
        self._log("Direct TCP: scanning network for Sirona devices...", "info")

        def _do():
            # ── Step 1: Auto-discover Sirona on the local network ────────
            sirona_host = os.environ.get("SIRONA_IP", "")
            sirona_port = int(os.environ.get("SIRONA_PORT", "0"))

            if not sirona_host:
                found_ip = self._discover_sirona_tcp()
                if found_ip:
                    sirona_host = found_ip
                    sirona_port = sirona_port or SIRONA_PORT

            # Fall back to hardcoded defaults if discovery didn't find anything
            if not sirona_host:
                sirona_host = SIRONA_IP
                self.after(
                    0, self._log,
                    f"Discovery: no Sirona found, using default {SIRONA_IP}:{SIRONA_PORT}",
                    "warning",
                )
            if not sirona_port:
                sirona_port = SIRONA_PORT

            # ── Step 2: Connect via TCP ──────────────────────────────────
            self.after(0, lambda: self._hb_monitor_btn.configure(
                state="disabled", text="Connecting..."))
            self.after(
                0, self._log,
                f"Direct TCP: connecting to {sirona_host}:{sirona_port}...",
                "info",
            )

            try:
                client = SironaLiveClient(
                    host=sirona_host, port=sirona_port,
                    hb_interval=0.9, timeout=10.0,
                )
                # Wire callbacks (all fire on the HB thread → use self.after)
                client.on_hb.append(
                    lambda seq, rtt: self.after(
                        0, self._on_direct_hb, seq, rtt
                    )
                )
                client.on_status.append(
                    lambda s: self.after(0, self._log, f"Direct: {s}", "info")
                )
                client.on_device_status.append(
                    lambda code: self.after(
                        0, self._on_direct_device_status, code
                    )
                )
                client.on_event.append(
                    lambda e: self.after(0, self._on_direct_event, e)
                )
                client.on_kv_sample.append(
                    lambda s: self.after(0, self._on_direct_kv, s)
                )
                client.on_scanline.append(
                    lambda sl: self.after(0, self._on_direct_scanline, sl)
                )
                client.on_error.append(
                    lambda e: self.after(
                        0, self._log, f"Direct ERR: {e}", "error"
                    )
                )

                client.connect()
                client.start_hb_loop()
                self._sirona_client = client
                self.after(0, self._on_direct_connected, sirona_host, sirona_port)
            except Exception as exc:
                self.after(0, self._on_direct_connect_failed, exc)

        threading.Thread(target=_do, name="direct-connect", daemon=True).start()

    def _on_direct_connected(self, host: str = "", port: int = 0) -> None:
        self._direct_connected = True
        self._device_ready = True
        self._update_expose_eligibility()
        self._hb_monitor_btn.configure(
            text="Disconnect",
            fg_color="#880E4F",
            hover_color="#AD1457",
            state="normal",
        )
        # Re-evaluate expose button (needs HB active + patient set)
        self._update_expose_eligibility()
        self._set_status("Connected", "#4CAF50")
        self._device_info_label.configure(
            text=f"Sirona @ {host}:{port}" if host else "Direct TCP connected",
            text_color="#81D4FA",
        )
        self._log("Direct TCP: HB monitor active (0.9s interval)", "info")
        Toast(self, "Connected", level="success", duration_ms=2000)

    def _on_direct_connect_failed(self, exc: Exception) -> None:
        self._hb_monitor_btn.configure(
            text="Connect to Device", state="normal"
        )
        self._log(f"Direct TCP connect failed: {exc}", "error")
        Toast(self, f"Direct connect failed: {exc}", level="error")

    def _stop_hb_monitor_direct(self) -> None:
        if self._sirona_client:
            self._sirona_client.disconnect()
            self._sirona_client = None
        self._direct_connected = False
        self._device_ready = False
        self._exposing = False
        self._hb_monitor_btn.configure(
            text="Connect to Device",
            fg_color="#4A148C",
            hover_color="#6A1B9A",
            state="normal",
        )
        self._kv_progress.set(0)
        self._kv_value_label.configure(text="kV: --", text_color="#546E7A")
        self._heart_off_state()
        self._hb_label.configure(text="HB: --", text_color="#616161")
        self._set_status("OFFLINE", "#616161")
        # Re-evaluate expose (HB now off → disabled)
        self._update_expose_eligibility()
        self._log("Direct TCP: HB monitor stopped", "info")

    def _on_direct_hb(self, seq: int, rtt_ms: float) -> None:
        """Update status bar with direct HB data — animate the canvas heart."""
        self._pulse_heart()
        status_tag = "EXPOSING" if self._exposing else "DIRECT"
        self._hb_label.configure(
            text=f"HB: seq={seq}  {rtt_ms:.0f}ms  [{status_tag}]",
            text_color="#F48FB1",
        )

    def _on_direct_device_status(self, status_code: int) -> None:
        """Handle device status update from periodic status poll."""
        _names = {0x0000: "READY", 0x0001: "BUSY", 0x0002: "ERROR", 0x0003: "WARMUP"}
        self._log(
            f"status_code: 0x{status_code:04X} ({_names.get(status_code, '?')})",
            "debug",
        )
        was_ready = self._device_ready
        self._device_ready = status_code == 0x0000

        status_map = {
            0x0000: ("READY", "#4CAF50"),
            0x0001: ("BUSY", "#FFA726"),
            0x0002: ("ERROR", "#F44336"),
            0x0003: ("WARMUP", "#FFC107"),
        }
        label, color = status_map.get(status_code, (f"0x{status_code:04X}", "#9E9E9E"))

        # Only update status label when not mid-expose (expose owns the label)
        if not self._exposing:
            if status_code == 0x0003:
                self._set_status("WARMUP", "#FFC107",
                                 phase="Gantry positioning \u2014 align lasers")
            elif status_code == 0x0001:
                self._set_status("BUSY", "#FFA726",
                                 phase="Acquisition in progress")
            elif status_code == 0x0000:
                self._set_status("READY", "#4CAF50",
                                 phase="Device idle \u2014 ready for exposure")
            elif status_code == 0x0002:
                self._set_status("ERROR", "#F44336",
                                 phase="Device error \u2014 check unit")

        # Always re-evaluate after every status poll
        self._update_expose_eligibility()

        if was_ready != self._device_ready:
            if not self._device_ready and not self._exposing:
                self._log(
                    f"Device not ready (status={label}) \u2014 EXPOSE blocked",
                    "warning",
                )
            elif self._device_ready and not self._exposing:
                self._log("Device READY \u2014 EXPOSE enabled", "info")

    def _on_direct_kv(self, sample: object) -> None:
        """Handle a kV ramp sample — update the gauge and detect trigger."""
        s = sample  # KVSample
        # kV_raw is big-endian uint16; from ff.txt 0x02BC = 700 → 70.0 kV
        kv_display = s.kv_raw / 10.0
        self._kv_value_label.configure(
            text=f"kV: {kv_display:.1f}",
            text_color="#FF6F00" if self._exposing else "#81C784",
        )
        progress = min(kv_display / KV_MAX_DISPLAY, 1.0)
        self._kv_progress.set(progress)

        # Cancel the 2 s no-response watchdog — device is responding
        if not self._got_kv_or_scanline and self._exposing:
            self._got_kv_or_scanline = True
            if self._no_response_timer_id is not None:
                self.after_cancel(self._no_response_timer_id)
                self._no_response_timer_id = None

        # Toast on first kV sample of this expose (ramp detected)
        if self._exposing and self._expose_kv_peak == 0.0 and kv_display > 0:
            Toast(self, f"kV ramp detected \u2014 tube energising",
                  level="info", duration_ms=2000)
            self._log("kV ramp started", "info")

        if kv_display > self._expose_kv_peak:
            self._expose_kv_peak = kv_display

        # Update phase label during kV ramp (phase 1: pre-exposure)
        if self._exposing and not s.is_expose_trigger:
            self._phase_label.configure(
                text=f"Phase 1: pre-exposure \u2014 kV ramp ({kv_display:.0f} kV)",
                text_color="#FFD54F",
            )

        if s.is_expose_trigger and self._exposing:
            self._log(
                f"kV TRIGGER pos={s.position} ramp=0x{s.field3:04X} "
                f"(peak {self._expose_kv_peak:.1f} kV)",
                "warning",
            )
            Toast(self,
                  f"kV trigger \u2014 {self._expose_kv_peak:.0f} kV peak, X-ray active",
                  level="warning", duration_ms=2500)

    def _on_direct_scanline(self, sl: object) -> None:
        """Handle a live scanline — append to preview and update canvas."""
        # Cancel the 2 s no-response watchdog — device is responding
        if not self._got_kv_or_scanline and self._exposing:
            self._got_kv_or_scanline = True
            if self._no_response_timer_id is not None:
                self.after_cancel(self._no_response_timer_id)
                self._no_response_timer_id = None

        self._expose_scanlines.append(sl)
        count = len(self._expose_scanlines)
        self._scanline_count_label.configure(text=f"{count} lines")
        self._log(
            f"Scanline 0x{sl.scanline_id:02X}: {sl.pixel_count} px "
            f"({count}/13)",
            "info",
        )

        # Toast on first scanline — data is arriving
        if count == 1:
            Toast(self,
                  f"Scanline data arriving (ID 0x{sl.scanline_id:02X}, "
                  f"{sl.pixel_count} px)",
                  level="info", duration_ms=2000)

        # Live-render the growing panoramic strip on the preview canvas
        self._render_scanline_preview()

    def _on_direct_event(self, event_str: str) -> None:
        """Handle events from the live client during expose."""
        self._log(f"Direct: {event_str}", "info")
        ev_lower = event_str.lower()

        # EXPOSE_STARTED — physical button pressed, data incoming
        if "expose_started" in ev_lower:
            if self._exposing:
                self._got_kv_or_scanline = True
                self._set_status(
                    "\u2622 EXPOSING \u2014 receiving data", "#FF6F00",
                    phase="Phase 2: exposure \u2014 X-ray active",
                )
                Toast(self, "Expose button pressed \u2014 data incoming!",
                      level="info", duration_ms=2000)
                # NOW start the hard timeout (data is flowing)
                if self._expose_timer_id:
                    self.after_cancel(self._expose_timer_id)
                self._expose_timer_id = self.after(
                    int(EXPOSE_TIMEOUT_S * 1000), self._on_expose_timeout
                )

        # Live gantry phase updates on the phase label
        if "recording_start" in ev_lower:
            self._phase_label.configure(
                text="Phase 2: exposure \u2014 X-ray active",
                text_color="#FF8A65",
            )
        elif "recording_stop" in ev_lower:
            self._phase_label.configure(
                text="Phase 3: readout \u2014 data transfer",
                text_color="#B0BEC5",
            )
        elif "imagetransfer_start" in ev_lower:
            self._phase_label.configure(
                text="Phase 3: image transfer in progress",
                text_color="#B0BEC5",
            )
        elif "imagetransfer_stop" in ev_lower:
            self._phase_label.configure(
                text="Phase 3: image transfer complete",
                text_color="#81C784",
            )

        # Detect SCAN_COMPLETE from the new _recv_scan_data path
        if "scan_complete" in ev_lower:
            if self._exposing:
                # Pull all scanlines from the client's batch buffer
                if self._sirona_client:
                    batch = getattr(self._sirona_client, '_scan_scanlines', [])
                    if batch:
                        self._expose_scanlines = list(batch)
                        self._sirona_client._scan_scanlines = []
                sl_count = len(self._expose_scanlines)
                elapsed = time.perf_counter() - self._expose_start_time
                self._log(
                    f"Scan complete \u2014 {sl_count} scanlines, "
                    f"peak {self._expose_kv_peak:.1f} kV, {elapsed:.1f}s",
                    "info",
                )
                Toast(self,
                      f"Scan complete \u2014 {sl_count} scanlines captured",
                      level="success", duration_ms=3000)
                self._scanline_count_label.configure(text=f"{sl_count} lines")
                self._render_scanline_preview()
                self._on_expose_complete()
                return  # don't process other events

        # Detect "Released" → scan complete
        if "state_released" in ev_lower or "released" in ev_lower:
            if self._exposing:
                self._on_expose_complete()

        # Detect E7 14 02 → treat as post-scan success (Released equivalent)
        if "e7_error" in ev_lower or "E7 14 02" in event_str:
            if self._exposing:
                self._on_expose_complete()

    # ── Direct TCP expose flow ───────────────────────────────────────────

    def _on_direct_expose(self) -> None:
        """User clicked the direct EXPOSE button."""
        if not self._direct_connected or not self._sirona_client:
            Toast(self, "HB monitor not active", level="warning")
            return
        if not self._patient.get("set"):
            Toast(self, "Set patient before expose", level="warning")
            return
        if self._exposing:
            Toast(self, "Expose already in progress", level="warning")
            return
        if not self._device_ready:
            Toast(
                self,
                "Rotate gantry to patient position "
                "(R/Return, lasers align). Wait READY green + HB pulse.",
                level="warning",
                duration_ms=6000,
            )
            self._log(
                "EXPOSE blocked: device not READY "
                f"(status=0x{self._sirona_client.device_status_code:04X})",
                "warning",
            )
            return

        p = self._patient
        exam = p.get("exam", "Panoramic")
        # Confirmation dialog
        confirm = messagebox.askyesno(
            "Confirm Expose",
            f"Arm device for {exam} expose?\n\n"
            f"Patient: {p['last']}, {p['first']}\n"
            f"DOB: {p['dob']}  |  ID: {p['id']}\n\n"
            "After clicking Yes:\n"
            "  1. Press R on the Orthophos keypad (gantry to patient position)\n"
            "  2. Verify laser crosshairs are visible and aligned\n"
            "  3. Press the physical EXPOSE button on the unit\n\n"
            "The unit will not fire until the gantry is in position.",
            icon="warning",
            parent=self,
        )
        if not confirm:
            self._log("Expose cancelled by user", "info")
            return

        self._start_direct_expose()

    def _start_direct_expose(self) -> None:
        """Fire the expose trigger and set up timeout watchdog."""
        self._exposing = True
        self._expose_scanlines = []
        self._expose_kv_peak = 0.0
        self._expose_start_time = time.perf_counter()
        self._got_kv_or_scanline = False

        # Update UI
        self._direct_expose_btn.configure(
            state="disabled",
            fg_color="#5D4037",
            text="\u2622 EXPOSING...",
        )
        self._set_status("\u2622 EXPOSING \u2014 kV ramping", "#FF6F00",
                         phase="Phase 1: pre-exposure \u2014 kV ramp")
        self._progress.start()
        # Show kV gauge and scanline preview during expose
        self._kv_frame.pack(fill="x", padx=8, pady=4, before=self._log_frame)
        self._scan_preview_frame.pack(fill="both", expand=True, padx=8, pady=4, before=self._log_frame)
        self._scanline_canvas.delete("all")
        self._scanline_count_label.configure(text="0 lines")
        self._save_pano_btn.configure(state="disabled")

        self._log("=" * 50, "info")
        self._log("DIRECT EXPOSE — arming device (CAPS + patient data)", "warning")

        # Arm device on background thread to avoid blocking GUI
        p = self._patient
        def _do():
            try:
                self._sirona_client.arm_for_expose(
                    last_name=p.get("last", "test"),
                    first_name=p.get("first", "test"),
                    doctor="Dr. Demo",
                    workstation="PUREXS",
                )
                self.after(0, self._on_trigger_sent)
            except Exception as exc:
                self.after(0, self._on_expose_error, exc)

        threading.Thread(target=_do, name="expose-arm", daemon=True).start()

        # NOTE: timeout watchdog is NOT started here — waiting for
        # the physical button can take minutes.  The timeout starts
        # when EXPOSE_STARTED is received (data is flowing).

    def _on_trigger_sent(self) -> None:
        """Device armed — CAPS + patient data accepted."""
        self._log(
            "Device ARMED \u2014 press R (if not positioned), then press EXPOSE on unit",
            "warning",
        )
        self._set_status(
            "\u2622 ARMED \u2014 1) R on keypad  2) EXPOSE button on unit",
            "#FF6F00",
            phase="Position gantry (R), then press physical EXPOSE button",
        )
        Toast(self,
              "Device armed!\n"
              "1. Press R on keypad (position gantry)\n"
              "2. Press EXPOSE button on the unit",
              level="warning", duration_ms=8000)

        # The no-response watchdog is less relevant now (waiting for
        # physical button which could take minutes), but keep a generous
        # timeout for the entire expose cycle
        self._got_kv_or_scanline = False
        if self._no_response_timer_id is not None:
            self.after_cancel(self._no_response_timer_id)
        self._no_response_timer_id = None  # don't start 2s watchdog

    def _on_no_response(self) -> None:
        """Watchdog: no kV ramp or scanlines received after arming."""
        self._no_response_timer_id = None
        if not self._exposing or self._got_kv_or_scanline:
            return

        # Dump diagnostics from the HB decoder ring buffer
        diag_lines = []
        if self._sirona_client:
            diag_lines = self._sirona_client.dump_diagnostics(10)

        diag_text = "\n".join(diag_lines) if diag_lines else "(no HB data)"

        self._log(
            "NO RESPONSE \u2014 device armed but no data received",
            "error",
        )
        for line in diag_lines:
            self._log(f"  DIAG: {line}", "error")

        # Dump to events.log for later grep
        self._dump_diag_to_file("no_response", diag_lines)

        Toast(
            self,
            "Device armed but no exposure data.\n"
            "Was the physical expose button pressed?\n"
            "Check gantry position, power cycle if needed.",
            level="error",
            duration_ms=8000,
        )

        # Show error modal with Orthophos troubleshooting hints
        self._show_no_response_modal(diag_text)

    def _on_expose_complete(self) -> None:
        """Scan finished (Released or E7 received)."""
        if not self._exposing:
            return
        self._exposing = False

        # Cancel the 2 s no-response watchdog if still pending
        if self._no_response_timer_id is not None:
            self.after_cancel(self._no_response_timer_id)
            self._no_response_timer_id = None

        # Cancel timeout watchdog
        if self._expose_timer_id:
            self.after_cancel(self._expose_timer_id)
            self._expose_timer_id = None

        elapsed = time.perf_counter() - self._expose_start_time

        # Pull batch scanlines and kV peak directly from the client
        # (the after(0,...) callbacks may not have processed yet).
        if self._sirona_client:
            batch = getattr(self._sirona_client, '_scan_scanlines', [])
            if batch and len(batch) > len(self._expose_scanlines):
                self._expose_scanlines = list(batch)
                self._sirona_client._scan_scanlines = []
            scan_kv = getattr(self._sirona_client, '_scan_kv_peak', 0.0)
            if scan_kv > self._expose_kv_peak:
                self._expose_kv_peak = scan_kv

        sl_count = len(self._expose_scanlines)

        # Update UI
        self._progress.stop()
        self._progress.set(0)
        self._direct_expose_btn.configure(text="\u2622 EXPOSE")
        self._update_expose_eligibility()
        self._set_status(
            f"\u2713 Scan complete | {sl_count} lines — Please be patient after pressing EXPOSE; processing may take a moment.",
            "#4CAF50", phase="",
        )
        self._kv_progress.set(0)
        self._kv_value_label.configure(text="kV: 0.0", text_color="#546E7A")

        self._expose_count += 1
        self._expose_count_label.configure(
            text=f"Exposures this session: {self._expose_count}"
        )

        self._log(
            f"Scan complete: {sl_count} scanlines, "
            f"peak {self._expose_kv_peak:.1f} kV, {elapsed:.1f}s",
            "info",
        )
        Toast(
            self,
            f"Scan complete: {sl_count} scanlines in {elapsed:.1f}s",
            level="success",
            duration_ms=4000,
        )

        # Log to persistent events file
        self._write_expose_event(elapsed, sl_count)

        # Save patient-named outputs (PNG, events, sessions.json)
        self._save_patient_outputs(elapsed, sl_count)

        # PHASE 3 — Auto-upload to PureChart
        self._phase3_upload_to_purechart()

        # DICOM export
        if self._expose_scanlines:
            self._log(
                f"Post-processing: stitch {sl_count} scanlines + DICOM export",
                "info",
            )
        self._export_dicom()

        # Auto-stitch panoramic if we got scanlines
        if self._expose_scanlines:
            self._stitch_panoramic()

        # Refresh history window if open
        self._refresh_history_if_open()

    def _on_expose_timeout(self) -> None:
        """Hard timeout — no Released received within EXPOSE_TIMEOUT_S."""
        if not self._exposing:
            return
        sl_count = len(self._expose_scanlines)
        self._log(
            f"EXPOSE TIMEOUT ({EXPOSE_TIMEOUT_S:.0f}s) \u2014 "
            f"force-completing with {sl_count} scanlines",
            "error",
        )

        # Dump last 10 HB/status diagnostics
        self._dump_expose_fail_diagnostics("TIMEOUT")

        Toast(
            self,
            f"Timeout ({EXPOSE_TIMEOUT_S:.0f}s) \u2014 "
            f"force-completing with {sl_count} scanlines",
            level="error",
            duration_ms=5000,
        )
        # Force complete with whatever we have
        self._on_expose_complete()

    def _on_expose_error(self, exc: Exception) -> None:
        """Expose trigger send failed."""
        self._exposing = False
        if self._expose_timer_id:
            self.after_cancel(self._expose_timer_id)
            self._expose_timer_id = None
        if self._no_response_timer_id is not None:
            self.after_cancel(self._no_response_timer_id)
            self._no_response_timer_id = None

        self._progress.stop()
        self._progress.set(0)
        self._direct_expose_btn.configure(text="\u2622 EXPOSE")
        self._update_expose_eligibility()
        self._set_status("ERROR", "#F44336")
        self._log(f"Expose send failed: {exc}", "error")

        # Dump diagnostics and show error modal
        self._dump_expose_fail_diagnostics(f"SEND_FAIL: {exc}")
        self._show_expose_fail_modal(str(exc))

        Toast(self, f"Expose failed: {exc}", level="error")

    # ── Diagnostics ────────────────────────────────────────────────────

    def _dump_expose_fail_diagnostics(self, reason: str) -> None:
        """Log + persist the last 10 HB/status entries from the live client."""
        diag_lines: list[str] = []
        if self._sirona_client:
            diag_lines = self._sirona_client.dump_diagnostics(10)

        self._log(f"=== EXPOSE FAIL DIAGNOSTICS ({reason}) ===", "error")
        for line in diag_lines:
            self._log(f"  DIAG: {line}", "error")
        if not diag_lines:
            self._log("  (no HB diagnostic data available)", "error")

        self._dump_diag_to_file(reason, diag_lines)

    def _dump_diag_to_file(self, reason: str, lines: list[str]) -> None:
        """Append diagnostic lines to events.log for post-mortem grep."""
        try:
            with open(EXPOSE_EVENT_LOG, "a", encoding="utf-8") as f:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n{ts}  DIAG  reason={reason}\n")
                for line in lines:
                    f.write(f"  {line}\n")
        except Exception:
            pass

    def _show_no_response_modal(self, diag_text: str) -> None:
        """Error modal with Orthophos gantry troubleshooting hints."""
        messagebox.showerror(
            "No Device Response",
            "Trigger sent but no kV ramp or scanlines received "
            "within 2 seconds.\n\n"
            "ORTHOPHOS Troubleshooting:\n"
            "1. Gantry must be in patient position (press R or Return "
            "on the unit keypad).\n"
            "2. Laser alignment crosshairs must be visible.\n"
            "3. Phase 0 (positioning) blocks exposure \u2014 the device "
            "will not fire the tube until positioning completes.\n"
            "4. If lasers are off: power cycle the unit and wait "
            "4 minutes for warm-up.\n"
            "5. Check TCP connection \u2014 HB heartbeat must be pulsing.\n\n"
            f"Last 10 HB/status entries:\n{diag_text}",
            parent=self,
        )

    def _show_expose_fail_modal(self, error_msg: str) -> None:
        """Error modal when expose trigger fails to send."""
        diag_lines: list[str] = []
        if self._sirona_client:
            diag_lines = self._sirona_client.dump_diagnostics(10)
        diag_text = "\n".join(diag_lines) if diag_lines else "(no HB data)"

        messagebox.showerror(
            "Expose Trigger Failed",
            f"Could not send trigger bytes to device.\n\n"
            f"Error: {error_msg}\n\n"
            "Possible causes:\n"
            "1. TCP connection dropped \u2014 restart HB monitor.\n"
            "2. Device powered off or network cable disconnected.\n"
            "3. Another Sidexis session owns the device.\n\n"
            f"Last 10 HB/status entries:\n{diag_text}",
            parent=self,
        )

    def _write_expose_event(self, elapsed: float, sl_count: int) -> None:
        """Append an expose record to the persistent events.log."""
        try:
            with open(EXPOSE_EVENT_LOG, "a", encoding="utf-8") as f:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(
                    f"{ts}  EXPOSE  "
                    f"scanlines={sl_count}  "
                    f"peak_kv={self._expose_kv_peak:.1f}  "
                    f"elapsed={elapsed:.1f}s  "
                    f"session_total={self._expose_count}\n"
                )
        except Exception as exc:
            log.debug("Failed to write expose event: %s", exc)

    # ── Scanline preview + panoramic stitch ──────────────────────────────

    def _render_scanline_preview(self) -> None:
        """Render accumulated scanlines as a panoramic on the preview canvas.

        Scales the full image to fit the canvas while maintaining aspect
        ratio.  Applies contrast enhancement for better visibility.
        """
        scanlines = self._expose_scanlines
        if not scanlines:
            return

        n_cols = len(scanlines)
        px_count = scanlines[0].pixel_count
        if px_count == 0:
            return

        # Build full-resolution 16-bit array (height × width)
        img_array = np.zeros((px_count, n_cols), dtype=np.uint16)
        for col, sl in enumerate(scanlines):
            img_array[:len(sl.pixels), col] = sl.pixels[:px_count]

        # Contrast enhancement: clip to 2nd-98th percentile, then normalize
        low = np.percentile(img_array[img_array > 0], 2) if np.any(img_array > 0) else 0
        high = np.percentile(img_array[img_array > 0], 98) if np.any(img_array > 0) else 1
        if high <= low:
            high = low + 1
        clipped = np.clip(img_array.astype(np.float32), low, high)
        normalized = ((clipped - low) / (high - low) * 255).astype(np.uint8)

        img = Image.fromarray(normalized, mode="L")

        # Scale to fit canvas while maintaining aspect ratio
        canvas_w = max(self._scanline_canvas.winfo_width(), 400)
        canvas_h = max(self._scanline_canvas.winfo_height(), 200)

        # Fit image within canvas bounds
        scale_w = canvas_w / n_cols
        scale_h = canvas_h / px_count
        scale = min(scale_w, scale_h)
        new_w = max(int(n_cols * scale), 1)
        new_h = max(int(px_count * scale), 1)

        img_scaled = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Center in canvas
        x_offset = (canvas_w - new_w) // 2
        y_offset = (canvas_h - new_h) // 2

        self._pano_photo = ImageTk.PhotoImage(img_scaled)
        self._scanline_canvas.delete("all")
        self._scanline_canvas.create_image(
            x_offset, y_offset, image=self._pano_photo, anchor="nw",
        )
        # Update canvas scroll region
        self._scanline_canvas.configure(scrollregion=(0, 0, canvas_w, canvas_h))

    def _stitch_panoramic(self) -> None:
        """Auto-stitch scanlines into a full panoramic and display it."""
        if not HAS_HB_DECODER or not self._expose_scanlines:
            return

        # Show loading state on canvas
        self._canvas.delete("all")
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw < 10:
            cw, ch = 800, 500
        self._canvas.create_text(
            cw // 2, ch // 2 - 20,
            text="Processing X-ray...",
            fill="#4FC3F7", font=("Helvetica", 18),
        )
        self._canvas.create_text(
            cw // 2, ch // 2 + 15,
            text=f"{len(self._expose_scanlines)} columns captured",
            fill="#546E7A", font=("Helvetica", 12),
        )
        self._progress.configure(mode="indeterminate")
        self._progress.start()

        # Run heavy reconstruct on background thread
        scanlines = list(self._expose_scanlines)
        repair_mask = getattr(self._sirona_client, '_repair_mask', None) if self._sirona_client else None

        def _do():
            try:
                img = reconstruct_image(scanlines, repair_mask=repair_mask)
                self.after(0, self._on_stitch_done, img, None)
            except Exception as exc:
                self.after(0, self._on_stitch_done, None, exc)

        threading.Thread(target=_do, name="stitch", daemon=True).start()

    def _on_stitch_done(self, img: "Image.Image | None", error: "Exception | None") -> None:
        """Callback after background reconstruct_image completes."""
        self._progress.stop()
        self._progress.set(0)

        if error:
            self._log(f"Panoramic stitch failed: {error}", "error")
            self._canvas.delete("all")
            cw = self._canvas.winfo_width()
            ch = self._canvas.winfo_height()
            self._canvas.create_text(
                cw // 2, ch // 2,
                text=f"Processing failed\n{error}",
                fill="#EF5350", font=("Helvetica", 14), justify="center",
            )
            Toast(self, f"Image processing failed: {error}", level="error")
            return

        if img is None:
            self._log("Panoramic stitch failed — no valid scanlines", "warning")
            return

        self._log(f"Panoramic stitched: {img.width}x{img.height}", "info")
        self._display_pil_image(img)
        self._save_pano_btn.configure(state="normal")
        self._enable_post_toolbar()
        self._new_patient_btn.configure(state="normal")
        self._set_status(
            f"Scan complete — {img.width}x{img.height}",
            "#4CAF50",
        )

    def _display_pil_image(self, img: Image.Image) -> None:
        """Display a PIL Image on the main canvas, scaled to fill."""
        self._last_pil_image = img  # store for resize re-render

        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw < 10 or ch < 10:
            cw, ch = 800, 500

        img_w, img_h = img.size
        scale = min(cw / img_w, ch / img_h)
        new_w = max(int(img_w * scale), 1)
        new_h = max(int(img_h * scale), 1)
        display_img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        self._photo_image = ImageTk.PhotoImage(display_img)
        self._canvas.delete("all")
        self._canvas.create_image(
            cw // 2, ch // 2, image=self._photo_image, anchor="center",
        )
        self._img_info_label.configure(
            text=(
                f"{img_w}\u00D7{img_h}  |  "
                f"{len(self._expose_scanlines)} columns  |  "
                f"Exposure #{self._expose_count}"
            )
        )

    # ── Post-display toolbar logic ──────────────────────────────────────

    def _enable_post_toolbar(self) -> None:
        """Enable brightness/contrast sliders and save buttons after image display."""
        self._brightness_slider.configure(state="normal")
        self._contrast_slider.configure(state="normal")
        self._reset_adj_btn.configure(state="normal")
        self._save_btn.configure(state="normal")
        self._save_raw_btn.configure(state="normal")

    def _disable_post_toolbar(self) -> None:
        """Disable all post-display toolbar controls."""
        self._brightness_slider.configure(state="disabled")
        self._contrast_slider.configure(state="disabled")
        self._reset_adj_btn.configure(state="disabled")
        self._save_btn.configure(state="disabled")
        self._save_raw_btn.configure(state="disabled")
        self._open_dcm_btn.configure(state="disabled")
        self._view_dcm_btn.configure(state="disabled")

    _adjust_debounce_id: str | None = None

    def _on_adjust_display(self, _value=None) -> None:
        """Debounced brightness/contrast adjustment (display-only, non-destructive)."""
        if self._adjust_debounce_id is not None:
            self.after_cancel(self._adjust_debounce_id)
        self._adjust_debounce_id = self.after(50, self._apply_adjustments)

    def _apply_adjustments(self) -> None:
        """Apply brightness/contrast to the stored PIL image and re-display."""
        self._adjust_debounce_id = None
        if self._last_pil_image is None:
            return

        from PIL import ImageEnhance

        brightness = self._brightness_var.get()  # -80 to +80
        contrast = self._contrast_var.get()       # 0.3 to 3.0

        img = self._last_pil_image
        # Apply contrast
        if abs(contrast - 1.0) > 0.01:
            img = ImageEnhance.Contrast(img).enhance(contrast)
        # Apply brightness as offset
        if abs(brightness) > 1:
            import numpy as _np
            arr = _np.array(img, dtype=_np.int16)
            arr = _np.clip(arr + int(brightness), 0, 255).astype(_np.uint8)
            img = Image.fromarray(arr)

        # Re-render adjusted image (don't overwrite _last_pil_image)
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw < 10 or ch < 10:
            cw, ch = 800, 500

        img_w, img_h = img.size
        scale = min(cw / img_w, ch / img_h)
        new_w = max(int(img_w * scale), 1)
        new_h = max(int(img_h * scale), 1)
        display_img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        self._photo_image = ImageTk.PhotoImage(display_img)
        self._canvas.delete("all")
        self._canvas.create_image(
            cw // 2, ch // 2, image=self._photo_image, anchor="center",
        )

    def _on_reset_adjustments(self) -> None:
        """Reset brightness/contrast to defaults."""
        self._brightness_var.set(0.0)
        self._contrast_var.set(1.0)
        if self._last_pil_image is not None:
            self._display_pil_image(self._last_pil_image)

    def _on_save_panoramic(self) -> None:
        """Save the stitched panoramic image."""
        if not self._expose_scanlines or not HAS_HB_DECODER:
            return

        img = reconstruct_image(self._expose_scanlines)
        if img is None:
            Toast(self, "No panoramic to save", level="warning")
            return

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save Panoramic",
            defaultextension=".png",
            initialfile=f"panoramic_{ts}.png",
            filetypes=[
                ("PNG Image", "*.png"),
                ("TIFF Image", "*.tif"),
                ("All Files", "*.*"),
            ],
        )
        if not path:
            return

        try:
            img.save(path)
            self._log(f"Panoramic saved: {path}", "info")
            Toast(self, f"Saved: {Path(path).name}", level="success")
        except Exception as exc:
            self._log(f"Save failed: {exc}", "error")
            Toast(self, f"Save failed: {exc}", level="error")

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  Patient History Window
    # ╚════════════════════════════════════════════════════════════════════════

    def _on_open_history(self) -> None:
        """Open the patient history window (singleton — one at a time)."""
        if not HAS_HISTORY:
            Toast(self, "history module not found", level="error")
            return

        # If window exists and is still open, just focus it
        if (
            self._history_window is not None
            and isinstance(self._history_window, ctk.CTkToplevel)
            and self._history_window.winfo_exists()
        ):
            self._history_window.focus_force()
            self._history_window.lift()
            return

        self._history_window = PatientHistoryWindow(
            self, patients_dir=PATIENTS_DIR
        )

    def _refresh_history_if_open(self) -> None:
        """If the history window is open, tell it to reload from disk."""
        if (
            self._history_window is not None
            and isinstance(self._history_window, ctk.CTkToplevel)
            and self._history_window.winfo_exists()
        ):
            self._history_window.refresh()

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  DICOM Export
    # ╚════════════════════════════════════════════════════════════════════════

    def _export_dicom(self) -> None:
        """Export the current scan as DICOM if patient is set and data exists."""
        if not HAS_DICOM:
            self._log("DICOM export skipped (pydicom not installed)", "debug")
            return
        if not self._patient.get("set"):
            return
        if not self._expose_scanlines:
            return

        outdir = self._patient_output_dir()

        def _do():
            try:
                exporter = PureXSDICOM()
                dcm_path = exporter.export(
                    self._patient,
                    self._expose_scanlines,
                    self._expose_kv_peak,
                    outdir,
                )
                self.after(0, self._on_dicom_exported, dcm_path)
            except Exception as exc:
                self.after(0, self._on_dicom_error, exc)

        threading.Thread(target=_do, name="dicom-export", daemon=True).start()

    def _on_dicom_exported(self, dcm_path: str) -> None:
        """DICOM file written successfully."""
        self._last_dcm_path = dcm_path
        filename = Path(dcm_path).name

        self._log(f"DICOM saved: {filename}", "info")
        Toast(
            self,
            f"\U0001F4BE DICOM saved: {filename}",
            level="success",
            duration_ms=3000,
        )

        # Enable DICOM buttons
        self._open_dcm_btn.configure(state="normal")
        self._view_dcm_btn.configure(state="normal")

        # Append dcm file to latest sessions.json entry
        self._append_dcm_to_session(dcm_path)

    def _on_dicom_error(self, exc: Exception) -> None:
        """DICOM export failed."""
        self._log(f"DICOM export failed: {exc}", "error")
        Toast(self, f"DICOM export failed: {exc}", level="error")

    def _append_dcm_to_session(self, dcm_path: str) -> None:
        """Add the dcm_file field to the most recent sessions.json entry."""
        if not self._patient.get("set"):
            return
        sessions_path = self._patient_output_dir() / "sessions.json"
        try:
            if sessions_path.exists():
                sessions = json.loads(sessions_path.read_text(encoding="utf-8"))
                if sessions:
                    sessions[-1]["dcm_file"] = Path(dcm_path).name
                    sessions_path.write_text(
                        json.dumps(sessions, indent=2), encoding="utf-8"
                    )
        except Exception as exc:
            log.debug("Failed to append dcm to sessions.json: %s", exc)

    def _on_open_dicom_folder(self) -> None:
        """Open the patient output folder in the system file manager."""
        if not self._last_dcm_path:
            return
        open_path(Path(self._last_dcm_path).parent)

    def _on_view_dicom(self) -> None:
        """Open the last DICOM file in the system default viewer."""
        if not self._last_dcm_path:
            return
        dcm = self._last_dcm_path

        if Path(dcm).exists():
            open_path(dcm)
            self._log(f"Opened DICOM viewer: {Path(dcm).name}", "info")
        else:
            self._log("DICOM file not found — showing PNG fallback", "warning")
            self._show_dicom_fallback()

    def _show_dicom_fallback(self) -> None:
        """Display the stitched panoramic in a popup window as a fallback."""
        if not self._expose_scanlines or not HAS_HB_DECODER:
            Toast(self, "No image data for fallback view", level="warning")
            return

        img = reconstruct_image(self._expose_scanlines)
        if img is None:
            return

        win = ctk.CTkToplevel(self)
        win.title("PureXS — DICOM Fallback Viewer")
        win.geometry("800x400")

        # Scale image to fit
        img_w, img_h = img.size
        scale = min(780 / img_w, 380 / img_h, 1.0)
        display = img.resize(
            (max(int(img_w * scale), 1), max(int(img_h * scale), 1)),
            Image.Resampling.LANCZOS,
        )
        photo = ImageTk.PhotoImage(display)

        canvas = tk.Canvas(win, bg="#0A0A0A", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_image(
            400, 200, image=photo, anchor="center"
        )
        # Prevent GC
        canvas._photo_ref = photo  # type: ignore[attr-defined]

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  Patient Workflow
    # ╚════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _normalize_dob(dob_str: str) -> str:
        """Convert YYYY-MM-DD to MM/DD/YYYY if needed."""
        dob_str = dob_str.strip()
        try:
            dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
            return dob.strftime("%m/%d/%Y")
        except ValueError:
            return dob_str

    def _validate_dob(self, dob_str: str) -> str | None:
        """Validate date (accepts MM/DD/YYYY or YYYY-MM-DD). Returns error or None."""
        dob_str = self._normalize_dob(dob_str.strip())
        if not dob_str:
            return "DOB is required"
        try:
            dob = datetime.strptime(dob_str, "%m/%d/%Y").date()
        except ValueError:
            return "DOB must be MM/DD/YYYY"
        today = date.today()
        if dob > today:
            return "DOB cannot be in the future"
        age_days = (today - dob).days
        if age_days > 120 * 365:
            return "Age exceeds 120 years"
        return None

    def _on_set_patient(self) -> None:
        """Validate fields and lock patient context."""
        first = self._pt_first.get().strip()
        last = self._pt_last.get().strip()
        dob = self._pt_dob.get().strip()
        pid = self._pt_id.get().strip()
        exam = self._pt_exam_var.get()

        # Validate required fields
        if not first:
            self._pt_status_label.configure(text="First name required")
            return
        if not last:
            self._pt_status_label.configure(text="Last name required")
            return
        dob_err = self._validate_dob(dob)
        if dob_err:
            self._pt_status_label.configure(text=dob_err)
            return

        # Auto-generate patient ID if blank
        if not pid:
            pid = uuid.uuid4().hex[:8]
            self._pt_id.delete(0, "end")
            self._pt_id.insert(0, pid)

        # Lock fields
        self._patient = {
            "first": first, "last": last, "dob": dob,
            "id": pid, "exam": exam, "set": True,
        }
        for w in (self._pt_first, self._pt_last, self._pt_dob, self._pt_id):
            w.configure(state="disabled")
        self._pt_set_btn.configure(state="disabled")
        self._pt_status_label.configure(
            text=f"\u2713 {last}, {first}", text_color="#81C784"
        )

        # Update status bar patient banner
        self._patient_banner.configure(
            text=f"Patient: {last}, {first} | DOB: {dob} | {exam}"
        )

        # Add to recent patients
        self._add_recent_patient()

        # Re-evaluate expose button eligibility
        self._update_expose_eligibility()

        self._log(f"Patient set: {last}, {first} (ID: {pid}, {exam})", "info")

        # Create patient output directory
        patient_dir = PATIENTS_DIR / pid
        patient_dir.mkdir(parents=True, exist_ok=True)

    def _on_clear_patient(self) -> None:
        """Clear patient context and lock expose."""
        if self._exposing:
            Toast(self, "Cannot clear patient during expose", level="warning")
            return

        self._patient = {
            "first": "", "last": "", "dob": "", "id": "",
            "exam": "Panoramic", "set": False,
        }
        for w in (self._pt_first, self._pt_last, self._pt_dob, self._pt_id):
            w.configure(state="normal")
            w.delete(0, "end")
        self._pt_exam_var.set("Panoramic")
        self._pt_set_btn.configure(state="normal")
        self._pt_status_label.configure(text="", text_color="#EF5350")
        self._patient_banner.configure(text="")
        self._selected_purechart = None
        self._hide_profile_card()
        self._show_dock()

        # Disable expose buttons
        self._update_expose_eligibility()

        self._log("Patient cleared", "info")

    def _on_new_patient(self) -> None:
        """One-click reset: clear patient, clear canvas, re-show dock."""
        if self._exposing:
            Toast(self, "Cannot switch patient during expose", level="warning")
            return

        # Clear patient
        self._on_clear_patient()

        # Reset canvas to placeholder
        self._last_pil_image = None
        self._canvas.delete("all")
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        self._canvas_text_id = self._canvas.create_text(
            cw // 2, ch // 2,
            text="No Image\n\nSelect a patient and press EXPOSE",
            fill="#3A3A3A", font=("Segoe UI", 16), justify="center",
        )
        self._img_info_label.configure(text="No image")

        # Reset toolbar
        self._disable_post_toolbar()
        self._new_patient_btn.configure(state="disabled")
        self._on_reset_adjustments()

        # Reset status
        if self._direct_connected:
            self._set_status("Connected", "#4CAF50")
        else:
            self._set_status("OFFLINE", "#616161")

        self._log("Ready for new patient", "info")

    def _update_expose_eligibility(self) -> None:
        """Enable/disable expose buttons based on HB + patient + device readiness."""
        patient_set = self._patient.get("set", False)
        hb_active = self._direct_connected

        # Direct expose button: needs HB active + patient + device READY
        if patient_set and hb_active and self._device_ready:
            self._direct_expose_btn.configure(
                state="normal", fg_color="#FF3B30",
                hover_color="#FF6659", text_color="#FFFFFF",
            )
        else:
            self._direct_expose_btn.configure(
                state="disabled", fg_color="#616161", text_color="#9E9E9E",
            )

        # API expose button: needs API connection + patient + last status READY
        api_ready = self._last_status in ("READY", "CONNECTED")
        if patient_set and self._connected and api_ready:
            self._expose_btn.configure(state="normal")
        else:
            self._expose_btn.configure(state="disabled")

    def _patient_file_prefix(self) -> str:
        """Build output filename prefix from patient context."""
        p = self._patient
        dob_nodash = p["dob"].replace("/", "")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{p['last']}_{p['first']}_{dob_nodash}_{ts}"

    def _patient_output_dir(self) -> Path:
        """Return the patient-specific output directory."""
        pid = self._patient.get("id", "unknown")
        d = PATIENTS_DIR / pid
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── Recent patients persistence ──────────────────────────────────────

    def _load_recent_patients(self) -> None:
        """Load recent patients JSON and populate the combo box."""
        try:
            if RECENT_PATIENTS_FILE.exists():
                data = json.loads(RECENT_PATIENTS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    labels = []
                    for p in data[:RECENT_PATIENTS_MAX]:
                        label = f"{p.get('last', '?')}, {p.get('first', '?')} ({p.get('id', '?')})"
                        labels.append(label)
                    pass  # recent combo removed
                    return
        except Exception as exc:
            log.debug("Failed to load recent patients: %s", exc)
        pass  # recent combo removed

    def _add_recent_patient(self) -> None:
        """Prepend current patient to recents list (max 10, deduped by ID)."""
        p = self._patient.copy()
        p.pop("set", None)

        recents: list[dict] = []
        try:
            if RECENT_PATIENTS_FILE.exists():
                recents = json.loads(
                    RECENT_PATIENTS_FILE.read_text(encoding="utf-8")
                )
        except Exception:
            recents = []

        # Remove existing entry with same ID
        recents = [r for r in recents if r.get("id") != p["id"]]
        # Prepend
        recents.insert(0, p)
        # Trim
        recents = recents[:RECENT_PATIENTS_MAX]

        try:
            RECENT_PATIENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            RECENT_PATIENTS_FILE.write_text(
                json.dumps(recents, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            log.debug("Failed to save recent patients: %s", exc)

        # Refresh combo
        self._load_recent_patients()

    def _on_recent_patient_selected(self, choice: str) -> None:
        """Auto-fill fields when a recent patient is selected."""
        if choice == "(none)" or not choice:
            return

        # Load the recents list and match by label
        try:
            recents = json.loads(
                RECENT_PATIENTS_FILE.read_text(encoding="utf-8")
            )
        except Exception:
            return

        # Extract ID from label: "Smith, John (abc123)"
        import re
        m = re.search(r"\(([^)]+)\)$", choice)
        if not m:
            return
        target_id = m.group(1)

        for p in recents:
            if p.get("id") == target_id:
                # Fill fields (unlock first if locked)
                for w in (self._pt_first, self._pt_last, self._pt_dob, self._pt_id):
                    w.configure(state="normal")
                self._pt_first.delete(0, "end")
                self._pt_first.insert(0, p.get("first", ""))
                self._pt_last.delete(0, "end")
                self._pt_last.insert(0, p.get("last", ""))
                self._pt_dob.delete(0, "end")
                self._pt_dob.insert(0, p.get("dob", ""))
                self._pt_id.delete(0, "end")
                self._pt_id.insert(0, p.get("id", ""))
                self._pt_exam_var.set(p.get("exam", "Panoramic"))
                self._pt_set_btn.configure(state="normal")
                self._pt_status_label.configure(
                    text="Click Set Patient to confirm", text_color="#FFA726"
                )
                break

    # ── Patient-aware output files ───────────────────────────────────────

    def _save_patient_outputs(self, elapsed: float, sl_count: int) -> None:
        """Save panoramic PNG, kV CSV, and session JSON for the current patient."""
        if not self._patient.get("set"):
            return

        outdir = self._patient_output_dir()
        prefix = self._patient_file_prefix()

        # 1. Save panoramic PNG
        pano_filename = ""
        if self._expose_scanlines and HAS_HB_DECODER:
            img = reconstruct_image(self._expose_scanlines)
            if img is not None:
                pano_filename = f"{prefix}_panoramic.png"
                pano_path = outdir / pano_filename
                img.save(pano_path)
                self._last_pano_path = str(pano_path)  # PHASE 3
                self._log(f"Panoramic saved: {pano_path}", "info")

        # 2. Save events log for this expose
        events_filename = f"{prefix}_events.log"
        events_path = outdir / events_filename
        try:
            with open(events_path, "w", encoding="utf-8") as f:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                p = self._patient
                f.write(f"Patient: {p['last']}, {p['first']}\n")
                f.write(f"DOB: {p['dob']}\n")
                f.write(f"Patient ID: {p['id']}\n")
                f.write(f"Exam: {p['exam']}\n")
                f.write(f"Timestamp: {ts}\n")
                f.write(f"Scanlines: {sl_count}\n")
                f.write(f"Peak kV: {self._expose_kv_peak:.1f}\n")
                f.write(f"Elapsed: {elapsed:.1f}s\n")
        except Exception as exc:
            log.debug("Failed to write patient events: %s", exc)

        # 3. Append to sessions.json
        sessions_path = outdir / "sessions.json"
        sessions: list[dict] = []
        try:
            if sessions_path.exists():
                sessions = json.loads(sessions_path.read_text(encoding="utf-8"))
        except Exception:
            sessions = []

        sessions.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "exam_type": self._patient.get("exam", ""),
            "kv_peak": round(self._expose_kv_peak, 1),
            "scanlines": sl_count,
            "image_file": pano_filename,
            "events_log": events_filename,
        })

        try:
            sessions_path.write_text(
                json.dumps(sessions, indent=2), encoding="utf-8"
            )
            self._log(
                f"Session logged: {sessions_path} ({len(sessions)} total)",
                "info",
            )
        except Exception as exc:
            log.debug("Failed to write sessions.json: %s", exc)

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  Status & Logging
    # ╚════════════════════════════════════════════════════════════════════════

    def _set_status(
        self, text: str, color: str, phase: str = "",
    ) -> None:
        """Update the large status label and optional gantry-phase sub-label."""
        self._last_status = text
        self._status_label.configure(text=text, text_color=color)
        self._phase_label.configure(
            text=phase,
            text_color="#B0BEC5" if phase else "#757575",
        )

    def _log(self, message: str, level: str = "info") -> None:
        """Append a timestamped line to the GUI log and Python logger."""
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]

        level_prefix = {
            "info":    "INF",
            "warning": "WRN",
            "error":   "ERR",
            "debug":   "DBG",
        }
        pfx = level_prefix.get(level, "   ")

        line = f"[{ts}] {pfx}  {message}\n"

        # Python logger
        getattr(log, level, log.info)(message)

        # GUI textbox (must run on main thread)
        self._log_text.configure(state="normal")
        self._log_text.insert("end", line)

        # Trim if too long
        line_count = int(self._log_text.index("end-1c").split(".")[0])
        if line_count > LOG_MAX_LINES:
            self._log_text.delete("1.0", f"{line_count - LOG_MAX_LINES}.0")

        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    # ╔════════════════════════════════════════════════════════════════════════
    # ║  Shutdown
    # ╚════════════════════════════════════════════════════════════════════════

    #region Phase 1+2 — PureChart Patient Search

    # PHASE 1
    def _phase1_load_patients(self) -> None:
        """Kick off an initial broad PureChart search in a background thread."""
        self._purechart_status.configure(
            text="Loading patients...", text_color="#FFA726"
        )
        self._log("PureChart: loading patients...", "info")
        self._purechart_run_search("a")

    # PHASE 2
    def _on_purechart_search_typed(self, *_args) -> None:
        """Called on every keystroke in the search entry. Debounces 400ms."""
        if not HAS_PURECHART or not self._purechart_loader:
            return
        # Cancel any pending debounce timer
        if self._purechart_debounce_id is not None:
            self.after_cancel(self._purechart_debounce_id)
            self._purechart_debounce_id = None
        query = self._purechart_search_var.get().strip()
        if len(query) < 2:
            # Too short — clear dock
            self._purechart_patients = []
            self._clear_avatar_dock()
            self._purechart_status.configure(text="", text_color="#757575")
            return
        # Schedule search after 400ms of no typing
        self._purechart_debounce_id = self.after(
            400, self._purechart_run_search, query
        )

    # PHASE 2
    def _purechart_run_search(self, query: str) -> None:
        """Launch a background thread to search PureChart."""
        if self._purechart_searching:
            return  # don't pile up concurrent searches
        self._purechart_searching = True
        self._purechart_status.configure(
            text=f"Searching \"{query}\"...", text_color="#FFA726"
        )
        threading.Thread(
            target=self._purechart_search_bg,
            args=(query,),
            daemon=True,
        ).start()

    # PHASE 2
    def _purechart_search_bg(self, query: str) -> None:
        """Background thread: call PureChart API then schedule UI update."""
        try:
            patients = self._purechart_loader.search(query)
            self.after(0, self._purechart_populate_combo, patients, None)
        except Exception as exc:
            self.after(0, self._purechart_populate_combo, [], exc)

    # PHASE 1+2
    def _purechart_populate_combo(
        self, patients: list, error: Exception | None,
    ) -> None:
        """Main-thread callback: populate the avatar dock with patient tiles."""
        self._purechart_searching = False

        if error is not None:
            self._purechart_status.configure(
                text="API unreachable — app continues normally",
                text_color="#EF5350",
            )
            self._log(f"PureChart search failed: {error}", "warning")
            return

        self._purechart_patients = patients
        if not patients:
            self._clear_avatar_dock()
            self._purechart_status.configure(
                text="0 patients found", text_color="#757575"
            )
            return

        self._purechart_status.configure(
            text=f"{len(patients)} results",
            text_color="#81C784",
        )
        self._log(f"PureChart: {len(patients)} results", "info")
        self._build_avatar_dock(patients)

    def _clear_avatar_dock(self) -> None:
        """Remove all avatar tiles from the dock."""
        for tile in self._avatar_tiles:
            tile["canvas"].destroy()
        self._avatar_tiles = []
        self._avatar_photos = {}
        self._avatar_raw_bytes = {}

    _DOCK_WIDTH = 90
    _dock_anim_id: str | None = None
    _dock_anim_step: int = 0

    def _hide_dock(self) -> None:
        """Smoothly collapse the patient avatar dock."""
        if not self._dock_visible:
            return
        self._dock_visible = False
        self._dismiss_hover_popup()
        # Phase 1: fade out content, then Phase 2: slide width
        self._dock_fade_then_slide(hiding=True)

    def _show_dock(self) -> None:
        """Smoothly expand the patient avatar dock."""
        if self._dock_visible:
            return
        self._dock_visible = True
        self._dock_frame.configure(width=1)
        self._main_grid.columnconfigure(0, weight=0, minsize=1)
        self._dock_frame.grid()
        # Phase 1: slide width open, then Phase 2: fade in content
        self._dock_fade_then_slide(hiding=False)

    def _dock_fade_then_slide(self, hiding: bool) -> None:
        """Two-phase animation: fade content + slide width."""
        if self._dock_anim_id is not None:
            self.after_cancel(self._dock_anim_id)
            self._dock_anim_id = None

        FADE_STEPS = 8
        FADE_MS = 20
        SLIDE_STEPS = 16
        SLIDE_MS = 16
        step = [0]

        if hiding:
            FADE_STEPS = 5
            FADE_MS = 15
            SLIDE_STEPS = 10
            SLIDE_MS = 12

            # Phase 1: fade content out
            def _fade():
                step[0] += 1
                t = step[0] / FADE_STEPS
                t_ease = t * t
                # Fade dock bg from #0D1117 toward #1A1A2E
                r = int(13 + (26 - 13) * t_ease)
                g = int(17 + (26 - 17) * t_ease)
                b = int(23 + (46 - 23) * t_ease)
                try:
                    self._dock_frame.configure(fg_color=f"#{r:02x}{g:02x}{b:02x}")
                except Exception:
                    pass
                # Hide avatar canvases progressively
                for tile in self._avatar_tiles:
                    try:
                        tile["canvas"].configure(bg=f"#{r:02x}{g:02x}{b:02x}")
                    except Exception:
                        pass
                if step[0] < FADE_STEPS:
                    self._dock_anim_id = self.after(FADE_MS, _fade)
                else:
                    step[0] = 0
                    _slide()

            # Phase 2: slide width closed
            def _slide():
                step[0] += 1
                t = step[0] / SLIDE_STEPS
                # Ease-in-out: smooth both ends
                t_ease = t * t * (3 - 2 * t)
                w = int(self._DOCK_WIDTH * (1 - t_ease))
                self._main_grid.columnconfigure(0, weight=0, minsize=max(w, 0))
                if step[0] < SLIDE_STEPS:
                    self._dock_anim_id = self.after(SLIDE_MS, _slide)
                else:
                    self._dock_anim_id = None
                    self._dock_frame.grid_remove()
                    self._main_grid.columnconfigure(0, weight=0, minsize=0)

            _fade()

        else:
            # Phase 1: slide width open
            def _slide():
                step[0] += 1
                t = step[0] / SLIDE_STEPS
                t_ease = t * t * (3 - 2 * t)
                w = int(self._DOCK_WIDTH * t_ease)
                self._main_grid.columnconfigure(0, weight=0, minsize=max(w, 1))
                if step[0] < SLIDE_STEPS:
                    self._dock_anim_id = self.after(SLIDE_MS, _slide)
                else:
                    step[0] = 0
                    _fade_in()

            # Phase 2: fade content in
            def _fade_in():
                step[0] += 1
                t = step[0] / FADE_STEPS
                t_ease = 1 - (1 - t) ** 2  # ease-out
                r = int(26 + (13 - 26) * t_ease)
                g = int(26 + (17 - 26) * t_ease)
                b = int(46 + (23 - 46) * t_ease)
                try:
                    self._dock_frame.configure(fg_color=f"#{r:02x}{g:02x}{b:02x}")
                except Exception:
                    pass
                for tile in self._avatar_tiles:
                    try:
                        tile["canvas"].configure(bg=f"#{r:02x}{g:02x}{b:02x}")
                    except Exception:
                        pass
                if step[0] < FADE_STEPS:
                    self._dock_anim_id = self.after(FADE_MS, _fade_in)
                else:
                    self._dock_anim_id = None
                    self._dock_frame.configure(fg_color="#0D1117")
                    for tile in self._avatar_tiles:
                        try:
                            tile["canvas"].configure(bg="#0D1117")
                        except Exception:
                            pass

            _slide()

    def _on_change_patient(self) -> None:
        """Re-show the dock to pick a different patient."""
        self._selected_purechart = None
        self._hide_profile_card()
        self._show_dock()

    # ── Avatar hover magnify + tooltip ────────────────────────────────

    _hover_popup: tk.Toplevel | None = None
    _hover_anim_id: str | None = None
    _hover_canvas: tk.Canvas | None = None
    _hover_patient: object | None = None
    _hover_size: int = 60
    _TILE_SIZE = 60
    _MAGNIFIED = 76
    _ANIM_STEPS = 6
    _ANIM_MS = 18

    def _on_avatar_enter(self, event, canvas: tk.Canvas, pt) -> None:
        """Start smooth magnify animation and show detail popup."""
        # Cancel any running animation
        if self._hover_anim_id is not None:
            self.after_cancel(self._hover_anim_id)
            self._hover_anim_id = None

        self._hover_canvas = canvas
        self._hover_patient = pt

        # Animate from current size toward MAGNIFIED
        self._animate_avatar(canvas, pt, self._hover_size, self._MAGNIFIED, grow=True)

        # Show detail popup immediately
        self._show_hover_popup(canvas, pt)

    def _on_avatar_leave(self, event, canvas: tk.Canvas) -> None:
        """Start smooth shrink animation and dismiss popup."""
        if self._hover_anim_id is not None:
            self.after_cancel(self._hover_anim_id)
            self._hover_anim_id = None

        pt = None
        for tile in self._avatar_tiles:
            if tile["canvas"] is canvas:
                pt = tile["patient"]
                break

        self._hover_canvas = None
        self._hover_patient = None

        # Animate from current size back to TILE_SIZE
        self._animate_avatar(canvas, pt, self._hover_size, self._TILE_SIZE, grow=False)

        self._dismiss_hover_popup()

    def _animate_avatar(self, canvas, pt, from_size, to_size, grow: bool) -> None:
        """Animate avatar size in steps."""
        step = 0
        total = self._ANIM_STEPS

        def _step():
            nonlocal step
            step += 1
            t = step / total
            # Ease-out cubic
            t_ease = 1 - (1 - t) ** 3
            size = int(from_size + (to_size - from_size) * t_ease)
            self._hover_size = size

            self._redraw_avatar(canvas, pt, size, highlight=grow)

            if step < total:
                self._hover_anim_id = self.after(self._ANIM_MS, _step)
            else:
                self._hover_anim_id = None
                self._hover_size = to_size

        _step()

    def _redraw_avatar(self, canvas, pt, size, highlight=False):
        """Redraw an avatar at the given size with circular crop + glow."""
        bg_color = "#1A2740" if highlight else "#0D1117"
        canvas.configure(width=size, height=size, bg=bg_color)
        canvas.delete("all")

        inner = size - 6  # image area inside padding
        raw_bytes = self._avatar_raw_bytes.get(pt.id) if pt else None

        if pt and raw_bytes:
            # Render circular-cropped photo at current size
            from PIL import ImageDraw
            img = Image.open(io.BytesIO(raw_bytes))
            img = img.resize((inner, inner), Image.LANCZOS)
            mask = Image.new("L", (inner, inner), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, inner - 1, inner - 1), fill=255)
            bg_img = Image.new("RGBA", (inner, inner),
                               tuple(int(bg_color[i:i+2], 16) for i in (1, 3, 5)) + (255,))
            img = img.convert("RGBA")
            bg_img.paste(img, (0, 0), mask)
            photo = ImageTk.PhotoImage(bg_img)
            # Store keyed by (patient_id, size) to prevent GC
            self._avatar_photos[(pt.id, size)] = photo
            canvas.create_image(size // 2, size // 2,
                                image=photo, anchor="center")
        else:
            # Initials fallback
            canvas.create_oval(3, 3, size - 3, size - 3,
                               fill="#0F3460", outline="#2A2A4A", width=2)
            if pt:
                initials = ""
                if pt.first_name:
                    initials += pt.first_name[0].upper()
                if pt.last_name:
                    initials += pt.last_name[0].upper()
                font_size = max(10, int(size * 0.28))
                canvas.create_text(size // 2, size // 2,
                                   text=initials or "?", fill="white",
                                   font=("Helvetica", font_size, "bold"))

        # Cyan glow ring on top (always visible over photo)
        outline_color = "#4FC3F7" if highlight else "#2A2A4A"
        outline_w = 3 if highlight else 2
        canvas.create_oval(3, 3, size - 3, size - 3,
                           fill="", outline=outline_color, width=outline_w)

    def _show_hover_popup(self, canvas, pt) -> None:
        """Show patient detail popup to the right of the dock."""
        self._dismiss_hover_popup()

        popup = tk.Toplevel(self)
        popup.overrideredirect(True)
        popup.configure(bg="#4FC3F7")
        popup.attributes("-topmost", True)
        # Start transparent, fade in
        popup.attributes("-alpha", 0.0)

        dock_x = self._dock_frame.winfo_rootx() + self._dock_frame.winfo_width() + 6
        avatar_y = canvas.winfo_rooty() - 10
        popup.geometry(f"+{dock_x}+{avatar_y}")

        # Outer border (1px cyan)
        inner = tk.Frame(popup, bg="#1E2A44", padx=14, pady=10)
        inner.pack(padx=1, pady=1)

        name = f"{pt.first_name} {pt.last_name}"
        tk.Label(inner, text=name, fg="white", bg="#1E2A44",
                 font=("Helvetica", 13, "bold")).pack(anchor="w")

        if pt.medical_record_number:
            tk.Label(inner, text=pt.medical_record_number,
                     fg="#4FC3F7", bg="#1E2A44",
                     font=("Consolas", 10)).pack(anchor="w", pady=(2, 0))

        details = []
        if pt.dob:
            details.append(f"DOB  {pt.dob}")
        if pt.phone:
            details.append(f"Tel  {pt.phone}")
        for d in details:
            tk.Label(inner, text=d, fg="#90A4AE", bg="#1E2A44",
                     font=("Helvetica", 10)).pack(anchor="w")

        self._hover_popup = popup

        # Fade in over 120ms
        self._fade_popup(popup, 0.0, 1.0, steps=6)

    def _fade_popup(self, popup, from_a, to_a, steps, step=0):
        """Animate popup alpha."""
        try:
            if not popup.winfo_exists():
                return
        except Exception:
            return
        step += 1
        t = step / steps
        alpha = from_a + (to_a - from_a) * t
        try:
            popup.attributes("-alpha", alpha)
        except Exception:
            return
        if step < steps:
            self.after(20, self._fade_popup, popup, from_a, to_a, steps, step)

    def _dismiss_hover_popup(self) -> None:
        if self._hover_popup is not None:
            try:
                self._hover_popup.destroy()
            except Exception:
                pass
            self._hover_popup = None

    def _build_avatar_dock(self, patients: list) -> None:
        """Build clickable avatar tiles in the vertical dock (single column)."""
        self._clear_avatar_dock()
        TILE_SIZE = 60

        for idx, pt in enumerate(patients):
            # Single column — each tile is one row
            canvas = tk.Canvas(
                self._avatar_dock_frame, width=TILE_SIZE, height=TILE_SIZE,
                bg="#0D1117", highlightthickness=0, cursor="hand2",
            )
            canvas.pack(padx=4, pady=3)

            # Draw initials circle
            initials = ""
            if pt.first_name:
                initials += pt.first_name[0].upper()
            if pt.last_name:
                initials += pt.last_name[0].upper()
            initials = initials or "?"

            canvas.create_oval(3, 3, TILE_SIZE - 3, TILE_SIZE - 3,
                               fill="#0F3460", outline="#2A2A4A", width=2)
            canvas.create_text(TILE_SIZE // 2, TILE_SIZE // 2,
                               text=initials, fill="white",
                               font=("Helvetica", 14, "bold"))

            # Click binding
            canvas.bind("<Button-1>", lambda e, p=pt: self._on_avatar_clicked(p))

            # Hover: magnify + show tooltip
            canvas.bind("<Enter>", lambda e, c=canvas, p=pt: self._on_avatar_enter(e, c, p))
            canvas.bind("<Leave>", lambda e, c=canvas, p=pt: self._on_avatar_leave(e, c))

            # Forward mousewheel to the scrollable frame
            def _on_mousewheel(e):
                self._avatar_dock_frame._parent_canvas.yview_scroll(
                    int(-1 * (e.delta / 120)), "units"
                )
            canvas.bind("<MouseWheel>", _on_mousewheel)

            self._avatar_tiles.append({
                "canvas": canvas,
                "patient": pt,
            })

            # Download profile picture in background
            if pt.profile_picture_url:
                threading.Thread(
                    target=self._download_avatar_image,
                    args=(pt.profile_picture_url, pt.id, idx),
                    daemon=True,
                ).start()

    def _download_avatar_image(self, url: str, patient_id: str, tile_idx: int) -> None:
        """Background thread: download profile pic for avatar dock tile."""
        try:
            # Use the PureChart loader's session (has auth headers)
            if self._purechart_loader:
                resp = self._purechart_loader._session.get(url, timeout=10)
            else:
                resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                self.after(0, self._set_avatar_tile_image, resp.content, patient_id, tile_idx)
        except Exception:
            pass

    def _set_avatar_tile_image(self, image_bytes: bytes, patient_id: str, tile_idx: int) -> None:
        """Main-thread: set a dock tile's avatar to the downloaded image."""
        if tile_idx >= len(self._avatar_tiles):
            return
        tile = self._avatar_tiles[tile_idx]
        if tile["patient"].id != patient_id:
            return
        self._avatar_raw_bytes[patient_id] = image_bytes
        pt = tile["patient"]
        self._redraw_avatar(tile["canvas"], pt, self._TILE_SIZE, highlight=False)

    def _on_avatar_clicked(self, pt: "PureChartPatient") -> None:
        """User clicked an avatar tile — select patient, hide dock, show card."""
        self._selected_purechart = pt

        # Hide the dock
        self._hide_dock()

        # Auto-fill the patient fields
        for w in (self._pt_first, self._pt_last, self._pt_dob, self._pt_id):
            w.configure(state="normal")

        self._pt_first.delete(0, "end")
        self._pt_first.insert(0, pt.first_name)

        self._pt_last.delete(0, "end")
        self._pt_last.insert(0, pt.last_name)

        self._pt_dob.delete(0, "end")
        self._pt_dob.insert(0, self._normalize_dob(pt.dob))

        self._pt_id.delete(0, "end")
        self._pt_id.insert(0, pt.id)

        self._pt_set_btn.configure(state="normal")
        self._pt_status_label.configure(
            text="Click Set Patient to confirm", text_color="#FFA726"
        )
        self._log(
            f"PureChart patient selected: {pt.first_name} {pt.last_name} "
            f"(ID: {pt.id}, MRN: {pt.medical_record_number})",
            "info",
        )

        # Show the profile card
        self._show_profile_card(pt)

    # ── Profile card helpers ──────────────────────────────────────────────

    def _show_profile_card(self, pt: "PureChartPatient") -> None:
        """Populate and show the patient profile card."""
        self._profile_name_label.configure(text=f"{pt.first_name} {pt.last_name}")
        self._profile_mrn_label.configure(text=f"MRN: {pt.medical_record_number}" if pt.medical_record_number else "")
        self._profile_dob_label.configure(text=f"DOB: {pt.dob}" if pt.dob else "")
        self._profile_phone_label.configure(text=f"Phone: {pt.phone}" if pt.phone else "")

        # Reset avatar to initials
        initials = ""
        if pt.first_name:
            initials += pt.first_name[0].upper()
        if pt.last_name:
            initials += pt.last_name[0].upper()
        initials = initials or "?"
        self._profile_avatar_canvas.delete("all")
        self._profile_avatar_canvas.create_oval(2, 2, 62, 62, fill="#37474F", outline="#546E7A", width=2)
        self._profile_initials = self._profile_avatar_canvas.create_text(
            32, 32, text=initials, fill="#B0BEC5",
            font=("Helvetica", 18, "bold"),
        )
        self._profile_photo = None

        # Show the card inside the patient frame
        if not self._profile_card_visible:
            self._profile_card.pack(fill="x", padx=12, pady=(4, 4))
            self._profile_card_visible = True

        # Download profile picture in background if URL available
        if pt.profile_picture_url:
            threading.Thread(
                target=self._download_profile_image,
                args=(pt.profile_picture_url, pt.id),
                daemon=True,
            ).start()

    def _hide_profile_card(self) -> None:
        """Hide the patient profile card."""
        if self._profile_card_visible:
            self._profile_card.pack_forget()
            self._profile_card_visible = False
            self._profile_photo = None

    def _download_profile_image(self, url: str, patient_id: str) -> None:
        """Background thread: download profile picture and update avatar."""
        try:
            if self._purechart_loader:
                resp = self._purechart_loader._session.get(url, timeout=10)
            else:
                resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                return
            self.after(0, self._set_profile_image, resp.content, patient_id)
        except Exception as exc:
            log.debug("Failed to download profile image: %s", exc)

    def _set_profile_image(self, image_bytes: bytes, patient_id: str) -> None:
        """Main-thread callback: set the avatar to the downloaded image."""
        # Only apply if the same patient is still selected
        if not self._selected_purechart or self._selected_purechart.id != patient_id:
            return
        try:
            img = Image.open(io.BytesIO(image_bytes))
            # Resize to fit avatar (60x60 with 2px padding = 64x64 canvas)
            img = img.resize((60, 60), Image.LANCZOS)
            # Create circular mask
            mask = Image.new("L", (60, 60), 0)
            from PIL import ImageDraw
            draw = ImageDraw.Draw(mask)
            draw.ellipse((0, 0, 59, 59), fill=255)
            # Apply mask — paste onto dark background
            bg = Image.new("RGBA", (60, 60), (22, 32, 41, 255))
            img = img.convert("RGBA")
            bg.paste(img, (0, 0), mask)

            self._profile_photo = ImageTk.PhotoImage(bg)
            self._profile_avatar_canvas.delete("all")
            self._profile_avatar_canvas.create_oval(2, 2, 62, 62, fill="#162029", outline="#546E7A", width=2)
            self._profile_avatar_canvas.create_image(32, 32, image=self._profile_photo, anchor="center")
        except Exception as exc:
            log.debug("Failed to render profile image: %s", exc)

    #endregion

    #region Phase 3 — Auto-upload to PureChart

    # PHASE 3
    def _phase3_upload_to_purechart(self) -> None:
        """After expose completes, auto-upload the panoramic PNG to PureChart."""
        if not HAS_PURECHART or not self._purechart_uploader:
            return
        if not self._selected_purechart:
            self._log("PureChart upload skipped: no PureChart patient selected", "debug")
            return
        if not self._patient.get("set"):
            return
        if not self._expose_scanlines:
            self._log("PureChart upload skipped: no scanlines captured", "debug")
            return

        # Use the PNG path stored by _save_patient_outputs
        if not self._last_pano_path or not Path(self._last_pano_path).exists():
            self._log("PureChart upload skipped: panoramic PNG not found", "warning")
            return
        pano_path = Path(self._last_pano_path)

        patient_id = self._selected_purechart.id
        exam_type = self._patient.get("exam", "Panoramic")
        upload_type = EXAM_TYPE_MAP.get(exam_type, "xrays") if HAS_PURECHART else "xrays"
        title = f"{exam_type} — {self._patient['last']}, {self._patient['first']}"

        self._log(
            f"PureChart: uploading {pano_path.name} to patient {patient_id} "
            f"(type={upload_type})",
            "info",
        )

        # PHASE 5 — store args for retry and show upload UI
        args = (patient_id, str(pano_path), upload_type, title)
        self._last_upload_args = args
        self._phase5_show_uploading()

        threading.Thread(
            target=self._phase3_upload_bg,
            args=args,
            daemon=True,
        ).start()

    # PHASE 3
    def _phase3_upload_bg(
        self, patient_id: str, file_path: str,
        upload_type: str, title: str,
    ) -> None:
        """Background thread: upload file to PureChart."""
        try:
            result = self._purechart_uploader.upload_file(
                patient_id=patient_id,
                file_path=file_path,
                upload_type=upload_type,
                title=title,
            )
            self.after(0, self._phase3_upload_done, result)
        except Exception as exc:
            self.after(0, self._phase3_upload_error, exc)

    # PHASE 3
    def _phase3_upload_done(self, result) -> None:
        """Main-thread callback: upload finished."""
        if result.success:
            self._phase5_show_success(result)
            self._log(
                f"PureChart upload OK: {result.filename} "
                f"(attachment {result.attachment_id})",
                "info",
            )
            Toast(
                self,
                f"X-ray uploaded to PureChart",
                level="success",
                duration_ms=4000,
            )
        else:
            self._phase5_show_failure(f"Upload failed: {result.error}")
            self._log(
                f"PureChart upload failed (HTTP {result.http_status}): {result.error}",
                "warning",
            )
            Toast(
                self,
                f"PureChart upload failed: {result.error}",
                level="warning",
                duration_ms=5000,
            )

    # PHASE 3
    def _phase3_upload_error(self, exc: Exception) -> None:
        """Main-thread callback: upload threw an exception."""
        self._phase5_show_failure(f"Upload error: {exc}")
        self._log(f"PureChart upload error: {exc}", "error")
        Toast(
            self,
            f"PureChart upload error — app continues normally",
            level="warning",
            duration_ms=5000,
        )

    #endregion

    #region Phase 5 — Upload Status / Retry UI

    # PHASE 5
    def _phase5_show_uploading(self) -> None:
        """Show the upload frame with indeterminate progress bar spinning."""
        self._upload_frame.pack(fill="x", padx=12, pady=(0, 4))
        self._upload_progress.configure(mode="indeterminate")
        self._upload_progress.start()
        self._upload_status_label.configure(
            text="Uploading to PureChart...", text_color="#FFA726"
        )
        self._upload_retry_btn.pack_forget()
        self._purechart_status.configure(
            text="Uploading...", text_color="#FFA726"
        )

    # PHASE 5
    def _phase5_show_success(self, result) -> None:
        """Upload succeeded — show green bar and hide retry."""
        self._upload_progress.stop()
        self._upload_progress.configure(mode="determinate")
        self._upload_progress.set(1.0)
        self._upload_status_label.configure(
            text=f"Uploaded ({result.size:,} bytes)",
            text_color="#81C784",
        )
        self._upload_retry_btn.pack_forget()
        self._purechart_status.configure(
            text=f"Uploaded to PureChart ({result.size:,} bytes)",
            text_color="#81C784",
        )
        self._last_upload_args = ()  # clear — no retry needed

    # PHASE 5
    def _phase5_show_failure(self, message: str) -> None:
        """Upload failed — show red status and retry button."""
        self._upload_progress.stop()
        self._upload_progress.configure(mode="determinate")
        self._upload_progress.set(0)
        self._upload_status_label.configure(
            text=message, text_color="#EF5350"
        )
        self._upload_retry_btn.pack(side="right", padx=(4, 0))
        self._purechart_status.configure(
            text="Upload failed — click Retry", text_color="#EF5350"
        )

    # PHASE 5
    def _on_upload_retry(self) -> None:
        """Retry the last failed upload."""
        if not self._last_upload_args:
            self._log("PureChart retry: no upload to retry", "warning")
            return
        patient_id, file_path, upload_type, title = self._last_upload_args
        if not Path(file_path).exists():
            self._phase5_show_failure("Retry failed: file no longer exists")
            self._log(f"PureChart retry failed: {file_path} not found", "error")
            return

        self._log("PureChart: retrying upload...", "info")
        self._phase5_show_uploading()

        threading.Thread(
            target=self._phase3_upload_bg,
            args=self._last_upload_args,
            daemon=True,
        ).start()

    #endregion

    def _on_close(self) -> None:
        """Clean shutdown: stop HB, disconnect device, destroy window."""
        self._stop_hb_monitor()

        # Stop direct TCP monitor if active
        if self._direct_connected and self._sirona_client:
            try:
                self._sirona_client.disconnect()
            except Exception:
                pass

        if self._connected and self._mac:
            try:
                self.api.disconnect(self._mac)
            except Exception:
                pass

        self._log("PureXS GUI shutting down", "info")
        self.destroy()


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Entry Point
# ╚══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    app = PureXSApp()
    app.mainloop()


if __name__ == "__main__":
    main()
