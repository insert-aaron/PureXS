"""
PureChart patient integration — Phases 1-3.

Provides:
    PureChartPatient       — lightweight patient model
    PureChartPatientLoader — calls PureChart xray-patient-search edge function
    PureChartUploader      — uploads X-ray files to a patient's chart (Phase 3)
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("purexs.purechart")

# ── PureChart Supabase config ────────────────────────────────────────────────
_SEARCH_URL = (
    "https://whzohbzqhqaohpohmqah.supabase.co/functions/v1/xray-patient-search"
)
_UPLOAD_URL = (
    "https://whzohbzqhqaohpohmqah.supabase.co/functions/v1/upload-xray"
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
        data = resp.json()

        # The edge function may return {"patients": [...]} or a bare list
        records = data if isinstance(data, list) else data.get("patients", [])

        patients: List[PureChartPatient] = []
        for rec in records:
            patients.append(PureChartPatient(
                id=rec.get("id", ""),
                first_name=rec.get("first_name", ""),
                last_name=rec.get("last_name", ""),
                medical_record_number=rec.get("medical_record_number", ""),
                dob=rec.get("dob", ""),
                phone=rec.get("phone", ""),
                profile_picture_url=rec.get("profile_picture_url", ""),
            ))
        return patients


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
    """Parsed response from the upload-xray edge function."""

    success: bool = False
    file_url: str = ""
    attachment_id: str = ""
    patient_id: str = ""
    filename: str = ""
    upload_type: str = ""
    size: int = 0
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
        upload_type: str = "xrays",
        title: Optional[str] = None,
    ) -> UploadResult:
        """Upload a file to the patient's chart. Returns UploadResult.

        Raises on network errors — caller must handle.
        """
        path = Path(file_path)
        file_bytes = path.read_bytes()
        b64 = base64.b64encode(file_bytes).decode("ascii")

        ext = path.suffix.lower()
        content_type = _CONTENT_TYPE_MAP.get(ext, "image/png")

        payload: Dict[str, Any] = {
            "patientId": patient_id,
            "base64Data": b64,
            "contentType": content_type,
            "type": upload_type,
            "title": title or f"{upload_type} capture",
            "originalFilename": path.name,
        }

        resp = self._session.post(_UPLOAD_URL, json=payload, timeout=60)
        data = resp.json() if resp.text else {}

        result = UploadResult(
            success=data.get("success", False),
            file_url=data.get("fileUrl", ""),
            attachment_id=data.get("attachmentId", ""),
            patient_id=data.get("patientId", patient_id),
            filename=data.get("filename", ""),
            upload_type=data.get("type", upload_type),
            size=data.get("size", 0),
            error=data.get("error", "") or data.get("message", ""),
            http_status=resp.status_code,
        )

        if not resp.ok:
            result.success = False
            if not result.error:
                result.error = f"HTTP {resp.status_code}"
            log.warning("PureChart upload failed: %s", result.error)

        return result
