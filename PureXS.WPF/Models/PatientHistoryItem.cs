namespace PureXS.Models;

public class PatientHistoryItem
{
    public string PatientId { get; set; } = "";
    public string DisplayName { get; set; } = "";
    public string LastScan { get; set; } = "";
    public int SessionCount { get; set; }
    public string FolderPath { get; set; } = "";
    public List<SessionEntry> Sessions { get; set; } = [];
}
