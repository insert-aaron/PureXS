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
                Id = el.GetStringOrDefault("id"),
                FirstName = el.GetStringOrDefault("first_name"),
                LastName = el.GetStringOrDefault("last_name"),
                MedicalRecordNumber = el.GetStringOrDefault("medical_record_number"),
                Dob = el.GetStringOrDefault("dob"),
                Phone = el.GetStringOrDefault("phone"),
                Email = el.GetStringOrDefault("email"),
                ProfilePictureUrl = el.GetStringOrDefault("profile_picture_url"),
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
            result.Success = r.GetBoolOrDefault("success");
            result.FileUrl = r.GetStringOrDefault("fileUrl");
            result.AttachmentId = r.GetStringOrDefault("attachmentId");
            result.PatientId = r.GetStringOrDefault("patientId");
            result.Filename = r.GetStringOrDefault("filename");
            result.UploadType = r.GetStringOrDefault("type");
            result.Error = r.GetStringOrDefault("error");
            if (string.IsNullOrEmpty(result.Error))
                result.Error = r.GetStringOrDefault("message");
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
/// Helper extensions for safe JSON property access — handles mixed types
/// (the upload-xray edge function returns "success" as a JSON boolean,
/// while string fields like "fileUrl" are strings).
/// </summary>
internal static class JsonElementExtensions
{
    public static string GetStringOrDefault(this JsonElement el, string name, string fallback = "")
    {
        if (!el.TryGetProperty(name, out var prop)) return fallback;
        return prop.ValueKind switch
        {
            JsonValueKind.String => prop.GetString() ?? fallback,
            JsonValueKind.True => "true",
            JsonValueKind.False => "false",
            JsonValueKind.Number => prop.GetRawText(),
            _ => fallback,
        };
    }

    public static bool GetBoolOrDefault(this JsonElement el, string name, bool fallback = false)
    {
        if (!el.TryGetProperty(name, out var prop)) return fallback;
        return prop.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            JsonValueKind.String => bool.TryParse(prop.GetString(), out var b) && b,
            _ => fallback,
        };
    }
}
