using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using PureXS.Models;

namespace PureXS.Services;

/// <summary>
/// Calls the PureChart Supabase edge functions for patient search and X-ray upload.
/// </summary>
public sealed class PureChartService : IPureChartService, IDisposable
{
    private const string SearchUrl =
        "https://whzohbzqhqaohpohmqah.supabase.co/functions/v1/xray-patient-search";

    private const string UploadUrl =
        "https://whzohbzqhqaohpohmqah.supabase.co/functions/v1/upload-xray";

    private const string AnonKey =
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        + "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Indoem9oYnpxaHFhb2hwb2htcWFoIiwi"
        + "cm9sZSI6ImFub24iLCJpYXQiOjE3NTkyNTUzNzQsImV4cCI6MjA3NDgzMTM3NH0."
        + "p_BZ1XaPIihSdo-41YKbr4ZmS-NZRfGr9AerEEgpmcc";

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly HttpClient _http;

    public PureChartService(string facilityToken)
    {
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
        _http.DefaultRequestHeaders.Add("Authorization", $"Bearer {AnonKey}");
        _http.DefaultRequestHeaders.Add("apikey", AnonKey);
        _http.DefaultRequestHeaders.Add("x-api-key", facilityToken);
    }

    public async Task<List<PureChartPatient>> SearchAsync(string query, CancellationToken ct = default)
    {
        var payload = new { q = query };
        var response = await _http.PostAsJsonAsync(SearchUrl, payload, ct);
        response.EnsureSuccessStatusCode();

        var json = await response.Content.ReadAsStringAsync(ct);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;

        // The edge function may return {"patients": [...]} or a bare list
        var array = root.ValueKind == JsonValueKind.Array
            ? root
            : root.TryGetProperty("patients", out var p) ? p : root;

        var patients = new List<PureChartPatient>();
        foreach (var el in array.EnumerateArray())
        {
            patients.Add(new PureChartPatient
            {
                Id = el.GetPropertyOrDefault("id"),
                FirstName = el.GetPropertyOrDefault("first_name"),
                LastName = el.GetPropertyOrDefault("last_name"),
                MedicalRecordNumber = el.GetPropertyOrDefault("medical_record_number"),
                Dob = el.GetPropertyOrDefault("dob"),
                Phone = el.GetPropertyOrDefault("phone"),
                Email = el.GetPropertyOrDefault("email"),
                ProfilePictureUrl = el.GetPropertyOrDefault("profile_picture_url"),
            });
        }

        return patients;
    }

    public async Task<UploadResult> UploadAsync(
        string patientId, byte[] fileBytes, string contentType,
        string uploadType, string title, CancellationToken ct = default)
    {
        var payload = new
        {
            patientId,
            base64Data = Convert.ToBase64String(fileBytes),
            contentType,
            type = uploadType,
            title,
        };

        var response = await _http.PostAsJsonAsync(UploadUrl, payload, ct);
        var json = await response.Content.ReadAsStringAsync(ct);

        var result = new UploadResult { HttpStatus = (int)response.StatusCode };

        if (!string.IsNullOrEmpty(json))
        {
            using var doc = JsonDocument.Parse(json);
            var r = doc.RootElement;
            result.Success = r.GetPropertyOrDefault("success") == "True"
                             || r.GetPropertyOrDefault("success") == "true";
            result.FileUrl = r.GetPropertyOrDefault("fileUrl");
            result.AttachmentId = r.GetPropertyOrDefault("attachmentId");
            result.PatientId = r.GetPropertyOrDefault("patientId");
            result.Filename = r.GetPropertyOrDefault("filename");
            result.UploadType = r.GetPropertyOrDefault("type");
            result.Error = r.GetPropertyOrDefault("error");
            if (string.IsNullOrEmpty(result.Error))
                result.Error = r.GetPropertyOrDefault("message");
        }

        if (!response.IsSuccessStatusCode)
        {
            result.Success = false;
            if (string.IsNullOrEmpty(result.Error))
                result.Error = $"HTTP {(int)response.StatusCode}";
        }

        return result;
    }

    public async Task<byte[]?> DownloadImageAsync(string url, CancellationToken ct = default)
    {
        if (string.IsNullOrEmpty(url)) return null;

        try
        {
            var response = await _http.GetAsync(url, ct);
            if (!response.IsSuccessStatusCode) return null;
            return await response.Content.ReadAsByteArrayAsync(ct);
        }
        catch
        {
            return null;
        }
    }

    public void Dispose() => _http.Dispose();
}

/// <summary>
/// Helper extension for safe JSON property access.
/// </summary>
internal static class JsonElementExtensions
{
    public static string GetPropertyOrDefault(this JsonElement el, string name, string fallback = "")
    {
        return el.TryGetProperty(name, out var prop) ? prop.GetString() ?? fallback : fallback;
    }
}
