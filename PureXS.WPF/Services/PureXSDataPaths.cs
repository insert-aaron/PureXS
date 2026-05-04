using System.IO;

namespace PureXS.Services;

/// <summary>
/// Single source of truth for every filesystem path the PureXS WPF app
/// reads or writes. Consolidates what used to be scattered across
/// %TEMP%\PureXS, %APPDATA%\Roaming\PureXS, %LOCALAPPDATA%\PureXS\logs,
/// and %USERPROFILE%\Documents\PureXS\DICOM into one root with named
/// subfolders.
///
/// Default root is %LOCALAPPDATA%\PureXS — not roaming (so big patient
/// .bins don't sync to a domain server) and not %TEMP% (so Windows
/// won't auto-purge patient records).
///
/// Override the root by setting the PUREXS_DATA_DIR environment
/// variable before launch. Useful for facilities that want PureXS
/// data on a non-system drive (e.g. set PUREXS_DATA_DIR=D:\PureXS).
///
/// Layout under Root/:
///   temp/      — intermediate raw .bin and decoded .png handed to the
///                Python decoder; safe to clear at any time
///   logs/      — JSON activity log (purexs_YYYY-MM-DD.log)
///   debug/     — Python decoder diagnostics (hb_decoder.log,
///                last_scan_raw.bin, debug_hole_*.png, flat_field_raw.bin)
///   patients/  — per-patient panoramic PNG + per-scan events.log
///   DICOM/     — DICOM exports
/// </summary>
public static class PureXSDataPaths
{
    private static readonly string _root = ResolveRoot();

    public static string Root => _root;
    public static string Temp => Path.Combine(_root, "temp");
    public static string Logs => Path.Combine(_root, "logs");
    public static string Debug => Path.Combine(_root, "debug");
    public static string Patients => Path.Combine(_root, "patients");
    public static string Dicom => Path.Combine(_root, "DICOM");

    /// <summary>
    /// Ensure every subfolder exists. Cheap (Directory.CreateDirectory
    /// no-ops if present), safe to call repeatedly.
    /// </summary>
    public static void EnsureCreated()
    {
        foreach (var dir in new[] { Root, Temp, Logs, Debug, Patients, Dicom })
        {
            try { Directory.CreateDirectory(dir); }
            catch { /* permission issues surface later when files actually fail to write */ }
        }
    }

    private static string ResolveRoot()
    {
        var envOverride = Environment.GetEnvironmentVariable("PUREXS_DATA_DIR");
        if (!string.IsNullOrWhiteSpace(envOverride))
            return envOverride;

        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        return Path.Combine(local, "PureXS");
    }
}
