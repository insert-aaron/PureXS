@echo off
echo ============================================
echo  PureXS — Windows Installer
echo ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.11+ from python.org
    echo        Check "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo Removing old PureXS system install (if any)...
pip uninstall purexs -y >nul 2>&1

echo.
echo Installing dependencies...
pip install -r requirements.txt

echo.
echo ============================================
echo  PureXS installed successfully.
echo.
echo  To launch (no terminal window):
echo    Double-click purexs_launcher.pyw
echo.
echo  Or from command line:
echo    python purexs_launcher.py
echo ============================================
pause
