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

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH  = os.path.join(SCRIPT_DIR, "set_config.json")
STATE_PATH   = os.path.join(SCRIPT_DIR, "set_signal_state.json")
PORT_PATH    = os.path.join(SCRIPT_DIR, "set_portfolio.json")
FUND_CACHE   = os.path.join(SCRIPT_DIR, "set_fundamental_cache.json")

BKK = datetime.timezone(datetime.timedelta(hours=7))
def now_bkk():
    return datetime.datetime.now(BKK)

with open(CONFIG_PATH) as f:
    cfg = json.load(f)

LINE_TOKEN   = os.environ.get("LINE_TOKEN",   cfg.get("line_channel_access_token", ""))
LINE_USER_ID = os.environ.get("USER_ID") or os.environ.get("LINE_USER_ID") or cfg.get("line_user_id", "")


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

    # Fallback: check instruments list in set_config.json
    for inst in cfg.get("instruments", []):
        ticker = inst.get("ticker", "") if isinstance(inst, dict) else inst
        name   = inst.get("name",   ticker) if isinstance(inst, dict) else inst
        if ticker.upper() == query or ticker.upper() == query + ".BK" or name.upper() == query:
            # Return a minimal stub so live price fetch can still work
            return ticker, {"name": name, "score": 0, "comp_score": 0.0,
                            "signal": "—", "fund": {}, "_stub": True}

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
        send_line(f"❌ '{query}' not found.\nTry the ticker e.g. PTT, KBANK, ADVANC")
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

    # ── Fundamentals (current) ────────────────────────────────────────────────
    pe      = fund.get("pe")
    pbv     = fund.get("pbv")
    roe     = fund.get("roe")
    div_yld = fund.get("div_yld")
    ex_div  = fund.get("ex_div_date", "")
    de      = fund.get("de_ratio")
    eps_g   = fund.get("eps_growth")
    fcf_y   = fund.get("fcf_yield")

    lines.append("")
    lines.append("📊 Financials")
    lines.append("─" * 32)

    def fmt(label, val, fmt_str, suffix=""):
        if val is None: return None
        try:    return f"  {label:<12s}: {fmt_str.format(val)}{suffix}"
        except: return None

    for row in [
        fmt("P/E",       pe,      "{:.1f}", "x"),
        fmt("P/BV",      pbv,     "{:.2f}", "x"),
        fmt("ROE",       (roe or 0)*100 if roe else None, "{:.1f}", "%"),
        fmt("D/E",       de,      "{:.2f}", "x"),
        fmt("EPS growth",(eps_g or 0)*100 if eps_g else None, "{:+.1f}", "%"),
        fmt("FCF yield", (fcf_y or 0)*100 if fcf_y else None, "{:+.1f}", "%"),
        fmt("Div yield", div_yld, "{:.1f}", "%") if div_yld and div_yld > 0 else None,
    ]:
        if row: lines.append(row)

    if ex_div:
        lines.append(f"  {'Ex-div':<12s}: {ex_div}")

    # ── Historical financials (last 3 years) ──────────────────────────────────
    if os.path.exists(FUND_CACHE):
        try:
            with open(FUND_CACHE) as f:
                fc = json.load(f)
            entry  = fc.get(ticker, {})
            yearly = entry.get("yearly", {})
            all_years = sorted(yearly.keys())

            # Split into full years (have EPS/ROE) and valuation-only years
            full_years = [y for y in all_years
                          if yearly[y].get("eps") is not None
                          or yearly[y].get("roe") is not None]
            val_years  = [y for y in all_years if y not in full_years]

            # Show last 3 full years as table
            show_years = full_years[-3:]
            if show_years:
                lines.append("")
                lines.append("📅 3-Year History")
                lines.append("─" * 32)
                lines.append(f"  {'Year':<6s} {'EPS':>6s} {'ROE':>7s} {'Marg':>7s} {'D/E':>6s}")
                for yr in show_years:
                    d      = yearly[yr]
                    eps    = d.get("eps")
                    roe_   = d.get("roe")
                    marg   = d.get("net_margin")
                    de_    = d.get("de_ratio")
                    eps_s  = f"{eps:5.2f}"       if eps  is not None else "   n/a"
                    roe_s  = f"{roe_:5.1f}%"     if roe_ is not None else "   n/a"
                    marg_s = f"{marg:5.1f}%"     if marg is not None else "   n/a"
                    de_s   = f"{de_:5.2f}x"      if de_  is not None else "   n/a"
                    lines.append(f"  {yr:<6s} {eps_s:>6s} {roe_s:>7s} {marg_s:>7s} {de_s:>6s}")

            # Show valuation-only years (e.g. latest year partial data)
            for yr in val_years[-1:]:
                d   = yearly[yr]
                pe_ = d.get("pe")
                pb_ = d.get("pbv")
                dy_ = d.get("div_yield")
                parts = []
                if pe_: parts.append(f"P/E {pe_:.1f}x")
                if pb_: parts.append(f"P/BV {pb_:.2f}x")
                if dy_: parts.append(f"Div {dy_*100:.1f}%")
                if parts:
                    lines.append("")
                    lines.append(f"  {yr} (partial): {' · '.join(parts)}")

        except Exception as e:
            print(f"  Fund cache error: {e}")

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
