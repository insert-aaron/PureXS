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
            // Wire up dependencies
            var host = Environment.GetEnvironmentVariable("SIRONA_IP") ?? "192.168.139.170";
            var port = int.TryParse(Environment.GetEnvironmentVariable("SIRONA_PORT"), out var p) ? p : 12837;
            var facilityToken = Environment.GetEnvironmentVariable("PURECHART_FACILITY_TOKEN")
                ?? "43bd5ee3a662f5cbf468bfc6402eb56ec685fb315461114275b4204402a2cf17";

            ISironaService sirona = new SironaService(host, port);
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
