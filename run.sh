#!/usr/bin/env bash
# =============================================================================
# run.sh — Run the Weekly Activity Report
#
# This script generates a Markdown report of last week's customer meetings,
# enriched with call summaries and Salesforce use cases.
#
# Usage:
#   bash run.sh                       # report for last Mon–Sun week
#   bash run.sh --week 2025-01-06     # report for the week starting Jan 6 2025
#
# Output is saved to:  output/week_YYYY-MM-DD.md
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓  $*${NC}"; }
warn() { echo -e "${YELLOW}  !  $*${NC}"; }
err()  { echo -e "${RED}  ✗  $*${NC}"; }
info() { echo -e "     $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

# ---------- Check setup has been run -----------------------------------------
if [ ! -f "$VENV_PYTHON" ]; then
    err "Setup has not been completed yet."
    echo ""
    info "Please run setup first:"
    echo ""
    echo "    bash \"$SCRIPT_DIR/setup.sh\""
    echo ""
    exit 1
fi

# ---------- Check credentials exist ------------------------------------------
SECRETS_DIR="$SCRIPT_DIR/.secrets"
SNOWHOUSE_ENV="$SECRETS_DIR/snowhouse.env"
GCAL_CREDS="$SECRETS_DIR/google_credentials.json"

if [ ! -f "$SNOWHOUSE_ENV" ]; then
    err "Snowflake credentials not found."
    echo ""
    info "Expected file:  .secrets/snowhouse.env"
    info ""
    info "Copy the example file and fill in your details:"
    echo ""
    echo "    cp \"$SCRIPT_DIR/.secrets.example\" \"$SNOWHOUSE_ENV\""
    echo "    open \"$SNOWHOUSE_ENV\""
    echo ""
    exit 1
fi

if [ ! -f "$GCAL_CREDS" ]; then
    err "Google Calendar credentials not found."
    echo ""
    info "Expected file:  .secrets/google_credentials.json"
    info ""
    info "To get this file:"
    info "  1. Go to https://console.cloud.google.com/"
    info "  2. Select your project → APIs & Services → Credentials"
    info "  3. Click the OAuth 2.0 Client ID for this tool → Download JSON"
    info "  4. Save the file as:  .secrets/google_credentials.json"
    echo ""
    exit 1
fi

# ---------- Run the report ---------------------------------------------------
echo ""
echo "  Generating weekly activity report..."
echo ""

"$VENV_PYTHON" "$SCRIPT_DIR/weekly_report.py" "$@"

echo ""
ok "Done. Check the output/ folder for your report."
echo ""
