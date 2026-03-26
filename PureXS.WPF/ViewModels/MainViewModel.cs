using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using PureXS.Models;
using PureXS.Services;

namespace PureXS.ViewModels;

public partial class MainViewModel : ObservableObject, IAsyncDisposable
{
    private readonly ISironaService _sirona;
    private readonly IPureChartService _pureChart;

    // ── Machine state ────────────────────────────────────────────────────
    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(ExposeCommand))]
    private string _machineStatus = "Searching for machine...";

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(ExposeCommand))]
    private bool _isConnected;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(ExposeCommand))]
    private bool _isExposing;

    [ObservableProperty]
    private bool _heartbeatPulse;

    [ObservableProperty]
    private Brush _machineIndicator = Brushes.Yellow;

    // ── Image review state ───────────────────────────────────────────────
    [ObservableProperty]
    private BitmapSource? _receivedImage;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(ConfirmImageCommand))]
    [NotifyCanExecuteChangedFor(nameof(RetakeCommand))]
    [NotifyCanExecuteChangedFor(nameof(ExposeCommand))]
    private bool _isReviewingImage;

    [ObservableProperty]
    private string _reviewStatus = "";

    [ObservableProperty]
    private Brush _reviewStatusColor = Brushes.Transparent;

    [ObservableProperty]
    private bool _isUploading;

    private byte[]? _lastImageBytes;

    // ── Patient state (PureChart only) ───────────────────────────────────
    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(ExposeCommand))]
    private PureChartPatient? _selectedPatient;

    [ObservableProperty]
    private bool _isDockVisible = true;

    [ObservableProperty]
    private string _patientBanner = "";

    [ObservableProperty]
    private string _pureChartSearchQuery = "";

    [ObservableProperty]
    private string _pureChartStatus = "";

    [ObservableProperty]
    private Brush _pureChartStatusColor = new SolidColorBrush(Color.FromRgb(117, 117, 117));

    public ObservableCollection<PureChartPatient> PureChartResults { get; } = [];

    private CancellationTokenSource? _searchDebounce;
    private bool _isSearching;

    // ── Constructor ──────────────────────────────────────────────────────

    public MainViewModel(ISironaService sirona, IPureChartService pureChart)
    {
        _sirona = sirona;
        _pureChart = pureChart;

        _sirona.ConnectionStateChanged += OnConnectionStateChanged;
        _sirona.HeartbeatTick += OnHeartbeatTick;
        _sirona.ImageReceived += OnImageReceived;

        _ = AutoConnectAsync();
        _ = LoadInitialPatientsAsync();
    }

    // ── Auto-connect ─────────────────────────────────────────────────────

    private async Task AutoConnectAsync()
    {
        MachineStatus = "Searching for machine...";
        MachineIndicator = Brushes.Yellow;

        try
        {
            await _sirona.ConnectAsync();
        }
        catch
        {
            MachineStatus = "Machine not found — retrying...";
            MachineIndicator = Brushes.Red;
            await Task.Delay(3000);
            _ = AutoConnectAsync();
        }
    }

    // ── Expose ───────────────────────────────────────────────────────────

    [RelayCommand(CanExecute = nameof(CanExpose))]
    private async Task ExposeAsync()
    {
        try
        {
            _lastImageBytes = null;
            ReceivedImage = null;
            IsReviewingImage = false;
            ReviewStatus = "";
            await _sirona.ExposeAsync();
        }
        catch (Exception ex)
        {
            MachineStatus = $"Expose error — {ex.Message}";
            MachineIndicator = Brushes.Red;
        }
    }

    private bool CanExpose() => IsConnected && !IsExposing && !IsReviewingImage && SelectedPatient is not null;

    // ── Image review + upload ────────────────────────────────────────────

    [RelayCommand(CanExecute = nameof(CanConfirmImage))]
    private async Task ConfirmImageAsync()
    {
        if (SelectedPatient is null || _lastImageBytes is null) return;

        IsUploading = true;
        ReviewStatus = "Sending to PureChart...";
        ReviewStatusColor = new SolidColorBrush(Color.FromRgb(255, 167, 38)); // orange

        try
        {
            var uploadType = ExamTypes.ToPureChartType("Panoramic");
            var result = await _pureChart.UploadAsync(
                SelectedPatient.Id,
                _lastImageBytes,
                "image/png",
                uploadType,
                $"PureXS {uploadType} capture",
                CancellationToken.None);

            if (result.Success)
            {
                ReviewStatus = "Sent to PureChart";
                ReviewStatusColor = new SolidColorBrush(Color.FromRgb(129, 199, 132)); // green

                // Brief pause so user sees the success message
                await Task.Delay(1500);

                // Full reset
                await ResetFlowAsync();
            }
            else
            {
                ReviewStatus = $"Upload failed — {result.Error}";
                ReviewStatusColor = new SolidColorBrush(Color.FromRgb(239, 83, 80)); // red
            }
        }
        catch (Exception ex)
        {
            ReviewStatus = $"Upload failed — {ex.Message}";
            ReviewStatusColor = new SolidColorBrush(Color.FromRgb(239, 83, 80));
        }
        finally
        {
            IsUploading = false;
        }
    }

    private bool CanConfirmImage() => IsReviewingImage && !IsUploading;

    [RelayCommand(CanExecute = nameof(CanRetake))]
    private void Retake()
    {
        // Clear image, go back to expose-ready state
        ReceivedImage = null;
        _lastImageBytes = null;
        IsReviewingImage = false;
        ReviewStatus = "";
    }

    private bool CanRetake() => IsReviewingImage && !IsUploading;

    /// <summary>
    /// Full reset: clear patient, clear image, disconnect, restart flow.
    /// </summary>
    private async Task ResetFlowAsync()
    {
        // Clear image
        ReceivedImage = null;
        _lastImageBytes = null;
        IsReviewingImage = false;
        ReviewStatus = "";

        // Clear patient
        SelectedPatient = null;

        // Disconnect and reconnect (stops HB, restarts discovery)
        await _sirona.DisconnectAsync();
        _ = AutoConnectAsync();
    }

    // ── Patient selection ────────────────────────────────────────────────

    [RelayCommand]
    private void SelectPatient(PureChartPatient? patient)
    {
        SelectedPatient = patient;
    }

    partial void OnSelectedPatientChanged(PureChartPatient? value)
    {
        if (value is not null)
        {
            PatientBanner = $"Patient: {value.LastName}, {value.FirstName} | DOB: {value.Dob} | MRN: {value.MedicalRecordNumber}";
            IsDockVisible = false;
        }
        else
        {
            PatientBanner = "";
            IsDockVisible = true;
        }
    }

    [RelayCommand]
    private void ChangePatient()
    {
        SelectedPatient = null;
    }

    // ── PureChart search ─────────────────────────────────────────────────

    partial void OnPureChartSearchQueryChanged(string value)
    {
        _ = DebouncedSearchAsync(value);
    }

    private async Task DebouncedSearchAsync(string query)
    {
        _searchDebounce?.Cancel();
        _searchDebounce = new CancellationTokenSource();
        var token = _searchDebounce.Token;

        query = query.Trim();
        if (query.Length < 2)
        {
            if (query.Length == 0)
                await RunSearchAsync("a", token);
            else
            {
                PureChartStatus = "Type 2+ characters...";
                PureChartStatusColor = new SolidColorBrush(Color.FromRgb(117, 117, 117));
            }
            return;
        }

        try { await Task.Delay(400, token); }
        catch (TaskCanceledException) { return; }

        await RunSearchAsync(query, token);
    }

    private async Task RunSearchAsync(string query, CancellationToken ct)
    {
        if (_isSearching) return;
        _isSearching = true;

        PureChartStatus = $"Searching \"{query}\"...";
        PureChartStatusColor = new SolidColorBrush(Color.FromRgb(255, 167, 38));

        try
        {
            var patients = await _pureChart.SearchAsync(query, ct);

            PureChartResults.Clear();
            foreach (var p in patients)
                PureChartResults.Add(p);

            _ = LoadProfileImagesAsync(patients, ct);

            PureChartStatus = patients.Count == 0
                ? "0 patients found"
                : $"{patients.Count} patients";
            PureChartStatusColor = patients.Count > 0
                ? new SolidColorBrush(Color.FromRgb(129, 199, 132))
                : new SolidColorBrush(Color.FromRgb(117, 117, 117));
        }
        catch (TaskCanceledException) { }
        catch
        {
            PureChartStatus = "PureChart unavailable";
            PureChartStatusColor = new SolidColorBrush(Color.FromRgb(239, 83, 80));
        }
        finally
        {
            _isSearching = false;
        }
    }

    private async Task LoadInitialPatientsAsync()
    {
        PureChartStatus = "Loading patients...";
        PureChartStatusColor = new SolidColorBrush(Color.FromRgb(255, 167, 38));
        await RunSearchAsync("a", CancellationToken.None);
    }

    private async Task LoadProfileImagesAsync(List<PureChartPatient> patients, CancellationToken ct)
    {
        foreach (var patient in patients)
        {
            if (string.IsNullOrEmpty(patient.ProfilePictureUrl)) continue;
            if (ct.IsCancellationRequested) break;

            try
            {
                var bytes = await _pureChart.DownloadImageAsync(patient.ProfilePictureUrl, ct);
                if (bytes is { Length: > 0 })
                {
                    Application.Current.Dispatcher.Invoke(() =>
                    {
                        var bitmap = new BitmapImage();
                        using var ms = new System.IO.MemoryStream(bytes);
                        bitmap.BeginInit();
                        bitmap.CacheOption = BitmapCacheOption.OnLoad;
                        bitmap.StreamSource = ms;
                        bitmap.DecodePixelWidth = 180;
                        bitmap.EndInit();
                        bitmap.Freeze();
                        patient.ProfileImage = bitmap;

                        var idx = PureChartResults.IndexOf(patient);
                        if (idx >= 0)
                        {
                            PureChartResults.RemoveAt(idx);
                            PureChartResults.Insert(idx, patient);
                        }
                    });
                }
            }
            catch { }
        }
    }

    // ── Sirona event handlers ────────────────────────────────────────────

    private void OnConnectionStateChanged(object? sender, ConnectionState state)
    {
        Application.Current.Dispatcher.Invoke(() =>
        {
            switch (state)
            {
                case ConnectionState.Disconnected:
                    // Only auto-reconnect if we're not in review mode (reset handles it)
                    if (!IsReviewingImage)
                    {
                        MachineStatus = "Machine disconnected — reconnecting...";
                        MachineIndicator = Brushes.Red;
                        IsConnected = false;
                        IsExposing = false;
                        _ = AutoConnectAsync();
                    }
                    break;
                case ConnectionState.Connecting:
                    MachineStatus = "Searching for machine...";
                    MachineIndicator = Brushes.Yellow;
                    IsConnected = false;
                    break;
                case ConnectionState.Connected:
                    MachineStatus = "Machine found — Ready";
                    MachineIndicator = Brushes.LimeGreen;
                    IsConnected = true;
                    IsExposing = false;
                    break;
                case ConnectionState.Exposing:
                    MachineStatus = "Exposing...";
                    MachineIndicator = Brushes.Orange;
                    IsExposing = true;
                    break;
                case ConnectionState.Reconnecting:
                    MachineStatus = "Reconnecting to machine...";
                    MachineIndicator = Brushes.Yellow;
                    IsConnected = false;
                    IsExposing = false;
                    break;
            }
        });
    }

    private void OnHeartbeatTick(object? sender, EventArgs e)
    {
        Application.Current.Dispatcher.Invoke(() => HeartbeatPulse = !HeartbeatPulse);
    }

    private void OnImageReceived(object? sender, byte[] imageBytes)
    {
        Application.Current.Dispatcher.Invoke(() =>
        {
            try
            {
                // Store raw bytes for upload
                _lastImageBytes = imageBytes;

                var bitmap = new BitmapImage();
                using var ms = new System.IO.MemoryStream(imageBytes);
                bitmap.BeginInit();
                bitmap.CacheOption = BitmapCacheOption.OnLoad;
                bitmap.StreamSource = ms;
                bitmap.EndInit();
                bitmap.Freeze();
                ReceivedImage = bitmap;

                // Enter review mode
                IsReviewingImage = true;
                IsExposing = false;
                MachineStatus = "Scan complete — review image";
                MachineIndicator = new SolidColorBrush(Color.FromRgb(79, 195, 247)); // light blue
                ReviewStatus = "Review the image with the patient";
                ReviewStatusColor = new SolidColorBrush(Color.FromRgb(79, 195, 247));
            }
            catch { }
        });
    }

    public async ValueTask DisposeAsync()
    {
        _searchDebounce?.Cancel();
        _searchDebounce?.Dispose();
        _sirona.ConnectionStateChanged -= OnConnectionStateChanged;
        _sirona.HeartbeatTick -= OnHeartbeatTick;
        _sirona.ImageReceived -= OnImageReceived;
        await _sirona.DisposeAsync();
        if (_pureChart is IDisposable disposable)
            disposable.Dispose();
    }
}
