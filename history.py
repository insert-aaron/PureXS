#!/usr/bin/env python3
"""
PureXS Patient History — browse all past scans across patients.

Standalone module: only depends on customtkinter, Pillow, tkinter, json, pathlib.
Imported by purexs_gui.py and opened as a CTkToplevel window.

Data source: ~/.purexs/patients/*/sessions.json
Each sessions.json is an append-only array of expose records written by
purexs_gui.py._save_patient_outputs().
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from typing import Any

import customtkinter as ctk
from PIL import Image, ImageTk

from utils import get_data_dir, open_path

log = logging.getLogger("purexs.history")

# Default patients dir — caller can override
DEFAULT_PATIENTS_DIR = get_data_dir() / "patients"


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Data Loader
# ╚══════════════════════════════════════════════════════════════════════════════

class PatientRecord:
    """Aggregated patient record with all sessions."""

    __slots__ = ("patient_id", "display_name", "sessions", "last_scan", "folder")

    def __init__(
        self,
        patient_id: str,
        display_name: str,
        sessions: list[dict],
        folder: Path,
    ) -> None:
        self.patient_id = patient_id
        self.display_name = display_name
        self.sessions = sessions
        self.folder = folder
        # Most recent session timestamp
        if sessions:
            self.last_scan = sessions[-1].get("timestamp", "")
        else:
            self.last_scan = ""

    @property
    def scan_count(self) -> int:
        return len(self.sessions)

    @property
    def last_scan_short(self) -> str:
        """Date portion of last scan, e.g. '2026-03-20'."""
        ts = self.last_scan
        if len(ts) >= 10:
            return ts[:10]
        return ts or "—"


def load_all_patients(base_dir: Path | None = None) -> list[PatientRecord]:
    """Scan all patient directories and load session histories.

    Returns a list of PatientRecord sorted by most recent scan first.
    Corrupt or empty sessions.json files are silently skipped.
    """
    base = base_dir or DEFAULT_PATIENTS_DIR
    if not base.exists():
        return []

    records: list[PatientRecord] = []

    for patient_dir in sorted(base.iterdir()):
        if not patient_dir.is_dir():
            continue
        sessions_file = patient_dir / "sessions.json"
        if not sessions_file.exists():
            continue

        try:
            data = json.loads(sessions_file.read_text(encoding="utf-8"))
            if not isinstance(data, list) or not data:
                continue
        except (json.JSONDecodeError, OSError) as exc:
            log.debug("Skipping %s: %s", sessions_file, exc)
            continue

        # Extract patient name from the first session's image_file
        # Format: LastName_FirstName_DOB_timestamp_panoramic.png
        display_name = patient_dir.name  # fallback to folder name
        first_img = data[0].get("image_file", "")
        if first_img:
            parts = first_img.split("_")
            if len(parts) >= 2:
                display_name = f"{parts[0]}, {parts[1]}"

        records.append(PatientRecord(
            patient_id=patient_dir.name,
            display_name=display_name,
            sessions=data,
            folder=patient_dir,
        ))

    # Sort by most recent scan descending
    records.sort(key=lambda r: r.last_scan, reverse=True)
    return records


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  File opener (cross-platform)
# ╚══════════════════════════════════════════════════════════════════════════════

def _open_file(path: str | Path) -> None:
    """Open a file or folder with the system default application."""
    open_path(path)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  Patient History Window
# ╚══════════════════════════════════════════════════════════════════════════════

# Alternating row colors
_ROW_BG_EVEN = "#1E1E2E"
_ROW_BG_ODD = "#252536"
_ROW_BG_SELECTED = "#37474F"


class PatientHistoryWindow(ctk.CTkToplevel):
    """Non-blocking toplevel window showing all patient scan history."""

    def __init__(
        self,
        parent: ctk.CTk,
        patients_dir: Path | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(parent, **kwargs)
        self.title("\U0001F5C2 PureXS — Patient History")
        self.geometry("960x620")
        self.minsize(700, 450)

        self._patients_dir = patients_dir or DEFAULT_PATIENTS_DIR
        self._records: list[PatientRecord] = []
        self._selected_record: PatientRecord | None = None
        self._selected_session_idx: int = -1
        self._thumb_photo: ImageTk.PhotoImage | None = None  # prevent GC

        self._build_ui()
        self.after(100, self.refresh)

    # ── UI Construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Title bar ────────────────────────────────────────────────────
        top = ctk.CTkFrame(self, height=44, corner_radius=0)
        top.pack(fill="x")
        top.pack_propagate(False)

        ctk.CTkLabel(
            top, text="\U0001F5C2 Patient History",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="#81D4FA",
        ).pack(side="left", padx=12)

        ctk.CTkButton(
            top, text="\u2715 Close", width=70, height=28,
            fg_color="#37474F", hover_color="#455A64",
            command=self.destroy,
        ).pack(side="right", padx=8)

        ctk.CTkButton(
            top, text="\U0001F504 Refresh", width=90, height=28,
            fg_color="#1565C0", hover_color="#1976D2",
            command=self.refresh,
        ).pack(side="right", padx=4)

        # ── Main split ───────────────────────────────────────────────────
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        body.columnconfigure(0, weight=1, minsize=240)
        body.columnconfigure(1, weight=3, minsize=400)
        body.rowconfigure(0, weight=1)

        # ── Left: patient list ───────────────────────────────────────────
        left = ctk.CTkFrame(body, corner_radius=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        # Search bar
        self._search_var = ctk.StringVar()
        self._search_var.trace_add("write", self._on_search_changed)
        search = ctk.CTkEntry(
            left, textvariable=self._search_var,
            placeholder_text="Search patients...",
            height=30,
        )
        search.pack(fill="x", padx=8, pady=(8, 4))

        # Patient buttons scroll area
        self._patient_scroll = ctk.CTkScrollableFrame(
            left, fg_color="transparent",
        )
        self._patient_scroll.pack(fill="both", expand=True, padx=4, pady=(0, 8))

        # Empty state label (shown when no patients)
        self._empty_label = ctk.CTkLabel(
            self._patient_scroll,
            text="No patient history yet.\nComplete a scan to see records here.",
            font=ctk.CTkFont(size=12),
            text_color="#757575",
            justify="center",
        )

        # ── Right: session table + detail ────────────────────────────────
        right = ctk.CTkFrame(body, corner_radius=8)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        right.rowconfigure(0, weight=2)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        # Session table (scrollable)
        self._session_scroll = ctk.CTkScrollableFrame(
            right, fg_color="transparent", label_text="Sessions",
            label_font=ctk.CTkFont(size=12, weight="bold"),
        )
        self._session_scroll.grid(row=0, column=0, sticky="nsew", padx=4, pady=(4, 2))
        self._session_scroll.columnconfigure(0, weight=1)

        # ── Table header ─────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self._session_scroll, fg_color="#263238", height=28)
        hdr.pack(fill="x", pady=(0, 2))
        for col, (text, w) in enumerate([
            ("Date/Time", 160), ("Exam", 80), ("kV", 55),
            ("Lines", 45), ("Files", 170),
        ]):
            ctk.CTkLabel(
                hdr, text=text, width=w, anchor="w",
                font=ctk.CTkFont(size=10, weight="bold"),
                text_color="#90A4AE",
            ).pack(side="left", padx=2)

        # Session rows container
        self._rows_frame = ctk.CTkFrame(self._session_scroll, fg_color="transparent")
        self._rows_frame.pack(fill="both", expand=True)

        # ── Detail card ──────────────────────────────────────────────────
        self._detail_frame = ctk.CTkFrame(right, corner_radius=8, fg_color="#1A1A2E")
        self._detail_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=(2, 4))

        self._detail_label = ctk.CTkLabel(
            self._detail_frame,
            text="Select a session to view details",
            font=ctk.CTkFont(size=11),
            text_color="#757575",
            justify="left",
            anchor="nw",
        )
        self._detail_label.pack(fill="x", padx=12, pady=(10, 4))

        self._thumb_label = ctk.CTkLabel(
            self._detail_frame, text="",
        )
        self._thumb_label.pack(padx=12, pady=(0, 4))

        detail_btn_row = ctk.CTkFrame(self._detail_frame, fg_color="transparent")
        detail_btn_row.pack(fill="x", padx=12, pady=(0, 10))

        self._open_folder_btn = ctk.CTkButton(
            detail_btn_row,
            text="\U0001F4C2 Open Patient Folder",
            width=160, height=28,
            fg_color="#37474F", hover_color="#455A64",
            command=self._on_open_folder,
            state="disabled",
        )
        self._open_folder_btn.pack(side="left")

    # ── Data loading ─────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Reload all patient data from disk (runs I/O in thread)."""
        def _load():
            records = load_all_patients(self._patients_dir)
            self.after(0, self._on_data_loaded, records)

        threading.Thread(target=_load, daemon=True).start()

    def _on_data_loaded(self, records: list[PatientRecord]) -> None:
        self._records = records
        self._render_patient_list()

    # ── Patient list rendering ───────────────────────────────────────────

    def _render_patient_list(self, filter_text: str = "") -> None:
        """Rebuild the patient button list, optionally filtered."""
        # Clear existing
        for w in self._patient_scroll.winfo_children():
            if w is not self._empty_label:
                w.destroy()

        filtered = self._records
        if filter_text:
            ft = filter_text.lower()
            filtered = [
                r for r in self._records
                if ft in r.display_name.lower() or ft in r.patient_id.lower()
            ]

        if not filtered:
            self._empty_label.pack(pady=40)
            return
        self._empty_label.pack_forget()

        for rec in filtered:
            btn = ctk.CTkButton(
                self._patient_scroll,
                text=f"{rec.display_name}\n{rec.scan_count} scan(s) | Last: {rec.last_scan_short}",
                height=48,
                anchor="w",
                font=ctk.CTkFont(size=11),
                fg_color="#1E1E2E" if rec != self._selected_record else _ROW_BG_SELECTED,
                hover_color="#37474F",
                text_color="#E0E0E0",
                command=lambda r=rec: self._on_patient_selected(r),
            )
            btn.pack(fill="x", pady=1)

    def _on_search_changed(self, *_args: Any) -> None:
        self._render_patient_list(self._search_var.get())

    # ── Patient selection ────────────────────────────────────────────────

    def _on_patient_selected(self, record: PatientRecord) -> None:
        self._selected_record = record
        self._selected_session_idx = -1
        self._render_patient_list(self._search_var.get())  # update highlight
        self._render_sessions(record)
        self._open_folder_btn.configure(state="normal")
        self._detail_label.configure(
            text=f"Patient: {record.display_name}  |  ID: {record.patient_id}\n"
                 f"Scans: {record.scan_count}  |  Folder: {record.folder.name}",
            text_color="#B0BEC5",
        )
        self._thumb_label.configure(image=None, text="")
        self._thumb_photo = None

    # ── Session table rendering ──────────────────────────────────────────

    def _render_sessions(self, record: PatientRecord) -> None:
        """Build session rows for the selected patient."""
        # Clear old rows
        for w in self._rows_frame.winfo_children():
            w.destroy()

        if not record.sessions:
            ctk.CTkLabel(
                self._rows_frame, text="No sessions recorded",
                text_color="#757575",
            ).pack(pady=20)
            return

        # Reverse chronological
        for idx, sess in enumerate(reversed(record.sessions)):
            real_idx = len(record.sessions) - 1 - idx
            bg = _ROW_BG_EVEN if idx % 2 == 0 else _ROW_BG_ODD

            row = ctk.CTkFrame(self._rows_frame, fg_color=bg, height=32, corner_radius=4)
            row.pack(fill="x", pady=1)
            row.pack_propagate(False)

            # Make entire row clickable
            row.bind(
                "<Button-1>",
                lambda e, r=record, i=real_idx: self._on_session_clicked(r, i),
            )

            # Date/Time
            ts = sess.get("timestamp", "—")
            ts_display = ts.replace("T", " ")[:19] if "T" in ts else ts[:19]
            ctk.CTkLabel(
                row, text=ts_display, width=160, anchor="w",
                font=ctk.CTkFont(family="Consolas", size=10),
                text_color="#CFD8DC",
            ).pack(side="left", padx=(6, 2))

            # Exam type
            ctk.CTkLabel(
                row, text=sess.get("exam_type", "—"), width=80, anchor="w",
                font=ctk.CTkFont(size=10), text_color="#CFD8DC",
            ).pack(side="left", padx=2)

            # kV
            kv = sess.get("kv_peak", 0)
            ctk.CTkLabel(
                row, text=f"{kv} kV", width=55, anchor="w",
                font=ctk.CTkFont(size=10), text_color="#CFD8DC",
            ).pack(side="left", padx=2)

            # Scanlines
            sl = sess.get("scanlines", 0)
            ctk.CTkLabel(
                row, text=str(sl), width=45, anchor="w",
                font=ctk.CTkFont(size=10), text_color="#CFD8DC",
            ).pack(side="left", padx=2)

            # File buttons
            file_frame = ctk.CTkFrame(row, fg_color="transparent")
            file_frame.pack(side="left", padx=2)

            img_file = sess.get("image_file", "")
            dcm_file = sess.get("dcm_file", "")
            log_file = sess.get("events_log", "")

            if img_file:
                p = record.folder / img_file
                ctk.CTkButton(
                    file_frame, text="\U0001F5BC", width=28, height=22,
                    font=ctk.CTkFont(size=10),
                    fg_color="#1B5E20", hover_color="#2E7D32",
                    command=lambda f=p: self._open_if_exists(f),
                ).pack(side="left", padx=1)

            if dcm_file:
                p = record.folder / dcm_file
                ctk.CTkButton(
                    file_frame, text="\U0001F4CB", width=28, height=22,
                    font=ctk.CTkFont(size=10),
                    fg_color="#4A148C", hover_color="#6A1B9A",
                    command=lambda f=p: self._open_if_exists(f),
                ).pack(side="left", padx=1)

            if log_file:
                p = record.folder / log_file
                ctk.CTkButton(
                    file_frame, text="\U0001F4C4", width=28, height=22,
                    font=ctk.CTkFont(size=10),
                    fg_color="#37474F", hover_color="#455A64",
                    command=lambda f=p: self._open_if_exists(f),
                ).pack(side="left", padx=1)

    # ── Session detail ───────────────────────────────────────────────────

    def _on_session_clicked(self, record: PatientRecord, idx: int) -> None:
        self._selected_session_idx = idx
        if idx < 0 or idx >= len(record.sessions):
            return

        sess = record.sessions[idx]
        ts = sess.get("timestamp", "—").replace("T", " ")
        exam = sess.get("exam_type", "—")
        kv = sess.get("kv_peak", 0)
        sl = sess.get("scanlines", 0)
        img_file = sess.get("image_file", "")
        dcm_file = sess.get("dcm_file", "")

        dcm_info = ""
        if dcm_file:
            dcm_path = record.folder / dcm_file
            if dcm_path.exists():
                size = dcm_path.stat().st_size
                dcm_info = f"\nDICOM: {dcm_file} ({size:,} bytes)"
            else:
                dcm_info = f"\nDICOM: {dcm_file} (file missing)"

        self._detail_label.configure(
            text=(
                f"Patient: {record.display_name}  |  ID: {record.patient_id}\n"
                f"Scan: {ts}  |  {exam}  |  {kv} kV  |  {sl} scanlines"
                f"{dcm_info}"
            ),
            text_color="#B0BEC5",
        )

        # Load thumbnail
        self._load_thumbnail(record.folder, img_file)

    def _load_thumbnail(self, folder: Path, img_file: str) -> None:
        """Load and display a 200×50 thumbnail of the panoramic PNG."""
        if not img_file:
            self._thumb_label.configure(image=None, text="")
            self._thumb_photo = None
            return

        path = folder / img_file

        def _load():
            try:
                if not path.exists():
                    self.after(0, self._show_thumb_placeholder)
                    return
                img = Image.open(path)
                img.load()
                # Scale to max 200×50
                img.thumbnail((200, 50), Image.Resampling.LANCZOS)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                self.after(0, self._show_thumb, img)
            except Exception:
                self.after(0, self._show_thumb_placeholder)

        threading.Thread(target=_load, daemon=True).start()

    def _show_thumb(self, img: Image.Image) -> None:
        self._thumb_photo = ImageTk.PhotoImage(img)
        self._thumb_label.configure(image=self._thumb_photo, text="")

    def _show_thumb_placeholder(self) -> None:
        self._thumb_photo = None
        self._thumb_label.configure(
            image=None, text="[image not found]", text_color="#616161",
        )

    # ── Actions ──────────────────────────────────────────────────────────

    def _on_open_folder(self) -> None:
        if self._selected_record:
            _open_file(self._selected_record.folder)

    @staticmethod
    def _open_if_exists(path: Path) -> None:
        if path.exists():
            _open_file(path)
        else:
            log.warning("File not found: %s", path)
