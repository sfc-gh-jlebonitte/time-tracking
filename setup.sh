#!/usr/bin/env bash
# =============================================================================
# setup.sh — First-time setup for the Weekly Activity Report
#
# What this script does:
#   1. Checks that Python 3.11 or newer is available on your Mac
#   2. Checks whether Google Cloud SDK (gcloud) is installed
#      — if not, offers to download and install it for you
#   3. Checks whether Google Calendar access is authorised
#      — if not, opens a browser to authenticate
#   4. Checks whether a Python virtual environment already exists
#      — if not, offers to create one for you
#   5. Installs all required Python packages (via pip, no Homebrew needed)
#
# You only need to run this once. It is safe to run again at any time —
# it will skip steps that are already complete.
#
# Usage:
#   bash setup.sh
# =============================================================================

set -euo pipefail

# ---------- colour helpers ---------------------------------------------------
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓  $*${NC}"; }
warn() { echo -e "${YELLOW}  !  $*${NC}"; }
err()  { echo -e "${RED}  ✗  $*${NC}"; }
info() { echo -e "     $*"; }

# ---------- locate this script -----------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"

echo ""
echo "=========================================="
echo "  Weekly Activity Report — Setup"
echo "=========================================="
echo ""

# ---------- 1. Check Python --------------------------------------------------
echo "Step 1 of 5: Checking Python..."

PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    err "Python 3.11 or newer was not found on your computer."
    echo ""
    info "To fix this:"
    info "  1. Go to  https://www.python.org/downloads/"
    info "  2. Download and install the latest Python 3 release for macOS"
    info "  3. Open a new Terminal window and run this script again"
    echo ""
    exit 1
fi

