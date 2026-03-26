#!/usr/bin/env python3
"""
PureXS Launcher — zero-terminal one-click startup.

Launches purexs_gui.py from the same directory.  No API server, no FastAPI,
no uvicorn — the GUI connects directly to the Sirona Orthophos via raw TCP.

Windows:  Double-click purexs_launcher.pyw (no console window).
macOS:    python3 purexs_launcher.py
"""

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    # Work from the launcher's own directory
    here = Path(__file__).resolve().parent
    os.chdir(here)

    gui = here / "purexs_gui.py"
    if not gui.exists():
        _show_error(f"purexs_gui.py not found in:\n{here}")
        return 1

    # Build command
    cmd = [sys.executable, str(gui)]

    # Windows: suppress the console window
    kwargs: dict = {}
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(cmd, **kwargs)
        proc.wait()
        return proc.returncode or 0
    except Exception as exc:
        _show_error(f"Failed to start PureXS GUI:\n{exc}")
        return 1


def _show_error(msg: str) -> None:
    """Show an error popup (works even if customtkinter isn't installed)."""
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "PureXS", 0x10)
        else:
            import tkinter as _tk
            from tkinter import messagebox
            root = _tk.Tk()
            root.withdraw()
            messagebox.showerror("PureXS", msg)
            root.destroy()
    except Exception:
        print(f"ERROR: {msg}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
