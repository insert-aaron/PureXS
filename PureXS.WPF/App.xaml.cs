using System.IO;
using System.Windows;
using System.Windows.Media;
using PureXS.Services;
using PureXS.ViewModels;
using PureXS.Views;

namespace PureXS;

public partial class App : Application
{
    public static bool IsDarkMode { get; private set; } = true;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        // Apply dark mode brushes
        ApplyTheme(isDark: true);

        // Catch all unhandled exceptions and show them
        AppDomain.CurrentDomain.UnhandledException += (s, args) =>
        {
            var ex = args.ExceptionObject as Exception;
            var msg = $"Fatal error:\n{ex?.Message}\n\n{ex?.StackTrace}";
            File.WriteAllText(Path.Combine(AppContext.BaseDirectory, "crash.log"), msg);
            MessageBox.Show(msg, "PureXS Crash", MessageBoxButton.OK, MessageBoxImage.Error);
        };

        DispatcherUnhandledException += (s, args) =>
        {
            var msg = $"UI error:\n{args.Exception.Message}\n\n{args.Exception.StackTrace}";
            File.WriteAllText(Path.Combine(AppContext.BaseDirectory, "crash.log"), msg);
            MessageBox.Show(msg, "PureXS Error", MessageBoxButton.OK, MessageBoxImage.Error);
            args.Handled = true;
        };

        try
        {
            // Config service — loads persisted settings from %AppData%/PureXS/config.json
            IConfigService config = new ConfigService();

            // Wire up dependencies — priority: env var > persisted config > hardcoded default
            var host = Environment.GetEnvironmentVariable("SIRONA_IP")
                ?? config.SironaHost
                ?? "192.168.139.170";
            var port = int.TryParse(Environment.GetEnvironmentVariable("SIRONA_PORT"), out var p)
                ? p
                : config.SironaPort ?? 12837;

            // Resolve facility token: env var > persisted config > first-launch prompt
            var facilityToken = Environment.GetEnvironmentVariable("PURECHART_FACILITY_TOKEN")
                ?? config.FacilityToken;

            if (string.IsNullOrWhiteSpace(facilityToken))
            {
                var dialog = new FacilityTokenDialog();
                if (dialog.ShowDialog() == true && !string.IsNullOrWhiteSpace(dialog.Token))
                {
                    facilityToken = dialog.Token;
                    config.SaveFacilityToken(facilityToken);
                }
                else
                {
                    MessageBox.Show(
                        "No facility token was provided.\nPureChart cloud features will be unavailable.",
                        "PureXS",
                        MessageBoxButton.OK,
                        MessageBoxImage.Warning);
                    facilityToken = string.Empty;
                }
            }

            ISironaService sirona = new SironaService(host, port, config: config);
            IPureChartService pureChart = new PureChartService(facilityToken);
            IImageProcessingService imageProcessor = new ImageProcessingService();
            IPatientOutputService patientOutput = new PatientOutputService();
            IToastService toast = new ToastService();
            IEventLogService log = new EventLogService();
            IDicomExportService dicom = new DicomExportService();
            var viewModel = new MainViewModel(sirona, pureChart, imageProcessor, toast, log, patientOutput, dicom);
            var window = new MainWindow(viewModel);

            MainWindow = window;
            window.Show();
        }
        catch (Exception ex)
        {
            var msg = $"Startup failed:\n{ex.Message}\n\n{ex.StackTrace}";
            File.WriteAllText(Path.Combine(AppContext.BaseDirectory, "crash.log"), msg);
            MessageBox.Show(msg, "PureXS Startup Error", MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown(1);
        }
    }

    /// <summary>
    /// Switches all theme-dependent brushes between Dark and Light mode.
    /// Call from any window via App.ToggleTheme() or App.ApplyTheme(isDark).
    /// </summary>
    public static void ToggleTheme()
    {
        ApplyTheme(!IsDarkMode);
    }

