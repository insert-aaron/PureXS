using System.Diagnostics;
using System.IO;

namespace PureXS.Services;

/// <summary>
/// Calls the Python decoder (shipped as .py files in the decoder/ subdirectory)
/// to process raw Orthophos scan bytes into a finished panoramic PNG.
///
/// The decoder directory sits next to PureXS.exe:
///   PureXS.exe
///   decoder/
///     purexs_decoder_cli.py
///     hb_decoder.py
///     utils.py
///     ...
///
/// Python is found via (in order):
///   1. PUREXS_PYTHON env var (set by SetupAndRun.bat)
///   2. Embedded python at {install_dir}\python\python.exe
///   3. System "python" on PATH
/// </summary>
public sealed class ImageProcessingService : IImageProcessingService
{
    private readonly string _decoderScript;
    private readonly string? _pythonPath;
    private readonly IEventLogService? _log;
    private readonly IConfigService? _config;

    public ImageProcessingService(IEventLogService? log = null, IConfigService? config = null)
    {
        var appDir = AppContext.BaseDirectory;
        _decoderScript = Path.Combine(appDir, "decoder", "purexs_decoder_cli.py");
        _pythonPath = ResolvePython(appDir);
        _log = log;
        _config = config;
    }

    /// <inheritdoc />
    public async Task<ProcessedScan?> ProcessRawScanAsync(byte[] rawBytes, string examType = "Panoramic", CancellationToken ct = default)
    {
        if (_pythonPath is null)
        {
            const string msg = "Decoder unavailable: Python interpreter not found. " +
                               "Run SetupAndRun.bat from the install directory to install " +
                               "the embedded Python and decoder dependencies. The image will " +
                               "fall back to a low-resolution scanline preview.";
            Debug.WriteLine($"[ImageProcessing] {msg}");
            _log?.Log(msg, "warning");
            return null;
        }

        if (!File.Exists(_decoderScript))
        {
            var msg = $"Decoder script missing at {_decoderScript}. " +
                      "The decoder/ folder appears to be missing from the install — " +
                      "redeploy or rerun SetupAndRun.bat. Falling back to scanline preview.";
            Debug.WriteLine($"[ImageProcessing] {msg}");
            _log?.Log(msg, "error");
            return null;
        }

        // Was %TEMP%\PureXS — moved into the consolidated layout under
        // %LOCALAPPDATA%\PureXS\temp so all PureXS files live in one
        // root. Same scratch semantics: TrimScanHistory keeps only the
        // last 5 .bin/.png pairs.
        var tempDir = PureXSDataPaths.Temp;
        Directory.CreateDirectory(tempDir);
        var rawPath = Path.Combine(tempDir, $"scan_{DateTime.Now:yyyyMMdd_HHmmss}.bin");
        var outPath = Path.ChangeExtension(rawPath, ".png");
        var tifPath = Path.ChangeExtension(rawPath, ".tif");

        // Decoder writes a parallel TIFF only when the facility opted in
        // via config flag — the file is ~5× the PNG, so off by default.
        var saveTif = _config?.SaveTifExport ?? false;
        var tifFlag = saveTif ? " --save-tif" : string.Empty;

        try
        {
            await File.WriteAllBytesAsync(rawPath, rawBytes, ct);

            var psi = new ProcessStartInfo
            {
                FileName = _pythonPath,
                Arguments = $"\"{_decoderScript}\" --input \"{rawPath}\" --output \"{outPath}\" --exam-type \"{examType}\"{tifFlag}",
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                WorkingDirectory = Path.GetDirectoryName(_decoderScript) ?? appDir,
            };

            using var proc = Process.Start(psi);
            if (proc is null)
            {
                var msg = $"Failed to start Python process at {_pythonPath}. " +
                          "Check that the interpreter has execute permission and " +
                          "isn't blocked by antivirus. Falling back to scanline preview.";
                Debug.WriteLine($"[ImageProcessing] {msg}");
                _log?.Log(msg, "error");
                return null;
            }

            var stdout = proc.StandardOutput.ReadToEndAsync(ct);
            var stderr = proc.StandardError.ReadToEndAsync(ct);

            // Timeout after 60 seconds — reconstruction is heavy
            using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeoutCts.CancelAfter(TimeSpan.FromSeconds(60));

            try
            {
                await proc.WaitForExitAsync(timeoutCts.Token);
            }
            catch (OperationCanceledException)
            {
                proc.Kill(entireProcessTree: true);
                const string msg = "Decoder timed out after 60s and was killed. " +
                                   "Falling back to scanline preview.";
                Debug.WriteLine($"[ImageProcessing] {msg}");
                _log?.Log(msg, "warning");
                return null;
            }

            var stdoutText = await stdout;
            var stderrText = await stderr;

            if (!string.IsNullOrWhiteSpace(stdoutText))
                Debug.WriteLine($"[ImageProcessing] stdout: {stdoutText}");
            if (!string.IsNullOrWhiteSpace(stderrText))
                Debug.WriteLine($"[ImageProcessing] stderr: {stderrText}");

            // Per-scan fleet telemetry — one line per scan (success OR refused)
            // in <root>\logs\scan_telemetry.jsonl, attributed to the unit, so the
            // misalignment rate per machine can be computed from one place.
            var columns = ParseInt(stderrText, "COLUMNS");
            var phaseErr = ParseInt(stderrText, "PHASE_ERR");
            var sharpness = ParseDouble(stderrText, "SHARPNESS");
            var isBlurry = ParseInt(stderrText, "BLURRY") == 1;
            WriteTelemetry(examType, columns, phaseErr, sharpness, isBlurry, proc.ExitCode switch
            {
                0 => "ok",
                2 => "incomplete",
                3 => "detector_mismatch",
                _ => "error",
            });

            if (proc.ExitCode != 0)
            {
                // Tail of stderr usually contains the Python traceback's last
                // line, which is the most useful single fact about why decode
                // failed (missing module, bad bytes, etc.).
                var tail = (stderrText ?? string.Empty).TrimEnd();
                if (tail.Length > 240) tail = "..." + tail[^240..];

                // Exit code 2 means the decoder refused to reconstruct because
                // the scan was truncated (device aborted mid-sweep). Surface a
                // retake prompt instead of the generic decoder-failed warning,
                // and skip the scanline-preview fallback — the same shortfall
                // that breaks reconstruction breaks the preview too.
                if (proc.ExitCode == 2)
                {
                    var retake = ExtractDecoderMessage(stderrText, "INCOMPLETE_SCAN:")
                                 ?? "Scan incomplete — please retake.";
                    _log?.Log($"Decoder refused: {retake}", "warning");
                    throw new ScanIncompleteException(retake);
                }

                // Exit code 3: the unit's detector geometry doesn't match the
                // Orthophos XG the pipeline targets. Not retakeable — fail loud
                // with an "unsupported unit" error instead of a garbage image.
                if (proc.ExitCode == 3)
                {
                    var mismatch = ExtractDecoderMessage(stderrText, "DETECTOR_MISMATCH:")
                                   ?? "Unsupported detector geometry on this unit.";
                    _log?.Log($"Decoder refused — detector mismatch: {mismatch}", "error");
                    throw new DetectorMismatchException(mismatch);
                }

                var msg = $"Decoder exited with code {proc.ExitCode}. " +
                          $"Falling back to scanline preview. stderr tail: {tail}";
                Debug.WriteLine($"[ImageProcessing] {msg}");
                _log?.Log(msg, "warning");
                return null;
            }

            if (!File.Exists(outPath))
            {
                const string msg = "Decoder finished cleanly but produced no PNG. " +
                                   "Falling back to scanline preview.";
                Debug.WriteLine($"[ImageProcessing] {msg}");
                _log?.Log(msg, "warning");
                return null;
            }

            var pngBytes = await File.ReadAllBytesAsync(outPath, ct);

            // Only report a TIF source path if the flag was set AND the
            // decoder actually produced the file — guards against silent
            // pillow/format failures inside the subprocess.
            string? tifSource = null;
            if (saveTif && File.Exists(tifPath))
            {
                tifSource = tifPath;
            }
            else if (saveTif)
            {
                _log?.Log(
                    $"TIF export requested but decoder produced no TIF at {tifPath}",
                    "warning");
            }

            // columns / phaseErr / sharpness were parsed above (for telemetry)
            // and are reused here for the returned scan.
            return new ProcessedScan(pngBytes, tifSource, columns, phaseErr, sharpness, isBlurry);
        }
        finally
        {
            TrimScanHistory(tempDir, keepMostRecent: 5);
        }
    }

