using System.Windows;
using System.Windows.Input;
using PureXS.ViewModels;

namespace PureXS.Views;

public partial class MainWindow : Window
{
    private bool _isDragging;
    private Point _dragStart;
    private double _startPanX;
    private double _startPanY;

    private const double ZoomFactor = 1.15;
    private const double MinZoom = 0.2;
    private const double MaxZoom = 20.0;

    private MainViewModel ViewModel => (MainViewModel)DataContext;

    public MainWindow(MainViewModel viewModel)
    {
        InitializeComponent();
        DataContext = viewModel;

        ImageContainer.MouseWheel += ImageContainer_MouseWheel;
        ImageContainer.MouseLeftButtonDown += ImageContainer_MouseLeftButtonDown;
        ImageContainer.MouseMove += ImageContainer_MouseMove;
        ImageContainer.MouseLeftButtonUp += ImageContainer_MouseLeftButtonUp;

        // Window chrome buttons
        MinimizeBtn.Click += (_, _) => WindowState = WindowState.Minimized;
        MaximizeBtn.Click += (_, _) =>
        {
            WindowState = WindowState == WindowState.Maximized
                ? WindowState.Normal
                : WindowState.Maximized;
        };
        CloseBtn.Click += (_, _) => Close();

        // Theme toggle
        ThemeToggleBtn.Click += (_, _) => ToggleTheme();

        // Auto-focus search box when search expands
        PatientSearchBox.IsVisibleChanged += (_, e) =>
        {
            if (PatientSearchBox.Visibility == Visibility.Visible)
            {
                PatientSearchBox.Focus();
                PatientSearchBox.SelectAll();
            }
        };
    }

    /// <summary>
    /// Toggles between dark and light theme. Wire to a button or keyboard shortcut.
    /// </summary>
    public void ToggleTheme()
    {
        App.ToggleTheme();
    }

    // ── Custom title bar drag ───────────────────────────────────────────
    protected override void OnMouseLeftButtonDown(MouseButtonEventArgs e)
    {
        base.OnMouseLeftButtonDown(e);

        // Only drag from the top toolbar area (first 50 pixels)
        var pos = e.GetPosition(this);
        if (pos.Y <= 50 && !_isDragging)
        {
            // Double-click on title bar = toggle maximize
            if (e.ClickCount == 2)
            {
                WindowState = WindowState == WindowState.Maximized
                    ? WindowState.Normal
                    : WindowState.Maximized;
                return;
            }

            DragMove();
        }
    }

    private void ImageContainer_MouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (!ViewModel.HasImage)
            return;

        double oldZoom = ViewModel.ZoomLevel;

        // Compute new zoom level
        double newZoom = e.Delta > 0
            ? oldZoom * ZoomFactor
            : oldZoom / ZoomFactor;

        newZoom = Math.Clamp(newZoom, MinZoom, MaxZoom);

        if (Math.Abs(newZoom - oldZoom) < 1e-9)
            return;

        // Mouse position relative to the container center
        Point mousePos = e.GetPosition(ImageContainer);
        double containerCenterX = ImageContainer.ActualWidth / 2.0;
        double containerCenterY = ImageContainer.ActualHeight / 2.0;
        double relX = mousePos.X - containerCenterX;
        double relY = mousePos.Y - containerCenterY;

        // Adjust pan so the point under the cursor stays fixed.
        ViewModel.PanX = relX - (relX - ViewModel.PanX) / oldZoom * newZoom;
        ViewModel.PanY = relY - (relY - ViewModel.PanY) / oldZoom * newZoom;
        ViewModel.ZoomLevel = newZoom;

        e.Handled = true;
    }

    private void ImageContainer_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (!ViewModel.HasImage)
            return;

        // Double-click: reset zoom
        if (e.ClickCount == 2)
        {
            ViewModel.FitToCanvasCommand.Execute(null);
            e.Handled = true;
            return;
        }

        if (ViewModel.ZoomLevel <= 1.01)
            return;

        _isDragging = true;
        _dragStart = e.GetPosition(ImageContainer);
        _startPanX = ViewModel.PanX;
        _startPanY = ViewModel.PanY;

        ImageContainer.CaptureMouse();
        e.Handled = true;
    }

    private void ImageContainer_MouseMove(object sender, MouseEventArgs e)
    {
        if (!_isDragging)
            return;

        Point current = e.GetPosition(ImageContainer);
        ViewModel.PanX = _startPanX + (current.X - _dragStart.X);
        ViewModel.PanY = _startPanY + (current.Y - _dragStart.Y);

        e.Handled = true;
    }

    private void ImageContainer_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (!_isDragging)
            return;

        _isDragging = false;
        ImageContainer.ReleaseMouseCapture();
        e.Handled = true;
    }
}
