using System.Windows.Media.Imaging;

namespace PureXS.Models;

/// <summary>
/// A single patient record returned by the PureChart edge function.
/// </summary>
public class PureChartPatient
{
    public string Id { get; set; } = "";
    public string FirstName { get; set; } = "";
    public string LastName { get; set; } = "";
    public string MedicalRecordNumber { get; set; } = "";
    public string Dob { get; set; } = "";
    public string Phone { get; set; } = "";
    public string Email { get; set; } = "";
    public string ProfilePictureUrl { get; set; } = "";

    /// <summary>Downloaded profile picture bitmap (loaded with auth headers).</summary>
    public BitmapImage? ProfileImage { get; set; }

    /// <summary>True if ProfileImage has been loaded.</summary>
    public bool HasProfileImage => ProfileImage is not null;

    public string DisplayName => $"{FirstName} {LastName} — {MedicalRecordNumber}";

    public string Initials =>
        $"{(FirstName.Length > 0 ? FirstName[0] : '?')}{(LastName.Length > 0 ? LastName[0] : '?')}";
}

/// <summary>
/// Parsed response from the upload-xray edge function.
/// </summary>
public class UploadResult
{
    public bool Success { get; set; }
    /// <summary>The created X-ray record id (response field "xrayId").</summary>
    public string AttachmentId { get; set; } = "";
    /// <summary>True when the gateway detected this image was already uploaded.</summary>
    public bool Duplicate { get; set; }
    public string Error { get; set; } = "";
    public int HttpStatus { get; set; }
}

/// <summary>
/// Exam type options matching the Python GUI's EXAM_TYPES list.
/// </summary>
public static class ExamTypes
{
    public static readonly string[] All =
    [
        "Panoramic",
        "Ceph Lateral",
        "Ceph Frontal",
        "Periapical"
    ];

    /// <summary>Maps exam type to PureChart attachment type.</summary>
    public static string ToPureChartType(string examType) => examType switch
    {
        "Panoramic" => "panoramic_xray",
        "Ceph Lateral" or "Ceph Frontal" => "xrays",
        "Periapical" => "periapical",
        _ => "xrays"
    };

    /// <summary>
    /// Maps exam type to the xray-gateway `metadata.slotCode` (free-form text;
    /// the gateway stores it verbatim and titles the row "X-ray &lt;slotCode&gt;").
    /// This is OUR vocabulary — it must match whatever slot codes the PureXR
    /// desktop app uses to place the image in the correct slot.
    /// </summary>
    public static string ToSlotCode(string examType) => examType switch
    {
        "Panoramic" => "PAN",
        "Ceph Lateral" => "CEPH-L",
        "Ceph Frontal" => "CEPH-F",
        "Periapical" => "PA",
        _ => "PAN"
    };
}
