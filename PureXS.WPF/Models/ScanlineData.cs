namespace PureXS.Models;

/// <summary>
/// Represents a single scanline (vertical column) extracted from the live TCP stream
/// during a panoramic exposure. Each scanline contains 16-bit pixel data.
/// </summary>
public sealed class ScanlineData
{
    /// <summary>Column identifier (increments from 0x40 during a sweep).</summary>
    public byte ScanlineId { get; init; }

    /// <summary>Number of pixels in this scanline (typically 240).</summary>
    public int PixelCount { get; init; }

    /// <summary>Raw 16-bit pixel values (big-endian, length = PixelCount).</summary>
    public ushort[] Pixels { get; init; } = [];
}
