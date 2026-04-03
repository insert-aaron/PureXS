using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace PureXS.Services;

public class ConfigService : IConfigService
{
    private readonly string _configDir;
    private readonly string _configPath;
    private string? _facilityToken;

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

    public void SaveFacilityToken(string token)
    {
        _facilityToken = token;

        // Read existing config to preserve other fields
        JsonObject root;
        if (File.Exists(_configPath))
        {
            try
            {
                var existing = File.ReadAllText(_configPath);
                root = JsonNode.Parse(existing)?.AsObject() ?? new JsonObject();
            }
            catch
            {
                root = new JsonObject();
            }
        }
        else
        {
            root = new JsonObject();
        }

        root["facility_token"] = token;

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
        }
        catch
        {
            // Corrupt config — treat as empty
        }
    }
}
