namespace PureXS.Services;

/// <summary>
/// Result of processing a raw scan: the encoded PNG bytes for display/upload,
/// and optionally the on-disk path of an uncompressed TIFF copy that the
/// decoder produced when <c>SaveTifExport</c> was enabled.
/// </summary>
public sealed record ProcessedScan(byte[] PngBytes, string? TifSourcePath);

/// <summary>
/// Processes raw Orthophos scan bytes into a finished PNG
/// by calling the Python-based decoder (bundled as purexs_decoder.exe).
/// </summary>
public interface IImageProcessingService
{
    /// <summary>
    /// Takes raw scan bytes from the TCP stream and returns the processed
    /// PNG bytes plus, when TIF export is enabled, the temp-dir path of an
    /// uncompressed TIFF copy. Returns null if processing fails.
    /// </summary>
    /// <param name="rawBytes">Raw scan bytes from the TCP stream.</param>
    /// <param name="examType">Exam type for pipeline routing ("Panoramic", "Ceph Lateral", "Ceph Frontal").</param>
    /// <param name="ct">Cancellation token.</param>
    Task<ProcessedScan?> ProcessRawScanAsync(byte[] rawBytes, string examType = "Panoramic", CancellationToken ct = default);
}
