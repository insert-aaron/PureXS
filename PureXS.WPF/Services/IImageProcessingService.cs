namespace PureXS.Services;

/// <summary>
/// Processes raw Orthophos scan bytes into a finished PNG
/// by calling the Python-based decoder (bundled as purexs_decoder.exe).
/// </summary>
public interface IImageProcessingService
{
    /// <summary>
    /// Takes raw scan bytes from the TCP stream and returns the
    /// processed PNG bytes ready for display and upload.
    /// Returns null if processing fails.
    /// </summary>
    /// <param name="rawBytes">Raw scan bytes from the TCP stream.</param>
    /// <param name="examType">Exam type for pipeline routing ("Panoramic", "Ceph Lateral", "Ceph Frontal").</param>
    /// <param name="ct">Cancellation token.</param>
    Task<byte[]?> ProcessRawScanAsync(byte[] rawBytes, string examType = "Panoramic", CancellationToken ct = default);
}
