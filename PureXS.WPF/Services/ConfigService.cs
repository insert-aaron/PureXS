using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using PureXS.Models;

namespace PureXS.Services;

public class ConfigService : IConfigService
{
    private readonly string _configDir;
    private readonly string _configPath;
    private string? _facilityToken;
    private List<FacilityConfig> _facilities = new();
    private int _activeFacilityIndex;
    private string? _unitId;
    private string? _sironaHost;
    private int? _sironaPort;
    private bool _saveTifExport;
    private int? _reconnectMaxAttempts;
    private int? _reconnectDelayMs;
    private int? _reconnectConnectTimeoutMs;
    private int? _rearmGateCycles;

    public ConfigService()
    {
        _configDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            "PureXS");
        _configPath = Path.Combine(_configDir, "config.json");

        Load();
    }

    public string? FacilityToken => _facilityToken;

    public IReadOnlyList<FacilityConfig> Facilities => _facilities;

    public int ActiveFacilityIndex => _activeFacilityIndex;

    public string ConfigDirectory => _configDir;

    public string? UnitId => _unitId;

    public string? SironaHost => _sironaHost;

    public int? SironaPort => _sironaPort;

    public bool SaveTifExport => _saveTifExport;

    public int? ReconnectMaxAttempts => _reconnectMaxAttempts;

    public int? ReconnectDelayMs => _reconnectDelayMs;

    public int? ReconnectConnectTimeoutMs => _reconnectConnectTimeoutMs;

    public int? RearmGateCycles => _rearmGateCycles;

    public void SaveFacilityToken(string token)
    {
        // Back-compat path (used by the 401 re-prompt): update the ACTIVE
        // facility's token, or seed a first facility if none exist.
        if (_facilities.Count > 0
            && _activeFacilityIndex >= 0
            && _activeFacilityIndex < _facilities.Count)
        {
            _facilities[_activeFacilityIndex].Token = token;
        }
        else
        {
            _facilities = new List<FacilityConfig>
            {
                new() { Name = FacilityConfig.DerivePlaceholderName(token), Token = token },
            };
            _activeFacilityIndex = 0;
        }
        SaveFacilities(_facilities, _activeFacilityIndex);
    }

    public void SaveFacilities(IReadOnlyList<FacilityConfig> facilities, int activeIndex)
    {
        _facilities = facilities
            .Where(f => !string.IsNullOrWhiteSpace(f.Token))
            .Select(f => new FacilityConfig
            {
                Name = string.IsNullOrWhiteSpace(f.Name)
                    ? FacilityConfig.DerivePlaceholderName(f.Token)
                    : f.Name.Trim(),
                Token = f.Token.Trim(),
            })
            .ToList();

        _activeFacilityIndex = _facilities.Count == 0
            ? 0
            : Math.Clamp(activeIndex, 0, _facilities.Count - 1);
        _facilityToken = _facilities.Count > 0 ? _facilities[_activeFacilityIndex].Token : null;

        var root = LoadRoot();
        var arr = new JsonArray();
        foreach (var f in _facilities)
            arr.Add(new JsonObject { ["name"] = f.Name, ["token"] = f.Token });
        root["facilities"] = arr;
        root["active_facility"] = _activeFacilityIndex;
        root["facility_token"] = _facilityToken ?? "";
        WriteRoot(root);
    }

    public void SetActiveFacility(int index)
    {
        if (_facilities.Count == 0) return;
        SaveFacilities(_facilities, index);
    }

    public void SaveSironaEndpoint(string host, int port)
    {
        _sironaHost = host;
        _sironaPort = port;

        var root = LoadRoot();
        root["sirona_host"] = host;
        root["sirona_port"] = port;
        WriteRoot(root);
    }

    private void SaveField(string key, string value)
    {
        var root = LoadRoot();
        root[key] = value;
        WriteRoot(root);
    }

    private JsonObject LoadRoot()
    {
        if (File.Exists(_configPath))
        {
            try
            {
                var existing = File.ReadAllText(_configPath);
                return JsonNode.Parse(existing)?.AsObject() ?? new JsonObject();
            }
            catch { }
        }
        return new JsonObject();
    }

    private void WriteRoot(JsonObject root)
    {
        Directory.CreateDirectory(_configDir);
        var options = new JsonSerializerOptions { WriteIndented = true };
        File.WriteAllText(_configPath, root.ToJsonString(options));
    }

    private void Load()
    {
        if (!File.Exists(_configPath))
            return;

        try
        {
            var json = File.ReadAllText(_configPath);
            var root = JsonNode.Parse(json)?.AsObject();
            _facilityToken = root?["facility_token"]?.GetValue<string>();
            LoadFacilities(root);
            _unitId = root?["unit_id"]?.GetValue<string>();
            _sironaHost = root?["sirona_host"]?.GetValue<string>();
            var portNode = root?["sirona_port"];
            if (portNode is not null)
                _sironaPort = portNode.GetValue<int>();
            var tifNode = root?["save_tif_export"];
            if (tifNode is not null)
                _saveTifExport = tifNode.GetValue<bool>();

            var rcMax = root?["reconnect_max_attempts"];
            if (rcMax is not null)
                _reconnectMaxAttempts = rcMax.GetValue<int>();
            var rcDelay = root?["reconnect_delay_ms"];
            if (rcDelay is not null)
                _reconnectDelayMs = rcDelay.GetValue<int>();
            var rcTimeout = root?["reconnect_connect_timeout_ms"];
            if (rcTimeout is not null)
                _reconnectConnectTimeoutMs = rcTimeout.GetValue<int>();
            var rearmGate = root?["rearm_gate_cycles"];
            if (rearmGate is not null)
                _rearmGateCycles = rearmGate.GetValue<int>();
        }
        catch
        {
            // Corrupt config — treat as empty
        }
    }

    /// <summary>
    /// Populate <see cref="_facilities"/> / <see cref="_activeFacilityIndex"/>
    /// from config, migrating a legacy single <c>facility_token</c> into a
    /// one-entry list when no <c>facilities</c> array is present.
    /// </summary>
    private void LoadFacilities(JsonObject? root)
    {
        _facilities = new List<FacilityConfig>();
        _activeFacilityIndex = 0;

        if (root?["facilities"] is JsonArray arr)
        {
            foreach (var node in arr)
            {
                var token = node?["token"]?.GetValue<string>()?.Trim();
                if (string.IsNullOrWhiteSpace(token)) continue;
                var name = node?["name"]?.GetValue<string>()?.Trim();
                _facilities.Add(new FacilityConfig
                {
                    Name = string.IsNullOrWhiteSpace(name)
                        ? FacilityConfig.DerivePlaceholderName(token)
                        : name,
                    Token = token,
                });
            }
        }

        if (_facilities.Count > 0)
        {
            var idxNode = root?["active_facility"];
            if (idxNode is not null)
                _activeFacilityIndex = Math.Clamp(idxNode.GetValue<int>(), 0, _facilities.Count - 1);
            return;
        }

        // Legacy migration: single facility_token → one-entry list
        if (!string.IsNullOrWhiteSpace(_facilityToken))
        {
            _facilities.Add(new FacilityConfig
            {
                Name = FacilityConfig.DerivePlaceholderName(_facilityToken),
                Token = _facilityToken,
            });
        }
    }
}
