using System.Text.Json.Serialization;

namespace PureXS.Models;

public class SessionsFile
{
    [JsonPropertyName("patient_id")]
    public string PatientId { get; set; } = "";

    [JsonPropertyName("sessions")]
    public List<SessionEntry> Sessions { get; set; } = [];
}

public class SessionEntry
{
    [JsonPropertyName("timestamp")]
    public string Timestamp { get; set; } = "";

    [JsonPropertyName("exam_type")]
    public string ExamType { get; set; } = "";

    [JsonPropertyName("kv_peak")]
    public double KvPeak { get; set; }

    [JsonPropertyName("scanlines")]
    public int Scanlines { get; set; }

    [JsonPropertyName("image_file")]
    public string? ImageFile { get; set; }

    [JsonPropertyName("events_log")]
    public string? EventsLog { get; set; }

    [JsonPropertyName("dcm_file")]
    public string? DcmFile { get; set; }
}
