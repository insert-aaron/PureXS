namespace PureXS.Services;

public interface IConfigService
{
    string? FacilityToken { get; }
    void SaveFacilityToken(string token);
    string ConfigDirectory { get; }
}
