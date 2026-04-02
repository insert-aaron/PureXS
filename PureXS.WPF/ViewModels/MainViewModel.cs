using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Microsoft.Win32;
using PureXS.Models;
using PureXS.Services;
using PureXS.Views;

namespace PureXS.ViewModels;

public partial class MainViewModel : ObservableObject, IAsyncDisposable
{
    private readonly ISironaService _sirona;
    private readonly IPureChartService _pureChart;
    private readonly IImageProcessingService _imageProcessor;
    private readonly IToastService _toast;
    private readonly IEventLogService _log;
    private readonly IPatientOutputService _patientOutput;
    private readonly IDicomExportService _dicom;

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

    // ── Scan progress state ─────────────────────────────────────────────
    [ObservableProperty]
    private bool _isScanInProgress;

    [ObservableProperty]
    private string _scanProgressText = "";

    [ObservableProperty]
    private int _scanByteCount;

    public double ScanProgressMB => ScanByteCount / 1_048_576.0;

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
    [NotifyCanExecuteChangedFor(nameof(RetryUploadCommand))]
    private bool _isUploading;

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(RetryUploadCommand))]
    private bool _isUploadFailed;

    private (string patientId, byte[] fileBytes, string contentType, string uploadType, string title)? _lastUploadArgs;

    private Window? _historyWindow;

    private byte[]? _lastImageBytes;

    // ── DICOM export state ──────────────────────────────────────────────
    [ObservableProperty]
    private string? _lastDcmPath;

    public bool HasDicomFile => !string.IsNullOrEmpty(LastDcmPath);

    // ── Image viewer state ───────────────────────────────────────────────
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(ZoomPercentText))]
    private double _zoomLevel = 1.0;

    public string ZoomPercentText => $"{(int)(ZoomLevel * 100)}%";

    [ObservableProperty]
    private double _panX = 0.0;

    [ObservableProperty]
    private double _panY = 0.0;

    [ObservableProperty]
    private double _brightness = 0;

    [ObservableProperty]
    private double _contrast = 1.0;

    [ObservableProperty]
    private BitmapSource? _displayImage;

    public bool HasImage => ReceivedImage is not null;

    // ── Exam type ────────────────────────────────────────────────────────
    [ObservableProperty]
    private string _selectedExamType = "Panoramic";

    public string[] ExamTypeOptions => ExamTypes.All;

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

    // ── Toast state ─────────────────────────────────────────────────────
    public ObservableCollection<ToastItem> ActiveToasts { get; } = [];

    // ── Constructor ──────────────────────────────────────────────────────

    public MainViewModel(
        ISironaService sirona,
        IPureChartService pureChart,
        IImageProcessingService imageProcessor,
        IToastService toast,
        IEventLogService log,
        IPatientOutputService patientOutput,
        IDicomExportService dicom)
    {
        _sirona = sirona;
        _pureChart = pureChart;
        _imageProcessor = imageProcessor;
        _toast = toast;
        _log = log;
        _patientOutput = patientOutput;
        _dicom = dicom;

        _toast.ToastRequested += OnToastRequested;
        _sirona.ConnectionStateChanged += OnConnectionStateChanged;
        _sirona.HeartbeatTick += OnHeartbeatTick;
        _sirona.ImageReceived += OnImageReceived;
        _sirona.ScanProgress += OnScanProgress;

        _log.Log("PureXS application started");
        _ = AutoConnectAsync();
        _ = LoadInitialPatientsAsync();
    }

    private void OnToastRequested(object? sender, ToastItem item)
    {
        Application.Current.Dispatcher.Invoke(() =>
        {
            ActiveToasts.Add(item);

            var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(item.DurationMs) };
            timer.Tick += (_, _) =>
            {
                timer.Stop();
                ActiveToasts.Remove(item);
            };
            timer.Start();
        });
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
        catch (Exception ex)
        {
            MachineStatus = "Machine not found — retrying...";
            MachineIndicator = Brushes.Red;
            _log.Log($"Connection failed: {ex.Message}", "warning");
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
            IsScanInProgress = true;
            ScanByteCount = 0;
            ScanProgressText = "Waiting for scan data...";
            _log.Log($"Expose started for patient {SelectedPatient?.Id}, exam={SelectedExamType}");
            _toast.Show("Exposure started", "info", 2000);
            await _sirona.ExposeAsync();
        }
        catch (Exception ex)
        {
            MachineStatus = $"Expose error — {ex.Message}";
            MachineIndicator = Brushes.Red;
            _toast.Show($"Expose error: {ex.Message}", "error", 5000);
            _log.Log($"Expose error: {ex.Message}", "error");
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
            var uploadType = ExamTypes.ToPureChartType(SelectedExamType);
            var title = $"PureXS {uploadType} capture";

            // Store args for retry
            _lastUploadArgs = (SelectedPatient.Id, _lastImageBytes, "image/png", uploadType, title);

            var result = await _pureChart.UploadAsync(
                SelectedPatient.Id,
                _lastImageBytes,
                "image/png",
                uploadType,
                title,
                CancellationToken.None);

            if (result.Success)
            {
                IsUploadFailed = false;
                _lastUploadArgs = null;

                ReviewStatus = "Sent to PureChart";
                ReviewStatusColor = new SolidColorBrush(Color.FromRgb(129, 199, 132)); // green
                _toast.Show("Image uploaded to PureChart", "success", 3000);
                _log.Log($"Upload success for patient {SelectedPatient?.Id}, type={uploadType}");

                // Brief pause so user sees the success message
                await Task.Delay(1500);

                // Full reset
                await ResetFlowAsync();
            }
            else
            {
                IsUploadFailed = true;
                ReviewStatus = $"Upload failed — {result.Error}";
                ReviewStatusColor = new SolidColorBrush(Color.FromRgb(239, 83, 80)); // red
                _toast.Show($"Upload failed: {result.Error}", "error", 5000);
                _log.Log($"Upload failed for patient {SelectedPatient?.Id}: {result.Error}", "error");
            }
        }
        catch (Exception ex)
        {
            IsUploadFailed = true;
            ReviewStatus = $"Upload failed — {ex.Message}";
            ReviewStatusColor = new SolidColorBrush(Color.FromRgb(239, 83, 80));
            _toast.Show($"Upload error: {ex.Message}", "error", 5000);
            _log.Log($"Upload exception for patient {SelectedPatient?.Id}: {ex.Message}", "error");
        }
        finally
        {
            IsUploading = false;
        }
    }

    private bool CanConfirmImage() => IsReviewingImage && !IsUploading;

    [RelayCommand(CanExecute = nameof(CanRetryUpload))]
    private async Task RetryUploadAsync()
    {
        if (!_lastUploadArgs.HasValue) return;

        var args = _lastUploadArgs.Value;

        IsUploading = true;
        ReviewStatus = "Retrying upload to PureChart...";
        ReviewStatusColor = new SolidColorBrush(Color.FromRgb(255, 167, 38)); // orange

        try
        {
            var result = await _pureChart.UploadAsync(
                args.patientId,
                args.fileBytes,
                args.contentType,
                args.uploadType,
                args.title,
                CancellationToken.None);

            if (result.Success)
            {
                IsUploadFailed = false;
                _lastUploadArgs = null;

                ReviewStatus = "Sent to PureChart";
                ReviewStatusColor = new SolidColorBrush(Color.FromRgb(129, 199, 132)); // green
                _toast.Show("Image uploaded to PureChart", "success", 3000);
                _log.Log($"Retry upload success for patient {args.patientId}, type={args.uploadType}");

                await Task.Delay(1500);
                await ResetFlowAsync();
            }
            else
            {
                IsUploadFailed = true;
                ReviewStatus = $"Upload failed — {result.Error}";
                ReviewStatusColor = new SolidColorBrush(Color.FromRgb(239, 83, 80));
                _toast.Show($"Retry failed: {result.Error}", "error", 5000);
                _log.Log($"Retry upload failed for patient {args.patientId}: {result.Error}", "error");
            }
        }
        catch (Exception ex)
        {
            IsUploadFailed = true;
            ReviewStatus = $"Upload failed — {ex.Message}";
            ReviewStatusColor = new SolidColorBrush(Color.FromRgb(239, 83, 80));
            _toast.Show($"Retry error: {ex.Message}", "error", 5000);
            _log.Log($"Retry upload exception for patient {args.patientId}: {ex.Message}", "error");
        }
        finally
        {
            IsUploading = false;
        }
    }

    private bool CanRetryUpload() => IsUploadFailed && _lastUploadArgs.HasValue && !IsUploading;

    [RelayCommand(CanExecute = nameof(CanRetake))]
    private void Retake()
    {
        // Clear image, go back to expose-ready state
        ReceivedImage = null;
        _lastImageBytes = null;
        IsReviewingImage = false;
        IsUploadFailed = false;
        _lastUploadArgs = null;
        ReviewStatus = "";
        IsScanInProgress = false;
        ScanByteCount = 0;
        ScanProgressText = "";
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
        IsUploadFailed = false;
        _lastUploadArgs = null;
        ReviewStatus = "";
        LastDcmPath = null;

        // Clear patient
        SelectedPatient = null;

        // Disconnect and reconnect (stops HB, restarts discovery)
        await _sirona.DisconnectAsync();
        _ = AutoConnectAsync();
    }

    // ── Image viewer commands ────────────────────────────────────────────

    [RelayCommand]
    private void FitToCanvas()
    {
        ZoomLevel = 1.0;
        PanX = 0;
        PanY = 0;
    }

    [RelayCommand]
    private void ResetAdjustments()
    {
        Brightness = 0;
        Contrast = 1.0;
        ApplyImageAdjustments();
    }

    [RelayCommand]
    private void SavePng()
    {
        if (_lastImageBytes is null) return;

        var timestamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
        string defaultFilename;

        if (SelectedPatient is not null)
        {
            defaultFilename = $"{SelectedPatient.LastName}_{SelectedPatient.FirstName}_{timestamp}_panoramic.png";
        }
        else
        {
            defaultFilename = $"purexs_scan_{timestamp}.png";
        }

        var dlg = new SaveFileDialog
        {
            Filter = "PNG Image|*.png",
            FileName = defaultFilename
        };

        if (dlg.ShowDialog() == true)
        {
            File.WriteAllBytes(dlg.FileName, _lastImageBytes);
        }
    }

    [RelayCommand]
    private void NewPatient()
    {
        ReceivedImage = null;
        DisplayImage = null;
        _lastImageBytes = null;
        SelectedPatient = null;
        LastDcmPath = null;
        IsUploadFailed = false;
        _lastUploadArgs = null;
        Brightness = 0;
        Contrast = 1.0;
        ZoomLevel = 1.0;
        PanX = 0;
        PanY = 0;
        IsReviewingImage = false;
        ReviewStatus = "";
        IsScanInProgress = false;
        ScanByteCount = 0;
        ScanProgressText = "";
    }

    [RelayCommand]
    private void OpenHistory()
    {
        if (_historyWindow is not null)
        {
            _historyWindow.Activate();
            return;
        }

        var vm = new HistoryViewModel();
        var window = new HistoryWindow { DataContext = vm };
        window.Closed += (_, _) => _historyWindow = null;
        _historyWindow = window;
        _ = vm.LoadCommand.ExecuteAsync(null);
        window.Show();
    }

    [RelayCommand]
    private void OpenPreviewEdit()
    {
        if (ReceivedImage is null) return;

        var window = new ImageEditWindow(ReceivedImage, _lastImageBytes);
        window.Owner = Application.Current.MainWindow;
        window.ImageApplied += (_, editedImage) =>
        {
            ReceivedImage = editedImage;
            ApplyImageAdjustments();
        };
        window.ShowDialog();
    }

    [RelayCommand]
    private void OpenDicomFolder()
    {
        if (string.IsNullOrEmpty(LastDcmPath)) return;
        var dir = Path.GetDirectoryName(LastDcmPath);
        if (dir is not null && Directory.Exists(dir))
        {
            Process.Start("explorer.exe", dir);
        }
    }

    [RelayCommand]
    private void ViewDicom()
    {
        if (string.IsNullOrEmpty(LastDcmPath) || !File.Exists(LastDcmPath)) return;
        Process.Start(new ProcessStartInfo(LastDcmPath) { UseShellExecute = true });
    }

    // ── Partial change handlers ─────────────────────────────────────────

    partial void OnBrightnessChanged(double value)
    {
        ApplyImageAdjustments();
    }

    partial void OnContrastChanged(double value)
    {
        ApplyImageAdjustments();
    }

    partial void OnReceivedImageChanged(BitmapSource? value)
    {
        ApplyImageAdjustments();
        OnPropertyChanged(nameof(HasImage));
        OnPropertyChanged(nameof(HasDicomFile));
    }

    partial void OnLastDcmPathChanged(string? value)
    {
        OnPropertyChanged(nameof(HasDicomFile));
    }

    // ── Image adjustment helper ─────────────────────────────────────────

    private void ApplyImageAdjustments()
    {
        if (ReceivedImage is null)
        {
            DisplayImage = null;
            return;
        }

        var converted = new FormatConvertedBitmap(ReceivedImage, PixelFormats.Bgra32, null, 0);
        int width = converted.PixelWidth;
        int height = converted.PixelHeight;
        int stride = width * 4;
        var pixels = new byte[height * stride];
        converted.CopyPixels(pixels, stride, 0);

        double contrast = Contrast;
        double brightness = Brightness;

        for (int i = 0; i < pixels.Length; i += 4)
        {
            // B, G, R channels (skip A at i+3)
            for (int c = 0; c < 3; c++)
            {
                double original = pixels[i + c];
                double newVal = 128 + contrast * (original - 128) + brightness;
                pixels[i + c] = (byte)Math.Clamp(newVal, 0, 255);
            }
        }

        var wb = new WriteableBitmap(width, height, converted.DpiX, converted.DpiY, PixelFormats.Bgra32, null);
        wb.WritePixels(new Int32Rect(0, 0, width, height), pixels, stride, 0);
        wb.Freeze();
        DisplayImage = wb;
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
                        _toast.Show("Machine disconnected", "warning", 4000);
                        _log.Log("Machine disconnected", "warning");
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
                    _toast.Show("Machine connected", "success", 3000);
                    _log.Log("Machine connected");
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
                    _toast.Show("Reconnecting to machine...", "warning", 3000);
                    _log.Log("Reconnecting to machine", "warning");
                    break;
            }
        });
    }

    private void OnHeartbeatTick(object? sender, EventArgs e)
    {
        Application.Current.Dispatcher.Invoke(() => HeartbeatPulse = !HeartbeatPulse);
    }

    private void OnScanProgress(object? sender, int byteCount)
    {
        Application.Current.Dispatcher.Invoke(() =>
        {
            ScanByteCount = byteCount;
            ScanProgressText = $"Receiving scan data... {byteCount / 1_048_576.0:F1} MB";
            IsScanInProgress = true;
        });
    }

    partial void OnScanByteCountChanged(int value)
    {
        OnPropertyChanged(nameof(ScanProgressMB));
    }

    private void OnImageReceived(object? sender, byte[] rawBytes)
    {
        // Process raw scan bytes through the Python decoder pipeline (async)
        _ = ProcessAndDisplayImageAsync(rawBytes);
    }

    private async Task ProcessAndDisplayImageAsync(byte[] rawBytes)
    {
        Application.Current.Dispatcher.Invoke(() =>
        {
            IsScanInProgress = false;
            MachineStatus = "Processing image...";
            MachineIndicator = new SolidColorBrush(Color.FromRgb(255, 167, 38)); // orange
            IsExposing = false;
        });

        _log.Log($"Image received, {rawBytes.Length} raw bytes, processing...");
        _toast.Show("Processing scan image...", "info", 2000);

        // Run the decoder — produces a finished panoramic PNG
        byte[]? processedBytes = null;
        try
        {
            processedBytes = await _imageProcessor.ProcessRawScanAsync(rawBytes);
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine($"[MainVM] Decoder failed: {ex.Message}");
            _log.Log($"Image decoder failed: {ex.Message}", "warning");
            _toast.Show("Decoder unavailable, showing raw preview", "warning", 4000);
        }

        // Fall back to raw bytes if decoder is not available or fails
        var displayBytes = processedBytes ?? rawBytes;

        Application.Current.Dispatcher.Invoke(() =>
        {
            try
            {
                _lastImageBytes = displayBytes;

                var bitmap = new BitmapImage();
                using var ms = new System.IO.MemoryStream(displayBytes);
                bitmap.BeginInit();
                bitmap.CacheOption = BitmapCacheOption.OnLoad;
                bitmap.StreamSource = ms;
                bitmap.EndInit();
                bitmap.Freeze();
                ReceivedImage = bitmap;

                // Reset viewer state for new image
                ZoomLevel = 1.0;
                PanX = 0;
                PanY = 0;
                Brightness = 0;
                Contrast = 1.0;

                IsReviewingImage = true;
                MachineStatus = processedBytes is not null
                    ? "Scan complete — review image"
                    : "Scan complete — raw preview (decoder unavailable)";
                MachineIndicator = new SolidColorBrush(Color.FromRgb(79, 195, 247));
                ReviewStatus = "Review the image with the patient";
                ReviewStatusColor = new SolidColorBrush(Color.FromRgb(79, 195, 247));

                _toast.Show("Scan complete -- ready for review", "success", 3000);
                _log.Log($"Image displayed, {displayBytes.Length} bytes, decoded={processedBytes is not null}");
            }
            catch (Exception ex)
            {
                _toast.Show("Failed to display image", "error", 5000);
                _log.Log($"Image display error: {ex.Message}", "error");
            }
        });

        // Auto-export DICOM in background if patient is selected
        if (SelectedPatient is not null && _lastImageBytes is not null)
        {
            _ = ExportDicomInBackgroundAsync(SelectedPatient, _lastImageBytes);
        }
    }

    private async Task ExportDicomInBackgroundAsync(PureChartPatient patient, byte[] imageBytes)
    {
        try
        {
            var patientName = $"{patient.LastName}^{patient.FirstName}";
            var patientDob = ParseDobToDicom(patient.Dob);
            var outputDir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                "PureXS", "DICOM");
            var timestamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
            var prefix = $"{patient.LastName}_{patient.FirstName}_{timestamp}";

            var dcmPath = await _dicom.ExportAsync(
                patientName,
                patient.MedicalRecordNumber,
                patientDob,
                SelectedExamType,
                imageBytes,
                kvPeak: 60.0,
                outputDir,
                prefix,
                CancellationToken.None);

            Application.Current.Dispatcher.Invoke(() =>
            {
                if (dcmPath is not null)
                {
                    LastDcmPath = dcmPath;
                    _toast.Show("DICOM file saved", "success", 3000);
                    _log.Log($"DICOM exported: {dcmPath}");
                }
                else
                {
                    _toast.Show("DICOM export failed", "error", 4000);
                    _log.Log("DICOM export returned null", "warning");
                }
            });
        }
        catch (Exception ex)
        {
            Application.Current.Dispatcher.Invoke(() =>
            {
                _toast.Show($"DICOM export error: {ex.Message}", "error", 4000);
                _log.Log($"DICOM export error: {ex.Message}", "error");
            });
        }
    }

    private static string ParseDobToDicom(string dob)
    {
        // Try common formats and convert to YYYYMMDD
        if (DateTime.TryParse(dob, out var parsed))
            return parsed.ToString("yyyyMMdd");
        // Already in YYYYMMDD format or unrecognized — return as-is
        return dob.Replace("-", "").Replace("/", "");
    }

    public async ValueTask DisposeAsync()
    {
        _searchDebounce?.Cancel();
        _searchDebounce?.Dispose();
        _toast.ToastRequested -= OnToastRequested;
        _sirona.ConnectionStateChanged -= OnConnectionStateChanged;
        _sirona.HeartbeatTick -= OnHeartbeatTick;
        _sirona.ImageReceived -= OnImageReceived;
        _sirona.ScanProgress -= OnScanProgress;
        _log.Log("PureXS application shutting down");
        await _sirona.DisposeAsync();
        if (_pureChart is IDisposable disposable)
            disposable.Dispose();
    }
}
