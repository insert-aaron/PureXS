#!/usr/bin/env python3
"""Per-unit scan validation harness for the PureXS panoramic pipeline.

Run this once per Orthophos unit during install/acceptance, on a phantom or
volunteer scan, to confirm the unit's raw data decodes to a proper panoramic
BEFORE it's used on patients. It turns "I hope it works on this unit" into a
green/red checklist, and prints a geometry fingerprint you can diff across the
fleet to catch the odd-one-out (different detector/firmware).

Usage:
    python tools/validate_unit.py <scan.bin> [--sidexis ref.tif] [--unit "OP3"]

Exit code 0 = PASS, 1 = FAIL (so it can gate an install script).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Import the production decoder (this file lives in PureXS/tools/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hb_decoder as hb  # noqa: E402

# Expected detector geometry for the Orthophos XG / DX41 the pipeline targets.
EXPECT_HEIGHT = hb.PANO_DEFAULT_HEIGHT          # 1316
EXPECT_OUT = (2440, 1280)                        # final reconstructed size
SCANLINE_MIN = 2400                              # full pano sweep ≈ 2700 cols
SCANLINE_FULL = 2600
CONTENT_MEAN = (40, 220)                         # blank/garbage guard
CONTENT_STD_MIN = 25


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []
        self.fingerprint: dict[str, object] = {}

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, ok, detail))
        return ok

    def fp(self, key: str, val: object) -> None:
        self.fingerprint[key] = val

    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.rows)


def _silent_extract(raw: bytes):
    """Run extraction with the decoder's verbose stdout/logging muted."""
    import logging
    logging.disable(logging.CRITICAL)
    with contextlib.redirect_stdout(io.StringIO()):
        res = hb._extract_panoramic(raw)
        scanlines, rm = res if isinstance(res, tuple) else (res, None)
        img = hb.reconstruct_image(scanlines, repair_mask=rm) if scanlines else None
    logging.disable(logging.NOTSET)
    return scanlines, rm, img


def validate(bin_path: Path, sidexis: Path | None, unit: str) -> Report:
    r = Report()
    raw = bin_path.read_bytes()
    r.fp("unit", unit)
    r.fp("bin", bin_path.name)
    r.fp("raw_bytes", len(raw))

    r.check("raw buffer non-trivial (>1 MB)", len(raw) > 1_000_000,
            f"{len(raw)/1e6:.1f} MB")

    try:
        scanlines, rm, img = _silent_extract(raw)
    except Exception as exc:  # extraction blew up → definitely a mismatch
        r.check("panoramic extraction runs without error", False, repr(exc))
        return r
    r.check("panoramic extraction runs without error", True)

    n = len(scanlines) if scanlines else 0
    r.fp("scanlines", n)
    r.check("scanlines extracted", n > 0, f"{n} columns")
    if n == 0:
        return r

    # Detector height — the single strongest "different hardware" signal.
    height = int(scanlines[0].pixel_count)
    r.fp("detector_height", height)
    r.check(f"detector height == {EXPECT_HEIGHT} (expected detector)",
            height == EXPECT_HEIGHT,
            f"got {height}" + ("" if height == EXPECT_HEIGHT
                               else "  ← DIFFERENT DETECTOR / firmware!"))

    # Column count — truncation / aborted sweep.
    r.check(f"scanline count >= {SCANLINE_MIN} (full sweep)", n >= SCANLINE_MIN,
            f"{n} (full≈{SCANLINE_FULL}+)")

    # Reconstruction produced a real image of the expected size.
    ok_img = img is not None and tuple(img.size) == EXPECT_OUT
    r.check(f"reconstruct → {EXPECT_OUT[0]}x{EXPECT_OUT[1]}", ok_img,
            "None" if img is None else f"{img.size[0]}x{img.size[1]}")
    if not ok_img:
        return r

    arr = np.asarray(img.convert("L"), np.float64)
    mean, std = float(arr.mean()), float(arr.std())
    r.fp("img_mean", round(mean, 1))
    r.fp("img_std", round(std, 1))
    r.check("image not blank/garbage (mean in range, has variance)",
            CONTENT_MEAN[0] <= mean <= CONTENT_MEAN[1] and std >= CONTENT_STD_MIN,
            f"mean={mean:.0f} std={std:.0f}")

    if sidexis is not None and sidexis.exists():
        ref = np.asarray(Image.open(sidexis).convert("L"), np.float64)
        if ref.shape == arr.shape:
            a = arr if np.abs(arr - ref).mean() <= np.abs(
                np.asarray(img.convert("L").rotate(180)) - ref).mean() else \
                np.asarray(img.convert("L").rotate(180), np.float64)
            mae = float(np.abs(a - ref).mean())
            r.fp("sidexis_MAE", round(mae, 1))
            # Informational, not pass/fail — structural gap to Sidexis is expected.
            r.check("Sidexis reference comparison ran", True,
                    f"MAE={mae:.1f} ({mae/255*100:.0f}%) [informational]")
        else:
            r.check("Sidexis reference comparison ran", True,
                    f"size mismatch {ref.shape[::-1]} vs {arr.shape[::-1]} — skipped")

    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bin", type=Path, help="raw scan buffer (.bin)")
    ap.add_argument("--sidexis", type=Path, default=None,
                    help="optional Sidexis .tif of the same scan (informational)")
    ap.add_argument("--unit", default=os.environ.get("PUREXS_UNIT_ID", "unknown"),
                    help="unit label for the report (or set PUREXS_UNIT_ID)")
    args = ap.parse_args()

    if not args.bin.exists():
        print(f"ERROR: {args.bin} not found", file=sys.stderr)
        return 2

    r = validate(args.bin, args.sidexis, args.unit)

    print("\n" + "=" * 64)
    print(f" PureXS unit validation — unit: {args.unit}")
    print("=" * 64)
    for name, ok, detail in r.rows:
        mark = "PASS" if ok else "FAIL"
        line = f" [{mark}] {name}"
        if detail:
            line += f"   ({detail})"
        print(line)
    print("-" * 64)
    print(" geometry fingerprint (compare across the 6 units):")
    for k, v in r.fingerprint.items():
        print(f"    {k:16} = {v}")
    print("=" * 64)
    verdict = "PASS — unit OK for patient use" if r.passed() \
        else "FAIL — DO NOT use on patients; investigate (see failed checks)"
    print(f" VERDICT: {verdict}\n")
    return 0 if r.passed() else 1


if __name__ == "__main__":
    raise SystemExit(main())
