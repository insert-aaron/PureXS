namespace PureXS.Services;

public interface IPatientOutputService
{
    string GetPatientDirectory(string patientId, string lastName, string firstName);
    string GetFilePrefix(string lastName, string firstName, string dob);
    Task<string> SavePanoramicAsync(string patientDir, string filePrefix, byte[] pngBytes, CancellationToken ct = default);
    Task SaveEventsLogAsync(string patientDir, string filePrefix, string examType, int scanlines, double peakKv, double elapsed, CancellationToken ct = default);
    Task AppendSessionAsync(string patientDir, string examType, double kvPeak, int scanlines, string? imageFile, string? eventsLog, string? dcmFile, CancellationToken ct = default);
}
