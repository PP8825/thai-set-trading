#!/usr/bin/env python3
"""
Thai SET — Real-Time Signal Monitor
─────────────────────────────────────────────────────────────────
Runs every 15 minutes during market hours (scheduled via Cowork).
• Fetches live prices for all SET100 instruments
• Detects NEW or CHANGED signals vs last check
• Immediately executes trades on meaningful signal changes
• Sends a LINE alert for EVERY trade the moment it happens
• Saves portfolio state after every trade

Market hours (Bangkok, UTC+7):
  Morning   10:00 – 12:30
  Afternoon 14:30 – 16:30

Signal change rules (to avoid noise):
  Buy  trigger : score >= +2  AND  was <= 0  last check
  Sell trigger : score <= -1  AND  was >= +1 last check, OR stop-loss hit (-8%)
  Stop-loss    : price fell >= 8% below avg cost at any check

Compatible with Python 3.8+
"""

import sys, os, json, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── Auto-install ─────────────────────────────────────────────────────────────
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
                [sys.executable, "-m", "pip", "install", pkg, "--break-system-packages", "-q"],
            ]:
                try:
                    subprocess.check_call(args,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
                    break
                except subprocess.CalledProcessError:
                    continue

ensure_packages()

import requests
import pandas as pd
import yfinance as yf

# ─── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH    = os.path.join(SCRIPT_DIR, "set_config.json")
PORTFOLIO_PATH = os.path.join(SCRIPT_DIR, "set_portfolio.json")
STATE_PATH     = os.path.join(SCRIPT_DIR, "set_signal_state.json")

# ─── Config ───────────────────────────────────────────────────────────────────
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = json.load(f)

LINE_TOKEN    = os.environ.get("LINE_TOKEN", cfg.get("line_channel_access_token", ""))
LINE_USER_ID  = os.environ.get("LINE_USER_ID", cfg.get("line_user_id", ""))
INSTRUMENTS   = [(i["name"], i["ticker"]) for i in cfg.get("instruments", [])]
RSI_PERIOD    = cfg.get("rsi_period", 14)
RSI_OB        = cfg.get("rsi_overbought", 70)
RSI_OS        = cfg.get("rsi_oversold", 30)
SMA_PERIOD    = cfg.get("sma_period", 50)
LOOKBACK      = cfg.get("lookback_days", 300)
MAX_WORKERS   = cfg.get("download_threads", 10)

# ─── Portfolio / trading constants ────────────────────────────────────────────
INITIAL_CAPITAL = 300_000.0
LOT_SIZE        = 100
TX_COST         = 0.0025
STOP_LOSS_PCT   = 0.08
MAX_POSITIONS   = 10
CASH_FLOOR_PCT  = 0.05

# ─── Signal-change thresholds (avoid noise) ───────────────────────────────────
BUY_SCORE_MIN  = 2     # score >= +2 triggers BUY
SELL_SCORE_MAX = -1    # score <= -1 triggers SELL
BUY_PREV_MAX   = 0     # only if previous score was <= 0
SELL_PREV_MIN  = 1     # only if previous score was >= +1

# ─── Bangkok time (UTC+7, no pytz needed) ─────────────────────────────────────
BKK_OFFSET = datetime.timezone(datetime.timedelta(hours=7))

def now_bkk():
    """Current datetime in Bangkok time."""
    return datetime.datetime.now(BKK_OFFSET)

def time_str():
    return now_bkk().strftime("%H:%M")

def is_market_open():
    now = now_bkk()
    if now.weekday() >= 5:          # Sat/Sun
        return False
    hm = now.hour * 60 + now.minute
    morning   = (10 * 60) <= hm <= (12 * 60 + 30)
    afternoon = (14 * 60 + 30) <= hm <= (16 * 60 + 30)
    return morning or afternoon

# ─── Signal state persistence ─────────────────────────────────────────────────
def load_signal_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_signal_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)

# ─── Portfolio persistence ────────────────────────────────────────────────────
def load_portfolio():
    if os.path.exists(PORTFOLIO_PATH):
        with open(PORTFOLIO_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "capital":    INITIAL_CAPITAL, "cash": INITIAL_CAPITAL,
        "holdings":   {}, "trades":     [],
        "start_date": datetime.date.today().isoformat(),
        "day_count":  0,  "peak_value": INITIAL_CAPITAL,
    }

