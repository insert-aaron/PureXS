using PureXS.Models;

namespace PureXS.Services;

/// <summary>
/// Abstraction over PureChart Supabase edge functions for patient search and upload.
/// </summary>
public interface IPureChartService
{
    /// <summary>Search patients by query string. Returns up to 15 results.</summary>
    Task<List<PureChartPatient>> SearchAsync(string query, CancellationToken ct = default);

    /// <summary>Upload an X-ray file to a patient's chart.</summary>
    Task<UploadResult> UploadAsync(string patientId, byte[] fileBytes, string contentType, string uploadType, string title, CancellationToken ct = default);

    /// <summary>Download a profile picture (requires auth headers). Returns image bytes or null.</summary>
    Task<byte[]?> DownloadImageAsync(string url, CancellationToken ct = default);
}
