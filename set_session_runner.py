#!/usr/bin/env python3
"""
Thai SET — Session Runner
─────────────────────────────────────────────────────────────────
Triggered ONCE at session start, then loops every 15 min internally.
This is far more reliable than depending on GitHub to fire 16+ cron jobs/day.

Usage:
  python set_session_runner.py           # auto-detect session from Bangkok time
  python set_session_runner.py morning   # force morning session
  python set_session_runner.py afternoon # force afternoon session

Sessions (Bangkok time UTC+7):
  Morning   : 10:00 – 12:30  (triggered at 09:55)
  Afternoon : 14:30 – 16:30  (triggered at 14:25)
  EOD Report: 16:35           (run at end of afternoon session)
"""

import subprocess, time, datetime, sys, os

BKK        = datetime.timezone(datetime.timedelta(hours=7))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

MORNING_START   = (10,  0)
MORNING_END     = (12, 30)
AFTERNOON_START = (14, 30)
AFTERNOON_END   = (16, 30)
EOD_TIME        = (16, 35)
SCAN_INTERVAL   = 900   # 15 minutes in seconds
WAIT_INTERVAL   = 60    # 1 minute polling while waiting for session start


def now_bkk():
    return datetime.datetime.now(BKK)


def hm(dt=None):
    t = dt or now_bkk()
    return (t.hour, t.minute)


def run_script(script_name):
    path = os.path.join(SCRIPT_DIR, script_name)
    print(f"  → Running {script_name}")
    result = subprocess.run(["python", path], check=False)
    return result.returncode == 0


def git_save(label):
    """Commit and push portfolio + signal state after each scan."""
    try:
        subprocess.run(["git", "config", "user.name",  "SET Trading Bot"],   check=False, capture_output=True)
        subprocess.run(["git", "config", "user.email", "bot@set-trading.local"], check=False, capture_output=True)
        subprocess.run(["git", "add",
                        "set_portfolio.json",
                        "set_signal_state.json"],
                       check=False, capture_output=True)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff.returncode != 0:   # there are staged changes
            subprocess.run(["git", "commit", "-m", f"{label} [skip ci]"],
                           check=False, capture_output=True)
            subprocess.run(["git", "push"], check=False, capture_output=True)
            print("  → State saved to GitHub")
        else:
            print("  → No state changes to save")
    except Exception as e:
        print(f"  → Git save error: {e}")


def detect_session():
    """Detect which session to run based on current Bangkok time."""
    now = hm()
    if now < AFTERNOON_START:
        return "morning"
    else:
        return "afternoon"


def run_morning():
    print("=" * 50)
    print("  MORNING SESSION")
    print(f"  Active: 10:00 – 12:30 Bangkok")
    print("=" * 50)

    # Wait until 10:00 AM
    while hm() < MORNING_START:
        print(f"  Waiting for market open... {now_bkk().strftime('%H:%M')} Bangkok")
        time.sleep(WAIT_INTERVAL)

    # Scan loop
    while True:
        now = now_bkk()
        current = hm(now)

        if current > MORNING_END:
            print(f"\n[{now.strftime('%H:%M')}] Morning session ended. Exiting.")
            break

        print(f"\n[{now.strftime('%H:%M')}] Running scan...")
        run_script("set_realtime_monitor.py")
        git_save(f"Morning scan {now.strftime('%H%M')}")

        print(f"  Sleeping 15 min until next scan...")
        time.sleep(SCAN_INTERVAL)


def run_afternoon():
    print("=" * 50)
    print("  AFTERNOON SESSION")
    print(f"  Active: 14:30 – 16:30 Bangkok | EOD at 16:35")
    print("=" * 50)

    # Wait until 14:30
    while hm() < AFTERNOON_START:
        print(f"  Waiting for afternoon session... {now_bkk().strftime('%H:%M')} Bangkok")
        time.sleep(WAIT_INTERVAL)

    # Scan loop
    while True:
        now = now_bkk()
        current = hm(now)

        # EOD report time
        if current >= EOD_TIME:
            print(f"\n[{now.strftime('%H:%M')}] Running EOD report...")
            run_script("set_eod_report.py")
            git_save("EOD report")
            print(f"\nAfternoon session complete. Exiting.")
            break

        # Market still open
        if current <= AFTERNOON_END:
            print(f"\n[{now.strftime('%H:%M')}] Running scan...")
            run_script("set_realtime_monitor.py")
            git_save(f"Afternoon scan {now.strftime('%H%M')}")
            print(f"  Sleeping 15 min until next scan...")
            time.sleep(SCAN_INTERVAL)
        else:
            # Between 16:30 and 16:35 — wait for EOD
            print(f"  Waiting for EOD report time... {now.strftime('%H:%M')}")
            time.sleep(30)


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    session = sys.argv[1] if len(sys.argv) > 1 else detect_session()
    print(f"\nSET Session Runner — {now_bkk().strftime('%Y-%m-%d %H:%M')} Bangkok")
    print(f"Session: {session.upper()}\n")

    if session == "morning":
        run_morning()
    elif session == "afternoon":
        run_afternoon()
    else:
        print(f"Unknown session: {session}. Use 'morning' or 'afternoon'.")
        sys.exit(1)
