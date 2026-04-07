using System.IO;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Microsoft.Win32;

namespace PureXS.Views;

/// <summary>
/// Modal preview/edit window for the captured X-ray image.
/// Provides non-destructive adjustment sliders (exposure, contrast,
/// brightness, sharpness) and an invert toggle with live preview.
/// </summary>
public partial class ImageEditWindow : Window
{
    private readonly BitmapSource _source;
    private readonly byte[]? _sourceBytes;
    private DispatcherTimer? _debounceTimer;

    /// <summary>
    /// Raised when the user clicks "Apply &amp; Close".
    /// The event arg is the edited BitmapSource.
    /// </summary>
    public event EventHandler<BitmapSource>? ImageApplied;

    public ImageEditWindow(BitmapSource source, byte[]? sourceBytes = null)
    {
        InitializeComponent();
        _source = source;
        _sourceBytes = sourceBytes;

        // Initial render
        Loaded += (_, _) => RenderPreview();
    }

    // ── Slider change handler (debounced) ────────────────────────────

    private void OnSliderChanged(object sender, RoutedEventArgs e)
    {
        // Guard: sliders fire ValueChanged during InitializeComponent() before
        // _source and XAML elements are assigned — ignore those early calls.
        if (!IsLoaded) return;

        // Update value labels
        ExposureLabel.Text = $"{ExposureSlider.Value:F0}";
        ContrastLabel.Text = $"{ContrastSlider.Value / 100.0:F2}";
        BrightnessLabel.Text = $"{BrightnessSlider.Value:F0}";
        SharpnessLabel.Text = $"{SharpnessSlider.Value / 100.0:F1}";

        // Debounce — 40ms
        _debounceTimer?.Stop();
        _debounceTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(40) };
        _debounceTimer.Tick += (_, _) =>
        {
            _debounceTimer.Stop();
            RenderPreview();
        };
        _debounceTimer.Start();
    }

    // ── Core image processing ────────────────────────────────────────

    private BitmapSource ApplyEdits()
    {
        var converted = new FormatConvertedBitmap(_source, PixelFormats.Bgra32, null, 0);
        int width = converted.PixelWidth;
        int height = converted.PixelHeight;
        int stride = width * 4;
        var pixels = new byte[height * stride];
        converted.CopyPixels(pixels, stride, 0);

        double exposure = ExposureSlider.Value;
        double contrast = ContrastSlider.Value / 100.0;
        double brightness = BrightnessSlider.Value;
        double sharpness = SharpnessSlider.Value / 100.0;
        bool invert = InvertCheck.IsChecked == true;

        // Exposure (EV-style gamma shift)
        double exposureFactor = Math.Pow(2.0, exposure / 50.0);

        for (int i = 0; i < pixels.Length; i += 4)
        {
            for (int c = 0; c < 3; c++)
            {
                double val = pixels[i + c];

                // Exposure
                if (Math.Abs(exposure) > 1)
                    val *= exposureFactor;

                // Contrast (around midpoint 128)
                val = 128 + contrast * (val - 128);

                // Brightness (additive)
                val += brightness;

                // Invert
                if (invert)
                    val = 255 - val;

                pixels[i + c] = (byte)Math.Clamp(val, 0, 255);
            }
        }

        // Sharpness: simple unsharp-mask-like approach using 3x3 kernel
        if (sharpness > 0.1)
        {
            var sharpened = new byte[pixels.Length];
            Array.Copy(pixels, sharpened, pixels.Length);

            for (int y = 1; y < height - 1; y++)
            {
                for (int x = 1; x < width - 1; x++)
                {
                    int idx = (y * width + x) * 4;
                    for (int c = 0; c < 3; c++)
                    {
                        // 3x3 Laplacian: center*5 - neighbors
                        int center = pixels[idx + c] * 5;
                        int neighbors =
                            pixels[idx - stride + c] +
                            pixels[idx + stride + c] +
                            pixels[idx - 4 + c] +
                            pixels[idx + 4 + c];
                        double detail = (center - neighbors) / 5.0;
                        double val = pixels[idx + c] + detail * sharpness;
                        sharpened[idx + c] = (byte)Math.Clamp(val, 0, 255);
                    }
                }
            }
            pixels = sharpened;
        }

        var wb = new WriteableBitmap(width, height, converted.DpiX, converted.DpiY,
                                      PixelFormats.Bgra32, null);
        wb.WritePixels(new Int32Rect(0, 0, width, height), pixels, stride, 0);
        wb.Freeze();
        return wb;
    }

    private void RenderPreview()
    {
        try
        {
            PreviewImage.Source = ApplyEdits();
        }
        catch
        {
            // Silently ignore rendering errors during slider drag
        }
    }

    // ── Button handlers ──────────────────────────────────────────────

    private void OnReset(object sender, RoutedEventArgs e)
    {
        ExposureSlider.Value = 0;
        ContrastSlider.Value = 100;
        BrightnessSlider.Value = 0;
        SharpnessSlider.Value = 0;
        InvertCheck.IsChecked = false;
        RenderPreview();
    }

    private void OnApply(object sender, RoutedEventArgs e)
    {
        var edited = ApplyEdits();
        ImageApplied?.Invoke(this, edited);
        Close();
    }

    private void OnSavePng(object sender, RoutedEventArgs e)
    {
        var edited = ApplyEdits();
        var timestamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
        var dlg = new SaveFileDialog
        {
            Filter = "PNG Image|*.png",
            FileName = $"purexs_edited_{timestamp}.png",
        };

        if (dlg.ShowDialog() == true)
        {
            var encoder = new PngBitmapEncoder();
            encoder.Frames.Add(BitmapFrame.Create(edited));
            using var fs = new FileStream(dlg.FileName, FileMode.Create);
            encoder.Save(fs);
        }
    }

    private void OnCancel(object sender, RoutedEventArgs e)
    {
        Close();
    }
}
