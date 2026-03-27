import re

with open("hb_decoder.py", "r") as f:
    text = f.read()

# 1. Insert helper functions and DEBUG_FILL before _calibration_driven_fill
helpers = """
DEBUG_FILL = True
_debug_holes_count = 0

def _safe_dose_sample(segment: bytearray, start_idx: int, direction: int, max_steps: int = 20) -> tuple[int | None, bool]:
    pixels = []
    idx = start_idx
    step_bytes = direction * 2
    for _ in range(max_steps):
        if idx < 0 or idx + 1 >= len(segment):
            break
        val = (segment[idx] << 8) | segment[idx + 1]
        pixels.append(val)
        idx += step_bytes
        
    if not pixels:
        return None, False
        
    median_val = __import__('numpy').median(pixels)
    walk_triggered = False
    for i, p in enumerate(pixels):
        if abs(p - median_val) / max(median_val, 1) < 0.15:
            if i > 0: walk_triggered = True
            return p, walk_triggered
    return int(median_val), True

def _detect_hole_column(global_byte_offset: int, img_height: int) -> int:
    return global_byte_offset // (img_height * 2)

def _validate_calibration_alignment(ff_shape, predicted) -> bool:
    import numpy as np
    import logging
    log = logging.getLogger(__name__)
    if np.std(ff_shape) > 1e-5 and np.std(predicted) > 1e-5:
        corr = np.corrcoef(ff_shape, predicted)[0, 1]
        if corr < 0.0:
            log.warning("Calibration validation warning: correlation %.2f is structurally oppositional, using linear fallback", corr)
            return False
    return True

def _calibration_driven_fill(
    block_start: int,
    telem: dict,
    segment: bytearray,
    ff2d,
    ff2d_mean: float,
    segment_row_offset: int = 0,
    segment_col_offset: int = 0,
) -> 'np.ndarray | None':
    import logging
    import numpy as np
    log = logging.getLogger(__name__)
    global _debug_holes_count
    
    TELEM_SIZE   = 72
    TELEM_PIXELS = 36
    bs = block_start
    be = bs + TELEM_SIZE

    val_top, walk_top = _safe_dose_sample(segment, bs - 2, -1, max_steps=20)
    val_bot, walk_bot = _safe_dose_sample(segment, be, 1, max_steps=20)
    
    val_top = float(val_top) if val_top is not None else float(val_bot or 0.0)
    val_bot = float(val_bot) if val_bot is not None else float(val_top or 0.0)
    if val_top == 0.0 and val_bot == 0.0:
        return None

    t_arr = np.linspace(1.0 / (TELEM_PIXELS + 1), TELEM_PIXELS / (TELEM_PIXELS + 1),
                        TELEM_PIXELS, dtype=np.float32)
    predicted = val_top * (1.0 - t_arr) + val_bot * t_arr

    first_px  = bs // 2
    first_row = (segment_row_offset + first_px) % 1316
    row_indices = [(first_row + j) % 1316 for j in range(TELEM_PIXELS)]
    
    global_byte_offset = (segment_col_offset * 1316 * 2) + bs
    exact_col_idx = _detect_hole_column(global_byte_offset, 1316)

    predicted_warped = predicted.copy()
    if ff2d is not None and len(ff2d) > max(row_indices):
        col_idx = min(max(exact_col_idx, 0), ff2d.shape[1] - 1)
        ff_shape = np.array([ff2d[r, col_idx] for r in row_indices], dtype=np.float32)
        
        ff_trend = np.linspace(ff_shape[0], ff_shape[-1], len(ff_shape), dtype=np.float32)
        ff_trend = np.maximum(ff_trend, 1.0)
        ff_texture = ff_shape / ff_trend
        
        predicted_warped = predicted_warped * ff_texture
        
        if not _validate_calibration_alignment(ff_shape, predicted_warped):
            pass
        else:
            predicted = predicted_warped
            
    if DEBUG_FILL and _debug_holes_count < 5:
        log.info(f"--- DEBUG_FILL HOLE {_debug_holes_count+1} ---")
        log.info(f"Target Column: {exact_col_idx} (from global byte offset: {global_byte_offset})")
        log.info(f"Dose Bounds: top={val_top:.0f} bot={val_bot:.0f} (Walk top={walk_top}, Walk bot={walk_bot})")
        log.info(f"Row Indices (target patch): {row_indices[0]} to {row_indices[-1]}")
        
        import os
        from PIL import Image
        flank_extract = 20
        raw_pixels = []
        for i in range(max(0, bs - flank_extract*2), min(len(segment), be + flank_extract*2), 2):
            raw_pixels.append((segment[i] << 8) | segment[i+1])
            
        filled_pixels = list(raw_pixels)
        patch_offset = (bs - max(0, bs - flank_extract*2)) // 2
        for i, p in enumerate(predicted):
            filled_pixels[patch_offset + i] = int(p)
            
        max_p = max(raw_pixels + filled_pixels + [1])
        raw_arr = (np.array(raw_pixels) / max_p * 255).astype(np.uint8)
        fill_arr = (np.array(filled_pixels) / max_p * 255).astype(np.uint8)
        
        img_arr = np.column_stack([np.tile(raw_arr, (50, 1)).T, np.zeros((len(raw_arr), 10), dtype=np.uint8), np.tile(fill_arr, (50, 1)).T])
        Image.fromarray(img_arr).save(f"/tmp/debug_hole_{_debug_holes_count+1}.png")
        log.info(f"Saved /tmp/debug_hole_{_debug_holes_count+1}.png")
        
        _debug_holes_count += 1

    return predicted\n\n"""

