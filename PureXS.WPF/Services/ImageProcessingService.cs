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

    public ImageProcessingService()
    {
        var appDir = AppContext.BaseDirectory;
        _decoderScript = Path.Combine(appDir, "decoder", "purexs_decoder_cli.py");
        _pythonPath = ResolvePython(appDir);
    }

    /// <inheritdoc />
    public async Task<byte[]?> ProcessRawScanAsync(byte[] rawBytes, string examType = "Panoramic", CancellationToken ct = default)
    {
        if (_pythonPath is null)
        {
            Debug.WriteLine("[ImageProcessing] No Python found — decoder unavailable");
            return null;
        }

        if (!File.Exists(_decoderScript))
        {
            Debug.WriteLine($"[ImageProcessing] Decoder script not found at {_decoderScript}");
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
                Debug.WriteLine("[ImageProcessing] Failed to start Python process");
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
                Debug.WriteLine("[ImageProcessing] Decoder timed out after 60s");
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
                Debug.WriteLine($"[ImageProcessing] Decoder exited with code {proc.ExitCode}");
                return null;
            }

            if (!File.Exists(outPath))
            {
                Debug.WriteLine("[ImageProcessing] Decoder produced no output file");
                return null;
            }

            return await File.ReadAllBytesAsync(outPath, ct);
        }
        finally
        {
            // TEMP 2026-04-22: preserve raw .bin for pipeline debugging.
            // Revert by uncommenting the delete once a live scan has been captured.
            // try { File.Delete(rawPath); } catch { }
            try { File.Delete(outPath); } catch { }
        }
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
