using System.Buffers.Binary;
using System.Diagnostics;
using System.Net.Sockets;
using PureXS.Models;

namespace PureXS.Services;

/// <summary>
/// Manages the raw TCP connection to a Sirona ORTHOPHOS XG unit on port 12837.
/// Implements the P2K session protocol: handshake (SESSION_OPEN + SESSION_INIT),
/// heartbeat keep-alive, session refresh, expose triggering, post-scan reconnect,
/// and image byte-stream buffering.
///
/// Protocol reverse-engineered from hb_decoder.py SironaLiveClient.
/// </summary>
public sealed class SironaService : ISironaService
{
    // ── P2K Protocol constants ─────────────────────────────────────────────
    private const ushort MAGIC = 0x072D;
    private const ushort PORT_MARKER = 0x07D0;
    private const int SESSION_HEADER_SIZE = 20;

    // Function codes
    private const ushort FC_SESSION_OPEN_REQ = 0x205C;
    private const ushort FC_SESSION_OPEN_ACK = 0x205D;
    private const ushort FC_SESSION_INIT = 0x2001;
    private const ushort FC_HB_REQUEST = 0x200B;
    private const ushort FC_HB_RESPONSE = 0x200C;
    private const ushort FC_IMAGE_ACK = 0x1008;

    // Post-scan disconnect marker
    private static readonly byte[] PostScanDisconnect = [0xE7, 0x14, 0x02];

    // Timing
    private const int HeartbeatIntervalMs = 900;      // matches Python hb_interval=0.9
    private const double SessionRefreshSeconds = 1.5;  // matches Python SESSION_REFRESH_S
    private const int ReconnectHeartbeatGate = 10;

    // Live parsing markers
    private static readonly byte[] ScanlineMarker = [0x00, 0x01, 0x00, 0xF0];
    private const int ScanlinePixels = 240;

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
    private Stopwatch _sessionTimer = new();
    private DateTime _lastHeartbeatTime = DateTime.MinValue;

    // Live parsing state
    private int _kvParseOffset;
    private int _scanlineParseOffset;
    private double _lastKvFired;

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
    public event EventHandler<ScanlineData>? ScanlineReceived;

    /// <inheritdoc />
    public event EventHandler<int>? ScanProgress;

    /// <inheritdoc />
    public ConnectionState State => _state;

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
        _tcp.ReceiveTimeout = 10000;
        _tcp.SendTimeout = 5000;
        await _tcp.ConnectAsync(_host, _port, _sessionCts.Token);
        _stream = _tcp.GetStream();

        // ── P2K Session Handshake ──────────────────────────────────────
        // Step 1: SESSION_OPEN_REQ (optional — some firmware ignores it)
        await SendSessionFrameAsync(FC_SESSION_OPEN_REQ, flags: 0x000F, ct);

        // Wait up to 1s for ACK
        try
        {
            using var ackCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            ackCts.CancelAfter(1000);
            var resp = await ReceiveFrameAsync(ackCts.Token);
            var fc = resp.Length >= 2 ? (ushort)((resp[0] << 8) | resp[1]) : (ushort)0;
            Debug.WriteLine(fc == FC_SESSION_OPEN_ACK
                ? "[Sirona] Session opened (0x205D ACK)"
                : $"[Sirona] SESSION_OPEN response: 0x{fc:X4} — proceeding to INIT");
        }
        catch (OperationCanceledException)
        {
            Debug.WriteLine("[Sirona] SESSION_OPEN ACK skipped (timeout) — proceeding to INIT");
        }

        // Step 2: SESSION_INIT (always sent)
        await SendSessionFrameAsync(FC_SESSION_INIT, ct: ct);
        try
        {
            using var initCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            initCts.CancelAfter(3000);
            var resp = await ReceiveFrameAsync(initCts.Token);
            var fc = resp.Length >= 2 ? (ushort)((resp[0] << 8) | resp[1]) : (ushort)0;
            Debug.WriteLine($"[Sirona] Session init response: 0x{fc:X4} ({resp.Length} bytes)");
        }
        catch (OperationCanceledException)
        {
            Debug.WriteLine("[Sirona] SESSION_INIT response timeout — proceeding anyway");
        }

