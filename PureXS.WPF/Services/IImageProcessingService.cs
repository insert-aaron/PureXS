namespace PureXS.Services;

/// <summary>
/// Result of processing a raw scan: the encoded PNG bytes for display/upload,
/// and optionally the on-disk path of an uncompressed TIFF copy that the
/// decoder produced when <c>SaveTifExport</c> was enabled.
/// </summary>
/// <param name="Columns">Real decoded column count from the decoder (~2700 for
/// a full panoramic), parsed from its "COLUMNS=" output. This is the true scan
/// length — unlike the host's sparse ~14-line live preview count — so it's what
/// the events.log / sessions.json and the "Scan complete — N lines" status use.
/// 0 when the decoder didn't report it.</param>
/// <param name="PhaseErr">Column-phase error in px (distance of the pixel-stream
/// start from a column boundary), parsed from the decoder's "PHASE_ERR=" line.
/// Recorded per scan alongside unit_id so misalignment frequency can be tracked
/// per machine. Small (~1-5) is healthy; large (e.g. 569) means the scan was
/// misaligned/folded.</param>
public sealed record ProcessedScan(byte[] PngBytes, string? TifSourcePath, int Columns = 0, int PhaseErr = 0);

/// <summary>
/// Thrown when the decoder reports that the scan completed transport but
/// delivered too few scanlines for a valid reconstruction (e.g. the Sirona
/// unit aborted mid-sweep). The caller should surface <see cref="Message"/>
/// to the operator as a retake prompt rather than falling through to the
/// scanline-preview fallback — that fallback would also be useless on a
/// truncated capture, and showing it has been mistaken for a real image.
/// </summary>
public sealed class ScanIncompleteException : Exception
{
    public ScanIncompleteException(string message) : base(message) { }
}

/// <summary>
/// Thrown when the decoder refuses to reconstruct because the unit's detector
/// geometry doesn't match the Orthophos XG the pipeline is calibrated for
/// (e.g. a fleet machine with a different detector or firmware). Unlike a
/// truncated scan this is NOT retakeable — it's a hardware/config fact — so the
/// caller should surface it as a hard "unsupported unit" error and refuse to
/// display an image rather than show a corrupted reconstruction.
/// </summary>
public sealed class DetectorMismatchException : Exception
{
    public DetectorMismatchException(string message) : base(message) { }
}

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