    public static void ApplyTheme(bool isDark)
    {
        IsDarkMode = isDark;
        var res = Current.Resources;

        // ── Core Surfaces ─────────────────────────────────────────────
        SetBrush(res, "WindowBg",        isDark ? "#0F172A" : "#F8FAFC");
        SetBrush(res, "PanelBg",         isDark ? "#111827" : "#F1F5F9");
        SetBrush(res, "CardBg",          isDark ? "#1E293B" : "#FFFFFF");
        SetBrush(res, "FieldBg",         isDark ? "#0F172A" : "#FFFFFF");
        SetBrush(res, "ToolbarBg",       isDark ? "#0F172A" : "#FFFFFF");
        SetBrush(res, "BottomToolbarBg", isDark ? "#111827" : "#F8FAFC");
        SetBrush(res, "StatusBarBg",     isDark ? "#020617" : "#F8FAFC");
        SetBrush(res, "ImageViewerBg",   isDark ? "#030712" : "#F1F5F9");

        // ── Top Bar Gradient ──────────────────────────────────────────
        SetColor(res, "TopBarStart", isDark ? "#0F172A" : "#1D4ED8");
        SetColor(res, "TopBarMid",   isDark ? "#1E3A5F" : "#2563EB");
        SetColor(res, "TopBarEnd",   isDark ? "#1D4ED8" : "#3B82F6");

        // ── Text ──────────────────────────────────────────────────────
        SetBrush(res, "TextPrimary",   isDark ? "#E2E8F0" : "#0F172A");
        SetBrush(res, "TextSecondary", isDark ? "#94A3B8" : "#64748B");
        SetBrush(res, "TextMuted",     isDark ? "#475569" : "#94A3B8");

        // ── Borders ───────────────────────────────────────────────────
        SetBrush(res, "BorderPrimary", isDark ? "#1E293B" : "#E2E8F0");
        SetBrush(res, "BorderSubtle",  isDark ? "#334155" : "#CBD5E1");

        // ── Interactive States ────────────────────────────────────────
        SetBrush(res, "HoverBg",       isDark ? "#1E3A5F" : "#DBEAFE");
        SetBrush(res, "SelectedBg",    isDark ? "#1E40AF" : "#BFDBFE");
        SetBrush(res, "DisabledBg",    isDark ? "#1E293B" : "#F1F5F9");
        SetBrush(res, "DisabledText",  isDark ? "#475569" : "#94A3B8");
        SetBrush(res, "DisabledBorder",isDark ? "#334155" : "#CBD5E1");

        // ── Toolbar Buttons ───────────────────────────────────────────
        SetBrush(res, "ToolbarBtnBg",    isDark ? "#1E293B" : "#F1F5F9");
        SetBrush(res, "ToolbarBtnHover", isDark ? "#334155" : "#E2E8F0");
        SetBrush(res, "ToolbarBtnBorder",isDark ? "#334155" : "#CBD5E1");

        // ── Patient / UI Specialty ────────────────────────────────────
        SetBrush(res, "InitialsBg",      isDark ? "#1E40AF" : "#DBEAFE");
        SetBrush(res, "TabActiveBg",     isDark ? "#1E40AF" : "#DBEAFE");
        SetBrush(res, "TabActiveBorder", isDark ? "#3B82F6" : "#2563EB");
        SetBrush(res, "TabInactiveBg",   isDark ? "#0F172A" : "#F1F5F9");
        SetBrush(res, "PatientBannerFg", isDark ? "#3B82F6" : "#1D4ED8");
        SetBrush(res, "ExamBadgeBg",     isDark ? "#1E40AF" : "#DBEAFE");
        SetBrush(res, "ExamBadgeFg",     isDark ? "#3B82F6" : "#1D4ED8");
        SetBrush(res, "AvatarBorder",    isDark ? "#334155" : "#CBD5E1");
        SetBrush(res, "WinControlHover", isDark ? "#1E293B" : "#E2E8F0");
    }

    private static void SetBrush(ResourceDictionary res, string key, string hex)
    {
        var color = (Color)ColorConverter.ConvertFromString(hex);
        // Replace the resource — DynamicResource bindings auto-update
        res[key] = new SolidColorBrush(color);
    }

    private static void SetColor(ResourceDictionary res, string key, string hex)
    {
        res[key] = (Color)ColorConverter.ConvertFromString(hex);
    }
}
