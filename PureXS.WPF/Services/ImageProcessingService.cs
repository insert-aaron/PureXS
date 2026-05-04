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

    public ImageProcessingService(IEventLogService? log = null)
    {
        var appDir = AppContext.BaseDirectory;
        _decoderScript = Path.Combine(appDir, "decoder", "purexs_decoder_cli.py");
        _pythonPath = ResolvePython(appDir);
        _log = log;
    }

    /// <inheritdoc />
    public async Task<byte[]?> ProcessRawScanAsync(byte[] rawBytes, string examType = "Panoramic", CancellationToken ct = default)
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

        var tempDir = Path.Combine(Path.GetTempPath(), "PureXS");
        Directory.CreateDirectory(tempDir);
        var rawPath = Path.Combine(tempDir, $"scan_{DateTime.Now:yyyyMMdd_HHmmss}.bin");
        var outPath = Path.ChangeExtension(rawPath, ".png");

        try
        {
            await File.WriteAllBytesAsync(rawPath, rawBytes, ct);

            var psi = new ProcessStartInfo
            {
                FileName = _pythonPath,
                Arguments = $"\"{_decoderScript}\" --input \"{rawPath}\" --output \"{outPath}\" --exam-type \"{examType}\"",
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

            if (proc.ExitCode != 0)
            {
                // Tail of stderr usually contains the Python traceback's last
                // line, which is the most useful single fact about why decode
                // failed (missing module, bad bytes, etc.).
                var tail = (stderrText ?? string.Empty).TrimEnd();
                if (tail.Length > 240) tail = "..." + tail[^240..];
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

            return await File.ReadAllBytesAsync(outPath, ct);
        }
        finally
        {
            TrimScanHistory(tempDir, keepMostRecent: 5);
        }
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
