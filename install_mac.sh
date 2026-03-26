#!/bin/bash
echo "============================================"
echo " PureXS — macOS / Linux Installer"
echo "============================================"
echo

if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 not found."
    echo "  macOS:  brew install python"
    echo "  Linux:  sudo apt install python3 python3-pip python3-tk"
    exit 1
fi

echo "Upgrading pip..."
python3 -m pip install --upgrade pip

echo
echo "Installing dependencies..."
pip3 install -r requirements.txt

echo
echo "============================================"
echo " PureXS installed successfully."
echo
echo " To launch:"
echo "   python3 purexs_launcher.py"
echo
echo " To run hardware test:"
echo "   python3 live_test.py --replay ff.txt"
echo "============================================"
