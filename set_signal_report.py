#!/usr/bin/env python3
"""
set_signal_report.py
────────────────────────────────────────────────────────────────────
On-demand signal snapshot — triggered when you send "Signal" in LINE.
Reads the last saved signal state (no fresh scan needed, instant)
and sends a formatted summary back via LINE push.

Called by the signal-on-demand GitHub Actions workflow.
"""

import sys, os, json, datetime

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "set_config.json")
PORT_PATH   = os.path.join(SCRIPT_DIR, "set_portfolio.json")
STATE_PATH  = os.path.join(SCRIPT_DIR, "set_signal_state.json")

BKK = datetime.timezone(datetime.timedelta(hours=7))
def now_bkk():
    return datetime.datetime.now(BKK)

# ── Config ────────────────────────────────────────────────────────────────────
with open(CONFIG_PATH) as f:
    cfg = json.load(f)

LINE_TOKEN   = os.environ.get("LINE_TOKEN",   cfg.get("line_channel_access_token", ""))
LINE_USER_ID = os.environ.get("LINE_USER_ID", cfg.get("line_user_id", ""))

BUY_SCORE_MIN  = 2
SELL_SCORE_MAX = -2


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
            status = "✅ Sent" if r.status == 200 else f"❌ {r.status}"
        print(f"[LINE] {status}")
    except Exception as e:
        print(f"[LINE] ❌ {e}")


def main():
    now = now_bkk()

    # ── Load data ─────────────────────────────────────────────────────────────
    if not os.path.exists(PORT_PATH):
        send_line("❌ No portfolio file found.")
        sys.exit(1)
    with open(PORT_PATH) as f:
        port = json.load(f)

    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            state = json.load(f)

    holdings  = port.get("holdings", {})
    cash      = port.get("cash", 0)
    capital   = port.get("capital", 300_000)
    day_count = port.get("day_count", 0)
    max_pos   = cfg.get("max_positions", 10)

    invested  = sum(h["shares"] * h.get("last_price", h["avg_cost"])
                    for h in holdings.values())
    total_val = cash + invested
    pnl       = total_val - capital
    pnl_pct   = pnl / capital * 100

    held_tickers = set(holdings.keys())
    regime_info  = state.get("_regime", {})
    regime       = regime_info.get("regime", "UNKNOWN")
    last_updated = regime_info.get("updated", "?")

    # ── Classify signals ──────────────────────────────────────────────────────
    buy_candidates  = []
    sell_candidates = []

    for ticker, s in state.items():
        if ticker.startswith("_"):
            continue
        score = s.get("score", 0)
        comp  = s.get("comp_score", 0.0)
        px    = s.get("price", 0.0)
        name  = s.get("name", ticker.replace(".BK", ""))

        if score >= BUY_SCORE_MIN and ticker not in held_tickers:
            buy_candidates.append((name, score, comp, px))
        elif score <= SELL_SCORE_MAX and ticker in held_tickers:
            sell_candidates.append((name, score, px))

    buy_candidates.sort(key=lambda x: (-x[1], -x[2]))   # score↓, comp↓
    sell_candidates.sort(key=lambda x: x[1])             # score↑ (worst first)

    # ── Format message ────────────────────────────────────────────────────────
    pnl_icon = "📈" if pnl >= 0 else "📉"
    pnl_sign = "+" if pnl >= 0 else ""

    lines = [
        f"📊 Signal Report — {now.strftime('%d %b %Y %H:%M')}",
        f"{'─' * 36}",
        f"📅 Day {day_count}  |  Regime: {regime}",
        f"💼 {len(holdings)}/{max_pos} holdings  ฿{total_val:,.0f}",
        f"{pnl_icon} P&L: {pnl_sign}฿{pnl:,.0f} ({pnl_pct:+.2f}%)",
    ]

    # BUY signals
    if buy_candidates:
        lines.append("")
        lines.append(f"🟢 BUY signals ({len(buy_candidates)}):")
        for name, sc, comp, px in buy_candidates[:5]:
            lines.append(f"   {name:<8s}  score:{sc:+d}  comp:{comp:.1f}  ฿{px:,.2f}")
        if len(buy_candidates) > 5:
            lines.append(f"   … and {len(buy_candidates) - 5} more")
    else:
        lines.append("")
        lines.append("🟢 No BUY signals right now")

    # SELL signals
    if sell_candidates:
        lines.append("")
        lines.append(f"🔴 SELL signals — held ({len(sell_candidates)}):")
        for name, sc, px in sell_candidates:
            lines.append(f"   {name:<8s}  score:{sc:+d}  ฿{px:,.2f}")

    # Holdings
    lines.append("")
    lines.append("📋 Holdings:")
    for ticker, h in holdings.items():
        px   = h.get("last_price", h["avg_cost"])
        cost = h["avg_cost"]
        chg  = (px - cost) / cost * 100 if cost else 0
        icon = "▲" if chg >= 0 else "▼"
        lines.append(f"   {h['name']:<8s}  ฿{px:,.2f}  {icon}{abs(chg):.1f}%")

    lines.append("")
    lines.append(f"⏱ Last scan: {last_updated} BKK")

    send_line("\n".join(lines))
    print("✅ Signal report sent")


if __name__ == "__main__":
    main()
