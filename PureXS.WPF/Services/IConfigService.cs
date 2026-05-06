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

    /// <summary>
    /// When true, every scan's panoramic is also written as an uncompressed
    /// 8-bit TIFF in the patient directory next to the PNG. Used only by
    /// facilities running per-device Sidexis LUT calibration; default false
    /// so non-calibrating facilities don't pay ~3 MB/scan disk cost.
    /// </summary>
    bool SaveTifExport { get; }
}
