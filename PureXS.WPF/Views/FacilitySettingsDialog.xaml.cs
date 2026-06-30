using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Controls;
using PureXS.Models;

namespace PureXS.Views;

/// <summary>
/// Add / edit / remove PureChart facilities and pick the active one. Edits a
/// working copy; on Save it exposes <see cref="ResultFacilities"/> and
/// <see cref="ResultActiveIndex"/> for the caller to persist.
/// </summary>
public partial class FacilitySettingsDialog : Window
{
    private readonly ObservableCollection<FacilityConfig> _working = new();

    /// <summary>Cleaned facilities (empty-token rows dropped) — valid after Save.</summary>
    public IReadOnlyList<FacilityConfig> ResultFacilities { get; private set; } = new List<FacilityConfig>();

    /// <summary>Active index into <see cref="ResultFacilities"/> — valid after Save.</summary>
    public int ResultActiveIndex { get; private set; }

    public FacilitySettingsDialog(
        IReadOnlyList<FacilityConfig> facilities, FacilityConfig? active)
    {
        InitializeComponent();

        foreach (var f in facilities)
        {
            _working.Add(new FacilityConfig
            {
                Name = f.Name,
                Token = f.Token,
                IsActive = ReferenceEquals(f, active)
                           || (active is not null && f.Token == active.Token && f.Name == active.Name),
            });
        }
        if (_working.Count > 0 && !_working.Any(f => f.IsActive))
            _working[0].IsActive = true;

        RowsControl.ItemsSource = _working;
        Owner = Application.Current?.MainWindow;
    }

    private void AddButton_Click(object sender, RoutedEventArgs e)
    {
        _working.Add(new FacilityConfig { Name = "", Token = "" });
    }

    private void RemoveButton_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { DataContext: FacilityConfig fac })
        {
            var wasActive = fac.IsActive;
            _working.Remove(fac);
            if (wasActive && _working.Count > 0 && !_working.Any(f => f.IsActive))
                _working[0].IsActive = true;
        }
    }

    private void Save_Click(object sender, RoutedEventArgs e)
    {
        var cleaned = _working
            .Where(f => !string.IsNullOrWhiteSpace(f.Token))
            .ToList();

        if (cleaned.Count == 0)
        {
            StatusText.Text = "Add at least one facility token.";
            return;
        }

        var activeIdx = cleaned.FindIndex(f => f.IsActive);
        if (activeIdx < 0) activeIdx = 0;

        ResultFacilities = cleaned;
        ResultActiveIndex = activeIdx;
        DialogResult = true;
        Close();
    }

    private void Cancel_Click(object sender, RoutedEventArgs e)
    {
        DialogResult = false;
        Close();
    }
}
