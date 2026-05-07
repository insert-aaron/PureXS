"""
Build a 3-scan median-averaged tone LUT for the current facility's Sirona unit.

Inputs (3 matched pairs, raw .bin → Sidexis .tif of the same scan):
    aaron_bin.bin       ↔ aaron_tiff.tif
    fani_bin.bin        ↔ fani.tif
    myraim_bin.bin      ↔ myriam_mes_tiff.tif

Process:
    1. Run each .bin through clean-mode reconstruct_image() → 8-bit greyscale
    2. For each pair: histogram-match LUT (source=ours, target=sidexis)
    3. Median element-wise across the 3 LUTs (outlier-resistant)
    4. Save facility_X_tone_lut_avg3.npy

Then a held-out generalization check:
    5. Apply averaged LUT to aaron_test_10.png
    6. Save 3-panel side-by-side and dump P10/P50/P90 deltas

If the over-darkening signature from the single-pair test is gone, the
LUT generalizes and we wire it into the pipeline. If it persists, we
need either more scans or a different averaging method (mean of pooled
histograms instead of median of per-pair LUTs).

Run from the repo root after editing the PAIRS list below to point at
your facility's three matched .bin/.tif pairs:

    /opt/homebrew/bin/python3 PureXS/tools/calibration/build_lut_3pair_median.py

If all three per-pair LUTs collapse to identity (max delta ≤ 1, mean
≈ 0.0), the unit doesn't need a LUT — the clean-mode pipeline already
matches Sidexis tonally on that hardware. That was the outcome on the
Facility X / 2026-05-07 calibration (see facility_x_no_lut_needed memo).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Layout: PureXS/tools/calibration/build_lut_3pair_median.py
# parents[0]=calibration/, [1]=tools/, [2]=PureXS/, [3]=workspace root
HERE = Path(__file__).resolve()
PUREXS_PKG = HERE.parents[2]          # the PureXS/ Python package
WORKSPACE  = HERE.parents[3]          # parent of the repo, holds .bin/.tif
sys.path.insert(0, str(PUREXS_PKG))

from hb_decoder import _extract_panoramic, reconstruct_image  # noqa: E402

# Matched pairs: (label, .bin path, .tif path). Both files in each pair
# must come from the SAME scan (timestamp-matched within seconds), or
# the LUT encodes anatomical differences as calibration drift.
PAIRS: list[tuple[str, Path, Path]] = [
    ("aaron",   WORKSPACE / "aaron_bin.bin",   WORKSPACE / "aaron_tiff.tif"),
    ("fani",    WORKSPACE / "fani_bin.bin",    WORKSPACE / "fani.tif"),
    ("myriam",  WORKSPACE / "myraim_bin.bin",  WORKSPACE / "myriam_mes_tiff.tif"),
]

OUT_LUT      = WORKSPACE / "facility_X_tone_lut_avg3.npy"
OUT_GEN_PNG  = WORKSPACE / "facility_X_avg3_lut_test_aaron10.png"
OUT_GEN_PANEL = WORKSPACE / "facility_X_avg3_lut_test_aaron10_3panel.png"
GEN_INPUT    = WORKSPACE / "aaron_test_10.png"
GEN_REF_TIF  = WORKSPACE / "aaron_tiff.tif"  # tonal-context only


def histogram_match_lut(src: np.ndarray, tgt: np.ndarray) -> np.ndarray:
    """256-entry uint8 LUT mapping src histogram → tgt histogram."""
    src_hist = np.bincount(src.ravel(), minlength=256).astype(np.float64)
    tgt_hist = np.bincount(tgt.ravel(), minlength=256).astype(np.float64)
    src_cdf = src_hist.cumsum() / max(src_hist.sum(), 1.0)
    tgt_cdf = tgt_hist.cumsum() / max(tgt_hist.sum(), 1.0)
    lut = np.zeros(256, dtype=np.uint8)
    for v in range(256):
        u = int(np.searchsorted(tgt_cdf, src_cdf[v]))
        lut[v] = min(u, 255)
    return lut


def pipeline_8bit(bin_path: Path) -> np.ndarray | None:
    """Run our clean-mode pipeline on a .bin and return 8-bit grayscale."""
    raw = bin_path.read_bytes()
    result = _extract_panoramic(raw)
    scanlines, repair_mask = (result if isinstance(result, tuple) else (result, None))
    if not scanlines:
        return None
    pil = reconstruct_image(scanlines, invert=True, repair_mask=repair_mask)
    if pil is None:
        return None
    return np.array(pil.convert("L"))


def main() -> int:
    # Sanity-check inputs
    for label, b, t in PAIRS:
        if not b.exists() or not t.exists():
            print(f"ERROR: missing file for pair {label!r} ({b.name}, {t.name})")
            return 1

    luts: list[np.ndarray] = []
    print("Building per-pair LUTs ...")
    for label, bin_path, tif_path in PAIRS:
        print(f"\n[{label}] Pipeline on {bin_path.name}")
        ours = pipeline_8bit(bin_path)
        if ours is None:
            print(f"[{label}] ERROR: pipeline returned None")
            return 1
        ref = np.array(Image.open(tif_path).convert("L"))
        print(f"[{label}]   ours: {ours.shape},  ref: {ref.shape}")
        lut = histogram_match_lut(ours, ref)
        luts.append(lut)
        ident = lut.astype(int) - np.arange(256)
        print(f"[{label}]   LUT identity-deltas: "
              f"min={int(ident.min())}, max={int(ident.max())}, "
              f"mean={float(ident.mean()):+.2f}")

    # Element-wise median across the 3 LUTs
    stacked = np.stack(luts, axis=0)  # shape (3, 256)
    avg_lut = np.median(stacked, axis=0).astype(np.uint8)
    np.save(OUT_LUT, avg_lut)
    avg_ident = avg_lut.astype(int) - np.arange(256)
    print()
    print("=" * 60)
    print(f"Median-averaged LUT saved: {OUT_LUT.name}")
    print(f"  identity-deltas: min={int(avg_ident.min())}, "
          f"max={int(avg_ident.max())}, mean={float(avg_ident.mean()):+.2f}")
    print("  per-grey-bin spread (max-min across the 3 LUTs):")
    spread = stacked.max(axis=0).astype(int) - stacked.min(axis=0).astype(int)
    print(f"    P50 spread={int(np.median(spread))}, "
          f"P95 spread={int(np.percentile(spread, 95))}, "
          f"max spread={int(spread.max())}")
    print("=" * 60)

    # ── Held-out generalization test on aaron_test_10.png ────────────
    if not GEN_INPUT.exists():
        print(f"\nWARNING: {GEN_INPUT.name} not found — skipping generalization test")
        return 0

    print(f"\nGeneralization test: applying avg-LUT to {GEN_INPUT.name}")
    img = np.array(Image.open(GEN_INPUT).convert("L"))
    mapped = avg_lut[img]
    Image.fromarray(mapped, mode="L").save(OUT_GEN_PNG)

    p10b, p50b, p90b = np.percentile(img, [10, 50, 90])
    p10a, p50a, p90a = np.percentile(mapped, [10, 50, 90])
    print(f"  Mean grey: {img.mean():.1f} → {mapped.mean():.1f}  "
          f"(delta={mapped.mean() - img.mean():+.1f})")
    print(f"  P10:  {int(p10b)} → {int(p10a)}  (delta={int(p10a - p10b):+d})")
    print(f"  P50:  {int(p50b)} → {int(p50a)}  (delta={int(p50a - p50b):+d}) "
          "← was -56 with single-scan LUT")
    print(f"  P90:  {int(p90b)} → {int(p90a)}  (delta={int(p90a - p90b):+d})")

    # 3-panel: original | after avg LUT | sidexis tonal reference
    if GEN_REF_TIF.exists():
        ref = np.array(Image.open(GEN_REF_TIF).convert("L"))
        h = img.shape[0]
        if ref.shape[0] != h:
            ref = np.array(
                Image.fromarray(ref, mode="L").resize(
                    (ref.shape[1] * h // ref.shape[0], h),
                    Image.Resampling.LANCZOS,
                )
            )
        gap = 24
        panels = [img, mapped, ref]
        widths = [p.shape[1] for p in panels]
        total_w = sum(widths) + gap * (len(panels) - 1)
        canvas = np.full((h, total_w), 255, dtype=np.uint8)
        x = 0
        for p in panels:
            canvas[:, x:x + p.shape[1]] = p
            x += p.shape[1] + gap
        Image.fromarray(canvas, mode="L").save(OUT_GEN_PANEL)
        print(f"\n3-panel saved: {OUT_GEN_PANEL.name}  "
              "(left=original, middle=avg-LUT, right=Sidexis ref)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