    /// <summary>
    /// Parses an operator-facing message out of the decoder's stderr following a
    /// known marker (e.g. "INCOMPLETE_SCAN:" for exit 2, "DETECTOR_MISMATCH:"
    /// for exit 3). The CLI emits a line like "ERROR &lt;marker&gt; &lt;message&gt;".
    /// Returns null if the marker isn't found, leaving the caller to use a
    /// generic fallback message.
    /// </summary>
    /// <summary>
    /// Appends one JSON line to <c>&lt;root&gt;\logs\scan_telemetry.jsonl</c> for
    /// every scan (success or refused), tagged with the unit, so misalignment
    /// rate per machine = count(outcome="misaligned") / total. Best-effort —
    /// never throws into the scan flow.
    /// </summary>
    private void WriteTelemetry(string examType, int columns, int phaseErr, double sharpness, bool isBlurry, string outcome)
    {
        try
        {
            var dir = PureXSDataPaths.Logs;
            Directory.CreateDirectory(dir);
            var line = System.Text.Json.JsonSerializer.Serialize(new
            {
                ts = DateTime.Now.ToString("yyyy-MM-ddTHH:mm:ss"),
                unit_id = _config?.UnitId ?? Environment.MachineName,
                device_host = _config?.SironaHost,
                exam = examType,
                phase_err = phaseErr,
                columns,
                sharpness = Math.Round(sharpness, 1),
                blurry = isBlurry,
                outcome,
            });
            File.AppendAllText(Path.Combine(dir, "scan_telemetry.jsonl"), line + "\n");
        }
        catch (Exception ex)
        {
            Debug.WriteLine($"[ImageProcessing] telemetry write failed: {ex.Message}");
        }
    }

