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

    // Which fleet unit produced this scan. unit_id is the installer-set label
    // (config.json "unit_id", defaults to machine name); device_host is the
    // Sirona endpoint IP. Lets a 6-unit fleet attribute every scan to a unit.
    [JsonPropertyName("unit_id")]
    public string? UnitId { get; set; }

    [JsonPropertyName("device_host")]
    public string? DeviceHost { get; set; }

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
