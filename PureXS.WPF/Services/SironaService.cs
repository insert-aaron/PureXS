using System.Buffers.Binary;
using System.Diagnostics;
using System.Net;
using System.Net.NetworkInformation;
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
    private const ushort FC_CAPS_REQ = 0x2110;
    private const ushort FC_CAPS_RESP = 0x2111;
    private const ushort FC_DATA_SEND = 0x1000;
    private const ushort FC_DATA_ACK = 0x1001;
    private const ushort FC_EXPOSE_NOTIFY = 0x1005;
    private const ushort FC_IMAGE_ACK = 0x1008;

    // Post-scan disconnect marker
    private static readonly byte[] PostScanDisconnect = [0xE7, 0x14, 0x02];

    // Timing
    private const int HeartbeatIntervalMs = 900;      // matches Python hb_interval=0.9
    private const double SessionRefreshSeconds = 1.5;  // matches Python SESSION_REFRESH_S
    private const int ReconnectHeartbeatGate = 10;
    private const int ScanIdleTimeoutMs = 2000;       // matches Python sock.settimeout(2.0) — end-of-stream detection
    private const int ExposeHardTimeoutMs = 90_000;    // matches Python EXPOSE_TIMEOUT_S — hard fallback

    // Live parsing markers
    private static readonly byte[] ScanlineMarker = [0x00, 0x01, 0x00, 0xF0];
    private const int ScanlinePixels = 240;

    // ── Configuration ───────────────────────────────────────────────────────
    private string _host;
    private int _port;
    private readonly string _defaultHost;
    private readonly int _defaultPort;
    private readonly int _maxReconnectAttempts;
    private readonly TimeSpan _reconnectDelay;
    private readonly IConfigService? _config;
    private readonly IEventLogService? _log;

    // ── State ───────────────────────────────────────────────────────────────
    private TcpClient? _tcp;
    private NetworkStream? _stream;
    private CancellationTokenSource? _sessionCts;
    private readonly SironaSession _session = new();
    private ConnectionState _state = ConnectionState.Disconnected;
    private Stopwatch _sessionTimer = new();
    private DateTime _lastHeartbeatTime = DateTime.MinValue;
    private bool _armed;

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
    public event EventHandler? DeviceArmed;

    /// <inheritdoc />
    public event EventHandler? ExposeStarted;

    /// <summary>Raised during auto-discovery with status messages for the UI.</summary>
    public event EventHandler<string>? DiscoveryStatus;

    /// <inheritdoc />
    public ConnectionState State => _state;

    public SironaService(
        string host = "192.168.139.170",
        int port = 12837,
        int maxReconnectAttempts = 5,
        TimeSpan? reconnectDelay = null,
        IConfigService? config = null,
        IEventLogService? log = null)
    {
        _host = host;
        _port = port;
        _defaultHost = host;
        _defaultPort = port;
        _maxReconnectAttempts = maxReconnectAttempts;
        _reconnectDelay = reconnectDelay ?? TimeSpan.FromSeconds(2);
        _config = config;
        _log = log;
    }

    // ── Public API ──────────────────────────────────────────────────────────

    /// <inheritdoc />
    public async Task ConnectAsync(CancellationToken ct = default)
    {
        await DisconnectAsync();

        _sessionCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        _session.Reset();

        SetState(ConnectionState.Connecting);

        // Try configured host first (quick 3s timeout)
        if (!await TryTcpConnectAsync(_host, _port, TimeSpan.FromSeconds(3), ct))
        {
            Debug.WriteLine($"[Sirona] Direct connect to {_host}:{_port} failed — starting discovery");

            // Auto-discover on the local network
            SetState(ConnectionState.Discovering);
            DiscoveryStatus?.Invoke(this, "Scanning network for Sirona device...");

            var discovered = await DiscoverSironaAsync(_port, ct);

            if (discovered is null)
            {
                SetState(ConnectionState.Disconnected);
                throw new ConnectionException(
                    $"Could not find Sirona device on {_host}:{_port} or local network.\n" +
                    "Verify the device is powered on and connected to this network.");
            }

            _host = discovered.Value.host;
            _port = discovered.Value.port;

            // Persist so next launch connects instantly
            _config?.SaveSironaEndpoint(_host, _port);
            DiscoveryStatus?.Invoke(this, $"Found Sirona at {_host}:{_port}");
            Debug.WriteLine($"[Sirona] Discovered device at {_host}:{_port}");

            SetState(ConnectionState.Connecting);

            // Connect to the discovered address
            if (!await TryTcpConnectAsync(_host, _port, TimeSpan.FromSeconds(5), ct))
                throw new ConnectionException($"Found Sirona at {_host}:{_port} but handshake failed.");
        }

        _stream = _tcp!.GetStream();

        // ── P2K Session Handshake ──────────────────────────────────────
        await PerformHandshakeAsync(ct);

        _sessionTimer.Restart();
        SetState(ConnectionState.Connected);

        // Start heartbeat and reader loops
        _ = HeartbeatLoopAsync(_sessionCts.Token);
        _ = ReaderLoopAsync(_sessionCts.Token);
    }

    /// <summary>
    /// Attempts a raw TCP connect with timeout. Stores TcpClient in _tcp on success.
    /// </summary>
    private async Task<bool> TryTcpConnectAsync(string host, int port, TimeSpan timeout, CancellationToken ct)
    {
        try
        {
            _tcp?.Dispose();
            _tcp = new TcpClient { ReceiveTimeout = 10000, SendTimeout = 5000 };
            using var connectCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            connectCts.CancelAfter(timeout);
            await _tcp.ConnectAsync(host, port, connectCts.Token);
            return true;
        }
        catch (Exception ex) when (ex is SocketException or OperationCanceledException or ObjectDisposedException)
        {
            Debug.WriteLine($"[Sirona] TCP connect to {host}:{port} failed: {ex.Message}");
            _tcp?.Dispose();
            _tcp = null;
            return false;
        }
    }

    /// <summary>
    /// P2K handshake: SESSION_OPEN_REQ + SESSION_INIT.
    /// Extracted so ConnectAsync and ReconnectAsync can share it.
    /// </summary>
    private async Task PerformHandshakeAsync(CancellationToken ct)
    {
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
    }

    // ── Auto-discovery ──────────────────────────────────────────────────

    /// <summary>
    /// Scans all local network subnets for a Sirona device accepting TCP on
    /// the given port (and fallback 1999). Mirrors purexs_gui.py _discover_sirona_tcp.
    /// Uses 50 concurrent probes with 300ms timeout per IP — full scan takes ~2s.
    /// </summary>
    private async Task<(string host, int port)?> DiscoverSironaAsync(int primaryPort, CancellationToken ct)
    {
        var subnets = GetLocalSubnets();
        var localIps = new HashSet<string>(
            NetworkInterface.GetAllNetworkInterfaces()
                .Where(n => n.OperationalStatus == OperationalStatus.Up)
                .SelectMany(n => n.GetIPProperties().UnicastAddresses)
                .Where(a => a.Address.AddressFamily == AddressFamily.InterNetwork)
                .Select(a => a.Address.ToString()));

        var ports = new List<int> { primaryPort };
        if (primaryPort != 1999) ports.Add(1999);

        foreach (var subnet in subnets)
        {
            DiscoveryStatus?.Invoke(this, $"Scanning {subnet}1-254...");
            Debug.WriteLine($"[Sirona] Discovery: scanning {subnet}1-254 on ports {string.Join(",", ports)}");

            var result = await ScanSubnetAsync(subnet, ports, localIps, ct);
            if (result is not null)
                return result;
        }

        return null;
    }

    private async Task<(string host, int port)?> ScanSubnetAsync(
        string subnet, List<int> ports, HashSet<string> localIps, CancellationToken ct)
    {
        var tcs = new TaskCompletionSource<(string host, int port)?>();
        using var scanCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        scanCts.CancelAfter(TimeSpan.FromSeconds(15));

        var semaphore = new SemaphoreSlim(50); // 50 concurrent probes
        var tasks = new List<Task>();

        for (var i = 1; i <= 254; i++)
        {
            var ip = $"{subnet}{i}";
            if (localIps.Contains(ip)) continue;

            tasks.Add(Task.Run(async () =>
            {
                await semaphore.WaitAsync(scanCts.Token);
                try
                {
                    foreach (var port in ports)
                    {
                        if (scanCts.IsCancellationRequested) return;
                        try
                        {
                            using var tcp = new TcpClient();
                            using var probeCts = CancellationTokenSource.CreateLinkedTokenSource(scanCts.Token);
                            probeCts.CancelAfter(300); // 300ms per probe — matches Python
                            await tcp.ConnectAsync(ip, port, probeCts.Token);
                            // Connection succeeded — this is our device
                            tcs.TrySetResult((ip, port));
                            scanCts.Cancel(); // stop other probes
                            return;
                        }
                        catch { /* not this IP/port */ }
                    }
                }
                finally
                {
                    semaphore.Release();
                }
            }, scanCts.Token));
        }

        // Wait for either a discovery or all probes to finish
        var allDone = Task.WhenAll(tasks).ContinueWith(_ => tcs.TrySetResult(null), TaskScheduler.Default);
        return await tcs.Task;
    }

    /// <summary>
    /// Gets all local IPv4 subnet prefixes (e.g. "192.168.139.") from active interfaces.
    /// </summary>
    private static List<string> GetLocalSubnets()
    {
        var subnets = new HashSet<string>();
        foreach (var iface in NetworkInterface.GetAllNetworkInterfaces())
        {
            if (iface.OperationalStatus != OperationalStatus.Up) continue;
            if (iface.NetworkInterfaceType is NetworkInterfaceType.Loopback) continue;

            foreach (var addr in iface.GetIPProperties().UnicastAddresses)
            {
                if (addr.Address.AddressFamily != AddressFamily.InterNetwork) continue;
                var parts = addr.Address.ToString().Split('.');
                var subnet = $"{parts[0]}.{parts[1]}.{parts[2]}.";
                subnets.Add(subnet);
            }
        }

        return subnets.ToList();
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
    public async Task ArmForExposeAsync(string lastName = "test", string firstName = "test", string examType = "Panoramic", CancellationToken ct = default)
    {
        if (_state != ConnectionState.Connected)
            throw new InvalidOperationException($"Cannot arm in state {_state}.");

        if (_stream is null)
            throw new InvalidOperationException("No active network stream.");

        // Reset scan state
        _session.ImageBuffer.Clear();
        _kvParseOffset = 0;
        _scanlineParseOffset = 0;
        _lastKvFired = 0;

        // 1. Fresh session refresh (matches Python: no prior HB before CAPS)
        await SessionRefreshAsync(ct);
        Debug.WriteLine("[Sirona] Fresh session for arm (no prior HB)");

        // 2. CAPS_REQ → CAPS_RESP
        await SendSessionFrameAsync(FC_CAPS_REQ, ct: ct);
        try
        {
            using var capsCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            capsCts.CancelAfter(3000);
            var resp = await ReceiveFrameAsync(capsCts.Token);
            var fc = resp.Length >= 2 ? (ushort)((resp[0] << 8) | resp[1]) : (ushort)0;
            Debug.WriteLine(fc == FC_CAPS_RESP
                ? $"[Sirona] CAPS_RESP received ({resp.Length} bytes)"
                : $"[Sirona] Expected CAPS_RESP (0x2111), got 0x{fc:X4} — continuing");
        }
        catch (OperationCanceledException)
        {
            Debug.WriteLine("[Sirona] CAPS_RESP timeout — continuing");
        }

        // 3. DATA_SEND (patient payload) + continuation
        var program = ExamTypeToProgram.GetValueOrDefault(examType, (ushort)0x01);
        var continuation = BuildDataContinuation(program);
        Debug.WriteLine($"[Sirona] Exam type: {examType} → program=0x{program:X2}");

        var payload = DataSendTemplate; // 156-byte known-good payload
        var totalLen = (ushort)(payload.Length + continuation.Length);
        var header = BuildSessionHeader(FC_DATA_SEND, payloadLength: totalLen);
        var frame = new byte[header.Length + payload.Length + continuation.Length];
        header.CopyTo(frame, 0);
        payload.CopyTo(frame, header.Length);
        continuation.CopyTo(frame, header.Length + payload.Length);

        await _stream.WriteAsync(frame, ct);
        await _stream.FlushAsync(ct);
        Debug.WriteLine($"[Sirona] DATA_SEND: {payload.Length}B payload + {continuation.Length}B continuation (program=0x{program:X2})");

        // 4. Wait for DATA_ACK (0x1001)
        try
        {
            using var ackCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            ackCts.CancelAfter(5000);
            var resp = await ReceiveFrameAsync(ackCts.Token);
            var fc = resp.Length >= 2 ? (ushort)((resp[0] << 8) | resp[1]) : (ushort)0;
            Debug.WriteLine(fc == FC_DATA_ACK
                ? "[Sirona] DATA_ACK received — device armed"
                : $"[Sirona] Expected DATA_ACK (0x1001), got 0x{fc:X4}");
        }
        catch (OperationCanceledException)
        {
            Debug.WriteLine("[Sirona] DATA_ACK timeout — device may not be armed");
        }

        // Device is now armed — waiting for physical button press
        _armed = true;
        _session.IsExposing = false; // not yet — waiting for EXPOSE_NOTIFY
        SetState(ConnectionState.Armed);
        DeviceArmed?.Invoke(this, EventArgs.Empty);
        Debug.WriteLine("[Sirona] Device ARMED — press R on keypad, then press EXPOSE button");
    }

    /// <inheritdoc />
    public async ValueTask DisposeAsync()
    {
        await DisconnectAsync();
    }

    // ── Patient data payloads (from Python hb_decoder.py, confirmed working) ──

    /// <summary>
    /// 156-byte DATA_SEND payload from ff.txt frame 750.
    /// Patient "test test", Doctor "Dr. Demo". Device does not validate
    /// these fields for exposure — they are for DICOM metadata only.
    /// </summary>
    private static readonly byte[] DataSendTemplate = [
        0xfc,0x30,0x00,0x00,0x1f,0x00,0x05,0x00,0xe6,0x07,0x11,0x00,
        0x0f,0x00,0x29,0x00,0xfa,0x00,0xdb,0x04,0x9b,0x08,0x00,0x04,
        0x00,0x74,0x00,0x65,0x00,0x73,0x00,0x74,0x00,0x04,0x00,0x74,
        0x00,0x65,0x00,0x73,0x00,0x74,0x00,0x01,0x00,0x01,0x07,0xd1,
        0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,
        0x00,0x08,0x00,0x44,0x00,0x72,0x00,0x2e,0x00,0x20,0x00,0x44,
        0x00,0x65,0x00,0x6d,0x00,0x6f,0x00,0x00,0x00,0x14,0x00,0x30,
        0x00,0x30,0x00,0x33,0x00,0x31,0x00,0x30,0x00,0x35,0x00,0x32,
        0x00,0x30,0x00,0x32,0x00,0x32,0x00,0x31,0x00,0x37,0x00,0x31,
        0x00,0x35,0x00,0x34,0x00,0x31,0x00,0x30,0x00,0x32,0x00,0x35,
        0x00,0x30,0x00,0x0f,0x00,0x44,0x00,0x45,0x00,0x53,0x00,0x4b,
        0x00,0x54,0x00,0x4f,0x00,0x50,0x00,0x2d,0x00,0x4e,0x00,0x4b,
        0x00,0x36,0x00,0x55,0x00,0x46,0x00,0x4d,0x00,0x4c,0x00,0x05,
    ];

    /// <summary>
    /// Maps user-facing exam type strings to DX81RecordingMode program codes.
    /// </summary>
    private static readonly Dictionary<string, ushort> ExamTypeToProgram = new()
    {
        ["Panoramic"]    = 0x01,
        ["Ceph Lateral"] = 0x02,
        ["Ceph Frontal"] = 0x03,
    };

    /// <summary>
    /// Base continuation data sent immediately after DATA_SEND payload (98 bytes).
    /// Byte 58-59 = DX81RecordingMode (big-endian uint16) — patched per exam type.
    /// </summary>
    private static readonly byte[] DataContinuationBase = [
        0x00,0x01,0x00,0x01,0x00,0x00,0x00,0x00,
        0x00,0x2c,0x00,0x02,0x00,0x01,0x00,0x00,
        0x00,0x00,0x00,0x2c,0x00,0x03,0x00,0x01,
        0x00,0x00,0x00,0x00,0x00,0x2c,0x00,0x01,
        0x00,0x02,0x00,0x00,0x00,0x00,0x00,0x2c,
        0x00,0x02,0x00,0x02,0x00,0x00,0x00,0x00,
        0x00,0x2c,0x00,0x00,0x00,0x00,0x00,0x04,
        0x00,0x08,0x00,0x01,0x00,0x0a,0x00,0x03,
        0xff,0xff,0x00,0x00,0x00,0x00,0x00,0x00,
        0x00,0x05,0x00,0x00,0x00,0x02,0xff,0xff,
        0x00,0x03,0x00,0x03,0x00,0x00,0x00,0x05,
        0xff,0xff,0x00,0x00,0x00,0x00,0x00,0x05,
        0xff,0xff,0x00,0x05,0xff,0xff,
    ];

    /// <summary>Offset of DX81RecordingMode in DataContinuationBase (big-endian uint16).</summary>
    private const int RecordingModeOffset = 58;

    /// <summary>
    /// Builds the DATA_SEND continuation with the specified program code.
    /// </summary>
    private static byte[] BuildDataContinuation(ushort program)
    {
        var buf = (byte[])DataContinuationBase.Clone();
        buf[RecordingModeOffset]     = (byte)((program >> 8) & 0xFF);
        buf[RecordingModeOffset + 1] = (byte)(program & 0xFF);
        return buf;
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
                    // Session refresh if needed — but NOT when armed
                    // (session must survive until scan completes)
                    if (!_armed && _sessionTimer.Elapsed.TotalSeconds >= SessionRefreshSeconds)
                    {
                        await SessionRefreshAsync(ct);
                        continue;
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
                int bytesRead;

                if (_session.IsExposing)
                {
                    // During exposure: use a 2-second idle timeout to detect
                    // end-of-stream, matching Python's sock.settimeout(2.0).
                    // ReadAsync doesn't respect ReceiveTimeout, so we use a
                    // CancellationToken with a timeout instead.
                    using var idleCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
                    idleCts.CancelAfter(ScanIdleTimeoutMs);

                    try
                    {
                        bytesRead = await _stream.ReadAsync(buffer.AsMemory(0, buffer.Length), idleCts.Token);
                    }
                    catch (OperationCanceledException) when (!ct.IsCancellationRequested)
                    {
                        // Idle timeout — no data for 2s, scan stream has ended
                        Debug.WriteLine($"[Sirona] Scan stream idle timeout ({ScanIdleTimeoutMs}ms) — completing exposure with {_session.ImageBuffer.Count} bytes");
                        CompleteExposure("idle-timeout-2s");
                        return;
                    }
                }
                else
                {
                    bytesRead = await _stream.ReadAsync(buffer.AsMemory(0, buffer.Length), ct);
                }

                if (bytesRead == 0)
                {
                    // Connection closed — if we were exposing, complete with what we have
                    if (_session.IsExposing)
                    {
                        Debug.WriteLine($"[Sirona] Connection closed during exposure — completing with {_session.ImageBuffer.Count} bytes");
                        CompleteExposure("connection-closed");
                        return;
                    }
                    break;
                }

                var data = buffer[..bytesRead];

                // Check for post-scan disconnect marker: E7 14 02
                if (ContainsSequence(data, PostScanDisconnect))
                {
                    _session.IsPostScanDisconnect = true;
                    _armed = false;

                    if (_session.IsExposing)
                    {
                        Debug.WriteLine($"[Sirona] PostScanDisconnect marker found — completing exposure with {_session.ImageBuffer.Count} bytes");
                        CompleteExposure("post-scan-marker");
                    }
                    else
                    {
                        _ = ReconnectAsync();
                    }
                    return;
                }

                // Detect EXPOSE_NOTIFY (0x1005) — physical button pressed
                if (_armed && !_session.IsExposing && bytesRead >= 2)
                {
                    var fc = (ushort)((data[0] << 8) | data[1]);
                    if (fc == FC_EXPOSE_NOTIFY)
                    {
                        Debug.WriteLine($"[Sirona] EXPOSE_NOTIFY received — exposure starting! {bytesRead} bytes");
                        _session.IsExposing = true;
                        SetState(ConnectionState.Exposing);
                        ExposeStarted?.Invoke(this, EventArgs.Empty);

                        // Start hard timeout watchdog (matches Python EXPOSE_TIMEOUT_S)
                        _ = ExposeHardTimeoutAsync(ct);

                        // Seed the image buffer with any payload after the header
                        if (bytesRead > SESSION_HEADER_SIZE)
                        {
                            var payload = data[SESSION_HEADER_SIZE..];
                            _session.ImageBuffer.AddRange(payload);
                            Debug.WriteLine($"[Sirona] Seeded scan buffer with {payload.Length} bytes from EXPOSE_NOTIFY");
                        }
                        continue;
                    }
                }

                // Buffer image bytes during expose
                if (_session.IsExposing)
                {
                    _session.ImageBuffer.AddRange(data);
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

            // If we were mid-expose when connection dropped, complete with what we have
            if (_session.IsExposing && _session.ImageBuffer.Count > 0)
            {
                Debug.WriteLine($"[Sirona] Reader error during exposure — completing with {_session.ImageBuffer.Count} bytes");
                CompleteExposure($"reader-error: {ex.Message}");
                return;
            }

            if (!ct.IsCancellationRequested)
                _ = ReconnectAsync();
        }
    }

    /// <summary>
    /// Finalizes an exposure: sends IMAGE_ACK, fires ImageReceived, and reconnects.
    /// Matches the Python flow where _recv_scan_data ends on socket timeout,
    /// then events are fired and reconnect happens.
    /// </summary>
    private void CompleteExposure(string reason)
    {
        _session.IsExposing = false;
        _session.IsPostScanDisconnect = true;
        _armed = false;

        // Send IMAGE_ACK (best effort, matches Python behavior)
        try { _ = SendSessionFrameAsync(FC_IMAGE_ACK); }
        catch { /* best effort */ }

        var imageBytes = _session.ImageBuffer.ToArray();
        Debug.WriteLine($"[Sirona] CompleteExposure ({reason}) — {imageBytes.Length} bytes, firing ImageReceived");
        // Mirror to the file log so post-mortem of bad scans doesn't require a
        // debugger or DebugView. The reason string identifies which of the five
        // CompleteExposure paths fired (idle-timeout-2s, post-scan-marker,
        // connection-closed, reader-error, hard-watchdog-90s).
        var level = reason.StartsWith("reader-error") || reason == "hard-watchdog-90s" ? "warning" : "info";
        _log?.Log($"Exposure ended: {reason} ({imageBytes.Length} raw bytes)", level);

        if (imageBytes.Length > 0)
            ImageReceived?.Invoke(this, imageBytes);

        _ = ReconnectAsync();
    }

    /// <summary>
    /// Hard timeout watchdog: if exposure hasn't completed within 90 seconds,
    /// force-complete with whatever data we have. Matches Python's
    /// _on_expose_timeout() fallback.
    /// </summary>
    private async Task ExposeHardTimeoutAsync(CancellationToken ct)
    {
        try
        {
            await Task.Delay(ExposeHardTimeoutMs, ct);

            if (_session.IsExposing)
            {
                Debug.WriteLine($"[Sirona] Hard exposure timeout ({ExposeHardTimeoutMs}ms) — force-completing with {_session.ImageBuffer.Count} bytes");
                CompleteExposure("hard-watchdog-90s");
            }
        }
        catch (OperationCanceledException) { /* Normal — exposure completed before timeout */ }
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

    private static bool ContainsSequence(byte[] data, byte[] sequence)
    {
        for (var i = 0; i <= data.Length - sequence.Length; i++)
        {
            var match = true;
            for (var j = 0; j < sequence.Length; j++)
            {
                if (data[i + j] != sequence[j])
                {
                    match = false;
                    break;
                }
            }
            if (match) return true;
        }
        return false;
    }
}

/// <summary>Simple exception for connection errors.</summary>
public class ConnectionException : Exception
{
    public ConnectionException(string message) : base(message) { }
}
