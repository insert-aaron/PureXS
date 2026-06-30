using PureXS.Models;

namespace PureXS.Services;

/// <summary>
/// Abstraction over PureChart Supabase edge functions for patient search and upload.
/// </summary>
public interface IPureChartService
{
    /// <summary>Search patients by query string. Returns up to 15 results.</summary>
    Task<List<PureChartPatient>> SearchAsync(string query, CancellationToken ct = default);

    /// <summary>Today's scheduled patients for this facility (or a specific 'YYYY-MM-DD' day).
    /// Seeds the avatar dock on startup and whenever the search box is empty.</summary>
    Task<List<PureChartPatient>> ScheduledTodayAsync(string? day = null, CancellationToken ct = default);

    /// <summary>Upload an X-ray file to a patient's chart.</summary>
    Task<UploadResult> UploadAsync(string patientId, byte[] fileBytes, string contentType, string uploadType, string title, string? originalFilename = null, CancellationToken ct = default);

    /// <summary>Download a profile picture (requires auth headers). Returns image bytes or null.</summary>
    Task<byte[]?> DownloadImageAsync(string url, CancellationToken ct = default);

    /// <summary>Swap the facility token used for the x-api-key header (after a 401 re-prompt).</summary>
    void UpdateToken(string facilityToken);

    /// <summary>
    /// Best-effort "auto from server": ask the backend for the facility name
    /// behind <paramref name="facilityToken"/>. Returns null when the backend
    /// exposes no name (caller falls back to a token-suffix label). Throws on
    /// auth/network errors so a bad token is distinguishable from a missing name.
    /// </summary>
    Task<string?> FetchFacilityNameAsync(string facilityToken, CancellationToken ct = default);
}
