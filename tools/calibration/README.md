# Per-device tone-LUT calibration utilities

These scripts build a histogram-matched 256-entry tone LUT that pulls
PureXS clean-mode output toward Sidexis's tonal signature for a specific
Sirona Orthophos unit. They're **not part of the runtime pipeline** —
they're one-off tools you run once per physical detector to decide
whether that unit needs a LUT and, if so, what the LUT should look like.

## When to use these

A facility's panoramic scans look noticeably different from their
Sidexis reference output even after the standard pipeline. Different
physical Orthophos units can have slightly different gain/dark
characteristics, so what looks "calibrated" on one unit may look
"washed out" on another.

## Workflow

1. **Capture matched pairs.** Have the operator scan three patients on
   the Sirona unit. For each scan:
   - Save the raw `.bin` from PureXS (enable
     `save_tif_export: true` in `config.json` — this also writes a
     parallel `.tif` for free).
   - Process the same exposure through Sidexis and export its TIFF.

   The `.bin` and Sidexis `.tif` must come from the **same scan**.
   File timestamps within seconds. Mismatched pairs (different patients
   or different days) make the LUT encode anatomical differences as
   calibration drift.

2. **Drop all six files in the workspace root** (one level above the
   `PureXS/` repo) and edit the `PAIRS` list at the top of
   `build_lut_3pair_median.py` to point at them.

3. **Run the median-3 builder:**
   ```
   /opt/homebrew/bin/python3 PureXS/tools/calibration/build_lut_3pair_median.py
   ```

4. **Read the per-pair identity-deltas.** If all three pairs report
   `min=0, max≤1, mean≈0.0`, the unit doesn't need a LUT — the
   pipeline already matches Sidexis. Stop here.

5. If the deltas are non-trivial, the printed median-averaged LUT and
   its generalization test (against `aaron_test_10.png`) tell you
   whether deploying it is safe. The single-pair script
   (`build_lut_single_pair.py`) is intentionally less robust — keep it
   for diagnostic comparisons, not for production.

## Files produced (in workspace root, not committed)

| File | What |
|---|---|
| `facility_X_tone_lut.npy` | Single-pair LUT (overfit-prone) |
| `facility_X_tone_lut_avg3.npy` | Median of 3 per-pair LUTs |
| `facility_X_purexs_*.png` | Pre/post-LUT preview |
| `facility_X_lut_sidebyside.png` | Visual L/R comparison |
| `facility_X_avg3_lut_test_aaron10*.png` | Held-out generalization check |

## Reference: 2026-05-07 Facility X result

3-pair median-averaged LUT collapsed to identity (max delta=1,
mean=+0.05). No LUT deployed. See `memory/facility_x_no_lut_needed.md`.
