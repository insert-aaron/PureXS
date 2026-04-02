#!/bin/bash
# ============================================================
# build-and-deploy.sh — PureXS (WPF + Python decoder bridge)
# Run from Mac dev machine to cross-compile and push to PureXS-releases
# Usage: ./build-and-deploy.sh [commit message]
# ============================================================

set -e

# --- Config ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$SCRIPT_DIR/PureXS.WPF/PureXS.WPF.csproj"
STAGING="$SCRIPT_DIR/releases-staging"
RELEASES_DIR="$HOME/Desktop/PureXS-releases"
RELEASES_REPO="git@github.com:insert-aaron/PureXS-releases.git"
BRANCH="main"

# Python decoder files to bundle alongside the WPF exe
DECODER_FILES=(
    "hb_decoder.py"
    "purexs_decoder_cli.py"
    "utils.py"
    "dicom_export.py"
    "calibration_capture.py"
)

DECODER_NPY=(
    "sidexis_tone_lut.npy"
    "sgf_frame_gain.npy"
)

# --- Colors ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN}  PureXS — Build & Deploy (WPF + Python)${NC}"
echo -e "${GREEN}==========================================${NC}"
echo ""

# --- Validate ---
if [ ! -f "$PROJECT" ]; then
    echo -e "${RED}ERROR: Project file not found at $PROJECT${NC}"
    exit 1
fi

if ! command -v dotnet &>/dev/null; then
    echo -e "${RED}ERROR: dotnet CLI not found. Install .NET 8 SDK first.${NC}"
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/hb_decoder.py" ]; then
    echo -e "${RED}ERROR: hb_decoder.py not found${NC}"
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

# --- Bundle Python decoder ---
echo "=========================================="
echo "Bundling Python decoder files..."
echo "=========================================="
mkdir -p "$STAGING/decoder" "$STAGING/x86/decoder"

for f in "${DECODER_FILES[@]}"; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        cp "$SCRIPT_DIR/$f" "$STAGING/decoder/$f"
        cp "$SCRIPT_DIR/$f" "$STAGING/x86/decoder/$f"
        echo "  + $f"
    else
        echo -e "  ${RED}! $f not found${NC}"
    fi
done

for f in "${DECODER_NPY[@]}"; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        cp "$SCRIPT_DIR/$f" "$STAGING/decoder/$f"
        cp "$SCRIPT_DIR/$f" "$STAGING/x86/decoder/$f"
        echo "  + $f (lookup table)"
    fi
done

# Copy decoder requirements
if [ -f "$SCRIPT_DIR/requirements-decoder.txt" ]; then
    cp "$SCRIPT_DIR/requirements-decoder.txt" "$STAGING/decoder/requirements.txt"
    cp "$SCRIPT_DIR/requirements-decoder.txt" "$STAGING/x86/decoder/requirements.txt"
    echo "  + requirements.txt"
fi

echo -e "${GREEN}[decoder] Bundled into both builds.${NC}"

# --- Write version ---
VERSION=$(date +"%Y.%m.%d-%H%M")
echo "$VERSION" > "$STAGING/version.txt"
echo "$VERSION" > "$STAGING/x86/version.txt"

echo ""
echo "=========================================="

if [ "$FAILED" -eq 0 ]; then
    echo -e "${GREEN}All builds succeeded.${NC}"
    echo "Staged output: $STAGING/"
    echo "  x64 binaries  -> $STAGING/ (root)"
    echo "  x86 binaries  -> $STAGING/x86/"
    echo "  Python decoder -> $STAGING/decoder/ and $STAGING/x86/decoder/"
    echo ""

    # --- Deploy to releases repo ---
    if [ -d "$RELEASES_DIR/.git" ]; then
        echo "Deploying to PureXS-releases..."
        cd "$RELEASES_DIR"
        git checkout "$BRANCH"
        git pull origin "$BRANCH"

        # Clear old binaries (keep .git, SetupAndRun.bat, README.md, Assets/, .gitignore)
        find "$RELEASES_DIR" -maxdepth 1 \
            ! -name '.git' ! -name 'SetupAndRun.bat' ! -name 'README.md' \
            ! -name 'Assets' ! -name '.gitignore' ! -name '.' \
            -type f -exec rm {} \;
        rm -rf "$RELEASES_DIR/x86" "$RELEASES_DIR/decoder"

        # Copy staged files
        cp -R "$STAGING/"* "$RELEASES_DIR/"

        COMMIT_MSG="${1:-PureXS release $VERSION}"
        git add -A
        if git diff --cached --quiet; then
            echo -e "${YELLOW}No changes to deploy.${NC}"
        else
            git commit -m "$COMMIT_MSG"
            git push origin "$BRANCH"
            echo -e "${GREEN}Deployed: $COMMIT_MSG${NC}"
        fi
    else
        echo "To deploy, copy releases-staging/ into your PureXS-releases clone."
    fi
else
    echo -e "${RED}One or more builds failed. Check output above.${NC}"
    exit 1
fi
