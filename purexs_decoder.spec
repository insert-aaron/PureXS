# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for purexs_decoder.exe

Build with:
    pyinstaller purexs_decoder.spec

Produces: dist/purexs_decoder/purexs_decoder.exe (one-dir mode)
"""

import os

block_cipher = None
spec_dir = os.path.dirname(os.path.abspath(SPEC))

# Data files that the decoder needs at runtime
datas = []
for npy in [
    'flat_field_norm.npy',
    'flat_field_gain_map.npy',
    'flat_field_row_profile.npy',
    'sgf_frame_gain.npy',
    'sidexis_tone_lut.npy',
]:
    npy_path = os.path.join(spec_dir, npy)
    if os.path.exists(npy_path):
        datas.append((npy_path, '.'))

a = Analysis(
    ['purexs_decoder_cli.py'],
    pathex=[spec_dir],
    binaries=[],
    datas=datas,
    hiddenimports=[
        'scipy.ndimage',
        'scipy.signal',
        'cv2',
        'PIL',
        'numpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'customtkinter', 'matplotlib', 'PyQt5', 'PyQt6'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='purexs_decoder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # need stdout/stderr for error reporting
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='purexs_decoder',
)
