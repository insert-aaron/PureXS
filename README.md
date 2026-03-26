# PureXS — Sidexis-Free Sirona Orthophos Controller

Direct TCP control of Dentsply Sirona Orthophos panoramic x-ray machines,
replacing Sidexis entirely via reverse-engineered P2K wire protocol.

## Quick Start

### Windows
1. Install [Python 3.11+](https://www.python.org/downloads/) (check **Add Python to PATH**)
2. Double-click `install_windows.bat`
3. Double-click `purexs_launcher.pyw` (no terminal window)

### macOS / Linux
```bash
bash install_mac.sh
python3 purexs_launcher.py
```

## Features

| Feature | Description |
|---|---|
| **HB Monitor** | Live heartbeat (0x200B/200C) at 0.1s intervals, RTT gauge |
| **Expose** | Direct trigger (`ff 12 01 03 42 0e 01`), 4s panoramic scan |
| **kV Gauge** | Real-time tube voltage display (70.0 kV peak) |
| **Patient Workflow** | Name/DOB/ID entry, expose gating, auto-named output files |
| **DICOM Export** | DX SOP class (1.2.840.10008.5.1.4.1.1.1.1), verified tags |
| **Patient History** | Searchable history view with PNG/DICOM/log file launchers |
| **Live Test Suite** | 5-test hardware validation (`python live_test.py`) |
| **Replay Mode** | Offline validation from Wireshark dumps (`--replay ff.txt`) |

## Network Setup

| Role | Address |
|---|---|
| Sirona Orthophos | `192.168.139.170:12837` |
| PC (any) | `192.168.139.x` subnet |

The PC must be on the same subnet as the Orthophos.
No Sidexis installation, DLLs, or licensing required.

## Files

```
PureXS/
  purexs_gui.py          GUI (CustomTkinter, dark mode)
  hb_decoder.py          P2K protocol decoder + live TCP client
  dicom_export.py        DICOM DX export (pydicom)
  history.py             Patient history viewer
  purexs_launcher.py     Zero-terminal launcher
  purexs_launcher.pyw    Windows no-console launcher (same file)
  live_test.py           Hardware test suite (5 tests)
  utils.py               Cross-platform path + file-open helpers
  requirements.txt       Python dependencies
  install_windows.bat    Windows one-click installer
  install_mac.sh         macOS/Linux installer
```

## Data Storage

All patient data, logs, and config are stored in:
- **Windows:** `%APPDATA%\PureXS\` (`C:\Users\{you}\AppData\Roaming\PureXS\`)
- **macOS/Linux:** `~/.purexs/`

```
PureXS/                         (or ~/.purexs/)
  logs/                         GUI session logs
  events.log                    All expose events
  recent_patients.json          Last 10 patients (quick-fill)
  patients/
    {PatientID}/
      sessions.json             Append-only scan history
      Smith_John_..._panoramic.png
      Smith_John_..._panoramic.dcm
      Smith_John_..._events.log
```

## Hardware Test

```bash
# Against real hardware (prompts before firing x-ray)
python live_test.py --host 192.168.139.170

# Offline replay from Wireshark capture
python live_test.py --replay ff.txt

# Skip expose test (connection + HB + DICOM only)
python live_test.py --skip-expose
```

## Protocol Reference

Reverse-engineered from SiNet2.dll, SiPanCtl.dll, and Wireshark captures.

| Element | Wire Format |
|---|---|
| Session header | 20 bytes BE: `[func_hi func_lo 07 2D 07 D0 00 01 00 0E ...]` |
| HB request | func `0x200B`, 20 bytes |
| HB response | func `0x200C`, 20 bytes, RTT 1.6-2.4ms |
| Expose trigger | `ff 12 01 03 42 0e 01` |
| kV ramp record | 15 bytes: `[01 KV_HI KV_LO 01 ... 0E 01]` |
| Scanline header | `[01 ID 00 01 00 F0 00 34]` + 240 x uint16 BE pixels |
| Post-scan error | `E7 14 02 (ERR_SIDEXIS_API)` — normal, triggers reconnect |

## PyInstaller (optional)

Build a single `.exe` on Windows (no Python install needed for end users):

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name PureXS purexs_launcher.py
```

Output: `dist/PureXS.exe`

## License

MIT