PYTHON_VERSION=$("$PYTHON_CMD" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
ok "Found Python $PYTHON_VERSION  ($PYTHON_CMD)"

# ---------- 2. Check / install Google Cloud SDK (gcloud) ---------------------
echo ""
echo "Step 2 of 5: Checking Google Cloud SDK..."

GCLOUD_CMD=""
# Check common install locations in addition to PATH
for candidate in \
    "gcloud" \
    "$HOME/google-cloud-sdk/bin/gcloud" \
    "$HOME/Downloads/google-cloud-sdk/bin/gcloud" \
    "/usr/local/google-cloud-sdk/bin/gcloud"
do
    if command -v "$candidate" &>/dev/null 2>&1; then
        GCLOUD_CMD="$candidate"
        break
    fi
done

if [ -n "$GCLOUD_CMD" ]; then
    GCLOUD_VERSION=$("$GCLOUD_CMD" --version 2>/dev/null | head -1 || echo "unknown version")
    ok "Google Cloud SDK found — $GCLOUD_VERSION"
else
    warn "Google Cloud SDK (gcloud) was not found."
    echo ""
    info "This tool uses gcloud to securely access your Google Calendar."
    info "It needs to be installed once on your computer."
    echo ""
    printf "     Would you like to download and install it now? [Y/n] "
    read -r answer
    answer="${answer:-Y}"

    if [[ "$answer" =~ ^[Yy]$ ]]; then
        echo ""
        # Detect chip type
        ARCH=$(uname -m)
        if [ "$ARCH" = "arm64" ]; then
            GCLOUD_ARCH="arm"
            info "Detected: Apple Silicon Mac (M1/M2/M3)"
        else
            GCLOUD_ARCH="x86_64"
            info "Detected: Intel Mac"
        fi

        GCLOUD_URL="https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-darwin-${GCLOUD_ARCH}.tar.gz"
        GCLOUD_INSTALL_DIR="$HOME/google-cloud-sdk"
        GCLOUD_ARCHIVE="/tmp/google-cloud-sdk.tar.gz"

        echo ""
        info "Downloading Google Cloud SDK (~100 MB)..."
        curl -# -L "$GCLOUD_URL" -o "$GCLOUD_ARCHIVE"

        info "Extracting..."
        tar -xzf "$GCLOUD_ARCHIVE" -C "$HOME"
        rm -f "$GCLOUD_ARCHIVE"

        info "Installing..."
        # Run the installer non-interactively; adds gcloud to ~/.bash_profile and ~/.zshrc
        "$GCLOUD_INSTALL_DIR/install.sh" \
            --quiet \
            --usage-reporting false \
            --path-update true \
            --command-completion true 2>/dev/null || true

        GCLOUD_CMD="$GCLOUD_INSTALL_DIR/bin/gcloud"

        # Make gcloud available in this shell session immediately
        export PATH="$GCLOUD_INSTALL_DIR/bin:$PATH"

        ok "Google Cloud SDK installed at:  ~/google-cloud-sdk/"
        echo ""
        warn "NOTE: To use 'gcloud' in future Terminal windows, close this window"
        info "      and open a new one (the installer updated your shell profile)."
    else
        echo ""
        err "Setup cancelled. Google Cloud SDK is required to access your calendar."
        info "Run this script again when you are ready to install it."
        echo ""
        exit 1
    fi
fi

# ---------- 3. Google Calendar authorisation -----------------------------------
echo ""
echo "Step 3 of 5: Checking Google Calendar authorisation..."

GOOGLE_CREDS="$SCRIPT_DIR/.secrets/google_credentials.json"
GOOGLE_TOKEN="$SCRIPT_DIR/.secrets/google_token.json"

if [ -f "$GOOGLE_TOKEN" ]; then
    ok "Google Calendar access is already authorised  (token cached at .secrets/google_token.json)"
elif [ -f "$GOOGLE_CREDS" ]; then
    ok "Google credentials file found at .secrets/google_credentials.json"
    info "A browser sign-in will open the first time you run  bash run.sh"
elif "$GCLOUD_CMD" auth application-default print-access-token > /dev/null 2>&1; then
    ok "Google Calendar access is authorised via Application Default Credentials."
else
    warn "Google Calendar access has not been set up yet."
    echo ""
    info "There are two ways to authorise Google Calendar access:"
    echo ""
    info "  Option A (recommended if you lack a GCP project):"
    info "    Copy the shared  google_credentials.json  file into:"
    info "      .secrets/google_credentials.json"
    info "    A browser sign-in will happen the first time you run the report."
    echo ""
    info "  Option B (requires a personal GCP project with Calendar API enabled):"
    info "    This will open a browser asking you to sign in with your"
    info "    Snowflake Google account (@snowflake.com)."
    echo ""
    printf "     Use Option B (Application Default Credentials)? [y/N] "
    read -r answer
    answer="${answer:-N}"

    if [[ "$answer" =~ ^[Yy]$ ]]; then
        echo ""
        "$GCLOUD_CMD" auth application-default login \
            --scopes="https://www.googleapis.com/auth/calendar.readonly,https://www.googleapis.com/auth/cloud-platform"
        echo ""
        ok "Google Calendar access authorised."
    else
        echo ""
        warn "Skipping Google Calendar authorisation."
        info "Place  google_credentials.json  in the .secrets/ folder and re-run,"
        info "or run this script again and choose Option B."
        echo ""
    fi
fi

# ---------- 4. Virtual environment -------------------------------------------
echo ""
echo "Step 4 of 5: Checking Python virtual environment..."

if [ -f "$VENV_DIR/bin/python" ]; then
    ok "Virtual environment already exists at:  .venv/"
else
    warn "No virtual environment found."
    echo ""
    info "A virtual environment keeps this tool's Python packages isolated"
    info "from the rest of your computer — think of it as a dedicated"
    info "workspace just for this tool."
    echo ""
    printf "     Would you like to create one now? [Y/n] "
    read -r answer
    answer="${answer:-Y}"

    if [[ "$answer" =~ ^[Yy]$ ]]; then
        echo ""
        info "Creating virtual environment..."
        "$PYTHON_CMD" -m venv "$VENV_DIR"
        ok "Virtual environment created at:  .venv/"
    else
        echo ""
        err "Setup cancelled. A virtual environment is required to run this tool."
        info "Run this script again when you are ready."
        echo ""
        exit 1
    fi
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

# ---------- 5. Install Python dependencies -----------------------------------
echo ""
echo "Step 5 of 5: Installing Python packages..."

if [ ! -f "$REQUIREMENTS" ]; then
    err "requirements.txt not found at:  $REQUIREMENTS"
    info "Make sure setup.sh is in the same folder as requirements.txt"
    exit 1
fi

# Upgrade pip silently first (avoids noisy warnings)
"$VENV_PYTHON" -m pip install --upgrade pip --quiet

# Install requirements — idempotent (pip skips already-installed packages)
"$VENV_PIP" install -r "$REQUIREMENTS" --quiet

ok "All packages installed."

# ---------- Done -------------------------------------------------------------
echo ""
echo "=========================================="
ok "Setup complete!"
echo "=========================================="
echo ""
info "To generate last week's activity report, run:"
echo ""
echo "    bash \"$SCRIPT_DIR/run.sh\""
echo ""
info "To report on a specific week, pass the Monday date:"
echo ""
echo "    bash \"$SCRIPT_DIR/run.sh\" --week 2025-01-06"
echo ""
info "Reports are saved to:  output/"
echo ""
