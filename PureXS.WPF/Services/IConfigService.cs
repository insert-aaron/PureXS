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
    /// Installer-set label identifying THIS physical unit in a multi-unit fleet
    /// (config.json "unit_id", e.g. "Op-3 Orthophos"). Stamped into every scan's
    /// sessions.json + events.log so scans are attributable to a unit. Null when
    /// unset — callers fall back to the machine name.
    /// </summary>
    string? UnitId { get; }

    /// <summary>
    /// When true, every scan's panoramic is also written as an uncompressed
    /// 8-bit TIFF in the patient directory next to the PNG. Used only by
    /// facilities running per-device Sidexis LUT calibration; default false
    /// so non-calibrating facilities don't pay ~3 MB/scan disk cost.
    /// </summary>
    bool SaveTifExport { get; }

    // ── Post-scan reconnect tuning (config.json) ─────────────────────────────
    // All null when the key is absent, so the service keeps its built-in
    // default. Lets a facility tune rescan recovery on-site without a rebuild.

    /// <summary>Max reconnect attempts after a post-scan drop. Null → default (5).</summary>
    int? ReconnectMaxAttempts { get; }

    /// <summary>Delay between reconnect attempts, ms. Null → default (2000).</summary>
    int? ReconnectDelayMs { get; }

    /// <summary>Per-attempt TCP connect timeout during reconnect, ms. Null → default (3000).</summary>
    int? ReconnectConnectTimeoutMs { get; }

    /// <summary>
    /// Heartbeat cycles to wait after a post-scan reconnect before re-enabling
    /// arm (gate). Each cycle ≈ 900 ms. Lower = faster rescan, less settle time.
    /// Null → default (3, ≈2.7 s).
    /// </summary>
    int? RearmGateCycles { get; }
}
