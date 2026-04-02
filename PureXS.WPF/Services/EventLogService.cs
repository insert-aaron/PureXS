using System.Globalization;
using System.IO;
using System.Text.Json;

namespace PureXS.Services;

public class EventLogService : IEventLogService
{
    private readonly object _lock = new();

    public string LogDirectory { get; }

    public EventLogService()
    {
        var appData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        LogDirectory = Path.Combine(appData, "PureXS", "logs");
        Directory.CreateDirectory(LogDirectory);
    }

    public void Log(string message, string level = "info")
    {
        var entry = new
        {
            timestamp = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            level,
            message
        };
        AppendLine(JsonSerializer.Serialize(entry));
    }

    public void WriteExposeEvent(string patientId, string examType, double elapsed, int scanlineCount, double peakKv)
    {
        var entry = new
        {
            timestamp = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            level = "info",
            message = "expose_complete",
            patientId,
            examType,
            elapsedSeconds = Math.Round(elapsed, 3),
            scanlineCount,
            peakKv = Math.Round(peakKv, 1)
        };
        AppendLine(JsonSerializer.Serialize(entry));
    }

    private void AppendLine(string json)
    {
        var fileName = $"purexs_{DateTime.Now:yyyy-MM-dd}.log";
        var filePath = Path.Combine(LogDirectory, fileName);

        lock (_lock)
        {
            File.AppendAllText(filePath, json + Environment.NewLine);
        }
    }
}
