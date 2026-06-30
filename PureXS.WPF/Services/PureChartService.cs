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
        "https://whzohbzqhqaohpohmqah.supabase.co/functions/v1/xray-gateway/search";

    private const string ScheduledUrl =
        "https://whzohbzqhqaohpohmqah.supabase.co/functions/v1/xray-gateway/scheduled";

    private const string UploadUrl =
        "https://whzohbzqhqaohpohmqah.supabase.co/functions/v1/xray-gateway/upload";

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
        var response = await _http.PostAsJsonAsync(SearchUrl, new { q = query }, ct);
        response.EnsureSuccessStatusCode();
        return ParsePatientList(await response.Content.ReadAsStringAsync(ct));
    }

    public async Task<List<PureChartPatient>> ScheduledTodayAsync(string? day = null, CancellationToken ct = default)
    {
        // Empty body → today (facility-local); {"date":"YYYY-MM-DD"} for a specific day.
        object payload = string.IsNullOrWhiteSpace(day) ? new { } : new { date = day };
        var response = await _http.PostAsJsonAsync(ScheduledUrl, payload, ct);
        response.EnsureSuccessStatusCode();
        return ParsePatientList(await response.Content.ReadAsStringAsync(ct));
    }

    /// <summary>
    /// Parse an edge-function response into patients. Shared by /search and
    /// /scheduled — both return {"patients":[...]} (or a bare list).
    /// </summary>
    private static List<PureChartPatient> ParsePatientList(string json)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;

        var array = root.ValueKind == JsonValueKind.Array
            ? root
            : root.TryGetProperty("patients", out var p) ? p : root;

        var patients = new List<PureChartPatient>();
        if (array.ValueKind != JsonValueKind.Array) return patients;
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
        string slotCode, string? originalFilename = null,
        CancellationToken ct = default)
    {
        // xray-gateway/upload contract (purechart-app): JSON body
        //   { file, filename, metadata }
        // - file:     plain base64 of the image bytes (NO data: prefix), ≤10 MB
        // - filename: derives content type server-side (.jpg/.jpeg→jpeg, else png)
        // - metadata: a JSON string with patientId + slotCode + uploadType
        // Facility is keyed off the x-api-key header, NOT the body.
        var metadata = JsonSerializer.Serialize(new
        {
            patientId,
            slotCode,               // chart slot code (e.g. "PAN")
            uploadType = "slot",    // "slot" (single image) | "composite"
        });
        var payload = new
        {
            file = Convert.ToBase64String(fileBytes),
            filename = originalFilename ?? "scan.png",
            metadata,
        };

        var response = await _http.PostAsJsonAsync(UploadUrl, payload, ct);
        var json = await response.Content.ReadAsStringAsync(ct);

        var result = new UploadResult { HttpStatus = (int)response.StatusCode };

        if (!string.IsNullOrEmpty(json))
        {
            // Success: { success:true, duplicate:bool, xrayId:uuid }
            // Failure: { success:false, error:"<msg>" }
            using var doc = JsonDocument.Parse(json);
            var r = doc.RootElement;
            result.Success = r.GetBoolOrDefault("success");
            result.AttachmentId = r.GetStringOrDefault("xrayId");
            result.Duplicate = r.GetBoolOrDefault("duplicate");
            result.Error = r.GetStringOrDefault("error");
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

    public void UpdateToken(string facilityToken)
    {
        _http.DefaultRequestHeaders.Remove("x-api-key");
        _http.DefaultRequestHeaders.Add("x-api-key", facilityToken);
    }

    public async Task<string?> FetchFacilityNameAsync(string facilityToken, CancellationToken ct = default)
    {
        // One-off client with the probed token (the instance _http holds the
        // *active* facility's token, which may differ from the one we resolve).
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
        http.DefaultRequestHeaders.Add("Authorization", $"Bearer {AnonKey}");
        http.DefaultRequestHeaders.Add("apikey", AnonKey);
        http.DefaultRequestHeaders.Add("x-api-key", facilityToken);

        var response = await http.PostAsJsonAsync(ScheduledUrl, new { }, ct);
        response.EnsureSuccessStatusCode();
        var json = await response.Content.ReadAsStringAsync(ct);
        if (string.IsNullOrWhiteSpace(json)) return null;

        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        if (root.ValueKind != JsonValueKind.Object) return null;

        foreach (var key in new[] { "facility_name", "facilityName" })
        {
            if (root.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.String)
            {
                var s = v.GetString();
                if (!string.IsNullOrWhiteSpace(s)) return s.Trim();
            }
        }
        if (root.TryGetProperty("facility", out var fac))
        {
            if (fac.ValueKind == JsonValueKind.String)
            {
                var s = fac.GetString();
                if (!string.IsNullOrWhiteSpace(s)) return s.Trim();
            }
            else if (fac.ValueKind == JsonValueKind.Object
                     && fac.TryGetProperty("name", out var n)
                     && n.ValueKind == JsonValueKind.String)
            {
                var s = n.GetString();
                if (!string.IsNullOrWhiteSpace(s)) return s.Trim();
            }
        }
        return null;
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
