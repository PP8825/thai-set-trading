#!/usr/bin/env python3
"""
set_watchlist_manage.py  <add|remove>  <TICKER>
────────────────────────────────────────────────────────────────────
Adds or removes a stock from set_watchlist.json.
Commits the change back to GitHub so it persists.
Triggered by GitHub Actions when user types "add TICKER" or "remove TICKER".
"""

import sys, os, json, datetime, subprocess

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH    = os.path.join(SCRIPT_DIR, "set_config.json")
STATE_PATH     = os.path.join(SCRIPT_DIR, "set_signal_state.json")
WATCHLIST_PATH = os.path.join(SCRIPT_DIR, "set_watchlist.json")

BKK = datetime.timezone(datetime.timedelta(hours=7))
def now_bkk():
    return datetime.datetime.now(BKK)

with open(CONFIG_PATH) as f:
    cfg = json.load(f)

LINE_TOKEN   = os.environ.get("LINE_TOKEN",   cfg.get("line_channel_access_token", ""))
LINE_USER_ID = os.environ.get("LINE_USER_ID", cfg.get("line_user_id", ""))


def send_line(msg):
    try:
        import urllib.request
        body = json.dumps({
            "to": LINE_USER_ID,
            "messages": [{"type": "text", "text": msg}]
        }).encode()
        req = urllib.request.Request(
            "https://api.line.me/v2/bot/message/push",
            data=body,
            headers={"Authorization": f"Bearer {LINE_TOKEN}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[LINE] {'✅ Sent' if r.status == 200 else f'❌ {r.status}'}")
    except Exception as e:
        print(f"[LINE] ❌ {e}")


def resolve_name(query):
    """Return the display name for a ticker if found in signal state."""
    if not os.path.exists(STATE_PATH):
        return query.upper(), None
    with open(STATE_PATH) as f:
        state = json.load(f)
    q = query.upper()
    if q + ".BK" in state:
        return state[q + ".BK"].get("name", q), q + ".BK"
    if q in state:
        return state[q].get("name", q), q
    for t, s in state.items():
        if t.startswith("_"): continue
        if s.get("name", "").upper() == q:
            return s.get("name", q), t
    return q, None


def git_commit(msg):
    try:
        subprocess.run(["git", "config", "user.name",  "SET Trading Bot"], check=False, capture_output=True)
        subprocess.run(["git", "config", "user.email", "bot@set-trading.local"], check=False, capture_output=True)
        subprocess.run(["git", "add", "set_watchlist.json"], check=False, capture_output=True)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", f"{msg} [skip ci]"], check=False, capture_output=True)
            subprocess.run(["git", "push"], check=False, capture_output=True)
            print("  → Watchlist saved to GitHub")
    except Exception as e:
        print(f"  → Git error: {e}")


def main():
    if len(sys.argv) < 3:
        print("Usage: python set_watchlist_manage.py <add|remove> <TICKER>")
        sys.exit(1)

    action = sys.argv[1].lower()
    query  = sys.argv[2].strip()
    now    = now_bkk()

    # Load or create watchlist
    if os.path.exists(WATCHLIST_PATH):
        with open(WATCHLIST_PATH) as f:
            wl = json.load(f)
    else:
        wl = {"stocks": [], "updated": ""}

    stocks = wl.get("stocks", [])
    stocks_upper = [s.upper() for s in stocks]

    # Resolve display name
    display_name, found_ticker = resolve_name(query)

    if action == "add":
        if display_name.upper() in stocks_upper:
            send_line(f"ℹ️ {display_name} is already in your watchlist.")
            sys.exit(0)
        if found_ticker is None:
            send_line(f"❌ '{query}' not found in the instrument list.\nCheck the ticker name and try again.")
            sys.exit(0)
        stocks.append(display_name)
        wl["stocks"]  = stocks
        wl["updated"] = now.isoformat()
        with open(WATCHLIST_PATH, "w") as f:
            json.dump(wl, f, indent=2, ensure_ascii=False)
        git_commit(f"watchlist: add {display_name}")
        send_line(f"✅ Added {display_name} to your watchlist.\n\nYou now have {len(stocks)} stock(s).\nTap Watchlist to view all.")

    elif action == "remove":
        # Case-insensitive remove
        new_stocks = [s for s in stocks if s.upper() != display_name.upper()]
        if len(new_stocks) == len(stocks):
            send_line(f"ℹ️ {display_name} is not in your watchlist.")
            sys.exit(0)
        wl["stocks"]  = new_stocks
        wl["updated"] = now.isoformat()
        with open(WATCHLIST_PATH, "w") as f:
            json.dump(wl, f, indent=2, ensure_ascii=False)
        git_commit(f"watchlist: remove {display_name}")
        send_line(f"✅ Removed {display_name} from your watchlist.\n\n{len(new_stocks)} stock(s) remaining.")

    else:
        send_line(f"❌ Unknown action '{action}'. Use 'add' or 'remove'.")


if __name__ == "__main__":
    main()
