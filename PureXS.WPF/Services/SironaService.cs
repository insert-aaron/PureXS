using System.Net.Sockets;
using PureXS.Models;

namespace PureXS.Services;

/// <summary>
/// Manages the raw TCP connection to a Sirona ORTHOPHOS XG unit on port 12837.
/// Handles heartbeat keep-alive, expose triggering, post-scan reconnect, and
/// image byte-stream buffering — all fully async.
/// </summary>
public sealed class SironaService : ISironaService
{
    // ── Protocol constants ──────────────────────────────────────────────────
    private static readonly byte[] HeartbeatPrefix = [0x20, 0x00, 0x07, 0x2D, 0x07, 0xD0];
    private static readonly byte[] ExposePacket = [0xFF, 0x12, 0x01, 0x03, 0x42, 0x0E, 0x01];
    private static readonly byte[] PostScanDisconnect = [0xE7, 0x14, 0x02];

    private const int HeartbeatIntervalMs = 100;
    private const int ReconnectHeartbeatGate = 10;

    // ── Configuration ───────────────────────────────────────────────────────
    private readonly string _host;
    private readonly int _port;
    private readonly int _maxReconnectAttempts;
    private readonly TimeSpan _reconnectDelay;

    // ── State ───────────────────────────────────────────────────────────────
    private TcpClient? _tcp;
    private NetworkStream? _stream;
    private CancellationTokenSource? _sessionCts;
    private readonly SironaSession _session = new();
    private ConnectionState _state = ConnectionState.Disconnected;

    // ── Events ──────────────────────────────────────────────────────────────

    /// <inheritdoc />
    public event EventHandler<ConnectionState>? ConnectionStateChanged;

    /// <inheritdoc />
    public event EventHandler? HeartbeatTick;

    /// <inheritdoc />
    public event EventHandler<byte[]>? ImageReceived;

    /// <inheritdoc />
    public event EventHandler<double>? KvChanged;

    /// <inheritdoc />
    public event EventHandler<int>? ScanProgress;

    /// <inheritdoc />
    public ConnectionState State => _state;

    /// <summary>
    /// Creates a new <see cref="SironaService"/> targeting the specified host and port.
    /// </summary>
    /// <param name="host">IP address of the Sirona unit (default 192.168.139.170).</param>
    /// <param name="port">TCP port (default 12837).</param>
    /// <param name="maxReconnectAttempts">Maximum reconnect attempts after post-scan disconnect.</param>
    /// <param name="reconnectDelay">Delay between reconnect attempts.</param>
    public SironaService(
        string host = "192.168.139.170",
        int port = 12837,
        int maxReconnectAttempts = 5,
        TimeSpan? reconnectDelay = null)
    {
        _host = host;
        _port = port;
        _maxReconnectAttempts = maxReconnectAttempts;
        _reconnectDelay = reconnectDelay ?? TimeSpan.FromSeconds(2);
    }

    // ── Public API ──────────────────────────────────────────────────────────

    /// <inheritdoc />
    public async Task ConnectAsync(CancellationToken ct = default)
    {
        await DisconnectAsync();

        _sessionCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _session.Reset();

        SetState(ConnectionState.Connecting);

        _tcp = new TcpClient();
        await _tcp.ConnectAsync(_host, _port, _sessionCts.Token);
        _stream = _tcp.GetStream();

        SetState(ConnectionState.Connected);

        // Fire-and-forget the heartbeat and reader loops
        _ = HeartbeatLoopAsync(_sessionCts.Token);
        _ = ReaderLoopAsync(_sessionCts.Token);
    }

    /// <inheritdoc />
    public async Task DisconnectAsync()
    {
        _sessionCts?.Cancel();
        _sessionCts?.Dispose();
        _sessionCts = null;

        if (_stream is not null)
        {
            await _stream.DisposeAsync();
            _stream = null;
        }

        _tcp?.Dispose();
        _tcp = null;

        SetState(ConnectionState.Disconnected);
    }

    /// <inheritdoc />
    /// <exception cref="InvalidOperationException">Thrown when not connected or already exposing.</exception>
    public async Task ExposeAsync(CancellationToken ct = default)
    {
        if (_state != ConnectionState.Connected)
            throw new InvalidOperationException($"Cannot expose in state {_state}.");

        if (_stream is null)
            throw new InvalidOperationException("No active network stream.");

        _session.IsExposing = true;
        _session.ImageBuffer.Clear();
        SetState(ConnectionState.Exposing);

        await _stream.WriteAsync(ExposePacket, ct);
        await _stream.FlushAsync(ct);
    }

    /// <inheritdoc />
    public async ValueTask DisposeAsync()
    {
        await DisconnectAsync();
    }

    // ── Heartbeat loop ──────────────────────────────────────────────────────

