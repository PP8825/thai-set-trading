#!/usr/bin/env bash
# ─── SET Monitor — launchd install / uninstall ────────────────────────────────
#
# INSTALL (run once):
#   bash set_schedule_install.sh install
#
# UNINSTALL:
#   bash set_schedule_install.sh uninstall
#
# STATUS:
#   bash set_schedule_install.sh status
#
# VIEW LOGS:
#   bash set_schedule_install.sh logs
# ──────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC="$SCRIPT_DIR/com.set.monitor.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.set.monitor.plist"
LABEL="com.set.monitor"

case "${1:-help}" in

  install)
    echo "Installing SET monitor schedule..."

    # Make run script executable
    chmod +x "$SCRIPT_DIR/set_run.sh"

    # Copy plist to LaunchAgents
    cp "$PLIST_SRC" "$PLIST_DST"
    echo "  ✅ Plist copied → $PLIST_DST"

    # Load the job
    launchctl load "$PLIST_DST"
    echo "  ✅ launchd job loaded"

    echo ""
    echo "Monitor will run every 30 minutes."
    echo "It exits immediately when market is closed (weekends, holidays, off-hours)."
    echo ""
    echo "Logs:  /tmp/set_monitor.log"
    echo "       /tmp/set_monitor_err.log"
    echo ""
    echo "To check status:  bash set_schedule_install.sh status"
    ;;

  uninstall)
    echo "Removing SET monitor schedule..."
    launchctl unload "$PLIST_DST" 2>/dev/null && echo "  ✅ Job unloaded"
    rm -f "$PLIST_DST" && echo "  ✅ Plist removed"
    echo "Done."
    ;;

  status)
    echo "── launchctl status ──────────────────────"
    launchctl list | grep "$LABEL" || echo "  Job not loaded."
    echo ""
    echo "── Last log (stdout) ─────────────────────"
    tail -40 /tmp/set_monitor.log 2>/dev/null || echo "  No log yet."
    echo ""
    echo "── Last errors ───────────────────────────"
    tail -20 /tmp/set_monitor_err.log 2>/dev/null || echo "  No errors."
    ;;

  logs)
    tail -f /tmp/set_monitor.log
    ;;

  *)
    echo "Usage: bash set_schedule_install.sh [install|uninstall|status|logs]"
    ;;

esac