def save_portfolio(port):
    with open(PORTFOLIO_PATH, "w", encoding="utf-8") as f:
        json.dump(port, f, indent=2, default=str, ensure_ascii=False)

def portfolio_value(port, prices):
    return port["cash"] + sum(
        h["shares"] * prices.get(t, h["avg_cost"])
        for t, h in port["holdings"].items()
    )

# ─── Technical indicators ─────────────────────────────────────────────────────
def calc_rsi(s, n=14):
    d  = s.diff()
    ag = d.clip(lower=0).ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    al = (-d).clip(lower=0).ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    return float((100 - 100 / (1 + ag / al.replace(0, 1e-10))).iloc[-1])

def calc_sma(s, n):
    return float(s.rolling(n).mean().iloc[-1])

def calc_macd(s, fast=12, slow=26, sig=9):
    ml = s.ewm(span=fast, adjust=False).mean() - s.ewm(span=slow, adjust=False).mean()
    sl = ml.ewm(span=sig, adjust=False).mean()
    return float(ml.iloc[-1]), float(sl.iloc[-1])

def score_signals(rsi, close, sma, macd, msig):
    rs = 1 if rsi < RSI_OS else (-1 if rsi > RSI_OB else 0)
    ms = 1 if close > sma  else -1
    mc = 1 if macd  > msig else -1
    sc = max(-3, min(3, rs + ms + mc))
    label = {
        3: "STRONG BUY", 2: "BUY",  1: "BUY",
        0: "HOLD",
       -1: "SELL",      -2: "SELL", -3: "STRONG SELL"
    }[sc]
    return sc, label, rs, ms, mc

# ─── Parallel stock analysis ──────────────────────────────────────────────────
def analyze(name, ticker):
    try:
        df = yf.download(ticker, period="{0}d".format(LOOKBACK),
                         auto_adjust=True, progress=False, timeout=25)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df is None or df.empty or "Close" not in df.columns:
            return {"ticker": ticker, "name": name, "error": "No data"}

        c = df["Close"].dropna()
        if len(c) < max(SMA_PERIOD + 5, 60):
            return {"ticker": ticker, "name": name, "error": "Not enough data"}

        rsi_v          = calc_rsi(c, RSI_PERIOD)
        sma_v          = calc_sma(c, SMA_PERIOD)
        macd_v, msig_v = calc_macd(c)
        price          = float(c.iloc[-1])
        prev           = float(c.iloc[-2])
        pct            = (price - prev) / prev * 100
        sc, label, rs, ms, mc = score_signals(rsi_v, price, sma_v, macd_v, msig_v)

        return {
            "ticker": ticker, "name": name, "error": None,
            "price":  price,  "pct":  pct,
            "rsi":    rsi_v,  "sma":  sma_v,
            "macd":   macd_v, "msig": msig_v,
            "score":  sc,     "signal": label,
            "rsi_sig": rs,    "ma_sig": ms, "macd_sig": mc,
        }
    except Exception as e:
        return {"ticker": ticker, "name": name, "error": str(e)[:60]}

