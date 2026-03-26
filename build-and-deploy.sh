#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# PureXS — Local macOS build script (cross-compile for Windows)
# Builds x64 and x86 self-contained binaries and stages them
# in releases-staging/ mirroring the PureXS-releases repo layout.
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$SCRIPT_DIR/PureXS.WPF/PureXS.WPF.csproj"
STAGING="$SCRIPT_DIR/releases-staging"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

if [ ! -f "$PROJECT" ]; then
    echo -e "${RED}ERROR: Project file not found at $PROJECT${NC}"
    exit 1
fi

# Ensure dotnet is available
if ! command -v dotnet &>/dev/null; then
    echo -e "${RED}ERROR: dotnet CLI not found. Install .NET 8 SDK first.${NC}"
    exit 1
fi

# Clean staging directory
rm -rf "$STAGING"
mkdir -p "$STAGING/x86"

FAILED=0

# --- Build x64 ---
echo "=========================================="
echo "Building x64 (win-x64) self-contained..."
echo "=========================================="
if dotnet publish "$PROJECT" \
    -c Release \
    -r win-x64 \
    --self-contained true \
    -p:EnableWindowsTargeting=true \
    -o "$STAGING/x64-tmp"; then
    # Move x64 files into staging root (mirrors releases repo structure)
    cp -R "$STAGING/x64-tmp/"* "$STAGING/"
    rm -rf "$STAGING/x64-tmp"
    echo -e "${GREEN}[x64] Build succeeded.${NC}"
else
    echo -e "${RED}[x64] Build FAILED.${NC}"
    FAILED=1
fi

echo ""

# --- Build x86 ---
echo "=========================================="
echo "Building x86 (win-x86) self-contained..."
echo "=========================================="
if dotnet publish "$PROJECT" \
    -c Release \
    -r win-x86 \
    --self-contained true \
    -p:EnableWindowsTargeting=true \
    -o "$STAGING/x86"; then
    echo -e "${GREEN}[x86] Build succeeded.${NC}"
else
    echo -e "${RED}[x86] Build FAILED.${NC}"
    FAILED=1
fi

echo ""
echo "=========================================="

if [ "$FAILED" -eq 0 ]; then
    echo -e "${GREEN}All builds succeeded.${NC}"
    echo "Staged output: $STAGING/"
    echo "  x64 binaries → $STAGING/ (root)"
    echo "  x86 binaries → $STAGING/x86/"
    echo ""
    echo "To deploy manually, copy the contents of releases-staging/"
    echo "into your local PureXS-releases clone, commit, and push."
else
    echo -e "${RED}One or more builds failed. Check output above.${NC}"
    exit 1
fi
