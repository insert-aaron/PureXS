namespace PureXS.Services;

public interface IConfigService
{
    string? FacilityToken { get; }
    void SaveFacilityToken(string token);
    string ConfigDirectory { get; }

    /// <summary>Last-known Sirona device IP (persisted across launches).</summary>
    string? SironaHost { get; }

    /// <summary>Last-known Sirona device port (persisted across launches).</summary>
    int? SironaPort { get; }

    /// <summary>Save discovered Sirona connection details so future launches connect instantly.</summary>
    void SaveSironaEndpoint(string host, int port);
}
