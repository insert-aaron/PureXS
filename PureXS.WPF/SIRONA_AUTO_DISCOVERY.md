# Sirona Auto-Discovery — Connection Fix for Multi-Facility Deployment

## Problem

When installing PureXS WPF on a different facility computer, the app failed to connect to the Sirona machine. The root cause was a hardcoded default IP (`192.168.139.170:12837`) that only works on the original facility's network. Different facilities have different network configurations, so the Sirona device sits on a different IP.

## Solution — Automatic Network Discovery

### Connection Flow

1. User clicks **Connect to Device**
2. App tries the configured IP with a quick 3-second timeout
3. If that fails, auto-discovery kicks in:
   - Status bar shows "Scanning network for Sirona device..."
   - Scans all local network subnets (1–254) on ports `12837` and `1999`
   - Uses 50 concurrent TCP probes with 300ms timeout each — full scan takes ~2 seconds
4. On success:
   - Saves the discovered IP to `%AppData%\PureXS\config.json`
   - Connects and performs P2K handshake
5. **Next launch: connects instantly** using the saved IP (no rescan needed)

### IP Resolution Priority

```
1. SIRONA_IP environment variable  (highest — manual override)
2. Persisted config.json           (saved from previous discovery)
3. Hardcoded default 192.168.139.170  (lowest — original facility)
```

### Files Changed

| File | Change |
|------|--------|
| `Services/ISironaService.cs` | Added `Discovering` connection state and `DiscoveryStatus` event |
| `Services/SironaService.cs` | Added `DiscoverSironaAsync()`, `TryTcpConnectAsync()`, `PerformHandshakeAsync()`, `ScanSubnetAsync()`, `GetLocalSubnets()` |
| `Services/IConfigService.cs` | Added `SironaHost`, `SironaPort`, `SaveSironaEndpoint()` |
| `Services/ConfigService.cs` | Implemented Sirona endpoint persistence in config.json |
| `App.xaml.cs` | Reads persisted Sirona IP from config, passes config to SironaService |
| `ViewModels/MainViewModel.cs` | Handles `Discovering` state in UI, wires `DiscoveryStatus` event |

### Manual Override

If auto-discovery doesn't work (e.g., firewall blocks port scanning), set environment variables before launching:

```batch
set SIRONA_IP=192.168.X.X
set SIRONA_PORT=12837
PureXS.exe
```

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Could not find Sirona device on local network" | Device powered off, wrong subnet, or firewall | Verify device is on, check `ping <device_ip>`, allow TCP 12837 in firewall |
| Discovery finds device but handshake fails | Another client (Sidexis) holding the session | Close Sidexis completely, wait 10s, retry |
| Connects on first launch but fails later | Device IP changed (DHCP) | Delete `sirona_host` from `%AppData%\PureXS\config.json` to force re-discovery |

### Config File Location

```
%AppData%\PureXS\config.json
```

Example contents after discovery:
```json
{
  "facility_token": "...",
  "sirona_host": "192.168.1.50",
  "sirona_port": 12837
}
```
