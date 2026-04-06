using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace PureXS.Services;

public class ConfigService : IConfigService
{
    private readonly string _configDir;
    private readonly string _configPath;
    private string? _facilityToken;
    private string? _sironaHost;
    private int? _sironaPort;

    public ConfigService()
    {
        _configDir = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            "PureXS");
        _configPath = Path.Combine(_configDir, "config.json");

        Load();
    }

    public string? FacilityToken => _facilityToken;

    public string ConfigDirectory => _configDir;

    public string? SironaHost => _sironaHost;

    public int? SironaPort => _sironaPort;

    public void SaveFacilityToken(string token)
    {
        _facilityToken = token;
        SaveField("facility_token", token);
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
            _sironaHost = root?["sirona_host"]?.GetValue<string>();
            var portNode = root?["sirona_port"];
            if (portNode is not null)
                _sironaPort = portNode.GetValue<int>();
        }
        catch
        {
            // Corrupt config — treat as empty
        }
    }
}
