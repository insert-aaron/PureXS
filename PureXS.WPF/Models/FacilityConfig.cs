using CommunityToolkit.Mvvm.ComponentModel;

namespace PureXS.Models;

/// <summary>
/// One configured PureChart facility: a friendly <see cref="Name"/> and the
/// per-facility <see cref="Token"/> (x-api-key) that authorizes its patients.
/// Observable so the toolbar toggle reflects a name resolved from the server
/// in the background, and so the settings dialog can edit rows live.
/// </summary>
public partial class FacilityConfig : ObservableObject
{
    [ObservableProperty]
    private string _name = "";

    [ObservableProperty]
    private string _token = "";

    /// <summary>UI-only flag used by the settings dialog's "active" radios; the
    /// persisted active facility is tracked by index, not this field.</summary>
    [ObservableProperty]
    private bool _isActive;

    /// <summary>
    /// Fast placeholder name from the token suffix (no network). The real
    /// facility name is filled in asynchronously ("auto from server") when the
    /// backend exposes one; the user can also rename it in Settings.
    /// </summary>
    public static string DerivePlaceholderName(string token)
    {
        var t = (token ?? "").Trim();
        var suffix = t.Length >= 4 ? t[^4..] : t;
        return $"Facility {suffix}";
    }
}
