#!/usr/bin/env bash
# =============================================================================
# run.sh — Activity Tracking & Aggregation
#
# Subcommands:
#   (none)                  Weekly activity report (last Mon–Sun)
#   --week 2026-06-15       Report for a specific week
#   --gsheet                Also export a CSV alongside the markdown
#   aggregate               Load all team CSVs from Drive → Snowflake
#   backfill [--weeks N]    Back-fill CSV summaries from Gong takeaways
#   summarize               Generate summaries from local transcript files
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

# ---------- Dispatch subcommand --------------------------------------------------
echo ""
SUBCMD="${1:-}"

if [ "$SUBCMD" = "aggregate" ]; then
    echo "  Aggregating team activity CSVs from Google Drive → Snowflake..."
    echo ""
    shift
    "$VENV_PYTHON" "$SCRIPT_DIR/aggregate_activities.py" "$@"
    echo ""
    ok "Done. Data loaded into TEMP.JLEBONITTE_EDA_ACTIVITY_TRACKING.SE_WEEKLY_ACTIVITIES"

elif [ "$SUBCMD" = "backfill" ]; then
    echo "  Back-filling CSV summaries from Gong takeaways..."
    echo ""
    shift
    "$VENV_PYTHON" "$SCRIPT_DIR/lib/gong_backfill.py" "$@"
    echo ""
    ok "Done. Check output/ CSVs for new summaries."

elif [ "$SUBCMD" = "summarize" ]; then
    echo "  Generating meeting summaries from local transcript files..."
    echo ""
    shift
    "$VENV_PYTHON" "$SCRIPT_DIR/lib/transcript_summaries.py" "$@"
    echo ""
    ok "Done. Check output/ CSVs for new summaries."

else
    echo "  Generating weekly activity report..."
    echo ""
    "$VENV_PYTHON" "$SCRIPT_DIR/weekly_report.py" "$@"
    echo ""
    ok "Done. Check the output/ folder for your report."
fi

echo ""
