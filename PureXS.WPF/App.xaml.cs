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

        // Wire up dependencies — replace with DI container if needed later
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
}
