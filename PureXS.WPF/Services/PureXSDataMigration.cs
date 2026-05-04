using System.Diagnostics;
using System.IO;

namespace PureXS.Services;

/// <summary>
/// One-time migration of pre-consolidation data into the new
/// %LOCALAPPDATA%\PureXS\ layout. Runs once on app startup; a marker
/// file inside Root prevents re-execution.
///
/// Migrates:
///   %APPDATA%\Roaming\PureXS\patients\          → Root\patients\
///   %USERPROFILE%\Documents\PureXS\DICOM\       → Root\DICOM\
///   %APPDATA%\Roaming\PureXS\*.{log,bin,png,json} → Root\debug\
///     (Python-decoder debug artifacts, calibration files)
///   %TEMP%\PureXS\                              → Root\temp\
///
/// Failures are logged via _log if available and Debug.WriteLine
/// otherwise, but never raised — a botched migration must not block
/// the app from starting.
/// </summary>
public static class PureXSDataMigration
{
    private const string MarkerFileName = ".migrated_v1";

    public static void RunOnce(IEventLogService? log = null)
    {
        PureXSDataPaths.EnsureCreated();

        var marker = Path.Combine(PureXSDataPaths.Root, MarkerFileName);
        if (File.Exists(marker))
            return;

        try
        {
            int moved = 0;
            moved += MigratePatients(log);
            moved += MigrateDicom(log);
            moved += MigrateRoamingDebug(log);
            moved += MigrateTempScratch(log);

            File.WriteAllText(marker, DateTime.UtcNow.ToString("o"));
            if (moved > 0)
                log?.Log($"Data layout migration v1 complete — {moved} item(s) relocated to {PureXSDataPaths.Root}");
            else
                log?.Log($"Data layout migration v1 complete — fresh install, nothing to relocate");
        }
        catch (Exception ex)
        {
            // Never crash startup over a bad migration. Note the marker
            // is NOT written, so we'll retry next launch.
            Debug.WriteLine($"[Migration] aborted: {ex}");
            log?.Log($"Data layout migration aborted: {ex.Message}", "warning");
        }
    }

    private static int MigratePatients(IEventLogService? log)
    {
        var oldRoot = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            "PureXS", "patients");
        return MoveDirectoryContents(oldRoot, PureXSDataPaths.Patients, "patients", log);
    }

    private static int MigrateDicom(IEventLogService? log)
    {
        var oldRoot = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
            "PureXS", "DICOM");
        return MoveDirectoryContents(oldRoot, PureXSDataPaths.Dicom, "DICOM", log);
    }

    /// <summary>
    /// Walk the old roaming PureXS root and move loose files (not the
    /// patients/ subfolder, which is handled separately) to debug/.
    /// </summary>
    private static int MigrateRoamingDebug(IEventLogService? log)
    {
        var oldRoot = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            "PureXS");
        if (!Directory.Exists(oldRoot)) return 0;

        Directory.CreateDirectory(PureXSDataPaths.Debug);
        int moved = 0;
        foreach (var file in Directory.EnumerateFiles(oldRoot))
        {
            var name = Path.GetFileName(file);
            // Skip the migration marker if it's already there
            if (name == MarkerFileName) continue;
            var dest = Path.Combine(PureXSDataPaths.Debug, name);
            if (TryMoveFile(file, dest)) moved++;
        }
        if (moved > 0)
            log?.Log($"Migrated {moved} debug file(s) from {oldRoot} → {PureXSDataPaths.Debug}");
        return moved;
    }

    private static int MigrateTempScratch(IEventLogService? log)
    {
        var oldRoot = Path.Combine(Path.GetTempPath(), "PureXS");
        return MoveDirectoryContents(oldRoot, PureXSDataPaths.Temp, "temp", log);
    }

    /// <summary>
    /// Move every file and subdirectory from <paramref name="oldRoot"/>
    /// to <paramref name="newRoot"/>, skipping items that already exist
    /// at the destination (so a partial prior migration can't clobber
    /// newer data).
    /// </summary>
    private static int MoveDirectoryContents(
        string oldRoot, string newRoot, string label, IEventLogService? log)
    {
        if (!Directory.Exists(oldRoot)) return 0;

        Directory.CreateDirectory(newRoot);
        int moved = 0;

        foreach (var entry in Directory.EnumerateFileSystemEntries(oldRoot))
        {
            var name = Path.GetFileName(entry);
            var dest = Path.Combine(newRoot, name);
            if (Directory.Exists(entry))
            {
                if (TryMoveDirectory(entry, dest)) moved++;
            }
            else
            {
                if (TryMoveFile(entry, dest)) moved++;
            }
        }

        if (moved > 0)
            log?.Log($"Migrated {moved} {label} item(s) from {oldRoot} → {newRoot}");
        return moved;
    }

    private static bool TryMoveFile(string src, string dest)
    {
        try
        {
            if (File.Exists(dest)) return false;  // don't clobber newer data
            File.Move(src, dest);
            return true;
        }
        catch (Exception ex)
        {
            Debug.WriteLine($"[Migration] file move failed {src} → {dest}: {ex.Message}");
            return false;
        }
    }

    private static bool TryMoveDirectory(string src, string dest)
    {
        try
        {
            if (Directory.Exists(dest)) return false;  // don't merge into existing
            Directory.Move(src, dest);
            return true;
        }
        catch (Exception ex)
        {
            Debug.WriteLine($"[Migration] dir move failed {src} → {dest}: {ex.Message}");
            return false;
        }
    }
}
