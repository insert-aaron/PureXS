namespace PureXS.Services;

public interface IDicomExportService
{
    Task<string?> ExportAsync(
        string patientName,
        string patientId,
        string patientDob,
        string examType,
        byte[] imageBytes,
        double kvPeak,
        string outputDirectory,
        string filePrefix,
        CancellationToken ct = default);
}