    /// <summary>
    /// Sends heartbeat packets every 100 ms to keep the Sirona session alive.
    /// After a post-scan reconnect, waits for <see cref="ReconnectHeartbeatGate"/>
    /// successful cycles before allowing a new expose.
    /// </summary>
    private async Task HeartbeatLoopAsync(CancellationToken ct)
    {
        try
        {
            while (!ct.IsCancellationRequested)
            {
                if (_stream is not null)
                {
                    var packet = BuildHeartbeatPacket();
                    await _stream.WriteAsync(packet, ct);
                    await _stream.FlushAsync(ct);

                    _session.HeartbeatCount++;
                    HeartbeatTick?.Invoke(this, EventArgs.Empty);

                    if (_session.IsPostScanDisconnect)
                    {
                        _session.ReconnectHeartbeatCycles++;
                        if (_session.ReconnectHeartbeatCycles >= ReconnectHeartbeatGate)
                        {
                            _session.IsPostScanDisconnect = false;
                            _session.ReconnectHeartbeatCycles = 0;
                            SetState(ConnectionState.Connected);
                        }
                    }
                }

                await Task.Delay(HeartbeatIntervalMs, ct);
            }
        }
        catch (OperationCanceledException) { /* Normal shutdown */ }
        catch (Exception)
        {
            // Connection lost — attempt reconnect if not already cancelled
            if (!ct.IsCancellationRequested)
                _ = ReconnectAsync();
        }
    }

    /// <summary>
    /// Builds a heartbeat packet with the current sequence number.
    /// </summary>
    private byte[] BuildHeartbeatPacket()
    {
        var packet = new byte[HeartbeatPrefix.Length + 1];
        HeartbeatPrefix.CopyTo(packet, 0);
        packet[^1] = _session.SequenceNumber;
        _session.SequenceNumber = (byte)((_session.SequenceNumber + 1) & 0xFF);
        return packet;
    }

    // ── Reader loop ─────────────────────────────────────────────────────────

    /// <summary>
    /// Continuously reads from the TCP stream, detecting post-scan disconnect
    /// signals and buffering image data during an active expose.
    /// </summary>
    private async Task ReaderLoopAsync(CancellationToken ct)
    {
        var buffer = new byte[8192];

        try
        {
            while (!ct.IsCancellationRequested && _stream is not null)
            {
                var bytesRead = await _stream.ReadAsync(buffer.AsMemory(0, buffer.Length), ct);
                if (bytesRead == 0)
                    break; // Server closed connection

                var data = buffer.AsMemory(0, bytesRead);

                // Check for post-scan disconnect marker: E7 14 02
                if (ContainsSequence(data.Span, PostScanDisconnect))
                {
                    _session.IsPostScanDisconnect = true;

                    // If we were exposing, finalize the image
                    if (_session.IsExposing)
                    {
                        _session.IsExposing = false;
                        var imageBytes = _session.ImageBuffer.ToArray();
                        if (imageBytes.Length > 0)
                            ImageReceived?.Invoke(this, imageBytes);
                    }

                    // Sirona drops the connection after this — reconnect
                    _ = ReconnectAsync();
                    return;
                }

                // Buffer image bytes during expose
                if (_session.IsExposing)
                {
                    _session.ImageBuffer.AddRange(data.Span.ToArray());
                    ScanProgress?.Invoke(this, _session.ImageBuffer.Count);
                }

                // TODO: Parse kV ramp packets and raise KvChanged
            }
        }
        catch (OperationCanceledException) { /* Normal shutdown */ }
        catch (Exception)
        {
            if (!ct.IsCancellationRequested)
                _ = ReconnectAsync();
        }
    }

    // ── Reconnect logic ─────────────────────────────────────────────────────

    /// <summary>
    /// Attempts to reconnect to the Sirona unit after a post-scan disconnect.
    /// Retries up to <see cref="_maxReconnectAttempts"/> times with a configurable delay.
    /// </summary>
    private async Task ReconnectAsync()
    {
        SetState(ConnectionState.Reconnecting);

        // Tear down old connection
        if (_stream is not null)
        {
            await _stream.DisposeAsync();
            _stream = null;
        }
        _tcp?.Dispose();
        _tcp = null;

        for (var attempt = 1; attempt <= _maxReconnectAttempts; attempt++)
        {
            try
            {
                await Task.Delay(_reconnectDelay);

                _tcp = new TcpClient();
                await _tcp.ConnectAsync(_host, _port);
                _stream = _tcp.GetStream();

                _session.ReconnectHeartbeatCycles = 0;
                _session.IsPostScanDisconnect = true;

                // Restart loops with a new CTS
                _sessionCts?.Dispose();
                _sessionCts = new CancellationTokenSource();
                _ = HeartbeatLoopAsync(_sessionCts.Token);
                _ = ReaderLoopAsync(_sessionCts.Token);

                SetState(ConnectionState.Reconnecting); // Will transition to Connected after HB gate
                return;
            }
            catch
            {
                // Retry
            }
        }

        SetState(ConnectionState.Disconnected);
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private void SetState(ConnectionState newState)
    {
        if (_state == newState) return;
        _state = newState;
        ConnectionStateChanged?.Invoke(this, newState);
    }

    /// <summary>
    /// Checks whether a byte span contains a given subsequence.
    /// </summary>
    private static bool ContainsSequence(ReadOnlySpan<byte> data, ReadOnlySpan<byte> sequence)
    {
        for (var i = 0; i <= data.Length - sequence.Length; i++)
        {
            if (data.Slice(i, sequence.Length).SequenceEqual(sequence))
                return true;
        }
        return false;
    }
}
