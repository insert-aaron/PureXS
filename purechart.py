"""
PureChart patient integration — Phases 1-3.

Provides:
    PureChartPatient       — lightweight patient model
    PureChartPatientLoader — calls PureChart xray-patient-search edge function
    PureChartUploader      — uploads X-ray files to a patient's chart (Phase 3)
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("purexs.purechart")

# ── PureChart Supabase config ────────────────────────────────────────────────
_SEARCH_URL = (
    "https://whzohbzqhqaohpohmqah.supabase.co/functions/v1/xray-gateway/search"
)
_SCHEDULED_URL = (
    "https://whzohbzqhqaohpohmqah.supabase.co/functions/v1/xray-gateway/scheduled"
)
_UPLOAD_URL = (
    "https://whzohbzqhqaohpohmqah.supabase.co/functions/v1/xray-gateway/upload"
)
_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Indoem9oYnpxaHFhb2hwb2htcWFoIiwi"
    "cm9sZSI6ImFub24iLCJpYXQiOjE3NTkyNTUzNzQsImV4cCI6MjA3NDgzMTM3NH0."
    "p_BZ1XaPIihSdo-41YKbr4ZmS-NZRfGr9AerEEgpmcc"
)


@dataclass
class PureChartPatient:
    """A single patient record returned by PureChart."""

    id: str = ""
    first_name: str = ""
    last_name: str = ""
    medical_record_number: str = ""
    dob: str = ""
    phone: str = ""
    profile_picture_url: str = ""

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name} — {self.medical_record_number}"

    def __str__(self) -> str:
        return self.display_name


def _parse_patient_list(data) -> List[PureChartPatient]:
    """Parse an edge-function response into PureChartPatient objects.

    Shared by /search and /scheduled — both return the same patient shape
    (``{"patients": [...]}`` or a bare list). Extra fields the scheduled route
    adds (appointment_status/_time) are ignored here.
    """
    records = data if isinstance(data, list) else data.get("patients", [])
    return [
        PureChartPatient(
            id=rec.get("id", ""),
            first_name=rec.get("first_name", ""),
            last_name=rec.get("last_name", ""),
            medical_record_number=rec.get("medical_record_number", ""),
            dob=rec.get("dob", ""),
            phone=rec.get("phone", ""),
            profile_picture_url=rec.get("profile_picture_url", ""),
        )
        for rec in records
    ]


def fetch_facility_name(token: str, timeout: int = 10) -> str:
    """Best-effort: ask the backend for the facility name behind ``token``.

    The xray-gateway maps each token → facility server-side. If a backend
    revision returns the facility's name in the /scheduled response (top-level
    ``facility_name`` / ``facilityName``, or a ``facility`` object with a
    ``name``), surface it so the UI can auto-label the toggle ("auto from
    server"). Returns ``""`` when the backend exposes no name — the caller then
    falls back to a token-suffix label the user can rename.

    Raises on HTTP / network errors (a 401 means the token itself is bad), so
    the caller can distinguish an invalid token from a merely missing name.
    """
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {_ANON_KEY}",
        "apikey": _ANON_KEY,
        "x-api-key": token,
        "Content-Type": "application/json",
    })
    resp = session.post(_SCHEDULED_URL, json={}, timeout=timeout)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        return ""
    if isinstance(data, dict):
        for key in ("facility_name", "facilityName"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        fac = data.get("facility")
        if isinstance(fac, str) and fac.strip():
            return fac.strip()
        if isinstance(fac, dict):
            name = fac.get("name") or fac.get("display_name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return ""


class PureChartPatientLoader:
    """Calls the PureChart xray-patient-search Supabase edge function.

    All network I/O is blocking (uses ``requests``).  The GUI is expected
    to call :meth:`search` from a background thread.
    """

    def __init__(self, facility_token: str) -> None:
        self._facility_token = facility_token
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {_ANON_KEY}",
            "apikey": _ANON_KEY,
            "x-api-key": facility_token,
            "Content-Type": "application/json",
        })

    def search(self, query: str) -> List[PureChartPatient]:
        """POST the search query and return parsed patient list (up to 15 results).

        Raises on HTTP / network errors — caller must handle.
        """
        resp = self._session.post(
            _SEARCH_URL,
            json={"q": query},
            timeout=15,
        )
        resp.raise_for_status()
        return _parse_patient_list(resp.json())

    def scheduled_today(self, day: str | None = None) -> List[PureChartPatient]:
        """Return patients with an appointment at this facility today (or `day`,
        a 'YYYY-MM-DD' string). Used to seed the avatar dock on startup and
        whenever the search box is empty.

        Raises on HTTP / network errors — caller must handle.
        """
        resp = self._session.post(
            _SCHEDULED_URL,
            json={"date": day} if day else {},
            timeout=15,
        )
        resp.raise_for_status()
        return _parse_patient_list(resp.json())


# ── PHASE 3 — X-ray upload ──────────────────────────────────────────────────

# Exam-type → PureChart attachment type mapping
EXAM_TYPE_MAP: Dict[str, str] = {
    "Panoramic":          "panoramic_xray",
    "Ceph Lateral":       "xrays",
    "Ceph Frontal":       "xrays",
    "Bitewing Left":      "bitewings",
    "Bitewing Right":     "bitewings",
    "Bitewing Bilateral": "bitewings",
    "Periapical":         "periapical",
}

# Exam-type → xray-gateway metadata.slotCode mapping (free-form text; the
# gateway stores it verbatim and titles the row "X-ray <slotCode>"). This is
# OUR vocabulary and MUST match WPF's ExamTypes.ToSlotCode and whatever slot
# codes the PureXR desktop app uses to place the image in the correct slot.
SLOT_CODE_MAP: Dict[str, str] = {
    "Panoramic":          "PAN",
    "Ceph Lateral":       "CEPH-L",
    "Ceph Frontal":       "CEPH-F",
    "Bitewing Left":      "BW-L",
    "Bitewing Right":     "BW-R",
    "Bitewing Bilateral": "BW",
    "Periapical":         "PA",
}

_CONTENT_TYPE_MAP: Dict[str, str] = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tiff": "image/tiff",
    ".tif":  "image/tiff",
    ".bmp":  "image/bmp",
    ".pdf":  "application/pdf",
    ".dcm":  "application/dicom",
}


@dataclass
class UploadResult:
    """Parsed response from the upload-xray edge function.

    Success: ``{success:true, duplicate:bool, xrayId:uuid}``.
    Failure: ``{success:false, error:"<msg>"}``.
    """

    success: bool = False
    attachment_id: str = ""   # response field "xrayId"
    duplicate: bool = False
    error: str = ""
    http_status: int = 0


class PureChartUploader:
    """Uploads X-ray / file to a patient's chart via the PureChart edge function.

    All network I/O is blocking — call from a background thread.
    """

    def __init__(self, facility_token: str) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {_ANON_KEY}",
            "apikey": _ANON_KEY,
            "x-api-key": facility_token,
            "Content-Type": "application/json",
        })

    def upload_file(
        self,
        patient_id: str,
        file_path: str | Path,
        slot_code: str = "PAN",
        title: Optional[str] = None,
    ) -> UploadResult:
        """Upload a file to the patient's chart. Returns UploadResult.

        ``slot_code`` is the xray-gateway ``metadata.slotCode`` (e.g. "PAN").
        ``title`` is accepted for caller compatibility but the gateway derives
        the row title from slotCode, so it is not sent.

        Raises on network errors — caller must handle.
        """
        path = Path(file_path)
        file_bytes = path.read_bytes()
        b64 = base64.b64encode(file_bytes).decode("ascii")

        # xray-gateway/upload contract (purechart-app): JSON body
        #   { file, filename, metadata }
        # - file:     plain base64 of the image bytes (NO data: prefix), ≤10 MB
        # - filename: derives content type server-side (.jpg/.jpeg→jpeg, else png)
        # - metadata: a JSON string with patientId + slotCode + uploadType
        # Facility is keyed off the x-api-key header, NOT the body.
        metadata = json.dumps({
            "patientId": patient_id,
            "slotCode": slot_code,     # chart slot code (e.g. "PAN")
            "uploadType": "slot",      # "slot" (single image) | "composite"
        })
        payload: Dict[str, Any] = {
            "file": b64,
            "filename": path.name,
            "metadata": metadata,
        }

        resp = self._session.post(_UPLOAD_URL, json=payload, timeout=60)
        data = resp.json() if resp.text else {}

        # Success: { success:true, duplicate:bool, xrayId:uuid }
        # Failure: { success:false, error:"<msg>" }
        result = UploadResult(
            success=bool(data.get("success", False)),
            attachment_id=data.get("xrayId", ""),
            duplicate=bool(data.get("duplicate", False)),
            error=data.get("error", ""),
            http_status=resp.status_code,
        )

        if not resp.ok:
            result.success = False
            if not result.error:
                result.error = f"HTTP {resp.status_code}"
            log.warning("PureChart upload failed: %s", result.error)

        return result
