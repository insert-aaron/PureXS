namespace PureXS.Models;

/// <summary>
/// Represents the state of an active TCP session with a Sirona ORTHOPHOS XG unit.
/// </summary>
public class SironaSession
{
    /// <summary>Current heartbeat sequence number (wraps at 255).</summary>
    public byte SequenceNumber { get; set; }

    /// <summary>Total heartbeat packets sent this session.</summary>
    public long HeartbeatCount { get; set; }

    /// <summary>Timestamp of the last successful heartbeat acknowledgement.</summary>
    public DateTime? LastHeartbeatAck { get; set; }

    /// <summary>True while a panoramic expose is in progress.</summary>
    public bool IsExposing { get; set; }

    /// <summary>True after the post-scan E7 14 02 disconnect has been received.</summary>
    public bool IsPostScanDisconnect { get; set; }

    /// <summary>Number of heartbeat cycles completed after a reconnect (used for the 10-cycle resume gate).</summary>
    public int ReconnectHeartbeatCycles { get; set; }

    /// <summary>kV ramp value reported during expose.</summary>
    public double CurrentKv { get; set; }

    /// <summary>Accumulated raw image bytes received during the scan.</summary>
    public List<byte> ImageBuffer { get; } = new();

    /// <summary>Resets session state for a fresh connection.</summary>
    public void Reset()
    {
        SequenceNumber = 0;
        HeartbeatCount = 0;
        LastHeartbeatAck = null;
        IsExposing = false;
        IsPostScanDisconnect = false;
        ReconnectHeartbeatCycles = 0;
        CurrentKv = 0;
        ImageBuffer.Clear();
    }
}
