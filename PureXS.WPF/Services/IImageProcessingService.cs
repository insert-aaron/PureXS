namespace PureXS.Services;

/// <summary>
/// Processes raw Orthophos scan bytes into a finished panoramic PNG
/// by calling the Python-based decoder (bundled as purexs_decoder.exe).
/// </summary>
public interface IImageProcessingService
{
    /// <summary>
    /// Takes raw scan bytes from the TCP stream and returns the
    /// processed panoramic PNG bytes ready for display and upload.
    /// Returns null if processing fails.
    /// </summary>
    Task<byte[]?> ProcessRawScanAsync(byte[] rawBytes, CancellationToken ct = default);
}
