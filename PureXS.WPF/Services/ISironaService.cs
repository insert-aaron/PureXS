namespace PureXS.Services;

/// <summary>
/// Abstraction over the Sirona ORTHOPHOS XG TCP protocol.
/// </summary>
public interface ISironaService : IAsyncDisposable
{
    /// <summary>Raised when the connection state changes.</summary>
    event EventHandler<ConnectionState>? ConnectionStateChanged;

    /// <summary>Raised on each successful heartbeat tick.</summary>
    event EventHandler? HeartbeatTick;

    /// <summary>Raised when a complete X-ray image has been received.</summary>
    event EventHandler<byte[]>? ImageReceived;

    /// <summary>Raised when kV ramp value changes during expose.</summary>
    event EventHandler<double>? KvChanged;

    /// <summary>Raised when a scanline is extracted from the live TCP stream during expose.</summary>
    event EventHandler<Models.ScanlineData>? ScanlineReceived;

    /// <summary>Raised periodically during exposure with the number of bytes received so far.</summary>
    event EventHandler<int>? ScanProgress;

    /// <summary>Raised when device is armed and waiting for physical button press.</summary>
    event EventHandler? DeviceArmed;

    /// <summary>Raised when the physical expose button is pressed (EXPOSE_NOTIFY 0x1005).</summary>
    event EventHandler? ExposeStarted;

    /// <summary>Raised during auto-discovery with status messages.</summary>
    event EventHandler<string>? DiscoveryStatus;

    /// <summary>Current connection state.</summary>
    ConnectionState State { get; }

    /// <summary>Connect to the Sirona unit and start the heartbeat loop.</summary>
    Task ConnectAsync(CancellationToken ct = default);

    /// <summary>Disconnect gracefully.</summary>
    Task DisconnectAsync();

    /// <summary>
    /// Arm the device for exposure: sends CAPS_REQ + patient DATA_SEND.
    /// After this call, the device waits for the physical expose button.
    /// The ExposeStarted event fires when the button is pressed.
    /// </summary>
    Task ArmForExposeAsync(string lastName = "test", string firstName = "test", CancellationToken ct = default);
}

/// <summary>
/// Represents the connection state of the Sirona TCP session.
/// </summary>
public enum ConnectionState
{
    Disconnected,
    Discovering,
    Connecting,
    Connected,
    Armed,
    Exposing,
    Reconnecting
}