        _sessionTimer.Restart();
        SetState(ConnectionState.Connected);

        // Start heartbeat and reader loops
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
        _sessionTimer.Stop();

        SetState(ConnectionState.Disconnected);
    }

    /// <inheritdoc />
    public async Task ExposeAsync(CancellationToken ct = default)
    {
        if (_state != ConnectionState.Connected)
            throw new InvalidOperationException($"Cannot expose in state {_state}.");

        if (_stream is null)
            throw new InvalidOperationException("No active network stream.");

        _session.IsExposing = true;
        _session.ImageBuffer.Clear();
        _kvParseOffset = 0;
        _scanlineParseOffset = 0;
        _lastKvFired = 0;
        SetState(ConnectionState.Exposing);

        // Note: expose is triggered by the physical button on the Orthophos,
        // not by a software command. The device sends EXPOSE_NOTIFY (0x1005)
        // when the button is pressed. We just need to be in ARMED state
        // (patient data sent) and listening.
    }

    /// <inheritdoc />
    public async ValueTask DisposeAsync()
    {
        await DisconnectAsync();
    }

    // ── P2K Frame building ─────────────────────────────────────────────────

    /// <summary>
    /// Builds a 20-byte P2K session header matching the Python _build_session_header.
    /// Layout: [func_code:2][magic:2][port:2][version:2][flags:2][reserved:8][payload_len:2]
    /// </summary>
    private static byte[] BuildSessionHeader(ushort funcCode, ushort flags = 0x000E, ushort payloadLength = 0)
    {
        var header = new byte[SESSION_HEADER_SIZE];
        header[0] = (byte)((funcCode >> 8) & 0xFF);
        header[1] = (byte)(funcCode & 0xFF);
        BinaryPrimitives.WriteUInt16BigEndian(header.AsSpan(2), MAGIC);
        BinaryPrimitives.WriteUInt16BigEndian(header.AsSpan(4), PORT_MARKER);
        BinaryPrimitives.WriteUInt16BigEndian(header.AsSpan(6), 0x0001); // version
        BinaryPrimitives.WriteUInt16BigEndian(header.AsSpan(8), flags);
        // bytes 10-17: reserved (zeros)
        BinaryPrimitives.WriteUInt16BigEndian(header.AsSpan(18), payloadLength);
        return header;
    }

    private async Task SendSessionFrameAsync(ushort funcCode, ushort flags = 0x000E, CancellationToken ct = default)
    {
        if (_stream is null)
            throw new InvalidOperationException("No active network stream.");

        var header = BuildSessionHeader(funcCode, flags);
        await _stream.WriteAsync(header, ct);
        await _stream.FlushAsync(ct);
    }

    private async Task<byte[]> ReceiveFrameAsync(CancellationToken ct)
    {
        if (_stream is null)
            throw new InvalidOperationException("No active network stream.");

        var buffer = new byte[4096];
        var bytesRead = await _stream.ReadAsync(buffer, ct);
        if (bytesRead == 0)
            throw new ConnectionException("Connection closed by device");

        return buffer[..bytesRead];
    }

    // ── Heartbeat loop ──────────────────────────────────────────────────────

    /// <summary>
    /// Sends P2K HB_REQUEST frames to keep the session alive.
    /// Also handles session refresh every 1.5s (re-sends SESSION_OPEN + SESSION_INIT).
    /// Matches Python SironaLiveClient._hb_loop behavior.
    /// </summary>
    private async Task HeartbeatLoopAsync(CancellationToken ct)
    {
        try
        {
            while (!ct.IsCancellationRequested)
            {
                if (_stream is null)
                    break;

                // Don't send HB during expose — device is flooding data
                if (!_session.IsExposing)
                {
                    // Session refresh if needed (matches Python SESSION_REFRESH_S = 1.5)
                    if (_sessionTimer.Elapsed.TotalSeconds >= SessionRefreshSeconds)
                    {
                        await SessionRefreshAsync(ct);
                        continue; // skip HB this cycle, refresh resets timer
                    }

                    // Send HB_REQUEST
                    await SendSessionFrameAsync(FC_HB_REQUEST, ct: ct);
                    _session.HeartbeatCount++;
                    _lastHeartbeatTime = DateTime.UtcNow;
                    HeartbeatTick?.Invoke(this, EventArgs.Empty);
                }

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

                await Task.Delay(HeartbeatIntervalMs, ct);
            }
        }
        catch (OperationCanceledException) { /* Normal shutdown */ }
        catch (Exception ex)
        {
            Debug.WriteLine($"[Sirona] Heartbeat error: {ex.Message}");
            if (!ct.IsCancellationRequested)
                _ = ReconnectAsync();
        }
    }

    /// <summary>
    /// Re-establishes the session by sending SESSION_OPEN + SESSION_INIT.
    /// The Orthophos requires periodic session refresh (~every 1.5s).
    /// Matches Python SironaLiveClient._session_refresh().
    /// </summary>
    private async Task SessionRefreshAsync(CancellationToken ct)
    {
        try
        {
            await SendSessionFrameAsync(FC_SESSION_OPEN_REQ, flags: 0x000F, ct);
            // Brief delay for device to process
            await Task.Delay(50, ct);
            await SendSessionFrameAsync(FC_SESSION_INIT, ct: ct);
            _sessionTimer.Restart();
            Debug.WriteLine("[Sirona] Session refreshed");
        }
        catch (Exception ex)
        {
            Debug.WriteLine($"[Sirona] Session refresh failed: {ex.Message}");
        }
    }

    // ── Reader loop ─────────────────────────────────────────────────────────

    private async Task ReaderLoopAsync(CancellationToken ct)
    {
        var buffer = new byte[65536]; // larger buffer for image data

        try
        {
            while (!ct.IsCancellationRequested && _stream is not null)
            {
                var bytesRead = await _stream.ReadAsync(buffer.AsMemory(0, buffer.Length), ct);
                if (bytesRead == 0)
                    break;

                var data = buffer.AsMemory(0, bytesRead);

                // Check for post-scan disconnect marker: E7 14 02
                if (ContainsSequence(data.Span, PostScanDisconnect))
                {
                    _session.IsPostScanDisconnect = true;

                    if (_session.IsExposing)
                    {
                        _session.IsExposing = false;

                        // Send IMAGE_ACK (matches Python behavior)
                        try { await SendSessionFrameAsync(FC_IMAGE_ACK, ct: ct); }
                        catch { /* best effort */ }

                        var imageBytes = _session.ImageBuffer.ToArray();
                        if (imageBytes.Length > 0)
                            ImageReceived?.Invoke(this, imageBytes);
                    }

                    _ = ReconnectAsync();
                    return;
                }

                // Buffer image bytes during expose
                if (_session.IsExposing)
                {
                    _session.ImageBuffer.AddRange(data.Span.ToArray());
                    ScanProgress?.Invoke(this, _session.ImageBuffer.Count);

                    ParseLiveKvSamples();
                    ParseLiveScanlines();
                }
            }
        }
        catch (OperationCanceledException) { /* Normal shutdown */ }
        catch (Exception ex)
        {
            Debug.WriteLine($"[Sirona] Reader error: {ex.Message}");
            if (!ct.IsCancellationRequested)
                _ = ReconnectAsync();
        }
    }

    // ── Reconnect logic ─────────────────────────────────────────────────────

    private async Task ReconnectAsync()
    {
        SetState(ConnectionState.Reconnecting);

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
                _tcp.ReceiveTimeout = 10000;
                _tcp.SendTimeout = 5000;
                await _tcp.ConnectAsync(_host, _port);
                _stream = _tcp.GetStream();

                // P2K handshake on reconnect (matches Python reconnect behavior)
                await SendSessionFrameAsync(FC_SESSION_OPEN_REQ, flags: 0x000F);
                await Task.Delay(100);
                await SendSessionFrameAsync(FC_SESSION_INIT);
                _sessionTimer.Restart();

                _session.ReconnectHeartbeatCycles = 0;
                _session.IsPostScanDisconnect = true;

                _sessionCts?.Dispose();
                _sessionCts = new CancellationTokenSource();
                _ = HeartbeatLoopAsync(_sessionCts.Token);
                _ = ReaderLoopAsync(_sessionCts.Token);

                Debug.WriteLine($"[Sirona] Reconnected on attempt {attempt}");
                SetState(ConnectionState.Reconnecting); // transitions to Connected after HB gate
                return;
            }
            catch (Exception ex)
            {
                Debug.WriteLine($"[Sirona] Reconnect attempt {attempt} failed: {ex.Message}");
            }
        }

        SetState(ConnectionState.Disconnected);
    }

    // ── Live stream parsing ────────────────────────────────────────────────

    private void ParseLiveKvSamples()
    {
        var buf = _session.ImageBuffer;
        const int kvRecordSize = 15;

        while (_kvParseOffset + kvRecordSize <= buf.Count)
        {
            var found = false;
            for (var i = _kvParseOffset + kvRecordSize - 3; i < buf.Count - 1; i++)
            {
                if (buf[i] == 0x0E && buf[i + 1] == 0x01)
                {
                    var recordStart = i - 12;
                    if (recordStart < _kvParseOffset || recordStart < 0)
                    {
                        _kvParseOffset = i + 2;
                        found = true;
                        break;
                    }

                    if (buf[recordStart] == 0x01 &&
                        buf[recordStart + 3] == 0x01 &&
                        buf[recordStart + 6] == 0x01 &&
                        buf[recordStart + 9] == 0x01)
                    {
                        var kvRaw = (buf[recordStart + 1] << 8) | buf[recordStart + 2];
                        var kv = kvRaw / 10.0;

                        if (Math.Abs(kv - _lastKvFired) > 0.5)
                        {
                            _lastKvFired = kv;
                            _session.CurrentKv = kv;
                            KvChanged?.Invoke(this, kv);
                        }
                    }

                    _kvParseOffset = i + 2;
                    found = true;
                    break;
                }
            }

            if (!found)
                break;
        }
    }

    private void ParseLiveScanlines()
    {
        var buf = _session.ImageBuffer;
        var markerLen = ScanlineMarker.Length;
        var scanlineBytes = ScanlinePixels * 2;
        var totalNeeded = markerLen + 2 + scanlineBytes;

        while (_scanlineParseOffset + totalNeeded <= buf.Count)
        {
            var found = false;
            for (var i = _scanlineParseOffset; i <= buf.Count - totalNeeded; i++)
            {
                if (buf[i] == ScanlineMarker[0] &&
                    buf[i + 1] == ScanlineMarker[1] &&
                    buf[i + 2] == ScanlineMarker[2] &&
                    buf[i + 3] == ScanlineMarker[3])
                {
                    var scanlineId = i >= 1 ? buf[i - 1] : (byte)0;
                    var pixelStart = i + markerLen + 2;

                    if (pixelStart + scanlineBytes > buf.Count)
                    {
                        _scanlineParseOffset = i;
                        return;
                    }

                    var pixels = new ushort[ScanlinePixels];
                    for (var p = 0; p < ScanlinePixels; p++)
                    {
                        var off = pixelStart + p * 2;
                        pixels[p] = (ushort)((buf[off] << 8) | buf[off + 1]);
                    }

                    ScanlineReceived?.Invoke(this, new ScanlineData
                    {
                        ScanlineId = scanlineId,
                        PixelCount = ScanlinePixels,
                        Pixels = pixels,
                    });

                    _scanlineParseOffset = pixelStart + scanlineBytes;
                    found = true;
                    break;
                }
            }

            if (!found)
                break;
        }
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private void SetState(ConnectionState newState)
    {
        if (_state == newState) return;
        _state = newState;
        ConnectionStateChanged?.Invoke(this, newState);
    }

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

/// <summary>Simple exception for connection errors.</summary>
public class ConnectionException : Exception
{
    public ConnectionException(string message) : base(message) { }
}