# ─── Trade execution ──────────────────────────────────────────────────────────
def execute_buy(port, r):
    """Buy stock r. Returns trade dict or None."""
    cash_floor = INITIAL_CAPITAL * CASH_FLOOR_PCT
    avail      = port["cash"] - cash_floor
    if avail <= 0:
        return None
    if r["ticker"] in port["holdings"]:
        return None
    if len(port["holdings"]) >= MAX_POSITIONS:
        return None

    price  = r["price"]
    n_free = max(1, MAX_POSITIONS - len(port["holdings"]))
    alloc  = min(avail / n_free, INITIAL_CAPITAL / MAX_POSITIONS * 1.5)
    shares = int(alloc / price / LOT_SIZE) * LOT_SIZE
    if shares <= 0:
        shares = LOT_SIZE
    cost = shares * price * (1 + TX_COST)
    if cost > avail:
        shares = int((avail / (price * (1 + TX_COST))) / LOT_SIZE) * LOT_SIZE
        cost   = shares * price * (1 + TX_COST)
    if shares <= 0 or cost > avail:
        return None

    today = datetime.date.today().isoformat()
    port["cash"] -= cost
    port["holdings"][r["ticker"]] = {
        "name":        r["name"],
        "shares":      shares,
        "avg_cost":    round(price, 2),
        "entry_date":  today,
        "entry_score": r["score"],
    }
    trade = {
        "date":    today,       "action":   "BUY",
        "ticker":  r["ticker"], "name":     r["name"],
        "shares":  shares,      "price":    round(price, 2),
        "value":   round(shares * price, 2),
        "avg_cost":round(price, 2), "pnl":  0,
        "reason":  r["signal"], "time":     time_str(),
    }
    port["trades"].append(trade)
    pv = portfolio_value(port, {r["ticker"]: price})
    if pv > port.get("peak_value", INITIAL_CAPITAL):
        port["peak_value"] = pv
    return trade

def execute_sell(port, ticker, price, reason):
    """Sell holding. Returns trade dict or None."""
    h = port["holdings"].get(ticker)
    if not h:
        return None

    today    = datetime.date.today().isoformat()
    proceeds = h["shares"] * price * (1 - TX_COST)
    pnl      = proceeds - h["shares"] * h["avg_cost"] * (1 + TX_COST)
    port["cash"] += proceeds
    del port["holdings"][ticker]

    trade = {
        "date":    today,    "action":   "SELL",
        "ticker":  ticker,   "name":     h["name"],
        "shares":  h["shares"], "price": round(price, 2),
        "value":   round(h["shares"] * price, 2),
        "avg_cost":round(h["avg_cost"], 2),
        "pnl":     round(pnl, 2),
        "reason":  reason,  "time":     time_str(),
    }
    port["trades"].append(trade)
    return trade

# ─── Day-1 initialisation ─────────────────────────────────────────────────────
def init_day_one(port, ok_results, prices):
    """Buy top-10 BUY signals on the very first run."""
    buys = sorted(
        [r for r in ok_results if r["score"] >= BUY_SCORE_MIN],
        key=lambda x: (-x["score"], x["rsi"])
    )[:10]

    trades = []
    cash_floor = INITIAL_CAPITAL * CASH_FLOOR_PCT
    n          = len(buys)
    if n == 0:
        return trades
    alloc = (port["cash"] - cash_floor) / n

    for r in buys:
        price  = r["price"]
        shares = int(alloc / price / LOT_SIZE) * LOT_SIZE
        if shares <= 0:
            shares = LOT_SIZE
        cost = shares * price * (1 + TX_COST)
        if cost > port["cash"] - cash_floor:
            continue
        port["cash"] -= cost
        today = datetime.date.today().isoformat()
        port["holdings"][r["ticker"]] = {
            "name":        r["name"],
            "shares":      shares,
            "avg_cost":    round(price, 2),
            "entry_date":  today,
            "entry_score": r["score"],
        }
        t = {
            "date":    today,       "action":   "BUY",
            "ticker":  r["ticker"], "name":     r["name"],
            "shares":  shares,      "price":    round(price, 2),
            "value":   round(shares * price, 2),
            "avg_cost":round(price, 2), "pnl":  0,
            "reason":  r["signal"] + " (Day 1)",
            "time":    time_str(),
        }
        port["trades"].append(t)
        trades.append((t, r))
        print(f"  D1 BUY  {r['name']:12s} {shares:,d}sh @ {price:.2f}")

    port["day_count"] = 1
    pv = portfolio_value(port, prices)
    port["peak_value"] = pv
    return trades