    /// <summary>
    /// Parses an integer the decoder emitted as "&lt;key&gt;=&lt;n&gt;" on stderr
    /// (e.g. COLUMNS, PHASE_ERR). Returns 0 if not present.
    /// </summary>
    private static int ParseInt(string? stderr, string key)
    {
        if (string.IsNullOrEmpty(stderr)) return 0;
        var m = System.Text.RegularExpressions.Regex.Match(stderr, key + @"=(\d+)");
        return m.Success && int.TryParse(m.Groups[1].Value, out var n) ? n : 0;
    }

    private static double ParseDouble(string? stderr, string key)
    {
        if (string.IsNullOrEmpty(stderr)) return 0;
        var m = System.Text.RegularExpressions.Regex.Match(stderr, key + @"=([\d.]+)");
        return m.Success && double.TryParse(m.Groups[1].Value,
            System.Globalization.CultureInfo.InvariantCulture, out var n) ? n : 0;
    }

    private static string? ExtractDecoderMessage(string? stderr, string marker)
    {
        if (string.IsNullOrEmpty(stderr)) return null;
        var idx = stderr.IndexOf(marker, StringComparison.Ordinal);
        if (idx < 0) return null;
        var tail = stderr[(idx + marker.Length)..].TrimStart();
        var newline = tail.IndexOfAny(new[] { '\r', '\n' });
        if (newline >= 0) tail = tail[..newline];
        return string.IsNullOrWhiteSpace(tail) ? null : tail.Trim();
    }

    private static void TrimScanHistory(string tempDir, int keepMostRecent)
    {
        try
        {
            var bins = new DirectoryInfo(tempDir)
                .GetFiles("scan_*.bin")
                .OrderByDescending(f => f.LastWriteTimeUtc)
                .Skip(keepMostRecent);

            foreach (var bin in bins)
            {
                try { bin.Delete(); } catch { }
                var png = Path.ChangeExtension(bin.FullName, ".png");
                try { if (File.Exists(png)) File.Delete(png); } catch { }
                var tif = Path.ChangeExtension(bin.FullName, ".tif");
                try { if (File.Exists(tif)) File.Delete(tif); } catch { }
            }
        }
        catch { }
    }

    private static string appDir => AppContext.BaseDirectory;

    /// <summary>
    /// Finds a usable Python interpreter.
    /// </summary>
    private static string? ResolvePython(string appDir)
    {
        // 1. PUREXS_PYTHON env var (set by SetupAndRun.bat)
        var envPython = Environment.GetEnvironmentVariable("PUREXS_PYTHON");
        if (!string.IsNullOrEmpty(envPython) && File.Exists(envPython))
            return envPython;

        // 2. Embedded Python next to the install dir
        //    SetupAndRun.bat installs to {install_dir}\python\python.exe
        var installDir = Path.GetDirectoryName(appDir) ?? appDir;
        var embeddedPython = Path.Combine(installDir, "python", "python.exe");
        if (File.Exists(embeddedPython))
            return embeddedPython;

        // Also check one level up (in case appDir has trailing separator)
        embeddedPython = Path.Combine(appDir, "..", "python", "python.exe");
        if (File.Exists(embeddedPython))
            return Path.GetFullPath(embeddedPython);

        // 3. System Python on PATH
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = "python",
                Arguments = "--version",
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
            };
            using var proc = Process.Start(psi);
            if (proc is not null)
            {
                proc.WaitForExit(3000);
                if (proc.ExitCode == 0)
                    return "python";
            }
        }
        catch { }

        return null;
    }
}
