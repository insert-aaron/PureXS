using System.IO;
using System.Text;
using System.Text.Json;
using PureXS.Models;

namespace PureXS.Services;

public class PatientOutputService : IPatientOutputService
{
    // Was %APPDATA%\Roaming\PureXS\patients — moved into the consolidated
    // layout under %LOCALAPPDATA%\PureXS\patients so big patient .bin
    // files don't sync to a domain server with the roaming profile, and
    // so all PureXS data sits under one root.
    private static readonly string BaseDir = PureXSDataPaths.Patients;

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true
    };

    private static readonly SemaphoreSlim FileLock = new(1, 1);

    public string GetPatientDirectory(string patientId, string lastName, string firstName)
    {
        var dir = Path.Combine(BaseDir, patientId);
        Directory.CreateDirectory(dir);
        return dir;
    }

    public string GetFilePrefix(string lastName, string firstName, string dob)
    {
        // Strip dashes/slashes from DOB to get a plain date string
        var dobNoDash = dob.Replace("-", "").Replace("/", "");
        var timestamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
        return $"{lastName}_{firstName}_{dobNoDash}_{timestamp}";
    }

    public async Task<string> SavePanoramicAsync(string patientDir, string filePrefix, byte[] pngBytes, CancellationToken ct = default)
    {
        var filename = $"{filePrefix}_panoramic.png";
        var filePath = Path.Combine(patientDir, filename);

        await FileLock.WaitAsync(ct);
        try
        {
            await File.WriteAllBytesAsync(filePath, pngBytes, ct);
        }
        finally
        {
            FileLock.Release();
        }

        return filePath;
    }

    public async Task<string> SavePanoramicTifAsync(
        string patientDir, string filePrefix, string sourceTifPath, CancellationToken ct = default)
    {
        var filename = $"{filePrefix}_panoramic.tif";
        var filePath = Path.Combine(patientDir, filename);

        await FileLock.WaitAsync(ct);
        try
        {
            // Async copy via stream so a large uncompressed TIF (~3 MB)
            // doesn't block the UI thread. File.Copy is synchronous.
            await using var src = new FileStream(
                sourceTifPath, FileMode.Open, FileAccess.Read, FileShare.Read,
                bufferSize: 81920, useAsync: true);
            await using var dst = new FileStream(
                filePath, FileMode.Create, FileAccess.Write, FileShare.None,
                bufferSize: 81920, useAsync: true);
            await src.CopyToAsync(dst, ct);
        }
        finally
        {
            FileLock.Release();
        }

        return filePath;
    }

    public async Task SaveEventsLogAsync(string patientDir, string filePrefix, string examType, int scanlines, double peakKv, double elapsed, CancellationToken ct = default)
    {
        var filename = $"{filePrefix}_events.log";
        var filePath = Path.Combine(patientDir, filename);

        var sb = new StringBuilder();
        sb.AppendLine("PureXS Exposure Events Log");
        sb.AppendLine("==========================");
        sb.AppendLine($"Timestamp:    {DateTime.Now:yyyy-MM-dd HH:mm:ss}");
        sb.AppendLine($"Exam Type:    {examType}");
        sb.AppendLine($"Scanlines:    {scanlines}");
        sb.AppendLine($"Peak kV:      {peakKv:F1}");
        sb.AppendLine($"Elapsed:      {elapsed:F2}s");
        sb.AppendLine("==========================");

        await FileLock.WaitAsync(ct);
        try
        {
            await File.WriteAllTextAsync(filePath, sb.ToString(), ct);
        }
        finally
        {
            FileLock.Release();
        }
    }

    public async Task AppendSessionAsync(string patientDir, string examType, double kvPeak, int scanlines, string? imageFile, string? eventsLog, string? dcmFile, CancellationToken ct = default)
    {
        var sessionsPath = Path.Combine(patientDir, "sessions.json");

        await FileLock.WaitAsync(ct);
        try
        {
            SessionsFile sessionsFile;

            if (File.Exists(sessionsPath))
            {
                var existingJson = await File.ReadAllTextAsync(sessionsPath, ct);
                sessionsFile = JsonSerializer.Deserialize<SessionsFile>(existingJson, JsonOptions) ?? new SessionsFile();
            }
            else
            {
                // Derive patient_id from the directory name
                sessionsFile = new SessionsFile
                {
                    PatientId = Path.GetFileName(patientDir) ?? ""
                };
            }

            var entry = new SessionEntry
            {
                Timestamp = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"),
                ExamType = examType,
                KvPeak = kvPeak,
                Scanlines = scanlines,
                ImageFile = imageFile is not null ? Path.GetFileName(imageFile) : null,
                EventsLog = eventsLog is not null ? Path.GetFileName(eventsLog) : null,
                DcmFile = dcmFile is not null ? Path.GetFileName(dcmFile) : null
            };

            sessionsFile.Sessions.Add(entry);

            var json = JsonSerializer.Serialize(sessionsFile, JsonOptions);
            await File.WriteAllTextAsync(sessionsPath, json, ct);
        }
        finally
        {
            FileLock.Release();
        }
    }
}
