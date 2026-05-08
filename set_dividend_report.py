#!/usr/bin/env python3
"""
set_dividend_report.py
────────────────────────────────────────────────────────────────────
On-demand top dividend stocks report.
Reads last signal state, ranks by dividend yield, sends LINE message.
Triggered by GitHub Actions when user taps "Dividend" in LINE rich menu.
"""

import sys, os, json, datetime

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "set_config.json")
STATE_PATH  = os.path.join(SCRIPT_DIR, "set_signal_state.json")
PORT_PATH   = os.path.join(SCRIPT_DIR, "set_portfolio.json")

BKK = datetime.timezone(datetime.timedelta(hours=7))
def now_bkk():
    return datetime.datetime.now(BKK)

with open(CONFIG_PATH) as f:
    cfg = json.load(f)

LINE_TOKEN   = os.environ.get("LINE_TOKEN",   cfg.get("line_channel_access_token", ""))
LINE_USER_ID = os.environ.get("USER_ID") or os.environ.get("LINE_USER_ID") or cfg.get("line_user_id", "")

MIN_DIV_YIELD = 3.0   # only show stocks with yield >= 3%
TOP_N         = 10    # max stocks to show


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


def main():
    now = now_bkk()

    if not os.path.exists(STATE_PATH):
        send_line("❌ No signal state found. Run a scan first.")
        sys.exit(1)

    with open(STATE_PATH) as f:
        state = json.load(f)

    with open(PORT_PATH) as f:
        port = json.load(f)

    held_tickers = set(port.get("holdings", {}).keys())
    regime_info  = state.get("_regime", {})
    last_updated = regime_info.get("updated", "?")

    # Collect dividend data
    candidates = []
    for ticker, s in state.items():
        if ticker.startswith("_"):
            continue
        fund    = s.get("fund", {}) or {}
        div_yld = fund.get("div_yld", 0) or 0
        if div_yld < MIN_DIV_YIELD:
            continue
        name    = s.get("name", ticker.replace(".BK", ""))
        px      = s.get("price", 0)
        score   = s.get("score", 0)
        comp    = s.get("comp_score", 0)
        held    = ticker in held_tickers
        ex_div  = fund.get("ex_div_date", "")
        candidates.append((name, ticker, div_yld, px, score, comp, held, ex_div))

    # Sort by yield descending
    candidates.sort(key=lambda x: -x[2])

    lines = [
        f"💰 Top Dividend Stocks — {now.strftime('%d %b %Y %H:%M')}",
        f"{'─' * 36}",
        f"Yield ≥ {MIN_DIV_YIELD}% | {len(candidates)} stocks found",
        f"⏱ Last scan: {last_updated} BKK",
        "",
    ]

    if not candidates:
        lines.append("No dividend stocks above threshold found.")
    else:
        lines.append(f"{'Stock':<8s}  {'Yield':>6s}  {'Price':>8s}  {'Score':>6s}  Status")
        lines.append("─" * 48)
        for name, ticker, yld, px, score, comp, held, ex_div in candidates[:TOP_N]:
            status = "✅ HELD" if held else ("🟢 BUY" if score >= 2 else "⚪ WATCH")
            sc_str = f"{score:+d}"
            lines.append(f"{name:<8s}  {yld:>5.1f}%  ฿{px:>7,.2f}  {sc_str:>5s}  {status}")
            if ex_div:
                lines.append(f"         ex-div: {ex_div}")

    lines.append("")
    lines.append("🟢=BUY signal  ✅=already held  ⚪=watching")

    send_line("\n".join(lines))
    print("✅ Dividend report sent")


if __name__ == "__main__":
    main()