# ─── LINE messaging ───────────────────────────────────────────────────────────
def build_trade_alert(trade, port, prices, result=None):
    """Instant alert for a single BUY or SELL trade."""
    is_buy = trade["action"] == "BUY"
    total  = portfolio_value(port, prices)
    start  = port["capital"]
    pnl_t  = total - start
    ps     = "+" if pnl_t >= 0 else ""
    icon   = "🟢" if is_buy else "🔴"

    lines = [
        "🚨 TRADE ALERT  —  Real-Time",
        "{0}  {1}".format(time_str(), datetime.date.today().strftime("%d %b %Y")),
        "─" * 32,
        "",
        "{0} {1} EXECUTED".format(icon, trade["action"]),
        "   Stock  : {0} ({1})".format(trade["name"], trade["ticker"]),
        "   Shares : {0:,d} shares  ({1:,d} lots)".format(
            trade["shares"], trade["shares"] // 100),
        "   Price  : \u0e3f{0:,.2f}".format(trade["price"]),
        "   Value  : \u0e3f{0:,.0f}".format(trade["value"]),
    ]

    if not is_buy and trade.get("pnl") is not None:
        ps2 = "+" if trade["pnl"] >= 0 else ""
        lines.append("   P&L    : {0}\u0e3f{1:,.0f}".format(ps2, trade["pnl"]))

    lines.append("   Signal : {0}".format(trade["reason"]))

    if result:
        lines += [
            "",
            "📊 Indicators",
            "   RSI {0:.0f}  {1}RSI  {2}MA  {3}MACD  Score: {4:+d}/3".format(
                result["rsi"],
                "▲" if result["rsi_sig"] > 0 else "▼",
                "▲" if result["ma_sig"]  > 0 else "▼",
                "▲" if result["macd_sig"]> 0 else "▼",
                result["score"],
            ),
        ]

    lines += [
        "",
        "💼 Portfolio",
        "   Value : \u0e3f{0:,.0f}  ({1}{2:.2f}%)".format(
            total, ps, pnl_t / start * 100),
        "   Cash  : \u0e3f{0:,.0f}".format(port["cash"]),
        "   Held  : {0}/{1} stocks".format(len(port["holdings"]), MAX_POSITIONS),
        "",
        "─" * 32,
        "⚠️ Educational only. Not financial advice.",
    ]
    return "\n".join(lines)

def build_status_update(results, port, prices):
    """30-minute status message when no trades triggered."""
    total  = portfolio_value(port, prices)
    start  = port["capital"]
    pnl    = total - start
    ps     = "+" if pnl >= 0 else ""
    n_buy  = sum(1 for r in results if r.get("score", 0) >= BUY_SCORE_MIN)
    n_sell = sum(1 for r in results if r.get("score", 0) <= SELL_SCORE_MAX)

    movers = sorted(
        [r for r in results if not r.get("error")],
        key=lambda x: abs(x.get("pct", 0)), reverse=True
    )[:3]
    mover_str = "  ".join(
        "{0} {1}{2:.1f}%".format(
            r["name"], "+" if r["pct"] >= 0 else "", r["pct"])
        for r in movers
    )

    lines = [
        "🇹🇭 Market Update  {0}  {1}".format(
            time_str(), datetime.date.today().strftime("%d %b %Y")),
        "Screened {0} stocks  |  🟢{1} BUY  🔴{2} SELL".format(
            len(results), n_buy, n_sell),
        "No new signals — no trades executed.",
        "",
        "💼 \u0e3f{0:,.0f}  ({1}{2:.2f}%)".format(total, ps, pnl / start * 100),
        "   Cash: \u0e3f{0:,.0f}  |  Positions: {1}/{2}".format(
            port["cash"], len(port["holdings"]), MAX_POSITIONS),
    ]

    if port["holdings"]:
        lines.append("")
        lines.append("📊 Holdings:")
        for i, (ticker, h) in enumerate(
                sorted(port["holdings"].items(),
                       key=lambda x: -(x[1]["shares"] *
                                       prices.get(x[0], x[1]["avg_cost"]))),
                1):
            px   = prices.get(ticker, h["avg_cost"])
            up   = (px - h["avg_cost"]) / h["avg_cost"] * 100
            icon = "▲" if up >= 0 else "▼"
            lines.append(
                "  {0:2d}. {1:8s} \u0e3f{2:,.2f}  {3}{4}{5:.1f}%".format(
                    i, h["name"], px, icon, "+" if up >= 0 else "", up)
            )

    if movers:
        lines += ["", "📈 Top movers: {0}".format(mover_str)]

    return "\n".join(lines)

def send_line(message):
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": "Bearer {0}".format(LINE_TOKEN),
                     "Content-Type": "application/json"},
            data=json.dumps({"to": LINE_USER_ID,
                             "messages": [{"type": "text", "text": message}]},
                            ensure_ascii=False).encode("utf-8"),
            timeout=15,
        )
        return resp.status_code == 200
    except Exception as e:
        print("  LINE error: {0}".format(e))
        return False

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    now = now_bkk()
    print("=" * 56)
    print("Thai SET Real-Time Monitor")
    print("Run: {0}  Bangkok".format(now.strftime("%Y-%m-%d %H:%M:%S")))
    print("=" * 56)

    # Market hours gate
    if not is_market_open():
        print("\n  Market closed at {0} — exiting.".format(time_str()))
        return

    print("\n  Market open at {0} — scanning...".format(time_str()))

    # Load state
    port       = load_portfolio()
    prev_state = load_signal_state()
    today_str  = datetime.date.today().isoformat()

    # Fetch all signals
    n_total = len(INSTRUMENTS)
    print("\nFetching {0} instruments ({1} threads)...\n".format(
        n_total, MAX_WORKERS))

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(analyze, nm, tk): (nm, tk)
                   for nm, tk in INSTRUMENTS}
        done = 0
        for future in as_completed(futures):
            r = future.result()
            done += 1
            if r.get("error"):
                print("  [{0:3d}/{1}] ⚠  {2:12s} {3}".format(
                    done, n_total, r["name"], r["error"]))
            else:
                sig_icon = ("🟢" if r["score"] >= BUY_SCORE_MIN else
                            "🔴" if r["score"] <= SELL_SCORE_MAX else "🟡")
                print("  [{0:3d}/{1}] {2} {3:12s} {4:12s} ({5:+d})"
                      "  \u0e3f{6:,.2f}  RSI:{7:.0f}".format(
                    done, n_total, sig_icon, r["name"],
                    r["signal"], r["score"], r["price"], r["rsi"]))
            results.append(r)

    ok      = [r for r in results if not r.get("error")]
    prices  = {r["ticker"]: r["price"] for r in ok}

    trades_executed = []

    # Day-1 initialisation
    if port["day_count"] == 0:
        print("\n  First run — buying top-10 BUY signals...")
        trades_executed = init_day_one(port, ok, prices)
        save_portfolio(port)
        # Build signal state from scratch
        new_state = {}
        for r in ok:
            new_state[r["ticker"]] = {
                "score":        r["score"],
                "signal":       r["signal"],
                "price":        r["price"],
                "buy_alerted":  r["score"] >= BUY_SCORE_MIN,
                "sell_alerted": False,
            }
        new_state["_last_reset_date"] = today_str
        new_state["_check_count"]     = 1
        save_signal_state(new_state)
        # Send alerts for all day-1 buys
        for trade, res in trades_executed:
            msg = build_trade_alert(trade, port, prices, res)
            ok_send = send_line(msg)
            print("  LINE: {0} {1}".format(
                "✅" if ok_send else "❌", trade["name"]))
        print("\nDay-1 portfolio started. {0} positions opened.".format(
            len(port["holdings"])))
        return

    # Reset alert flags each new day
    if prev_state.get("_last_reset_date") != today_str:
        print("  New day — resetting alert flags.")
        for k, v in prev_state.items():
            if isinstance(v, dict):
                v["buy_alerted"]  = False
                v["sell_alerted"] = False
        prev_state["_last_reset_date"] = today_str
        prev_state["_check_count"]     = 0

    check_count = int(prev_state.get("_check_count", 0)) + 1
    new_state   = dict(prev_state)
    new_state["_check_count"] = check_count
    alerted     = set()

    # Stop-loss check on holdings
    print("\nChecking stop-losses on {0} holdings...".format(
        len(port["holdings"])))
    for ticker in list(port["holdings"].keys()):
        h  = port["holdings"].get(ticker)
        if not h:
            continue
        px = prices.get(ticker, h["avg_cost"])
        if px <= h["avg_cost"] * (1 - STOP_LOSS_PCT):
            drop = (1 - px / h["avg_cost"]) * 100
            print("  STOP-LOSS {0}  \u0e3f{1:.2f} (avg \u0e3f{2:.2f}, -{3:.1f}%)".format(
                h["name"], px, h["avg_cost"], drop))
            trade = execute_sell(
                port, ticker, px,
                "Stop-loss -{0:.0f}%".format(STOP_LOSS_PCT * 100))
            if trade:
                trades_executed.append((trade, None))
                alerted.add(ticker)

    # Signal-change detection
    sig_map = {r["ticker"]: r for r in ok}
    for ticker, r in sig_map.items():
        score = r["score"]
        prev  = prev_state.get(ticker, {})
        prev_score = prev.get("score", 0) if isinstance(prev, dict) else 0

        new_state[ticker] = {
            "score":        score,
            "signal":       r["signal"],
            "price":        r["price"],
            "buy_alerted":  prev.get("buy_alerted",  False) if isinstance(prev, dict) else False,
            "sell_alerted": prev.get("sell_alerted", False) if isinstance(prev, dict) else False,
        }

        # BUY trigger
        if (score >= BUY_SCORE_MIN
                and prev_score <= BUY_PREV_MAX
                and ticker not in alerted
                and not (new_state[ticker].get("buy_alerted"))):
            trade = execute_buy(port, r)
            if trade:
                print("  BUY  {0:12s} {1:,d}sh @ \u0e3f{2:.2f}".format(
                    r["name"], trade["shares"], r["price"]))
                trades_executed.append((trade, r))
                alerted.add(ticker)
                new_state[ticker]["buy_alerted"]  = True
                new_state[ticker]["sell_alerted"] = False

        # SELL trigger
        elif (score <= SELL_SCORE_MAX
                and prev_score >= SELL_PREV_MIN
                and ticker not in alerted
                and not (new_state[ticker].get("sell_alerted"))):
            if ticker in port["holdings"]:
                trade = execute_sell(port, ticker, r["price"], r["signal"])
                if trade:
                    print("  SELL {0:12s} {1:,d}sh @ \u0e3f{2:.2f}"
                          "  P&L: \u0e3f{3:,.0f}".format(
                        r["name"], trade["shares"], r["price"], trade["pnl"]))
                    trades_executed.append((trade, r))
                    alerted.add(ticker)
                    new_state[ticker]["sell_alerted"] = True
                    new_state[ticker]["buy_alerted"]  = False

    # Save portfolio + state
    save_portfolio(port)
    save_signal_state(new_state)

    total_val = portfolio_value(port, prices)
    print("\n── After check #{0} ──────────────────────────────".format(check_count))
    print("   Portfolio : \u0e3f{0:,.0f}  ({1}{2:.2f}%)".format(
        total_val,
        "+" if total_val >= port["capital"] else "",
        (total_val - port["capital"]) / port["capital"] * 100))
    print("   Cash      : \u0e3f{0:,.0f}".format(port["cash"]))
    print("   Positions : {0}/{1}".format(len(port["holdings"]), MAX_POSITIONS))
    print("   Trades now: {0}".format(len(trades_executed)))

    # Send LINE messages
    msgs_sent = 0
    if trades_executed:
        for trade, res in trades_executed:
            msg    = build_trade_alert(trade, port, prices, res)
            ok_snd = send_line(msg)
            print("  LINE {0}: {1} {2}".format(
                "✅" if ok_snd else "❌", trade["action"], trade["name"]))
            if ok_snd:
                msgs_sent += 1
    else:
        if check_count % 2 == 0:    # every 30 minutes
            msg    = build_status_update(ok, port, prices)
            ok_snd = send_line(msg)
            print("  Status update: {0}".format("✅ sent" if ok_snd else "❌ failed"))
            if ok_snd:
                msgs_sent += 1

    print("\nDone. {0} LINE message(s) sent.".format(msgs_sent))

if __name__ == "__main__":
    import sys as _sys
    if "--force" in _sys.argv:
        # Bypass market hours check for testing
        _orig = is_market_open
        is_market_open = lambda: True
    main()
