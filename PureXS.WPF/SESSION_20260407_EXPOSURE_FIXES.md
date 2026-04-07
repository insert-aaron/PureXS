# Session: WPF Exposure Pipeline Fixes (2026-04-07)

## Problem
After a live exposure on the WPF/.NET app, the UI got stuck on **Phase 2** indefinitely and never produced an image. The Python `purexs_gui.py` worked perfectly for the same exposure flow.

---

## Root Cause Analysis

### Why Phase 2 hung forever
The **Python** version ends the scan data stream via `sock.settimeout(2.0)` — when no data arrives for 2 seconds, a `socket.timeout` exception breaks out of the recv loop. The device simply stops sending data after the scan completes.

The **WPF** version relied solely on detecting a binary marker `E7 14 02` (`PostScanDisconnect`) in the TCP stream. If this marker never arrives (or is split across TCP chunks), `ImageReceived` never fires and Phase 2 hangs forever. Additionally:
- No timeout watchdog existed (Python has a 90-second hard timeout)
- `ReadAsync` doesn't respect `ReceiveTimeout` — it only responds to `CancellationToken`
- If the connection closed during exposure, the buffered data was silently discarded

### Why "Failed to display image" after fixing Phase 2
Once the idle timeout fix allowed Phase 2 to complete, `ProcessAndDisplayImageAsync` tried the Python decoder subprocess — which failed because:
1. The `.csproj` didn't copy the Python decoder files into the build output
2. No `decoder/` directory existed in the build output
3. The fallback tried to create a `BitmapImage` from raw TCP bytes (not a valid image format) — crash

### Why "Upload failed" on Confirm & Send
The PureChart `upload-xray` edge function returns `"success": true` (a JSON boolean), but the C# `GetPropertyOrDefault()` helper called `prop.GetString()` on it, which throws: *"The requested operation requires an element of type 'String', but the target element has type 'True'"*.

### Why Preview/Edit crashed
WPF sliders fire `ValueChanged` during `InitializeComponent()` before `_source` and XAML element references are assigned — causing `NullReferenceException` at line 43 of `ImageEditWindow.xaml.cs`.

---

## Fixes Applied

### 1. Stream-idle timeout in SironaService.cs (main fix)
During exposure, `ReadAsync` now uses a `CancellationToken` with a **2-second timeout** — matching Python's `sock.settimeout(2.0)`. When no data arrives for 2s, the scan stream is considered ended and `CompleteExposure()` fires.

```csharp
using var idleCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
idleCts.CancelAfter(ScanIdleTimeoutMs); // 2000ms
bytesRead = await _stream.ReadAsync(buffer.AsMemory(0, buffer.Length), idleCts.Token);
```

### 2. Hard exposure timeout (90 seconds) in SironaService.cs
`ExposeHardTimeoutAsync()` fires if the exposure hasn't completed after 90s, force-completing with whatever data accumulated. Matches Python's `_on_expose_timeout()`.

### 3. Connection-close fallback in SironaService.cs
If `ReadAsync` returns 0 (connection closed) or throws during exposure, the accumulated buffer is delivered via `ImageReceived` instead of being silently discarded.

### 4. `CompleteExposure()` unified handler
All three completion paths (idle timeout, hard timeout, PostScanDisconnect marker) converge through `CompleteExposure()` which sends `IMAGE_ACK`, fires `ImageReceived`, and reconnects.

### 5. Python decoder files bundled in .csproj
Added `<None Include>` entries to copy `purexs_decoder_cli.py`, `hb_decoder.py`, `utils.py`, and `dicom_export.py` into `decoder/` in the build output:

```xml
<None Include="..\purexs_decoder_cli.py" Link="decoder\purexs_decoder_cli.py">
  <CopyToOutputDirectory>PreserveNewest</CopyToOutputDirectory>
</None>
```

### 6. Scanline fallback image in MainViewModel.cs
When the Python decoder is unavailable, `BuildImageFromScanlines()` constructs a proper grayscale image from the live-parsed `_scanlines` accumulated during Phase 2 — percentile stretch (2nd-98th) + invert (MONOCHROME1). This is the C# equivalent of Python's basic `reconstruct_image()` path.

### 7. JSON type handling in PureChartService.cs
Replaced `GetPropertyOrDefault()` (string-only) with:
- `GetBoolOrDefault()` — handles `"success"` as boolean or string
- `GetStringOrDefault()` — handles mixed JSON value types gracefully

### 8. Slider guard in ImageEditWindow.xaml.cs
Added `if (!IsLoaded) return;` at the top of `OnSliderChanged()` to skip slider events fired during `InitializeComponent()` before the window is fully initialized.

---

## Files Modified

| File | Changes |
|------|---------|
| `PureXS.WPF/Services/SironaService.cs` | Stream-idle timeout, hard timeout, `CompleteExposure()`, connection-close fallback |
| `PureXS.WPF/ViewModels/MainViewModel.cs` | `ProcessAndDisplayImageAsync` 3-path fallback, `BuildImageFromScanlines()` |
| `PureXS.WPF/PureXS.WPF.csproj` | Bundle Python decoder files into `decoder/` output |
| `PureXS.WPF/Services/PureChartService.cs` | `GetBoolOrDefault()` + `GetStringOrDefault()` for mixed JSON types |
| `PureXS.WPF/Views/ImageEditWindow.xaml.cs` | `IsLoaded` guard on `OnSliderChanged` |

---

## Python vs WPF Exposure Flow Comparison

### Python (purexs_gui.py) — working
```
EXPOSE button → _start_direct_expose() → arm_for_expose() [bg thread]
  → CAPS_REQ + DATA_SEND + patient info → DATA_ACK
  → Device ARMED → physical button pressed
  → _recv_scan_data() tight loop with sock.settimeout(2.0)
  → 2s timeout breaks loop → extract scanlines → fire events
  → _on_expose_complete() → _stitch_panoramic() [bg thread]
  → reconstruct_image() full pipeline → _display_pil_image()
```

### WPF (SironaService.cs) — now matching
```
EXPOSE button → ExposeAsync() → ArmForExposeAsync()
  → CAPS_REQ + DATA_SEND + patient info → DATA_ACK
  → Device ARMED → physical button pressed
  → ReaderLoopAsync with 2s idle CancellationToken timeout
  → Timeout/marker/close → CompleteExposure()
  → IMAGE_ACK → ImageReceived event → ProcessAndDisplayImageAsync
  → Python decoder CLI (or scanline fallback) → display BitmapSource
```

---

## Key Architecture Notes

- **Scan stream end detection**: The Orthophos XG simply stops sending TCP data after the scan. There is no guaranteed end-of-stream marker. Python detects this via socket timeout; WPF now uses CancellationToken timeout on ReadAsync.
- **PostScanDisconnect (E7 14 02)**: Sometimes sent, sometimes not. Kept as an additional detection path but no longer the only one.
- **Image dimensions**: 2706 columns x 1316 rows (Orthophos XG DX41), ~7MB raw scan data.
- **Reconnect after exposure**: Expected behavior — the device disconnects post-scan and the app reconnects for the next exposure. Both Python and WPF do this.
