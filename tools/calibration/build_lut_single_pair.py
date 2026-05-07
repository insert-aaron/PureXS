"""
Build a histogram-matched tone LUT calibrated to a specific facility's unit.

Inputs:
    scan_20260505_193217.bin  — raw TCP capture from PureXS
    aaron_tiff.tif            — Sidexis-processed reference of the same scan

Process:
    1. Run the .bin through clean-mode pipeline (main @ 01a84f6) → 8-bit PNG
    2. Compute histogram of our output (source) and the Sidexis TIF (target)
    3. Build a 256-entry uint8 LUT that maps source grey levels to the
       target grey level with the matching cumulative-histogram value
    4. Save as facility_X_tone_lut.npy

Run from the repo root after editing BIN_PATH / TIF_PATH below to point
at your facility's matched pair:

    /opt/homebrew/bin/python3 PureXS/tools/calibration/build_lut_single_pair.py

NOTE: the per-pair single-scan approach is OVERFIT-PRONE — anatomical
differences between scans get treated as calibration drift. Prefer
`build_lut_3pair_median.py` (which medians 3 LUTs from time-matched
pairs) before deploying any LUT.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Layout: PureXS/tools/calibration/build_lut_single_pair.py
# parents[0]=calibration/, [1]=tools/, [2]=PureXS/, [3]=workspace root
HERE = Path(__file__).resolve()
PUREXS_PKG = HERE.parents[2]          # the PureXS/ Python package
WORKSPACE  = HERE.parents[3]          # parent of the repo, holds .bin/.tif
sys.path.insert(0, str(PUREXS_PKG))

from hb_decoder import _extract_panoramic, reconstruct_image  # noqa: E402

# Edit these two paths for the facility being calibrated. Both files
# must come from the SAME scan (timestamp-matched), or the LUT will
# encode anatomical differences as calibration drift.
BIN_PATH = WORKSPACE / "scan_20260505_193217.bin"
TIF_PATH = WORKSPACE / "aaron_tiff.tif"

OUT_LUT     = WORKSPACE / "facility_X_tone_lut.npy"
OUT_OURS    = WORKSPACE / "facility_X_purexs_raw.png"
OUT_MAPPED  = WORKSPACE / "facility_X_purexs_after_lut.png"
OUT_SIDEBY  = WORKSPACE / "facility_X_lut_sidebyside.png"


def histogram_match_lut(src: np.ndarray, tgt: np.ndarray) -> np.ndarray:
    """Return 256-entry uint8 LUT that maps src histogram → tgt histogram."""
    src_hist = np.bincount(src.ravel(), minlength=256).astype(np.float64)
    tgt_hist = np.bincount(tgt.ravel(), minlength=256).astype(np.float64)
    src_cdf = src_hist.cumsum() / max(src_hist.sum(), 1.0)
    tgt_cdf = tgt_hist.cumsum() / max(tgt_hist.sum(), 1.0)
    lut = np.zeros(256, dtype=np.uint8)
    for v in range(256):
        u = int(np.searchsorted(tgt_cdf, src_cdf[v]))
        lut[v] = min(u, 255)
    return lut


def main() -> int:
    for p in (BIN_PATH, TIF_PATH):
        if not p.exists():
            print(f"ERROR: missing input {p}")
            return 1

    # Step 1 — run our pipeline on the .bin
    print(f"[1] Running clean-mode pipeline on {BIN_PATH.name} ...")
    raw = BIN_PATH.read_bytes()
    result = _extract_panoramic(raw)
    scanlines, repair_mask = (result if isinstance(result, tuple) else (result, None))
    if not scanlines:
        print("ERROR: no scanlines extracted")
        return 1
    pil = reconstruct_image(scanlines, invert=True, repair_mask=repair_mask)
    if pil is None:
        print("ERROR: reconstruct_image returned None")
        return 1
    pil.save(OUT_OURS)
    ours = np.array(pil.convert("L"))
    print(f"    Output: {pil.size[0]}×{pil.size[1]}, saved {OUT_OURS.name}")

    # Step 2 — load Sidexis TIF reference
    print(f"[2] Loading reference {TIF_PATH.name} ...")
    ref = np.array(Image.open(TIF_PATH).convert("L"))
    print(f"    Reference: {ref.shape[1]}×{ref.shape[0]}")

    # Step 3 — histogram match LUT
    print("[3] Building histogram-matched LUT (source=ours, target=sidexis) ...")
    lut = histogram_match_lut(ours, ref)
    np.save(OUT_LUT, lut)
    print(f"    Saved LUT: {OUT_LUT.name} (256 entries, uint8)")
    print(f"    Identity-deltas: min={int((lut.astype(int) - np.arange(256)).min())}, "
          f"max={int((lut.astype(int) - np.arange(256)).max())}, "
          f"mean={float((lut.astype(int) - np.arange(256)).mean()):+.2f}")

    # Step 4 — apply LUT to our output and save preview + side-by-side
    mapped = lut[ours]
    Image.fromarray(mapped, mode="L").save(OUT_MAPPED)

    # For side-by-side, ensure same height — resize ref to match ours if needed
    if ref.shape[0] != mapped.shape[0]:
        ref_resized = np.array(
            Image.fromarray(ref, mode="L").resize(
                (ref.shape[1], mapped.shape[0]), Image.Resampling.LANCZOS
            )
        )
    else:
        ref_resized = ref
    gap = 30
    h = mapped.shape[0]
    w_ours = mapped.shape[1]
    w_ref  = ref_resized.shape[1]
    side = np.full((h, w_ours + gap + w_ref), 255, dtype=np.uint8)
    side[:, :w_ours] = mapped
    side[:, w_ours + gap:] = ref_resized
    Image.fromarray(side, mode="L").save(OUT_SIDEBY)
    print(f"[4] Saved {OUT_MAPPED.name} (our pipeline output after LUT)")
    print(f"    Saved {OUT_SIDEBY.name} (left=ours+LUT, right=Sidexis ref)")

    # MAE just for context
    if mapped.shape == ref.shape:
        mae = float(np.abs(mapped.astype(float) - ref.astype(float)).mean())
    else:
        ref_for_mae = np.array(
            Image.fromarray(ref, mode="L").resize(
                (mapped.shape[1], mapped.shape[0]), Image.Resampling.LANCZOS
            )
        )
        mae = float(np.abs(mapped.astype(float) - ref_for_mae.astype(float)).mean())
    print(f"\n    Post-LUT MAE vs Sidexis reference: {mae:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