# Regex replacement
text = re.sub(r'def _calibration_driven_fill\(.*?return predicted\n+', helpers, text, flags=re.DOTALL)

# Modify _repair_inline_telemetry signature
text = text.replace('segment_row_offset: int = 0,\n) -> bytearray', 'segment_row_offset: int = 0,\n    segment_col_offset: int = 0,\n) -> bytearray')
text = text.replace('segment_row_offset=segment_row_offset\n            )', 'segment_row_offset=segment_row_offset,\n                segment_col_offset=segment_col_offset,\n            )')

# Modify _extract_panoramic calls
call1_old = """segment_row_offset = (len(clean) // 2) % 1316
            repaired, block_positions = _repair_inline_telemetry(
                segment, return_positions=True, segment_row_offset=segment_row_offset
            )"""
call1_new = """segment_row_offset = (len(clean) // 2) % 1316
            segment_col_offset = (len(clean) // 2) // 1316
            repaired, block_positions = _repair_inline_telemetry(
                segment, return_positions=True, 
                segment_row_offset=segment_row_offset,
                segment_col_offset=segment_col_offset
            )"""
text = text.replace(call1_old, call1_new)

call2_old = """repaired, block_positions = _repair_inline_telemetry(
            segment, return_positions=True,
        )"""
call2_new = """segment_row_offset = (len(clean) // 2) % 1316
        segment_col_offset = (len(clean) // 2) // 1316
        repaired, block_positions = _repair_inline_telemetry(
            segment, return_positions=True,
            segment_row_offset=segment_row_offset,
            segment_col_offset=segment_col_offset
        )"""
text = text.replace(call2_old, call2_new)

# Add assertion for 2D failure
assert_code = """    # BUG 1 FIX: Ensure 2D repair logic is COMPLETELY terminated!
    if hasattr(sys, '_CALLED_2D_REPAIR') and sys._CALLED_2D_REPAIR:
        raise RuntimeError("FATAL OVERLAP: Legacy 2D spatial block-copy algorithm triggered!")
"""
text = text.replace("img_2d = img_array.T.astype(np.float32)  # (height, width)", assert_code + "\n    img_2d = img_array.T.astype(np.float32)  # (height, width)")

with open("hb_decoder.py", "w") as f:
    f.write(text)
print("Patched!")
