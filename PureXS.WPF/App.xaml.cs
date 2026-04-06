using System.IO;
using System.Windows;
using PureXS.Services;
using PureXS.ViewModels;
using PureXS.Views;

namespace PureXS;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

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
}
