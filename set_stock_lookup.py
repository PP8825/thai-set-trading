#!/usr/bin/env python3
"""
set_stock_lookup.py  <stock_name>
────────────────────────────────────────────────────────────────────
Real-time stock info lookup. Called with a stock name or ticker.
Fetches live price via yfinance + reads signal state for full picture.
Triggered by GitHub Actions when user types a stock name in LINE.

Usage:
  python set_stock_lookup.py PTT
  python set_stock_lookup.py AMATA
"""

import sys, os, json, datetime

def ensure_packages():
    import importlib, subprocess
    for pkg in ["yfinance", "requests"]:
        try:
            importlib.import_module(pkg)
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

ensure_packages()
import yfinance as yf

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


def find_ticker(query):
    """Match query (name or ticker) to a .BK ticker in signal state."""
    query = query.strip().upper()

    with open(STATE_PATH) as f:
        state = json.load(f)

    # Direct ticker match (with or without .BK)
    if query + ".BK" in state:
        return query + ".BK", state[query + ".BK"]
    if query in state:
        return query, state[query]

    # Name match (case-insensitive)
    for ticker, s in state.items():
        if ticker.startswith("_"):
            continue
        name = s.get("name", "").upper()
        if name == query or ticker.replace(".BK", "") == query:
            return ticker, s

    # Partial name match
    matches = []
    for ticker, s in state.items():
        if ticker.startswith("_"):
            continue
        name = s.get("name", "").upper()
        if query in name or query in ticker:
            matches.append((ticker, s))

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Return best match (shortest name = most specific)
        matches.sort(key=lambda x: len(x[1].get("name", x[0])))
        return matches[0]

    return None, None


def fetch_live_price(ticker):
    """Fetch today's OHLCV and yesterday close for % change."""
    try:
        tk   = yf.Ticker(ticker)
        hist = tk.history(period="5d", interval="1d")
        if hist.empty:
            return None
        last  = hist.iloc[-1]
        prev  = hist.iloc[-2] if len(hist) >= 2 else None
        close = float(last["Close"])
        prev_close = float(prev["Close"]) if prev is not None else close
        chg_pct    = (close - prev_close) / prev_close * 100 if prev_close else 0
        volume     = int(last["Volume"]) if "Volume" in last else 0
        high       = float(last["High"])
        low        = float(last["Low"])
        return {
            "price":      close,
            "prev_close": prev_close,
            "chg_pct":    chg_pct,
            "high":       high,
            "low":        low,
            "volume":     volume,
        }
    except Exception as e:
        print(f"  yfinance error: {e}")
        return None


def signal_label(score):
    if score >= 3:  return "🟢 STRONG BUY"
    if score == 2:  return "🟢 BUY"
    if score == 1:  return "🔵 WATCH+"
    if score == 0:  return "⚪ NEUTRAL"
    if score == -1: return "🟡 WATCH-"
    if score == -2: return "🔴 SELL"
    return "🔴 STRONG SELL"


def main():
    if len(sys.argv) < 2:
        print("Usage: python set_stock_lookup.py <STOCK_NAME>")
        sys.exit(1)

    query = " ".join(sys.argv[1:]).strip()
    now   = now_bkk()

    # Find in signal state
    ticker, s = find_ticker(query)

    if ticker is None:
        send_line(f"❌ '{query}' not found in watchlist.\nTry the exact name e.g. PTT, AMATA, SCB")
        sys.exit(0)

    name  = s.get("name", ticker.replace(".BK", ""))
    score = s.get("score", 0)
    comp  = s.get("comp_score", 0.0)
    fund  = s.get("fund", {}) or {}

    # Fetch live price
    print(f"Fetching live price for {ticker}...")
    live = fetch_live_price(ticker)
    px   = live["price"] if live else s.get("price", 0)

    # Check if held
    with open(PORT_PATH) as f:
        port = json.load(f)
    holdings = port.get("holdings", {})
    held     = holdings.get(ticker)

    # ── Build message ─────────────────────────────────────────────────────────
    chg_str = ""
    if live:
        sign    = "+" if live["chg_pct"] >= 0 else ""
        chg_str = f"  {sign}{live['chg_pct']:.2f}% vs yesterday"

    lines = [
        f"📈 {name}  ({ticker})",
        f"{'─' * 32}",
        f"💵 Price   : ฿{px:,.2f}{chg_str}",
    ]

    if live:
        lines.append(f"📊 H/L     : ฿{live['high']:,.2f} / ฿{live['low']:,.2f}")
        if live["volume"] > 0:
            vol_m = live["volume"] / 1_000_000
            lines.append(f"📦 Volume  : {vol_m:.1f}M shares")

    lines += [
        "",
        f"🎯 Signal  : {signal_label(score)} (score: {score:+d})",
        f"⭐ Composite: {comp:.1f} / 10",
    ]

    # Fundamentals
    pe      = fund.get("pe")
    pbv     = fund.get("pbv")
    roe     = fund.get("roe")
    div_yld = fund.get("div_yld")
    ex_div  = fund.get("ex_div_date", "")

    fund_lines = []
    if pe:   fund_lines.append(f"  P/E      : {pe:.1f}x")
    if pbv:  fund_lines.append(f"  P/BV     : {pbv:.2f}x")
    if roe:  fund_lines.append(f"  ROE      : {roe*100:.1f}%")
    if div_yld and div_yld > 0:
        div_str = f"  Dividend : {div_yld:.1f}%"
        if ex_div:
            div_str += f"  (ex: {ex_div})"
        fund_lines.append(div_str)

    if fund_lines:
        lines.append("")
        lines.append("📋 Fundamentals:")
        lines += fund_lines

    # Position info if held
    if held:
        cost    = held["avg_cost"]
        shares  = held["shares"]
        pnl     = (px - cost) * shares
        pnl_pct = (px - cost) / cost * 100 if cost else 0
        stop    = held.get("atr_stop", cost * 0.92)
        entry   = held.get("entry_date", "?")
        lines += [
            "",
            f"💼 YOUR POSITION:",
            f"  Shares   : {shares:,}",
            f"  Avg cost : ฿{cost:,.2f}",
            f"  P&L      : {'+'if pnl>=0 else ''}฿{pnl:,.0f} ({pnl_pct:+.1f}%)",
            f"  ATR stop : ฿{stop:,.2f}",
            f"  Entry    : {entry}",
        ]

    lines.append("")
    lines.append(f"⏱ {now.strftime('%d %b %Y %H:%M')} BKK")

    send_line("\n".join(lines))
    print(f"✅ Stock lookup sent for {name}")


if __name__ == "__main__":
    main()
