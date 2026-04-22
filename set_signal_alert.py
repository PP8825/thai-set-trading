#!/usr/bin/env python3
"""
Thai SET100 Signal Alert + Live Portfolio Manager
─────────────────────────────────────────────────
• Screens all SET100 stocks with RSI / SMA50 / MACD
• Manages a live ฿300,000 paper portfolio
  - Day 1  : buys top 10 BUY signals, equal-weight
  - Daily  : sells on SELL signal or -8% stop-loss
              buys new BUY signals with free cash
• Sends a detailed LINE message every run showing:
  - Portfolio value, cash, P&L
  - Today's trades (stock, shares, price, value)
  - All current holdings with unrealised P&L
  - Top 3 BUY + Top 3 SELL signals with dividends
"""

import sys, os, json, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── Auto-install ──────────────────────────────────────────────────────────────
def ensure_packages():
    import importlib, subprocess
    for pkg in ["yfinance", "pandas", "requests"]:
        try:
            importlib.import_module(pkg)
        except ImportError:
            print(f"Installing {pkg}...")
            for args in [
                [sys.executable, "-m", "pip", "install", pkg, "-q"],
                [sys.executable, "-m", "pip", "install", pkg, "--user", "-q"],
                [sys.executable, "-m", "pip", "install", pkg,
                 "--break-system-packages", "-q"],
            ]:
                try:
                    subprocess.check_call(
                        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break
                except subprocess.CalledProcessError:
                    continue

ensure_packages()

import requests
import pandas as pd
import yfinance as yf

# ─── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH    = os.path.join(SCRIPT_DIR, "set_config.json")
PORTFOLIO_PATH = os.path.join(SCRIPT_DIR, "set_portfolio.json")

# ─── Config ────────────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"ERROR: Config not found at {CONFIG_PATH}"); sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

cfg           = load_config()
LINE_TOKEN    = cfg.get("line_channel_access_token", "")
LINE_USER_ID  = cfg.get("line_user_id", "")
INSTRUMENTS   = [(i["name"], i["ticker"]) for i in cfg.get("instruments", [])]
RSI_PERIOD    = cfg.get("rsi_period", 14)
RSI_OB        = cfg.get("rsi_overbought", 70)
RSI_OS        = cfg.get("rsi_oversold", 30)
SMA_PERIOD    = cfg.get("sma_period", 50)
LOOKBACK      = cfg.get("lookback_days", 300)
TOP_N         = cfg.get("top_n_results", 5)
MAX_WORKERS   = cfg.get("download_threads", 10)

# ─── Portfolio constants ────────────────────────────────────────────────────────
INITIAL_CAPITAL = 300_000.0   # ฿
LOT_SIZE        = 100          # Thai market standard lot
TX_COST         = 0.0025       # 0.25% commission per side
STOP_LOSS_PCT   = 0.08         # Exit when price falls 8% below avg cost
MAX_POSITIONS   = 10           # Maximum simultaneous holdings
CASH_FLOOR_PCT  = 0.05         # Always keep 5% as cash buffer

# ─── Portfolio persistence ─────────────────────────────────────────────────────
def load_portfolio() -> dict:
    if os.path.exists(PORTFOLIO_PATH):
        with open(PORTFOLIO_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "capital":    INITIAL_CAPITAL,
        "cash":       INITIAL_CAPITAL,
        "holdings":   {},   # {ticker: {name, shares, avg_cost, entry_date, entry_score}}
        "trades":     [],   # full history
        "start_date": datetime.date.today().isoformat(),
        "day_count":  0,
        "peak_value": INITIAL_CAPITAL,
    }

def save_portfolio(port: dict):
    with open(PORTFOLIO_PATH, "w", encoding="utf-8") as f:
        json.dump(port, f, indent=2, default=str, ensure_ascii=False)

def portfolio_value(port: dict, prices: dict) -> float:
    val = port["cash"]
    for ticker, h in port["holdings"].items():
        px = prices.get(ticker, h.get("avg_cost", 0))
        val += h["shares"] * px
    return val

# ─── Trade execution ───────────────────────────────────────────────────────────
def execute_daily_trades(port: dict, results: list, prices: dict) -> list:
    """
    Decide BUY / SELL actions for today, update portfolio state in-place.
    Returns list of trade dicts for today only.
    """
    today        = datetime.date.today().isoformat()
    today_trades = []
    cash_floor   = INITIAL_CAPITAL * CASH_FLOOR_PCT
    is_day_one   = (port["day_count"] == 0 and len(port["holdings"]) == 0)

    # ── SELL pass ──────────────────────────────────────────────────────────────
    if not is_day_one:
        sig_map = {r["ticker"]: r for r in results if not r.get("error")}
        for ticker in list(port["holdings"].keys()):
            h  = port["holdings"][ticker]
            px = prices.get(ticker)
            if not px or px <= 0:
                continue

            sig          = sig_map.get(ticker)
            sell_signal  = sig and sig["score"] < 0
            hit_stoploss = px <= h["avg_cost"] * (1 - STOP_LOSS_PCT)

            if sell_signal or hit_stoploss:
                proceeds   = h["shares"] * px * (1 - TX_COST)
                cost_basis = h["shares"] * h["avg_cost"] * (1 + TX_COST)
                pnl        = proceeds - cost_basis
                reason     = ("Stop-loss" if hit_stoploss
                              else sig.get("overall", "SELL signal"))
                port["cash"] += proceeds

                today_trades.append({
                    "date": today, "action": "SELL",
                    "ticker": ticker, "name": h["name"],
                    "shares": h["shares"], "price": round(px, 2),
                    "value":  round(h["shares"] * px, 2),
                    "avg_cost": round(h["avg_cost"], 2),
                    "pnl":   round(pnl, 2), "reason": reason,
                })
                del port["holdings"][ticker]

    # ── BUY pass ───────────────────────────────────────────────────────────────
    ok_buys     = sorted(
        [r for r in results if not r.get("error") and r["score"] > 0],
        key=lambda x: (-x["score"], x["rsi"])        # strong signal + oversold first
    )
    slots_free  = MAX_POSITIONS - len(port["holdings"])
    avail_cash  = port["cash"] - cash_floor

    if is_day_one:
        candidates = ok_buys[:10]            # First run: buy top-10
        n_buy      = len(candidates)
        alloc      = avail_cash / n_buy if n_buy else 0
    else:
        existing   = set(port["holdings"].keys())
        candidates = [r for r in ok_buys if r["ticker"] not in existing][:min(3, slots_free)]
        n_buy      = len(candidates)
        # Equal slice of available cash, capped at 1/MAX_POSITIONS of total capital
        alloc      = min(avail_cash / n_buy if n_buy else 0,
                         INITIAL_CAPITAL / MAX_POSITIONS * 1.5)

    for r in candidates:
        if avail_cash < alloc * 0.5:    # Not enough cash left
            break
        ticker = r["ticker"]
        px     = prices.get(ticker)
        if not px or px <= 0:
            continue

        shares = int(alloc / px / LOT_SIZE) * LOT_SIZE
        if shares <= 0:
            shares = LOT_SIZE           # Try a single lot
        cost = shares * px * (1 + TX_COST)
        if cost > avail_cash:
            shares = int((avail_cash / (px * (1 + TX_COST))) / LOT_SIZE) * LOT_SIZE
            cost   = shares * px * (1 + TX_COST)
        if shares <= 0 or cost > avail_cash:
            continue

        port["cash"] -= cost
        avail_cash   -= cost
        port["holdings"][ticker] = {
            "name":        r["name"],
            "shares":      shares,
            "avg_cost":    round(px, 2),
            "entry_date":  today,
            "entry_score": r["score"],
        }
        today_trades.append({
            "date": today, "action": "BUY",
            "ticker": ticker, "name": r["name"],
            "shares": shares, "price": round(px, 2),
            "value":  round(shares * px, 2),
            "avg_cost": round(px, 2), "pnl": 0,
            "reason": r["overall"],
        })

    # ── Update portfolio metadata ───────────────────────────────────────────────
    port["trades"].extend(today_trades)
    port["day_count"] = port.get("day_count", 0) + 1
    total_val = portfolio_value(port, prices)
    if total_val > port.get("peak_value", INITIAL_CAPITAL):
        port["peak_value"] = total_val

    return today_trades

# ─── Technical indicators ──────────────────────────────────────────────────────
def calc_rsi(s: pd.Series, n: int = 14) -> float:
    d  = s.diff()
    ag = d.clip(lower=0).ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    al = (-d).clip(lower=0).ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    rs = ag / al.replace(0, 1e-10)
    return float((100 - 100 / (1 + rs)).iloc[-1])

def calc_sma(s: pd.Series, n: int) -> float:
    return float(s.rolling(n).mean().iloc[-1])

def calc_macd_signal(s: pd.Series, fast=12, slow=26, sig=9):
    ml = s.ewm(span=fast, adjust=False).mean() - s.ewm(span=slow, adjust=False).mean()
    sl = ml.ewm(span=sig, adjust=False).mean()
    return float(ml.iloc[-1]), float(sl.iloc[-1]), float(ml.iloc[-2]), float(sl.iloc[-2])

def signals(rsi, close, sma, macd, msig):
    rs = "BUY" if rsi < RSI_OS else ("SELL" if rsi > RSI_OB else "HOLD")
    ms = "BUY" if close > sma  else "SELL"
    mc = "BUY" if macd  > msig else "SELL"
    sc = ((1 if rs == "BUY" else -1 if rs == "SELL" else 0) +
          (1 if ms == "BUY" else -1) +
          (1 if mc == "BUY" else -1))
    sc = max(-3, min(3, sc))
    ov = {3: "STRONG BUY", 2: "BUY", 1: "BUY",
          0: "HOLD", -1: "SELL", -2: "SELL", -3: "STRONG SELL"}[sc]
    return rs, ms, mc, ov, sc

# ─── Data fetch (parallel) ─────────────────────────────────────────────────────
def analyze(name: str, ticker: str) -> dict:
    try:
        df = yf.download(ticker, period=f"{LOOKBACK}d",
                         auto_adjust=True, progress=False, timeout=25)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df is None or df.empty or "Close" not in df.columns:
            return {"name": name, "ticker": ticker, "error": "No data"}

        c = df["Close"].dropna()
        if len(c) < max(SMA_PERIOD + 5, 60):
            return {"name": name, "ticker": ticker,
                    "error": f"Only {len(c)} rows"}

        rsi_v                    = calc_rsi(c, RSI_PERIOD)
        sma_v                    = calc_sma(c, SMA_PERIOD)
        macd_v, sig_v, pm, ps    = calc_macd_signal(c)
        last, prev               = float(c.iloc[-1]), float(c.iloc[-2])
        pct                      = (last - prev) / prev * 100
        cross_up                 = pm <= ps and macd_v > sig_v
        cross_dn                 = pm >= ps and macd_v < sig_v
        rs, ms, mc, ov, sc       = signals(rsi_v, last, sma_v, macd_v, sig_v)

        return {"name": name, "ticker": ticker, "error": None,
                "close": last, "pct": pct, "rsi": rsi_v, "sma": sma_v,
                "macd": macd_v, "msig": sig_v,
                "cross_up": cross_up, "cross_dn": cross_dn,
                "rsi_sig": rs, "ma_sig": ms, "macd_sig": mc,
                "overall": ov, "score": sc,
                "div_yield": None, "last_div": None, "last_div_date": None}
    except Exception as e:
        return {"name": name, "ticker": ticker, "error": str(e)[:60]}

def fetch_dividends(r: dict):
    try:
        t    = yf.Ticker(r["ticker"])
        divs = t.dividends
        if divs is None or divs.empty:
            return
        cutoff     = pd.Timestamp.now(tz=divs.index.tz) - pd.DateOffset(years=1)
        annual_div = float(divs[divs.index >= cutoff].sum())
        r["div_yield"]     = annual_div / r["close"] * 100
        r["last_div"]      = float(divs.iloc[-1])
        r["last_div_date"] = divs.index[-1].strftime("%b %Y")
    except Exception:
        pass

# ─── Message builders ──────────────────────────────────────────────────────────
BADGE = {"STRONG BUY": "🟢🟢", "BUY": "🟢", "HOLD": "🟡",
         "SELL": "🔴", "STRONG SELL": "🔴🔴"}
SE    = {"BUY": "▲", "SELL": "▼", "HOLD": "─"}

def session_label() -> str:
    return "🌅 Morning" if datetime.datetime.now().hour < 13 else "🌆 Afternoon"

def fmt_stock_line(rank: int, r: dict) -> str:
    chg   = f"+{r['pct']:.1f}%" if r["pct"] >= 0 else f"{r['pct']:.1f}%"
    cross = "  🚨 MACD cross!" if (r["cross_up"] or r["cross_dn"]) else ""
    if r.get("div_yield") is not None:
        div_line = (f"\n   💰 Div: {r['div_yield']:.1f}% yield"
                    f"  |  Last: ฿{r['last_div']:.2f} ({r['last_div_date']})")
    else:
        div_line = "\n   💰 Div: —"
    return (
        f"{rank}. {BADGE[r['overall']]} {r['name']} ({r['ticker']}){cross}\n"
        f"   ฿{r['close']:,.2f} ({chg})  RSI: {r['rsi']:.0f}\n"
        f"   {SE[r['rsi_sig']]}RSI  {SE[r['ma_sig']]}MA  {SE[r['macd_sig']]}MACD"
        f"  Score: {r['score']:+d}/3"
        f"{div_line}"
    )

def build_portfolio_message(port: dict, today_trades: list,
                            prices: dict, ok: list,
                            n_screened: int, n_total: int,
                            date_str: str) -> str:
    """Full LINE message: portfolio state + today's trades + top signals."""

    total_val  = portfolio_value(port, prices)
    start_cap  = port["capital"]
    pnl_total  = total_val - start_cap
    pnl_pct    = pnl_total / start_cap * 100
    peak       = port.get("peak_value", start_cap)
    drawdown   = (total_val - peak) / peak * 100 if peak > 0 else 0
    day_no     = port.get("day_count", 1)
    start_date = port.get("start_date", date_str)

    p_sign  = "+" if pnl_total >= 0 else ""
    p_icon  = "📈" if pnl_total >= 0 else "📉"
    dd_icon = "⚠️ " if drawdown < -5 else ""

    lines = [
        f"🇹🇭 SET Portfolio Daily Report",
        f"📅 {date_str}  {session_label()}",
        f"📌 Day {day_no} | Started {start_date}",
        f"Screened: {n_screened}/{n_total} stocks",
        "─" * 34,
        "",
        # ── Portfolio summary ──────────────────────────────────────
        "💼 PORTFOLIO SUMMARY",
        f"   Total Value : ฿{total_val:>10,.0f}",
        f"   Cash        : ฿{port['cash']:>10,.0f}",
        f"   Invested    : ฿{(total_val - port['cash']):>10,.0f}",
        f"   P&L         : {p_icon} {p_sign}฿{pnl_total:,.0f}  ({p_sign}{pnl_pct:.2f}%)",
        f"   Drawdown    : {dd_icon}{drawdown:.2f}%",
        f"   Positions   : {len(port['holdings'])} / {MAX_POSITIONS}",
        "",
    ]

    # ── Today's trades ──────────────────────────────────────────────────────────
    if today_trades:
        lines.append(f"📋 TODAY'S TRADES  ({len(today_trades)} orders)")
        for t in today_trades:
            icon = "🟢 BUY  " if t["action"] == "BUY" else "🔴 SELL "
            pnl_s = ""
            if t["action"] == "SELL" and t["pnl"] != 0:
                ps = "+" if t["pnl"] >= 0 else ""
                pnl_s = f"  P&L:{ps}฿{t['pnl']:,.0f}"
            lines.append(
                f"   {icon}{t['name']:8s}  {t['shares']:5,d}sh"
                f" @ ฿{t['price']:,.2f} = ฿{t['value']:,.0f}{pnl_s}"
            )
            if t["action"] == "SELL":
                lines.append(f"   └─ Reason: {t['reason']}")
    else:
        lines.append("📋 TODAY'S TRADES  No transactions today")
    lines.append("")

    # ── Current holdings ────────────────────────────────────────────────────────
    if port["holdings"]:
        lines.append(f"📊 HOLDINGS  ({len(port['holdings'])} stocks)")
        invested_total = 0
        unrealised_total = 0
        for i, (ticker, h) in enumerate(
                sorted(port["holdings"].items(),
                       key=lambda x: -prices.get(x[0], x[1]["avg_cost"]) * x[1]["shares"]),
                1):
            px         = prices.get(ticker, h["avg_cost"])
            mkt_val    = h["shares"] * px
            cost_val   = h["shares"] * h["avg_cost"]
            unreal     = mkt_val - cost_val
            unreal_pct = unreal / cost_val * 100 if cost_val > 0 else 0
            u_icon     = "▲" if unreal >= 0 else "▼"
            u_sign     = "+" if unreal >= 0 else ""
            invested_total   += cost_val
            unrealised_total += unreal
            lines.append(
                f"  {i:2d}. {h['name']:8s}  {h['shares']:5,d}sh"
                f"  ฿{h['avg_cost']:,.2f}→฿{px:,.2f}"
                f"  {u_icon}{u_sign}{unreal_pct:.1f}%"
                f"  ({u_sign}฿{unreal:,.0f})"
            )
        ut_sign = "+" if unrealised_total >= 0 else ""
        lines.append(f"  Total unrealised: {ut_sign}฿{unrealised_total:,.0f}")
    else:
        lines.append("📊 HOLDINGS  No open positions")
    lines.append("")

    # ── Top signals ─────────────────────────────────────────────────────────────
    buys  = sorted([r for r in ok if r["score"] > 0],
                   key=lambda x: (-x["score"], x["rsi"]))[:3]
    sells = sorted([r for r in ok if r["score"] < 0],
                   key=lambda x: (x["score"], -x["rsi"]))[:3]

    print(f"  Fetching dividends for top {len(buys)+len(sells)} candidates...")
    for r in buys + sells:
        fetch_dividends(r)

    if buys:
        lines.append(f"══ 🟢 TOP BUY SIGNALS ══")
        for i, r in enumerate(buys, 1):
            lines.append(fmt_stock_line(i, r))
    else:
        lines.append("══ 🟢 BUY  No bullish signals today")

    lines.append("")

    if sells:
        lines.append(f"══ 🔴 TOP SELL SIGNALS ══")
        for i, r in enumerate(sells, 1):
            lines.append(fmt_stock_line(i, r))
    else:
        lines.append("══ 🔴 SELL  No bearish signals today")

    lines += [
        "",
        "─" * 34,
        "⚠️ Educational only. Not financial advice.",
    ]
    return "\n".join(lines)

# ─── LINE API ─────────────────────────────────────────────────────────────────
def send_line(message: str) -> tuple:
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {LINE_TOKEN}",
                     "Content-Type": "application/json"},
            data=json.dumps({"to": LINE_USER_ID,
                             "messages": [{"type": "text", "text": message}]},
                            ensure_ascii=False).encode("utf-8"),
            timeout=15,
        )
        return resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
    except Exception as e:
        return False, str(e)

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 58)
    print("Thai SET100 Signal Alert + Portfolio Manager")
    print(f"Run: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 58)

    # Validate config
    for key, placeholder in [
        ("line_channel_access_token", "YOUR_CHANNEL_ACCESS_TOKEN_HERE"),
        ("line_user_id",              "YOUR_LINE_USER_ID_HERE"),
    ]:
        val = cfg.get(key, "")
        if not val or val == placeholder:
            print(f"\nERROR: '{key}' not set in {CONFIG_PATH}")
            sys.exit(1)

    if not INSTRUMENTS:
        print("ERROR: No instruments in set_config.json"); sys.exit(1)

    today   = datetime.date.today().strftime("%d %b %Y")
    n_total = len(INSTRUMENTS)

    # Load portfolio (creates new if first run)
    port = load_portfolio()
    is_day_one = port["day_count"] == 0

    if is_day_one:
        print(f"\n🚀 FIRST RUN — Starting portfolio with ฿{INITIAL_CAPITAL:,.0f}")
    else:
        print(f"\n📌 Day {port['day_count']} — Portfolio value: "
              f"฿{portfolio_value(port, {}):,.0f} (before today's prices)")

    # Fetch prices & signals
    print(f"\nScreening {n_total} SET stocks ({MAX_WORKERS} threads)...\n")
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(analyze, name, ticker): (name, ticker)
                   for name, ticker in INSTRUMENTS}
        done = 0
        for future in as_completed(futures):
            r = future.result()
            done += 1
            if r.get("error"):
                print(f"  [{done:3d}/{n_total}] ⚠️  {r['name']:12s} {r['error']}")
            else:
                print(f"  [{done:3d}/{n_total}] {BADGE[r['overall']]:4s} "
                      f"{r['name']:12s} {r['overall']:12s} ({r['score']:+d})  "
                      f"฿{r['close']:,.2f}  RSI:{r['rsi']:.0f}")
            results.append(r)

    ok     = [r for r in results if not r.get("error")]
    prices = {r["ticker"]: r["close"] for r in ok}

    print(f"\nScreened: {len(ok)}/{n_total} stocks")
    print(f"BUY signals: {sum(1 for r in ok if r['score']>0)}"
          f"  |  SELL: {sum(1 for r in ok if r['score']<0)}"
          f"  |  HOLD: {sum(1 for r in ok if r['score']==0)}")

    # Execute portfolio trades
    today_trades = execute_daily_trades(port, ok, prices)
    save_portfolio(port)

    total_val = portfolio_value(port, prices)
    pnl       = total_val - port["capital"]
    print(f"\n── Portfolio after trades ──────────────────────")
    print(f"   Total value : ฿{total_val:,.0f}  "
          f"({'+'if pnl>=0 else ''}{pnl/port['capital']*100:.2f}%)")
    print(f"   Cash        : ฿{port['cash']:,.0f}")
    print(f"   Positions   : {len(port['holdings'])}")
    if today_trades:
        print(f"   Trades today: {len(today_trades)}")
        for t in today_trades:
            print(f"     {t['action']:4s} {t['name']:10s} "
                  f"{t['shares']:,d}sh @ ฿{t['price']:,.2f}"
                  + (f"  P&L: ฿{t['pnl']:,.0f}" if t['action']=="SELL" else ""))
    else:
        print("   Trades today: No trades")

    # Build LINE message
    message = build_portfolio_message(
        port, today_trades, prices, ok, len(ok), n_total, today)

    print("\n── Message Preview ─────────────────────────────")
    print(message)
    print(f"\nMessage length: {len(message)} chars")

    # Send to LINE
    print("\n── Sending via LINE Messaging API... ───────────")
    ok_send, resp = send_line(message)
    if ok_send:
        print("✅ Sent to LINE successfully!")
    else:
        print(f"❌ Failed: {resp}")
        sys.exit(1)

if __name__ == "__main__":
    main()
