namespace PureXS.Services;

public interface IEventLogService
{
    void Log(string message, string level = "info");
    void WriteExposeEvent(string patientId, string examType, double elapsed, int scanlineCount, double peakKv);
    string LogDirectory { get; }
}
