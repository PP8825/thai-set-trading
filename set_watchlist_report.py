#!/usr/bin/env python3
"""
set_watchlist_report.py
────────────────────────────────────────────────────────────────────
Shows current signal status for all stocks in the personal watchlist.
Triggered when user taps "Watchlist" in LINE rich menu.
"""

import sys, os, json, datetime

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH    = os.path.join(SCRIPT_DIR, "set_config.json")
STATE_PATH     = os.path.join(SCRIPT_DIR, "set_signal_state.json")
PORT_PATH      = os.path.join(SCRIPT_DIR, "set_portfolio.json")
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


def signal_icon(score):
    if score >= 2:  return "🟢"
    if score == 1:  return "🔵"
    if score == 0:  return "⚪"
    if score == -1: return "🟡"
    return "🔴"


def main():
    now = now_bkk()

    # Load watchlist
    if not os.path.exists(WATCHLIST_PATH):
        send_line("📋 Your watchlist is empty.\n\nType 'add TICKER' to add a stock.\nExample: add PTT")
        sys.exit(0)

    with open(WATCHLIST_PATH) as f:
        wl = json.load(f)

    stocks = wl.get("stocks", [])
    if not stocks:
        send_line("📋 Your watchlist is empty.\n\nType 'add TICKER' to add a stock.\nExample: add PTT")
        sys.exit(0)

    # Load signal state and portfolio
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except Exception as e:
        send_line(f"❌ Could not load signal state: {e}")
        sys.exit(1)
    try:
        with open(PORT_PATH) as f:
            port = json.load(f)
    except Exception as e:
        port = {"holdings": {}}

    held_tickers = set(port.get("holdings", {}).keys())
    regime_info  = state.get("_regime", {})
    last_scan    = regime_info.get("updated", "?")

    lines = [
        f"📋 Watchlist — {now.strftime('%d %b %Y %H:%M')}",
        f"{'─' * 36}",
        f"{len(stocks)} stocks  |  Last scan: {last_scan} BKK",
        "",
    ]

    found    = []
    notfound = []

    for name in stocks:
        # Find ticker in state
        name_up = name.upper()
        ticker  = None
        entry   = None

        # Try direct match
        if name_up + ".BK" in state:
            ticker = name_up + ".BK"
            entry  = state[ticker]
        elif name_up in state:
            ticker = name_up
            entry  = state[ticker]
        else:
            # Name match
            for t, s in state.items():
                if t.startswith("_"): continue
                if s.get("name", "").upper() == name_up:
                    ticker = t
                    entry  = s
                    break

        if entry is None:
            # Show placeholder — stock is in watchlist but not yet scanned
            lines.append(f"⬜ {name:<8s}  (pending next scan)")
            continue

        score   = entry.get("score", 0)
        comp    = entry.get("comp_score", 0.0)
        px      = entry.get("price", 0)
        disp    = entry.get("name", name)
        held    = ticker in held_tickers
        icon    = signal_icon(score)
        status  = " ✅held" if held else ""
        fund    = entry.get("fund", {}) or {}
        div_yld = fund.get("div_yld", 0) or 0

        div_str = f"  div:{div_yld:.1f}%" if div_yld > 0 else ""
        lines.append(
            f"{icon} {disp:<8s}  ฿{px:,.2f}  score:{score:+d}  comp:{comp:.1f}{div_str}{status}"
        )
        found.append(name)

    lines += [
        "",
        "─" * 36,
        "Type 'add TICKER' or 'remove TICKER'",
        "to update your watchlist.",
    ]

    send_line("\n".join(lines))
    print(f"✅ Watchlist report sent ({len(found)} stocks)")


if __name__ == "__main__":
    main()
