using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using PureXS.Models;

namespace PureXS.ViewModels;

public partial class HistoryViewModel : ObservableObject
{
    private static readonly string PatientsDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "PureXS", "patients");

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true
    };

    public ObservableCollection<PatientHistoryItem> Patients { get; } = [];

    [ObservableProperty]
    private PatientHistoryItem? _selectedPatient;

    public ObservableCollection<SessionEntry> SelectedSessions { get; } = [];

    partial void OnSelectedPatientChanged(PatientHistoryItem? value)
    {
        SelectedSessions.Clear();
        if (value?.Sessions is not null)
        {
            // Show sessions in reverse chronological order
            foreach (var session in value.Sessions.OrderByDescending(s => s.Timestamp))
                SelectedSessions.Add(session);
        }
    }

    [RelayCommand]
    private async Task LoadAsync()
    {
        Patients.Clear();
        SelectedSessions.Clear();
        SelectedPatient = null;

        if (!Directory.Exists(PatientsDir))
            return;

        var items = new List<PatientHistoryItem>();

        await Task.Run(() =>
        {
            foreach (var patientDir in Directory.GetDirectories(PatientsDir))
            {
                var sessionsPath = Path.Combine(patientDir, "sessions.json");
                if (!File.Exists(sessionsPath))
                    continue;

                try
                {
                    var json = File.ReadAllText(sessionsPath);
                    var sessionsFile = JsonSerializer.Deserialize<SessionsFile>(json, JsonOptions);
                    if (sessionsFile is null || sessionsFile.Sessions.Count == 0)
                        continue;

                    var patientId = sessionsFile.PatientId;
                    if (string.IsNullOrEmpty(patientId))
                        patientId = Path.GetFileName(patientDir) ?? "Unknown";

                    // Derive display name from the image filenames or patient ID
                    var displayName = DeriveDisplayName(sessionsFile, patientDir);
                    var lastSession = sessionsFile.Sessions
                        .OrderByDescending(s => s.Timestamp)
                        .First();

                    items.Add(new PatientHistoryItem
                    {
                        PatientId = patientId,
                        DisplayName = displayName,
                        LastScan = lastSession.Timestamp,
                        SessionCount = sessionsFile.Sessions.Count,
                        FolderPath = patientDir,
                        Sessions = sessionsFile.Sessions
                    });
                }
                catch
                {
                    // Skip corrupt session files
                }
            }
        });

        // Sort by most recent scan first
        foreach (var item in items.OrderByDescending(i => i.LastScan))
            Patients.Add(item);

        // Auto-select first patient if available
        if (Patients.Count > 0)
            SelectedPatient = Patients[0];
    }

    private static string DeriveDisplayName(SessionsFile sessionsFile, string patientDir)
    {
        // Try to extract name from image filenames (format: LastName_FirstName_DOB_timestamp_panoramic.png)
        var firstSessionWithImage = sessionsFile.Sessions.FirstOrDefault(s => !string.IsNullOrEmpty(s.ImageFile));
        if (firstSessionWithImage?.ImageFile is not null)
        {
            var parts = firstSessionWithImage.ImageFile.Split('_');
            if (parts.Length >= 2)
                return $"{parts[1]} {parts[0]}";
        }

        // Fallback to directory name / patient ID
        return sessionsFile.PatientId.Length > 8
            ? sessionsFile.PatientId[..8] + "..."
            : sessionsFile.PatientId;
    }

    [RelayCommand]
    private void OpenFolder(PatientHistoryItem? item)
    {
        if (item is null || !Directory.Exists(item.FolderPath)) return;
        Process.Start(new ProcessStartInfo
        {
            FileName = item.FolderPath,
            UseShellExecute = true
        });
    }

    [RelayCommand]
    private void ViewImage(SessionEntry? session)
    {
        if (session?.ImageFile is null || SelectedPatient is null) return;

        var imagePath = Path.Combine(SelectedPatient.FolderPath, session.ImageFile);
        if (!File.Exists(imagePath)) return;

        Process.Start(new ProcessStartInfo
        {
            FileName = imagePath,
            UseShellExecute = true
        });
    }

    [RelayCommand]
    private async Task RefreshAsync()
    {
        await LoadAsync();
    }
}
