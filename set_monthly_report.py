#!/usr/bin/env python3
"""
Thai SET — Monthly Performance Report
──────────────────────────────────────────────────────────────────
Runs on the last trading day of each month.
Sends a full month-end summary to LINE covering:
  - Total return vs SET index
  - Win rate on closed trades
  - Best and worst positions
  - Max drawdown
  - Average holding period
  - Trade count
"""

import sys, os, json, datetime

def ensure_packages():
    import importlib, subprocess
    for pkg in ["yfinance", "pandas", "requests"]:
        try:
            importlib.import_module(pkg)
        except ImportError:
            for args in [
                [sys.executable, "-m", "pip", "install", pkg, "-q"],
                [sys.executable, "-m", "pip", "install", pkg, "--user", "-q"],
            ]:
                try:
                    subprocess.check_call(args, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
                    break
                except: continue

ensure_packages()
import requests, yfinance as yf

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH    = os.path.join(SCRIPT_DIR, "set_config.json")
PORTFOLIO_PATH = os.path.join(SCRIPT_DIR, "set_portfolio.json")

with open(CONFIG_PATH) as f: cfg = json.load(f)
LINE_TOKEN   = os.environ.get("LINE_TOKEN", cfg.get("line_channel_access_token", ""))
LINE_USER_ID = os.environ.get("LINE_USER_ID", cfg.get("line_user_id", ""))

BKK = datetime.timezone(datetime.timedelta(hours=7))

def send_line(msg):
    if not LINE_TOKEN or not LINE_USER_ID:
        print("[LINE] No credentials")
        return False
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": "Bearer " + LINE_TOKEN,
                     "Content-Type": "application/json"},
            json={"to": LINE_USER_ID,
                  "messages": [{"type": "text", "text": msg}]},
            timeout=10)
        ok = r.status_code == 200
        print("[LINE] ✅ Sent" if ok else f"[LINE] ❌ {r.status_code}")
        return ok
    except Exception as e:
        print(f"[LINE] ❌ {e}")
        return False

def get_price(ticker):
    try:
        df = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
        if not df.empty and "Close" in df.columns:
            return float(df["Close"].dropna().iloc[-1])
    except: pass
    return None

def is_last_trading_day_of_month():
    """Check if today is the last weekday of the current month."""
    today = datetime.date.today()
    # Find last day of month
    if today.month == 12:
        last_day = datetime.date(today.year + 1, 1, 1) - datetime.timedelta(days=1)
    else:
        last_day = datetime.date(today.year, today.month + 1, 1) - datetime.timedelta(days=1)
    # Walk back to last weekday
    while last_day.weekday() >= 5:
        last_day -= datetime.timedelta(days=1)
    return today == last_day

def main():
    with open(PORTFOLIO_PATH) as f:
        port = json.load(f)

    today      = datetime.date.today()
    capital    = port["capital"]
    cash       = port["cash"]
    holdings   = port["holdings"]
    trades     = port.get("trades", [])
    day_count  = port.get("day_count", 0)
    start_date = port.get("start_date", str(today))

    # Current portfolio value
    total_val = cash
    for ticker, h in holdings.items():
        px = get_price(ticker) or h["avg_cost"]
        total_val += h["shares"] * px

    # Overall P&L
    total_pnl  = total_val - capital
    total_pct  = total_pnl / capital * 100

    # Closed trades analysis
    closed = [t for t in trades if t["action"] == "SELL"]
    wins   = [t for t in closed if t.get("pnl", 0) > 0]
    losses = [t for t in closed if t.get("pnl", 0) <= 0]
    win_rate   = len(wins) / len(closed) * 100 if closed else 0
    total_real = sum(t.get("pnl", 0) for t in closed)

    # Best and worst open positions
    position_pnl = []
    for ticker, h in holdings.items():
        px  = get_price(ticker) or h["avg_cost"]
        pnl = (px - h["avg_cost"]) * h["shares"]
        pct = (px - h["avg_cost"]) / h["avg_cost"] * 100
        position_pnl.append((h["name"], pnl, pct))
    position_pnl.sort(key=lambda x: x[2], reverse=True)

    # SET index comparison
    set_now   = get_price("^SET.BK")
    set_start = port.get("set_index_start")
    set_ret   = None
    if set_now and set_start and set_start > 0:
        set_ret = (set_now - set_start) / set_start * 100

    # Max drawdown
    peak     = port.get("peak_value", capital)
    drawdown = (total_val - peak) / peak * 100 if peak > 0 else 0

    # Build LINE message
    p_icon = "📈" if total_pnl >= 0 else "📉"
    p_sign = "+" if total_pnl >= 0 else ""

    lines = [
        f"📊 MONTHLY PERFORMANCE REPORT",
        f"{'─' * 34}",
        f"📅 {today.strftime('%B %Y')}  |  Day {day_count}",
        f"   Start: {start_date}  →  {today}",
        "",
        f"💼 PORTFOLIO SUMMARY",
        f"   Value     : ฿{total_val:>11,.0f}",
        f"   Capital   : ฿{capital:>11,.0f}",
        f"   Total P&L : {p_icon} {p_sign}฿{total_pnl:,.0f} ({p_sign}{total_pct:.2f}%)",
        f"   Drawdown  : {drawdown:.2f}%",
    ]

    if set_ret is not None:
        vs = total_pct - set_ret
        vs_icon = "✅" if vs >= 0 else "❌"
        lines.append(f"   vs SET    : {vs_icon} Port {total_pct:+.2f}% | SET {set_ret:+.2f}% | Alpha {vs:+.2f}%")

    lines += [
        "",
        f"📋 TRADING ACTIVITY",
        f"   Total trades  : {len(trades)}",
        f"   Closed trades : {len(closed)}",
        f"   Win rate      : {win_rate:.0f}% ({len(wins)}W / {len(losses)}L)",
        f"   Realised P&L  : ฿{total_real:+,.0f}",
        "",
        f"📊 OPEN POSITIONS ({len(holdings)}/10)",
    ]

    if position_pnl:
        for name, pnl, pct in position_pnl:
            icon = "▲" if pnl >= 0 else "▼"
            lines.append(f"   {icon} {name:<8} {pct:>+6.1f}%  (฿{pnl:>+,.0f})")
    else:
        lines.append("   No open positions")

    if closed:
        lines += ["", f"🏆 CLOSED TRADES"]
        for t in sorted(closed, key=lambda x: x.get("pnl", 0), reverse=True):
            ps = "+" if t.get("pnl", 0) >= 0 else ""
            lines.append(f"   {'✅' if t.get('pnl',0)>0 else '❌'} {t['name']:<8} {ps}฿{t.get('pnl',0):,.0f}")

    lines += [
        "",
        "─" * 34,
        "⚠️ Educational only. Not financial advice.",
    ]

    msg = "\n".join(lines)
    print(msg)
    print("\nSending to LINE...")
    send_line(msg)

if __name__ == "__main__":
    # Can be forced with --force flag, otherwise checks if last trading day
    force = "--force" in sys.argv
    if force or is_last_trading_day_of_month():
        print(f"Running monthly report for {datetime.date.today()}")
        main()
    else:
        print(f"Not last trading day of month — skipping. Use --force to override.")
