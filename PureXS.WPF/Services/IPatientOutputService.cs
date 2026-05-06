namespace PureXS.Services;

public interface IPatientOutputService
{
    string GetPatientDirectory(string patientId, string lastName, string firstName);
    string GetFilePrefix(string lastName, string firstName, string dob);
    Task<string> SavePanoramicAsync(string patientDir, string filePrefix, byte[] pngBytes, CancellationToken ct = default);

    /// <summary>
    /// Copy an uncompressed TIFF produced by the decoder into the patient
    /// directory as <c>{filePrefix}_panoramic.tif</c>. Returns the destination
    /// path. Used only when the SaveTifExport config flag is on; the source
    /// TIF lives in the decoder's temp dir and is rotated out after a few
    /// scans, so copying makes the file persistent next to the PNG.
    /// </summary>
    Task<string> SavePanoramicTifAsync(string patientDir, string filePrefix, string sourceTifPath, CancellationToken ct = default);

    Task SaveEventsLogAsync(string patientDir, string filePrefix, string examType, int scanlines, double peakKv, double elapsed, CancellationToken ct = default);
    Task AppendSessionAsync(string patientDir, string examType, double kvPeak, int scanlines, string? imageFile, string? eventsLog, string? dcmFile, CancellationToken ct = default);
}
