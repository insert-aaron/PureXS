namespace PureXS.Services;

/// <summary>
/// Abstraction over the Sirona ORTHOPHOS XG TCP protocol for testability.
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

    /// <summary>Current connection state.</summary>
    ConnectionState State { get; }

    /// <summary>Connect to the Sirona unit and start the heartbeat loop.</summary>
    Task ConnectAsync(CancellationToken ct = default);

    /// <summary>Disconnect gracefully.</summary>
    Task DisconnectAsync();

    /// <summary>Send the expose trigger packet to start a panoramic scan.</summary>
    Task ExposeAsync(CancellationToken ct = default);
}

/// <summary>
/// Represents the connection state of the Sirona TCP session.
/// </summary>
public enum ConnectionState
{
    Disconnected,
    Connecting,
    Connected,
    Exposing,
    Reconnecting
}
