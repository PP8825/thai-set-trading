#!/usr/bin/env bash
# ─── SET Monitor — launchd wrapper ────────────────────────────────────────────
# This script is called by macOS launchd every 30 minutes.
# It sources credentials from set_env.sh so they never appear in the plist.
#
# Usage (manual test):
#   bash set_run.sh
# ──────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load credentials (LINE_TOKEN, LINE_USER_ID, etc.)
if [ -f "$SCRIPT_DIR/set_env.sh" ]; then
    source "$SCRIPT_DIR/set_env.sh"
else
    echo "⚠  set_env.sh not found at $SCRIPT_DIR — credentials may be missing"
fi

# Run the monitor
/usr/bin/python3 "$SCRIPT_DIR/set_realtime_monitor.py" "$@"
