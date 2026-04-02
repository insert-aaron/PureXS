# PureXS — Panoramic X-Ray Pipeline

Direct TCP control of the Dentsply Sirona Orthophos XG. Reconstructs clinical-quality panoramic dental X-rays from raw detector data without Sidexis.

## Architecture

```
PureXS.exe (WPF/.NET 8)              decoder/ (Python)
  GUI, TCP client, PureChart            Image reconstruction pipeline
       |                                     |
       |-- Connect to Orthophos XG          |-- hb_decoder.py
       |-- Heartbeat keep-alive             |-- purexs_decoder_cli.py
       |-- Receive raw scan bytes           |-- utils.py
       |-- Call decoder subprocess -------->|-- dicom_export.py
       |-- Display processed PNG            |-- calibration_capture.py
       |-- Upload to PureChart              |-- requirements.txt
       |                                     |-- *.npy lookup tables
```

### Bridge Pattern

The WPF app handles the GUI and device communication. After receiving raw scan bytes, it writes them to a temp file and calls the Python decoder as a subprocess:

```
Raw bytes -> temp.bin -> python purexs_decoder_cli.py --input temp.bin --output pano.png -> display PNG
```

This keeps the imaging pipeline in Python (easy to iterate) while the GUI stays in WPF (native Windows look).

### Two-Repo Pattern

| Repo | Purpose |
|---|---|
| `insert-aaron/PureXS` | Source code (.NET + Python) |
| `insert-aaron/PureXS-releases` | Deployed files — WPF binaries + Python decoder + SetupAndRun.bat |

### Deployment Flow

```
Push to PureXS main
        |
CI builds WPF (x64 + x86), bundles Python decoder files
        |
Deploys to PureXS-releases
        |
Clinic PC shortcut -> SetupAndRun.bat
        |
git fetch + hash compare -> git reset --hard if new version
        |
Installs Python (embedded) + pip deps if needed
        |
Launches PureXS.exe
```

## Development

### Prerequisites
- .NET 8 SDK
- Python 3.9+ (for testing the decoder locally)

### Deploy Methods

**1. GitHub Actions CI (automatic)**
Every push to `main` triggers `.github/workflows/deploy.yml`:
- Builds WPF app (x64 + x86, self-contained)
- Bundles Python decoder files into `decoder/` subdirectory
- Pushes to PureXS-releases

Requires `DEPLOY_TOKEN` secret (GitHub PAT with repo write access to PureXS-releases).

**2. Manual from Mac**
```bash
chmod +x build-and-deploy.sh
./build-and-deploy.sh "Fixed dead row interpolation"
```

### Key Files

| File | Purpose |
|---|---|
| `PureXS.WPF/` | WPF app — GUI, TCP client, PureChart |
| `hb_decoder.py` | Image reconstruction pipeline |
| `purexs_decoder_cli.py` | CLI entry point for decoder (called by WPF) |
| `utils.py` | Shared utilities |
| `dicom_export.py` | DICOM file generation |
| `requirements-decoder.txt` | Python dependencies for the decoder |
| `build-and-deploy.sh` | Manual Mac deploy script |
| `.github/workflows/deploy.yml` | CI auto-deploy |

## Machine-Specific (not in repo)

| File | Location on Clinic PC | Purpose |
|---|---|---|
| `flat_field_norm.npy` | `C:\PureXS\` | 2D flat-field calibration map |
| `flat_field_raw.bin` | `C:\PureXS\` | Raw air-scan backup |

These are generated per-machine. The `.gitignore` excludes them so `git reset --hard` never overwrites calibration data.

## Setting Up a New Clinic

1. Create `PureXS-releases` repo on GitHub (if not exists)
2. Push built files + `SetupAndRun.bat`
3. On clinic PC: download and double-click `SetupAndRun.bat`
4. It installs Git, embedded Python, pip deps, clones the repo, creates desktop shortcut
5. Capture flat-field: run blank air exposure with empty chair
6. Verify `C:\PureXS\flat_field_norm.npy` exists
7. Test with a patient scan

## Setting Up CI

1. Create a GitHub Personal Access Token with `repo` scope
2. In PureXS source repo -> Settings -> Secrets -> `DEPLOY_TOKEN`
3. Push to `main` -> CI builds and deploys
