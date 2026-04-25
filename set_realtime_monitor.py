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
HISTORY_PATH   = os.path.join(SCRIPT_DIR, "set_history.json")

# ─── Config ───────────────────────────────────────────────────────────────────
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = json.load(f)

LINE_TOKEN    = os.environ.get("LINE_TOKEN", cfg.get("line_channel_access_token", ""))
LINE_USER_ID  = os.environ.get("LINE_USER_ID", cfg.get("line_user_id", ""))
INSTRUMENTS   = [(i["name"], i["ticker"]) for i in cfg.get("instruments", [])]
RSI_PERIOD    = cfg.get("rsi_period", 14)
RSI_OB        = cfg.get("rsi_overbought", 70)
RSI_OS        = cfg.get("rsi_oversold", 30)
SMA_SHORT     = cfg.get("sma_short_period", 20)   # fast EMA for trend
SMA_LONG      = cfg.get("sma_long_period",  50)   # slow EMA for trend
SMA_PERIOD    = SMA_LONG                           # backward compat alias
ADX_PERIOD    = cfg.get("adx_period", 14)
VOL_SURGE_R   = cfg.get("volume_surge_ratio", 1.5)
LOOKBACK      = cfg.get("lookback_days", 300)
MAX_WORKERS   = cfg.get("download_threads", 10)

# ─── Portfolio / trading constants ────────────────────────────────────────────
INITIAL_CAPITAL = 300_000.0
LOT_SIZE        = 100
TX_COST         = 0.0025
STOP_LOSS_PCT   = 0.08
MAX_POSITIONS   = 10
CASH_FLOOR_PCT  = 0.05

# ─── Signal-change thresholds ─────────────────────────────────────────────────
# Tech score now ranges -5 to +5 (5 indicators).  Buy needs 3+ bullish (≥+3).
BUY_SCORE_MIN  = 3     # score >= +3 triggers BUY  (was +2 with 3 indicators)
SELL_SCORE_MAX = -2    # score <= -2 triggers SELL  (was -1)
BUY_PREV_MAX   = 0     # only buy if previous score was <= 0 (fresh cross)
SELL_PREV_MIN  = 1     # only sell if previous score was >= +1

# ─── Rotation parameters ──────────────────────────────────────────────────────
ROTATION_ENABLED        = True
ROTATION_MIN_HOLD_DAYS  = 5
ROTATION_MIN_HOLD_FAST  = 3     # fast-track: comp >= 8.5 bypasses min-hold
ROTATION_HELD_SCORE_MAX = 1     # held score <= +1 qualifies for rotation-out
ROTATION_MAX_LOSS_PCT   = 0.03
ROTATION_MAX_PER_DAY    = 2
ROTATION_COOLDOWN_DAYS  = 5

# ─── Composite-score rotation ─────────────────────────────────────────────────
# Rotation uses a LOWER incoming bar than fresh buys (comp-driven, not score-driven).
# Fresh buy still requires BUY_SCORE_MIN (+3).  Rotation only requires +2 incoming
# tech + decent composite — the 0.8 gain requirement prevents churn.
ROTATION_IN_SCORE_MIN  = 2     # incoming tech score for rotation (fresh buy = +3)
ROTATION_IN_COMP_MIN   = 7.0   # incoming comp must clear this floor
ROTATION_COMP_FLOOR    = 6.5   # held comp below this → eligible for rotation-out (was 7.2)
ROTATION_COMP_MIN_GAIN = 0.8   # incoming comp must beat held comp by at least this
ROTATION_FAST_COMP     = 8.5   # incoming comp >= this → fast-track (bypass min hold days)

# ─── Market regime filter ────────────────────────────────────────────────────
# If SET Index is below its 200-day MA → BEAR regime: all buys & rotations suspended.
# Only sells and stop-losses execute.  Prevents buying into a falling market.
REGIME_ENABLED   = True
REGIME_TICKER    = "^SET.BK"
REGIME_MA_PERIOD = 200

# ─── ATR-based stop-loss ─────────────────────────────────────────────────────
# Stop price = entry_price − ATR_MULTIPLIER × ATR(14).
# Adapts to each stock's own volatility — volatile stocks get wider stops,
# stable stocks get tighter stops.  Replaces the blunt fixed 8% stop.
ATR_PERIOD      = 14
ATR_MULTIPLIER  = 2.0    # 2 × ATR — standard Wilder / Van Tharp setting
ATR_FALLBACK_PCT = 0.08  # 8% fallback for holdings that pre-date ATR tracking

# ─── Dividend timing guard ────────────────────────────────────────────────────
EX_DIV_HOLD_DAYS = 14

# ─── Fundamental filter ────────────────────────────────────────────────────────
_ff            = cfg.get("fundamental_filter", {})
FUND_ENABLED   = _ff.get("enabled", True)
MAX_PE         = _ff.get("max_pe", 15)
MAX_PBV        = _ff.get("max_pbv", 3)
MIN_ROE        = _ff.get("min_roe", 0.08)
REQ_DIVIDEND   = _ff.get("require_dividend", True)

# ─── Composite scoring weights ────────────────────────────────────────────────
_sw          = cfg.get("scoring_weights", {})
WEIGHT_TECH  = _sw.get("technical",   0.6)
WEIGHT_FUND  = _sw.get("fundamental", 0.4)
TECH_MAX     = 5        # tech score range is now -5 to +5

# ─── Sector concentration ────────────────────────────────────────────────────
_sc           = cfg.get("sector_concentration", {})
SECTOR_ENABLED= _sc.get("enabled", True)
SECTOR_MAX    = _sc.get("max_per_sector", 2)
SECTOR_MAP    = cfg.get("sector_map", {})

# ─── Liquidity filter ────────────────────────────────────────────────────────
_lq                = cfg.get("liquidity_filter", {})
LIQUIDITY_ENABLED  = _lq.get("enabled", True)
MAX_POS_PCT_VOL    = _lq.get("max_position_pct_of_avg_volume", 0.10)

# ─── Position sizing (volatility-tiered) ─────────────────────────────────────
_ps              = cfg.get("position_sizing", {})
VOL_LB_DAYS      = _ps.get("vol_lookback_days",  20)
VOL_HIGH_THRESH  = _ps.get("vol_high_threshold", 0.030)   # >3% daily std → high vol
VOL_LOW_THRESH   = _ps.get("vol_low_threshold",  0.015)   # <1.5% daily std → low vol
VOL_HIGH_MULT    = _ps.get("vol_high_multiplier", 0.75)   # smaller position
VOL_LOW_MULT     = _ps.get("vol_low_multiplier",  1.25)   # larger position

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

def save_portfolio(port, prices=None):
    """Save portfolio. If prices provided, stamp last_price on each holding."""
    if prices:
        for ticker, h in port.get("holdings", {}).items():
            if ticker in prices:
                h["last_price"] = prices[ticker]
    with open(PORTFOLIO_PATH, "w", encoding="utf-8") as f:
        json.dump(port, f, indent=2, default=str, ensure_ascii=False)

def save_daily_snapshot(port, prices):
    """Record today's portfolio value with live prices into set_history.json."""
    today     = datetime.date.today().isoformat()
    total     = portfolio_value(port, prices)
    pnl       = total - port["capital"]
    pnl_pct   = pnl / port["capital"] * 100
    day_trades = sum(1 for t in port.get("trades", []) if t.get("date") == today)

    history = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            history = json.load(f)

    # Calculate peak for drawdown
    peak = port.get("peak_value", port["capital"])
    dd   = (total - peak) / peak * 100 if peak else 0

    entry = {
        "day":      len(history) + 1,
        "date":     today,
        "value":    round(total, 2),
        "cash":     round(port["cash"], 2),
        "pnl":      round(pnl, 2),
        "pnlPct":   round(pnl_pct, 2),
        "drawdown": round(dd, 2),
        "trades":   day_trades,
    }

    # Update existing entry for today or append new one
    existing = next((h for h in history if h.get("date") == today), None)
    if existing:
        existing.update(entry)
        existing["day"] = history.index(existing) + 1
    else:
        history.append(entry)

    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, default=str)

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

def calc_adx(df, n=14):
    """
    Average Directional Index + directional indicators.
    Returns (adx, di_plus, di_minus) — all floats.
    ADX > 20 = trending; +DI > -DI = uptrend; -DI > +DI = downtrend.
    """
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    up   = high.diff().clip(lower=0)
    down = (-low.diff()).clip(lower=0)
    dm_p = up.where(up > down, 0.0)
    dm_m = down.where(down > up, 0.0)
    atr_s = tr.ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    dmp_s = dm_p.ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    dmm_s = dm_m.ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    safe  = atr_s.replace(0, 1e-10)
    di_p  = 100 * dmp_s / safe
    di_m  = 100 * dmm_s / safe
    dx    = 100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, 1e-10)
    adx   = dx.ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    return float(adx.iloc[-1]), float(di_p.iloc[-1]), float(di_m.iloc[-1])

def calc_vol_20d(close):
    """20-day realised daily volatility (std of daily returns)."""
    ret = close.pct_change().dropna()
    return float(ret.tail(20).std()) if len(ret) >= 20 else 0.02

def calc_atr(df, n=14):
    """
    Average True Range — Wilder smoothing.
    TR = max(High−Low, |High−PrevClose|, |Low−PrevClose|)
    Returns latest ATR value as a price amount (same unit as the stock price).
    """
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    return float(atr.iloc[-1])

def score_signals(rsi, close, ema_short, ema_long, macd, msig,
                  adx=0, di_plus=0, di_minus=0, vol_score=0):
    """
    5-indicator technical score: range -5 to +5.

    1. RSI          : +1 oversold / -1 overbought / 0 neutral
    2. EMA trend    : +1 if fast EMA > slow EMA (bullish) / -1 if below
    3. MACD         : +1 if MACD line > signal / -1 if below
    4. ADX + DI     : +1 trending up (ADX>20, +DI>-DI) / -1 trending down / 0 choppy
    5. Volume surge : +1 if volume >1.5× avg AND price rose / -1 fell / 0 neutral

    Buy  threshold: >= +3  (≥3 of 5 bullish)
    Sell threshold: <= -2  (2+ bearish)
    """
    rs = 1 if rsi < RSI_OS else (-1 if rsi > RSI_OB else 0)
    ms = 1 if close > ema_long else -1          # long-term trend
    et = 1 if ema_short > ema_long else -1       # EMA crossover (short vs long)
    mc = 1 if macd > msig else -1
    # Blend ms and et into one EMA indicator to keep 5 clean components:
    # if both agree → full signal; if split → neutral
    trend = (1 if ms > 0 and et > 0 else (-1 if ms < 0 and et < 0 else 0))
    # ADX: only count if trending (ADX > 20)
    if adx > 20:
        ad = 1 if di_plus > di_minus else -1
    else:
        ad = 0   # choppy — no directional conviction
    vs = vol_score   # pre-computed: +1 / 0 / -1
    sc = max(-5, min(5, rs + trend + mc + ad + vs))
    label = {
         5: "STRONG BUY",  4: "STRONG BUY", 3: "BUY",
         2: "BUY",         1: "WATCH",       0: "HOLD",
        -1: "WATCH",      -2: "SELL",       -3: "SELL",
        -4: "STRONG SELL",-5: "STRONG SELL",
    }[sc]
    return sc, label, rs, trend, mc, ad, vs

# ─── Fundamental filter ───────────────────────────────────────────────────────
def fetch_fundamentals(ticker):
    """
    Fetch fundamentals from yfinance .info.
    Original fields: P/E, P/BV, ROE, dividend yield, ex-div date.
    New fields:      Debt/Equity, Earnings Growth, FCF Yield.
    """
    try:
        tk   = yf.Ticker(ticker)
        info = tk.info

        pe      = info.get("trailingPE") or info.get("forwardPE")
        pbv     = info.get("priceToBook")
        roe     = info.get("returnOnEquity")
        div_yld = info.get("dividendYield") or 0.0

        # Dividend history check
        try:
            divs    = tk.dividends
            cutoff  = pd.Timestamp.now(tz="UTC") - pd.DateOffset(years=3)
            has_div = len(divs[divs.index >= cutoff]) > 0
        except Exception:
            has_div = div_yld > 0

        # Ex-dividend date
        ex_div_date = None
        ex_ts = info.get("exDividendDate")
        if ex_ts:
            try:
                ex_div_date = datetime.date.fromtimestamp(ex_ts).isoformat()
            except Exception:
                pass

        # ── NEW: Debt/Equity ratio ────────────────────────────────────────────
        # debtToEquity from yfinance is expressed as a percentage (e.g. 45.3 = 0.453x)
        de_raw = info.get("debtToEquity")
        de_ratio = float(de_raw) / 100.0 if de_raw is not None else None

        # ── NEW: Earnings growth (trailing 12m EPS growth) ───────────────────
        eps_growth = info.get("earningsGrowth")   # float, e.g. 0.15 = 15% growth

        # ── NEW: Free Cash Flow Yield = FCF / Market Cap ─────────────────────
        fcf     = info.get("freeCashflow")
        mkt_cap = info.get("marketCap")
        fcf_yield = (float(fcf) / float(mkt_cap)
                     if fcf is not None and mkt_cap and mkt_cap > 0 else None)

        # ── NEW: Average daily volume (for liquidity check) ───────────────────
        avg_volume = info.get("averageVolume") or info.get("averageDailyVolume10Day")

        return {
            "pe":          pe,
            "pbv":         pbv,
            "roe":         roe,
            "div_yld":     div_yld,
            "has_div":     has_div,
            "ex_div_date": ex_div_date,
            "de_ratio":    de_ratio,
            "eps_growth":  eps_growth,
            "fcf_yield":   fcf_yield,
            "avg_volume":  avg_volume,
        }
    except Exception:
        return {}

def check_fundamentals(fund):
    """
    Returns (passes: bool, reasons: list, summary: str)
    If data is missing for a metric, that metric is skipped (benefit of the doubt).
    """
    if not FUND_ENABLED or not fund:
        return True, [], "N/A"

    fails  = []
    checks = []

    pe  = fund.get("pe")
    pbv = fund.get("pbv")
    roe = fund.get("roe")
    has_div = fund.get("has_div", True)

    if pe is not None:
        if pe > 0 and pe <= MAX_PE:
            checks.append("P/E {:.1f} OK".format(pe))
        elif pe > MAX_PE:
            fails.append("P/E {:.1f}>{:.0f}".format(pe, MAX_PE))
        # Negative P/E (loss-making) = fail
        elif pe < 0:
            fails.append("P/E neg (loss)")
    else:
        checks.append("P/E N/A")

    if pbv is not None:
        if pbv <= MAX_PBV:
            checks.append("PBV {:.2f} OK".format(pbv))
        else:
            fails.append("PBV {:.2f}>{:.1f}".format(pbv, MAX_PBV))
    else:
        checks.append("PBV N/A")

    if roe is not None:
        if roe >= MIN_ROE:
            checks.append("ROE {:.0%} OK".format(roe))
        else:
            fails.append("ROE {:.0%}<{:.0%}".format(roe, MIN_ROE))
    else:
        checks.append("ROE N/A")

    if REQ_DIVIDEND:
        if has_div:
            checks.append("DIV OK")
        else:
            fails.append("No dividend")

    passes  = len(fails) == 0
    summary = " | ".join(checks + ["❌ " + f for f in fails])
    return passes, fails, summary


_FUND_MAX_RAW = 19.0   # 3(P/E)+3(P/BV)+3(ROE)+4(Div)+2(D/E)+2(Growth)+2(FCF)

def calc_fundamental_score(fund):
    """
    Graduated fundamental quality score: 0–10 (normalised from raw 0–19).

    Original factors (0–13 pts):
      P/E  (0–3):  ≤8→3 | 8–12→2 | 12–15→1 | >15 or neg→0
      P/BV (0–3):  <1→3 | 1–1.5→2 | 1.5–3→1 | >3→0
      ROE  (0–3):  ≥20%→3 | 12–20%→2 | 8–12%→1 | <8%→0
      Div  (0–4):  ≥9%→4 | ≥8%→3.5 | ≥6.5%→3 | ≥5%→2.5 | ≥3%→2 | any→1 | none→0

    New quality factors (0–6 pts):
      D/E ratio   (0–2):  <0.5→2 | <1.0→1 | ≥1.0→0  (low debt = better)
      EPS growth  (0–2):  ≥15%→2 | >0%→1 | neg→0    (growing earnings)
      FCF yield   (0–2):  ≥6%→2 | ≥3%→1 | <3%→0    (cash generation quality)

    Missing data → 1 pt (benefit of the doubt). Normalised to 0–10 (÷19 × 10).
    """
    if not fund:
        return 5.0

    score = 0.0

    # ── P/E — max 3 pts ───────────────────────────────────────────────────────
    pe = fund.get("pe")
    if pe is None:      score += 1
    elif pe <= 0:       score += 0
    elif pe <= 8:       score += 3
    elif pe <= 12:      score += 2
    elif pe <= 15:      score += 1

    # ── P/BV — max 3 pts ──────────────────────────────────────────────────────
    pbv = fund.get("pbv")
    if pbv is None:     score += 1
    elif pbv < 1.0:     score += 3
    elif pbv <= 1.5:    score += 2
    elif pbv <= 3:      score += 1

    # ── ROE — max 3 pts ───────────────────────────────────────────────────────
    roe = fund.get("roe")
    if roe is None:     score += 1
    elif roe >= 0.20:   score += 3
    elif roe >= 0.12:   score += 2
    elif roe >= 0.08:   score += 1

    # ── Dividend — max 4 pts ──────────────────────────────────────────────────
    has_div = fund.get("has_div", False)
    div_yld = fund.get("div_yld") or 0.0
    if has_div:
        if div_yld >= 0.09:    score += 4.0
        elif div_yld >= 0.08:  score += 3.5
        elif div_yld >= 0.065: score += 3.0
        elif div_yld >= 0.05:  score += 2.5
        elif div_yld >= 0.03:  score += 2.0
        else:                  score += 1.0

    # ── NEW: Debt/Equity — max 2 pts (low leverage = resilience) ─────────────
    de = fund.get("de_ratio")
    if de is None:      score += 1      # unknown → neutral
    elif de < 0.5:      score += 2      # very low debt — financially strong
    elif de < 1.0:      score += 1      # moderate leverage — acceptable

    # ── NEW: Earnings Growth — max 2 pts (momentum in fundamentals) ──────────
    eg = fund.get("eps_growth")
    if eg is None:      score += 1      # unknown → neutral
    elif eg >= 0.15:    score += 2      # growing fast (≥15% YoY)
    elif eg > 0.0:      score += 1      # growing, even if slowly

    # ── NEW: Free Cash Flow Yield — max 2 pts (quality of earnings) ──────────
    fcf_y = fund.get("fcf_yield")
    if fcf_y is None:   score += 1      # unknown → neutral
    elif fcf_y >= 0.06: score += 2      # high FCF yield — real cash generator
    elif fcf_y >= 0.03: score += 1      # decent FCF

    return round(min(10.0, score / _FUND_MAX_RAW * 10.0), 1)


def calc_composite_score(tech_score, fund_score):
    """
    Blend technical (−5 to +5) and fundamental (0–10) into a single 0–10 score.
    Technical is first normalised to 0–10: (score + TECH_MAX) / (2 × TECH_MAX) × 10.
    Weights are set in set_config.json under scoring_weights.
    """
    tech_norm = (tech_score + TECH_MAX) / (2.0 * TECH_MAX) * 10.0
    composite = tech_norm * WEIGHT_TECH + fund_score * WEIGHT_FUND
    return round(composite, 2)


# ─── Dividend income tracking ─────────────────────────────────────────────────
def check_and_credit_dividends(port, prices):
    """
    For each holding, check if any dividend was paid since last check.
    Credits cash, logs a DIVIDEND trade, and returns list of credited trades.
    Only runs once per day per holding (tracked via 'last_div_check' in holding).
    """
    today     = datetime.date.today()
    today_str = today.isoformat()
    credited  = []

    for ticker, h in list(port["holdings"].items()):
        last_check = h.get("last_div_check")
        # Skip if already checked today
        if last_check == today_str:
            continue
        # Use entry_date as starting cutoff on first check
        cutoff_str = last_check or h.get("entry_date", today_str)
        try:
            cutoff_ts = pd.Timestamp(cutoff_str, tz="UTC")
            today_ts  = pd.Timestamp(today_str,  tz="UTC") + pd.DateOffset(days=1)

            divs = yf.Ticker(ticker).dividends
            if divs.empty:
                h["last_div_check"] = today_str
                continue

            recent = divs[(divs.index > cutoff_ts) & (divs.index <= today_ts)]
            for div_date, div_per_share in recent.items():
                div_per_share = float(div_per_share)
                amount        = round(h["shares"] * div_per_share, 2)
                port["cash"] += amount

                trade = {
                    "date":     div_date.date().isoformat(),
                    "action":   "DIVIDEND",
                    "ticker":   ticker,
                    "name":     h["name"],
                    "shares":   h["shares"],
                    "price":    round(div_per_share, 4),   # dividend per share
                    "value":    amount,
                    "avg_cost": h["avg_cost"],
                    "pnl":      amount,                    # full amount is income
                    "reason":   "Dividend ฿{0:.4f}/share".format(div_per_share),
                    "time":     time_str(),
                }
                port["trades"].append(trade)
                credited.append(trade)
                print("  💰 DIVIDEND {0:12s}  ฿{1:.4f}/sh × {2:,d}sh = ฿{3:,.2f}".format(
                    h["name"], div_per_share, h["shares"], amount))

            h["last_div_check"] = today_str

        except Exception as e:
            print("  ⚠  Dividend check failed for {0}: {1}".format(ticker, e))
            h["last_div_check"] = today_str

    return credited


def build_dividend_alert(credited_trades, port, prices):
    """LINE alert summarising dividends received today."""
    total_div  = sum(t["value"] for t in credited_trades)
    total_port = portfolio_value(port, prices)
    start      = port["capital"]
    pnl_t      = total_port - start
    ps         = "+" if pnl_t >= 0 else ""

    lines = [
        "💰 DIVIDEND INCOME  —  {0}  {1}".format(
            time_str(), datetime.date.today().strftime("%d %b %Y")),
        "─" * 34,
        "",
    ]
    for t in credited_trades:
        lines.append("  {0:10s}  ฿{1:.4f}/sh × {2:,d}sh  = ฿{3:,.2f}".format(
            t["name"], t["price"], t["shares"], t["value"]))
    lines += [
        "",
        "   Total dividend income : ฿{0:,.2f}".format(total_div),
        "",
        "💼 Portfolio after dividend",
        "   Total : ฿{0:,.0f}  ({1}{2:.2f}%)".format(
            total_port, ps, pnl_t / start * 100),
        "   Cash  : ฿{0:,.0f}".format(port["cash"]),
        "",
        "─" * 34,
        "⚠️ Educational only. Not financial advice.",
    ]
    return "\n".join(lines)


# ─── Dividend timing guard ────────────────────────────────────────────────────
def days_to_ex_div(fund):
    """
    Returns number of days until next ex-dividend date, or None if unknown.
    Negative means ex-div has already passed.
    """
    ex_date_str = (fund or {}).get("ex_div_date")
    if not ex_date_str:
        return None
    try:
        ex = datetime.date.fromisoformat(ex_date_str)
        return (ex - datetime.date.today()).days
    except Exception:
        return None

def is_near_ex_div(fund):
    """True if ex-dividend date is within EX_DIV_HOLD_DAYS days (don't sell yet)."""
    days = days_to_ex_div(fund)
    return days is not None and 0 <= days <= EX_DIV_HOLD_DAYS


# ─── Rotation logic ───────────────────────────────────────────────────────────
def find_rotation_pair(port, buy_candidates, prices, state, today_str):
    """
    When portfolio is full, check whether any holding should be swapped for a
    better opportunity.

    A holding is eligible to be rotated OUT if ALL of:
      1. Held >= ROTATION_MIN_HOLD_DAYS calendar days
      2. Current score <= ROTATION_HELD_SCORE_MAX (+1) — momentum has faded
      3. Unrealised loss is <= ROTATION_MAX_LOSS_PCT (3%) — don't sell deep losers

    A new stock is eligible to rotate IN if ALL of:
      1. Score >= BUY_SCORE_MIN (+2) — full buy signal
      2. Passes fundamental filter
      3. Not in rotation cooldown (wasn't rotated out in last ROTATION_COOLDOWN_DAYS)
      4. Score strictly > held stock score (genuine upgrade)

    Returns (held_info dict, buy_result dict) or (None, None).
    Only one pair is returned per call (max 1 rotation per scan enforced by caller).
    """
    today = datetime.date.today()

    # Collect rotation-out candidates (weakest holdings first)
    weak_holdings = []
    for ticker, h in port["holdings"].items():
        entry_date = datetime.date.fromisoformat(h["entry_date"])
        days_held  = (today - entry_date).days
        if days_held < ROTATION_MIN_HOLD_FAST:
            continue                               # under 3 days — never rotate regardless

        px      = prices.get(ticker, h["avg_cost"])
        pnl_pct = (px - h["avg_cost"]) / h["avg_cost"]
        if pnl_pct < -ROTATION_MAX_LOSS_PCT:
            continue                               # too deep in red — protect capital

        sig_state  = state.get(ticker, {})
        sig_state  = sig_state if isinstance(sig_state, dict) else {}
        curr_score = sig_state.get("score", 2)
        held_comp  = sig_state.get("comp_score", 10.0)

        # Two ways a holding qualifies to rotate out:
        #   1. Tech score has faded to <= +1  (original rule)
        #   2. Composite score is below floor (new rule — catches weak fundamentals
        #      even when tech score is still +2, e.g. comp < 7.2)
        score_weak = curr_score <= ROTATION_HELD_SCORE_MAX
        comp_weak  = held_comp  <  ROTATION_COMP_FLOOR
        if not (score_weak or comp_weak):
            continue                               # still strong on both measures

        # Ex-dividend guard: don't rotate out if ex-div is imminent
        held_fund = sig_state.get("fund", {})
        if is_near_ex_div(held_fund):
            d = days_to_ex_div(held_fund)
            print(f"  ⏳ Skip rotation of {ticker}: ex-div in {d}d — holding for dividend")
            continue

        weak_holdings.append({
            "ticker":    ticker,
            "h":         h,
            "px":        px,
            "pnl_pct":   pnl_pct,
            "days_held": days_held,
            "score":     curr_score,
            "comp_score": held_comp,
        })

    if not weak_holdings:
        return None, None

    # Sort: lowest composite score first, then worst P&L — rotate out the weakest first
    weak_holdings.sort(key=lambda x: (x["comp_score"], x["pnl_pct"]))

    # Rotation cooldown: tickers that were recently rotated out
    rotated_out = state.get("_rotated_out", {})
    allowed_buys = []
    for r in buy_candidates:
        if r["ticker"] not in rotated_out:
            allowed_buys.append(r)
            continue
        out_date = datetime.date.fromisoformat(rotated_out[r["ticker"]])
        if (today - out_date).days >= ROTATION_COOLDOWN_DAYS:
            allowed_buys.append(r)               # cooldown expired — eligible again

    if not allowed_buys:
        return None, None

    # Sort buy candidates: highest composite score first
    allowed_buys.sort(key=lambda x: -x.get("comp_score", 0))

    # Match: find first (weak holding, strong buy) pair where the upgrade is real.
    #
    # Two-tier hold rule (unchanged):
    #   days_held >= 5  →  incoming score +2 or +3 both allowed
    #   days_held 3-4   →  ONLY incoming score +3 (Strong Buy) qualifies
    #
    # Composite-score upgrade rule (new):
    #   Incoming comp_score must exceed held comp_score by ROTATION_COMP_MIN_GAIN (0.8).
    #   This prevents churning over tiny differences.
    for held in weak_holdings:
        for buy_r in allowed_buys:
            # Tech-score must be strictly better OR comp upgrade must be large enough
            tech_upgrade = buy_r["score"] > held["score"]
            comp_upgrade = buy_r.get("comp_score", 0) >= held["comp_score"] + ROTATION_COMP_MIN_GAIN

            if not (tech_upgrade or comp_upgrade):
                continue                          # neither dimension is a real upgrade

            if (held["days_held"] < ROTATION_MIN_HOLD_DAYS
                    and buy_r.get("comp_score", 0) < ROTATION_FAST_COMP):
                continue                          # fast-track only for comp >= 8.5

            gain = buy_r.get("comp_score", 0) - held["comp_score"]
            print(f"  🔄 Rotation candidate: sell {held['ticker']} (comp {held['comp_score']:.1f}) "
                  f"→ buy {buy_r['ticker']} (comp {buy_r.get('comp_score',0):.1f}, gain +{gain:.2f})")
            return held, buy_r

    return None, None


def build_rotation_alert(sell_trade, buy_trade, buy_result, port, prices):
    """LINE alert specifically for a rotation swap."""
    total  = portfolio_value(port, prices)
    start  = port["capital"]
    pnl_t  = total - start
    ps     = "+" if pnl_t >= 0 else ""

    pnl_sell = sell_trade.get("pnl", 0)
    ps2      = "+" if pnl_sell >= 0 else ""

    lines = [
        "🔄 ROTATION EXECUTED  —  {0}  {1}".format(
            time_str(), datetime.date.today().strftime("%d %b %Y")),
        "─" * 34,
        "",
        "🔴 SOLD   {0}".format(sell_trade["name"]),
        "   Reason : Score faded — {0}".format(sell_trade["reason"]),
        "   Price  : ฿{0:,.2f}".format(sell_trade["price"]),
        "   P&L    : {0}฿{1:,.0f}".format(ps2, pnl_sell),
        "",
        "🟢 BOUGHT {0}".format(buy_trade["name"]),
        "   Price  : ฿{0:,.2f}".format(buy_trade["price"]),
        "   Shares : {0:,d}  ({1:,d} lots)".format(
            buy_trade["shares"], buy_trade["shares"] // 100),
        "   Value  : ฿{0:,.0f}".format(buy_trade["value"]),
    ]

    if buy_result:
        fs      = buy_result.get("fund_score", 5.0)
        cs      = buy_result.get("comp_score", calc_composite_score(buy_result["score"], fs))
        rsi_s   = buy_result.get("rsi_sig",   0)
        trend_s = buy_result.get("trend_sig", buy_result.get("ma_sig", 0))
        macd_s  = buy_result.get("macd_sig",  0)
        adx_s   = buy_result.get("adx_sig",   0)
        vol_s   = buy_result.get("vol_sig",   0)
        lines += [
            "",
            "📊 New position scores",
            "   Technical  : {0:+d}/5  ({1}RSI {2}EMA {3}MACD {4}ADX {5}Vol  RSI={6:.0f})".format(
                buy_result["score"],
                "▲" if rsi_s   > 0 else ("▼" if rsi_s   < 0 else "─"),
                "▲" if trend_s > 0 else ("▼" if trend_s < 0 else "─"),
                "▲" if macd_s  > 0 else ("▼" if macd_s  < 0 else "─"),
                "▲" if adx_s   > 0 else ("▼" if adx_s   < 0 else "─"),
                "▲" if vol_s   > 0 else ("▼" if vol_s   < 0 else "─"),
                buy_result["rsi"],
            ),
            "   Fundamental : {0:.1f}/10".format(fs),
            "   Composite   : {0:.1f}/10".format(cs),
        ]
        fund = buy_result.get("fund", {})
        if fund:
            lines += [
                "   P/E {0}  PBV {1}  ROE {2}  D/E {3}  Div {4}".format(
                    "{:.1f}".format(fund["pe"])   if fund.get("pe")   else "N/A",
                    "{:.2f}".format(fund["pbv"])  if fund.get("pbv")  else "N/A",
                    "{:.0%}".format(fund["roe"])  if fund.get("roe")  else "N/A",
                    "{:.2f}x".format(fund["de_ratio"]) if fund.get("de_ratio") is not None else "N/A",
                    "✅" if fund.get("has_div") else "⚠️",
                ),
            ]

    lines += [
        "",
        "💼 Portfolio after rotation",
        "   Total : ฿{0:,.0f}  ({1}{2:.2f}%)".format(
            total, ps, pnl_t / start * 100),
        "   Cash  : ฿{0:,.0f}".format(port["cash"]),
        "   Held  : {0}/{1} stocks".format(len(port["holdings"]), MAX_POSITIONS),
        "",
        "─" * 34,
        "⚠️ Educational only. Not financial advice.",
    ]
    return "\n".join(lines)


# ─── Parallel stock analysis ──────────────────────────────────────────────────
def analyze(name, ticker):
    try:
        # Use Ticker.history() instead of yf.download() — download() mixes up
        # prices when called in parallel for Thai .BK tickers (known yfinance bug).
        df = yf.Ticker(ticker).history(period="{0}d".format(LOOKBACK),
                                        auto_adjust=True)
        if df is None or df.empty or "Close" not in df.columns:
            return {"ticker": ticker, "name": name, "error": "No data"}

        c = df["Close"].dropna()
        if len(c) < max(SMA_LONG + 5, 60):
            return {"ticker": ticker, "name": name, "error": "Not enough data"}

        rsi_v             = calc_rsi(c, RSI_PERIOD)
        ema_short_v       = calc_sma(c, SMA_SHORT)       # 20-period EMA (fast trend)
        ema_long_v        = calc_sma(c, SMA_LONG)        # 50-period EMA (slow trend)
        macd_v, msig_v    = calc_macd(c)
        adx_v, dip_v, dim_v = calc_adx(df)
        vol_20d           = calc_vol_20d(c)
        atr_v             = calc_atr(df, ATR_PERIOD)
        price             = float(c.iloc[-1])
        prev              = float(c.iloc[-2])
        pct               = (price - prev) / prev * 100

        # Volume surge score: bullish if vol spike on up-day, bearish if down-day
        avg_vol   = float(df["Volume"].rolling(20).mean().iloc[-1]) if "Volume" in df.columns else 0.0
        today_vol = float(df["Volume"].iloc[-1])          if "Volume" in df.columns else 0.0
        price_chg = price - prev
        if avg_vol > 0 and today_vol > VOL_SURGE_R * avg_vol:
            vol_s = 1 if price_chg > 0 else -1
        else:
            vol_s = 0

        sc, label, rs, trend, mc, ad, vs = score_signals(
            rsi_v, price, ema_short_v, ema_long_v, macd_v, msig_v,
            adx_v, dip_v, dim_v, vol_s)

        # Fundamental check (only fetch if stock is a BUY candidate — saves time)
        fund         = {}
        fund_ok      = True
        fund_fails   = []
        fund_summary = "N/A"
        fund_score   = 5.0   # neutral default when not fetched
        if sc >= BUY_SCORE_MIN and FUND_ENABLED:
            fund = fetch_fundamentals(ticker)
            fund_ok, fund_fails, fund_summary = check_fundamentals(fund)
            fund_score = calc_fundamental_score(fund)

        comp_score = calc_composite_score(sc, fund_score)

        return {
            "ticker":    ticker,       "name":      name,        "error":     None,
            "price":     price,        "pct":       pct,
            "rsi":       rsi_v,        "ema_short": ema_short_v, "ema_long":  ema_long_v,
            "macd":      macd_v,       "msig":      msig_v,
            "adx":       adx_v,        "di_plus":   dip_v,       "di_minus":  dim_v,
            "vol_20d":   vol_20d,      "avg_volume": avg_vol,
            "atr":       atr_v,
            "score":     sc,           "signal":    label,
            "rsi_sig":   rs,           "trend_sig": trend,       "macd_sig":  mc,
            "adx_sig":   ad,           "vol_sig":   vs,
            # backward-compat alias so rotation alerts still work
            "ma_sig":    trend,
            "fund":      fund,         "fund_ok":   fund_ok,
            "fund_fails": fund_fails,  "fund_summary": fund_summary,
            "fund_score": fund_score,  "comp_score":   comp_score,
        }
    except Exception as e:
        return {"ticker": ticker, "name": name, "error": str(e)[:60]}

# ─── Trade execution ──────────────────────────────────────────────────────────
def execute_buy(port, r):
    """
    Buy stock r with sector concentration, liquidity, and volatility-tiered sizing checks.
    Returns trade dict or None.
    """
    cash_floor = INITIAL_CAPITAL * CASH_FLOOR_PCT
    avail      = port["cash"] - cash_floor
    if avail <= 0:
        return None
    if r["ticker"] in port["holdings"]:
        return None
    if len(port["holdings"]) >= MAX_POSITIONS:
        return None

    # ── Sector concentration check ────────────────────────────────────────────
    sector = SECTOR_MAP.get(r["ticker"], "OTHER")
    if SECTOR_ENABLED and sector not in ("OTHER", "INDEX"):
        sector_count = sum(
            1 for t in port["holdings"]
            if SECTOR_MAP.get(t, "OTHER") == sector
        )
        if sector_count >= SECTOR_MAX:
            print(f"  ⛔ Sector cap ({sector}): {r['name']} blocked — "
                  f"already {sector_count}/{SECTOR_MAX} in sector")
            return None

    price  = r["price"]
    n_free = max(1, MAX_POSITIONS - len(port["holdings"]))

    # ── Volatility-tiered position sizing ─────────────────────────────────────
    vol_20d = r.get("vol_20d", 0.02)
    if vol_20d > VOL_HIGH_THRESH:
        size_mult = VOL_HIGH_MULT   # high vol → smaller position (less risk)
    elif vol_20d < VOL_LOW_THRESH:
        size_mult = VOL_LOW_MULT    # low vol → larger position (stable stock)
    else:
        size_mult = 1.0             # normal vol → standard allocation

    alloc  = min(avail / n_free, INITIAL_CAPITAL / MAX_POSITIONS * 1.5) * size_mult
    shares = int(alloc / price / LOT_SIZE) * LOT_SIZE

    # ── Liquidity check — cap at 10% of avg daily volume ─────────────────────
    avg_vol = r.get("avg_volume", 0) or 0
    if LIQUIDITY_ENABLED and avg_vol > 0:
        max_liq_shares = int(avg_vol * MAX_POS_PCT_VOL / LOT_SIZE) * LOT_SIZE
        if max_liq_shares > 0 and shares > max_liq_shares:
            print(f"  ⚠  Liquidity cap: {r['name']} trimmed "
                  f"{shares:,d}→{max_liq_shares:,d}sh "
                  f"(≤{MAX_POS_PCT_VOL:.0%} of avg vol {avg_vol:,.0f})")
            shares = max_liq_shares

    if shares <= 0:
        shares = LOT_SIZE
    cost = shares * price * (1 + TX_COST)
    if cost > avail:
        shares = int((avail / (price * (1 + TX_COST))) / LOT_SIZE) * LOT_SIZE
        cost   = shares * price * (1 + TX_COST)
    if shares <= 0 or cost > avail:
        return None

    # ATR-based stop price: entry − ATR_MULTIPLIER × ATR(14)
    atr_val  = r.get("atr", 0) or 0
    atr_stop = round(price - ATR_MULTIPLIER * atr_val, 2) if atr_val > 0 \
               else round(price * (1 - ATR_FALLBACK_PCT), 2)

    today = datetime.date.today().isoformat()
    port["cash"] -= cost
    port["holdings"][r["ticker"]] = {
        "name":        r["name"],
        "shares":      shares,
        "avg_cost":    round(price, 2),
        "entry_date":  today,
        "entry_score": r["score"],
        "atr":         round(atr_val, 4),
        "atr_stop":    atr_stop,
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
    """Buy top-10 BUY signals on the very first run — fundamentals required."""
    candidates = [r for r in ok_results if r["score"] >= BUY_SCORE_MIN]

    # Fetch fundamentals for all BUY candidates (if not already fetched)
    for r in candidates:
        if "fund_ok" not in r:
            fund = fetch_fundamentals(r["ticker"])
            r["fund_ok"], r["fund_fails"], r["fund_summary"] = check_fundamentals(fund)

    # Only buy stocks that pass fundamental filter
    passed  = [r for r in candidates if r.get("fund_ok", True)]
    skipped = [r for r in candidates if not r.get("fund_ok", True)]

    if skipped:
        print("  Fundamental filter blocked {0} stock(s): {1}".format(
            len(skipped),
            ", ".join("{0}({1})".format(r["name"], ",".join(r.get("fund_fails",[])))
                      for r in skipped)))

    # Rank by composite score (best fundamental quality among equal tech scores)
    for r in passed:
        if "fund_score" not in r:
            r["fund_score"]  = calc_fundamental_score(r.get("fund", {}))
        r["comp_score"] = calc_composite_score(r["score"], r["fund_score"])
    buys = sorted(passed, key=lambda x: -x["comp_score"])[:10]

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

    if is_buy and trade.get("ticker") in (port.get("holdings") or {}):
        h = port["holdings"].get(trade["ticker"], {})
        atr_stop = h.get("atr_stop")
        if atr_stop:
            lines.append("   ATR stop: ฿{0:.2f}  ({1:.1f}% below entry)".format(
                atr_stop,
                (1 - atr_stop / trade["price"]) * 100))

    if result:
        fs = result.get("fund_score", 5.0)
        cs = result.get("comp_score", calc_composite_score(result["score"], fs))
        rsi_s   = result.get("rsi_sig",   0)
        trend_s = result.get("trend_sig", result.get("ma_sig", 0))
        macd_s  = result.get("macd_sig",  0)
        adx_s   = result.get("adx_sig",   0)
        vol_s   = result.get("vol_sig",   0)
        lines += [
            "",
            "📊 Scores",
            "   Technical  : {0:+d}/5  ({1}RSI {2}EMA {3}MACD {4}ADX {5}Vol  RSI={6:.0f})".format(
                result["score"],
                "▲" if rsi_s   > 0 else ("▼" if rsi_s   < 0 else "─"),
                "▲" if trend_s > 0 else ("▼" if trend_s < 0 else "─"),
                "▲" if macd_s  > 0 else ("▼" if macd_s  < 0 else "─"),
                "▲" if adx_s   > 0 else ("▼" if adx_s   < 0 else "─"),
                "▲" if vol_s   > 0 else ("▼" if vol_s   < 0 else "─"),
                result["rsi"],
            ),
            "   Fundamental : {0:.1f}/10".format(fs),
            "   Composite   : {0:.1f}/10  (Tech {1:.0f}% · Fund {2:.0f}%)".format(
                cs, WEIGHT_TECH * 100, WEIGHT_FUND * 100),
        ]
        # Show fundamental data if available
        fund = result.get("fund", {})
        if fund:
            pe     = fund.get("pe")
            pbv    = fund.get("pbv")
            roe    = fund.get("roe")
            div    = fund.get("has_div")
            de     = fund.get("de_ratio")
            eg     = fund.get("eps_growth")
            fcf_y  = fund.get("fcf_yield")
            lines += [
                "",
                "📈 Fundamentals",
                "   P/E    : {0}".format("{:.1f}".format(pe)   if pe  is not None else "N/A"),
                "   P/BV   : {0}".format("{:.2f}".format(pbv)  if pbv is not None else "N/A"),
                "   ROE    : {0}".format("{:.1%}".format(roe)   if roe is not None else "N/A"),
                "   D/E    : {0}".format("{:.2f}x".format(de)  if de  is not None else "N/A"),
                "   EPS Gw : {0}".format("{:+.1%}".format(eg)  if eg  is not None else "N/A"),
                "   FCF Yld: {0}".format("{:.1%}".format(fcf_y) if fcf_y is not None else "N/A"),
                "   Div    : {0}".format("Yes ✅" if div else "No ⚠️"),
            ]

    lines += [
        "",
        "💼 Portfolio (real-time)",
        "   Total : \u0e3f{0:,.0f}  ({1}{2:.2f}%)".format(
            total, ps, pnl_t / start * 100),
        "   Cash  : \u0e3f{0:,.0f}".format(port["cash"]),
        "   Held  : {0}/{1} stocks".format(len(port["holdings"]), MAX_POSITIONS),
    ]

    # Show live value of each holding
    if port["holdings"]:
        lines.append("")
        lines.append("📈 Live holdings:")
        for ticker, h in sorted(port["holdings"].items(),
                                key=lambda x: -(x[1]["shares"] *
                                                prices.get(x[0], x[1]["avg_cost"]))):
            px     = prices.get(ticker, h["avg_cost"])
            mval   = h["shares"] * px
            upnl   = mval - h["shares"] * h["avg_cost"]
            upct   = (px - h["avg_cost"]) / h["avg_cost"] * 100
            icon   = "▲" if upnl >= 0 else "▼"
            ps2    = "+" if upnl >= 0 else ""
            lines.append("  {0} {1:8s} \u0e3f{2:,.2f}  val:\u0e3f{3:,.0f}  {4}{5:.1f}%".format(
                icon, h["name"], px, mval, ps2, upct))

    lines += [
        "",
        "─" * 32,
        "⚠️ Educational only. Not financial advice.",
    ]
    return "\n".join(lines)

def build_status_update(results, port, prices):
    """30-minute real-time portfolio snapshot."""
    total    = portfolio_value(port, prices)
    start    = port["capital"]
    pnl_tot  = total - start
    ps       = "+" if pnl_tot >= 0 else ""
    n_buy    = sum(1 for r in results if r.get("score", 0) >= BUY_SCORE_MIN)
    n_sell   = sum(1 for r in results if r.get("score", 0) <= SELL_SCORE_MAX)

    # Per-holding real-time values
    holdings_data = []
    invested = 0.0
    unreal_pnl = 0.0
    for ticker, h in port["holdings"].items():
        px       = prices.get(ticker, h["avg_cost"])
        mkt_val  = h["shares"] * px
        cost_val = h["shares"] * h["avg_cost"]
        upnl     = mkt_val - cost_val
        upnl_pct = (px - h["avg_cost"]) / h["avg_cost"] * 100
        invested    += cost_val
        unreal_pnl  += upnl
        holdings_data.append({
            "name": h["name"], "ticker": ticker,
            "shares": h["shares"],
            "avg_cost": h["avg_cost"],
            "price": px,
            "mkt_val": mkt_val,
            "upnl": upnl,
            "upnl_pct": upnl_pct,
        })
    # Sort by market value descending
    holdings_data.sort(key=lambda x: -x["mkt_val"])

    # Top 3 movers from all scanned stocks
    movers = sorted(
        [r for r in results if not r.get("error")],
        key=lambda x: abs(x.get("pct", 0)), reverse=True
    )[:3]

    lines = [
        "📊 LIVE PORTFOLIO  {0}  {1}".format(
            time_str(), datetime.date.today().strftime("%d %b %Y")),
        "─" * 32,
        "",
        "💼 Total Value  : \u0e3f{0:,.0f}".format(total),
        "   Start Capital: \u0e3f{0:,.0f}".format(start),
        "   Total P&L    : {0}\u0e3f{1:,.0f}  ({0}{2:.2f}%)".format(
            ps, abs(pnl_tot), abs(pnl_tot) / start * 100),
        "   Unrealised   : {0}\u0e3f{1:,.0f}".format(
            "+" if unreal_pnl >= 0 else "", unreal_pnl),
        "   Cash         : \u0e3f{0:,.0f}".format(port["cash"]),
        "   Positions    : {0}/{1}".format(len(port["holdings"]), MAX_POSITIONS),
        "",
        "─" * 32,
    ]

    if holdings_data:
        lines.append("📈 Holdings (live prices):")
        lines.append("")
        for d in holdings_data:
            icon   = "▲" if d["upnl_pct"] >= 0 else "▼"
            ps2    = "+" if d["upnl"] >= 0 else ""
            lines.append("{0} {1}".format(icon, d["name"]))
            lines.append("   Price  : \u0e3f{0:,.2f}  (entry \u0e3f{1:,.2f})".format(
                d["price"], d["avg_cost"]))
            lines.append("   Shares : {0:,d} ({1:,d} lots)".format(
                d["shares"], d["shares"] // 100))
            lines.append("   Value  : \u0e3f{0:,.0f}".format(d["mkt_val"]))
            lines.append("   P&L    : {0}\u0e3f{1:,.0f}  ({0}{2:.1f}%)".format(
                ps2, abs(d["upnl"]), abs(d["upnl_pct"])))
            lines.append("")
    else:
        lines.append("No holdings — cash only.")
        lines.append("")

    lines.append("─" * 32)
    lines.append("🔍 Market signals: 🟢{0} BUY  🔴{1} SELL  ({2} scanned)".format(
        n_buy, n_sell, len(results)))

    if movers:
        lines.append("📊 Top movers: " + "  ".join(
            "{0} {1}{2:.1f}%".format(
                r["name"], "+" if r["pct"] >= 0 else "", r["pct"])
            for r in movers))

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

# ─── Market regime ────────────────────────────────────────────────────────────
def get_market_regime():
    """
    Checks whether SET Index is above or below its 200-day moving average.

    Returns:
        regime  : "BULL" | "BEAR"
        price   : latest SET Index close (float or None)
        ma200   : 200-day MA value (float or None)
        pct_gap : how far price is above/below MA, as % (positive = above)

    In BEAR regime the caller should suspend all BUY orders and rotations —
    only sells and stop-losses should execute.
    """
    if not REGIME_ENABLED:
        return "BULL", None, None, 0.0
    try:
        df = yf.Ticker(REGIME_TICKER).history(period="14mo", auto_adjust=True)
        if df.empty or len(df) < REGIME_MA_PERIOD:
            print("  ⚠  Regime check: not enough SET data — defaulting to BULL")
            return "BULL", None, None, 0.0
        c     = df["Close"].dropna()
        price = float(c.iloc[-1])
        ma200 = float(c.rolling(REGIME_MA_PERIOD).mean().iloc[-1])
        gap   = (price - ma200) / ma200 * 100
        regime = "BULL" if price > ma200 else "BEAR"
        return regime, price, ma200, gap
    except Exception as e:
        print(f"  ⚠  Regime check failed ({e}) — defaulting to BULL")
        return "BULL", None, None, 0.0


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

    # ── Market regime check ───────────────────────────────────────────────────
    regime, set_px, ma200, gap = get_market_regime()
    if set_px:
        gap_str = "{0:+.1f}%".format(gap)
        print("\n  📊 Market Regime: {0}  SET {1:.0f}  MA200 {2:.0f}  ({3})".format(
            "🟢 BULL" if regime == "BULL" else "🔴 BEAR", set_px, ma200, gap_str))
        if regime == "BEAR":
            print("  ⚠  BEAR REGIME — all BUY orders and rotations SUSPENDED")
            print("  ⚠  Only stop-losses and signal-based SELLs will execute")
    else:
        print("\n  📊 Market Regime: could not fetch SET Index — defaulting to BULL")

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
                "fund_score":   r.get("fund_score", 5.0),
                "comp_score":   r.get("comp_score", calc_composite_score(r["score"], 5.0)),
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

    # Dividend income check (once per day)
    print("\nChecking dividend payments for {0} holdings...".format(
        len(port["holdings"])))
    div_trades = check_and_credit_dividends(port, prices)
    if div_trades:
        save_portfolio(port)
        div_msg = build_dividend_alert(div_trades, port, prices)
        ok_div  = send_line(div_msg)
        print("  LINE dividend alert: {0}".format("✅ sent" if ok_div else "❌ failed"))

    # ── ATR stop retroactive migration ───────────────────────────────────────
    # For holdings entered before ATR tracking, set atr_stop now using current ATR.
    sig_map_early = {r["ticker"]: r for r in ok}
    for ticker, h in port["holdings"].items():
        if "atr_stop" not in h:
            r = sig_map_early.get(ticker)
            atr_val = r.get("atr", 0) if r else 0
            if atr_val and atr_val > 0:
                h["atr"]      = round(atr_val, 4)
                h["atr_stop"] = round(h["avg_cost"] - ATR_MULTIPLIER * atr_val, 2)
            else:
                h["atr_stop"] = round(h["avg_cost"] * (1 - ATR_FALLBACK_PCT), 2)
            print("  📐 ATR stop set for {0}: ฿{1:.2f}".format(
                h["name"], h["atr_stop"]))
    save_portfolio(port)

    # ── ATR stop-loss check ───────────────────────────────────────────────────
    print("\nChecking ATR stop-losses on {0} holdings...".format(
        len(port["holdings"])))
    for ticker in list(port["holdings"].keys()):
        h  = port["holdings"].get(ticker)
        if not h:
            continue
        px       = prices.get(ticker, h["avg_cost"])
        atr_stop = h.get("atr_stop") or h["avg_cost"] * (1 - ATR_FALLBACK_PCT)
        if px <= atr_stop:
            drop = (1 - px / h["avg_cost"]) * 100
            print("  🛑 ATR STOP-LOSS {0}  ฿{1:.2f}  stop ฿{2:.2f}  (-{3:.1f}%)".format(
                h["name"], px, atr_stop, drop))
            trade = execute_sell(
                port, ticker, px,
                "ATR stop ฿{0:.2f} (-{1:.1f}%)".format(atr_stop, drop))
            if trade:
                trades_executed.append((trade, None))
                alerted.add(ticker)

    # Signal-change detection — pass 1: update state for all stocks
    sig_map      = {r["ticker"]: r for r in ok}
    sell_pending = []   # sells execute immediately, order doesn't matter
    buy_pending  = []   # buys sorted by composite score — best quality first

    for ticker, r in sig_map.items():
        score      = r["score"]
        prev       = prev_state.get(ticker, {})
        prev_score = prev.get("score", 0) if isinstance(prev, dict) else 0

        new_state[ticker] = {
            "score":        score,
            "signal":       r["signal"],
            "price":        r["price"],
            "fund_score":   r.get("fund_score", 5.0),
            "comp_score":   r.get("comp_score", calc_composite_score(score, 5.0)),
            "fund":         r.get("fund", prev.get("fund", {}) if isinstance(prev, dict) else {}),
            "vol_20d":      r.get("vol_20d", 0.02),
            "avg_volume":   r.get("avg_volume", 0),
            "buy_alerted":  prev.get("buy_alerted",  False) if isinstance(prev, dict) else False,
            "sell_alerted": prev.get("sell_alerted", False) if isinstance(prev, dict) else False,
        }

        fund_ok = r.get("fund_ok", True)

        # Queue BUY candidates
        if (score >= BUY_SCORE_MIN
                and prev_score <= BUY_PREV_MAX
                and ticker not in alerted
                and not new_state[ticker].get("buy_alerted")):
            if fund_ok:
                buy_pending.append(r)
            else:
                print("  SKIP {0:12s} — failed fundamentals: {1}".format(
                    r["name"], ", ".join(r.get("fund_fails", []))))

        # Queue SELL candidates
        elif (score <= SELL_SCORE_MAX
                and prev_score >= SELL_PREV_MIN
                and ticker not in alerted
                and not new_state[ticker].get("sell_alerted")
                and ticker in port["holdings"]):
            # Ex-dividend guard: defer sell if ex-div is imminent
            held_fund = new_state[ticker].get("fund", {}) if isinstance(new_state.get(ticker), dict) else {}
            if is_near_ex_div(held_fund):
                d = days_to_ex_div(held_fund)
                print(f"  ⏳ Defer SELL {r['name']}: ex-div in {d}d — waiting for dividend")
            else:
                sell_pending.append(r)

    # Pass 2: execute sells first (free up slots / cash)
    for r in sell_pending:
        trade = execute_sell(port, r["ticker"], r["price"], r["signal"])
        if trade:
            print("  SELL {0:12s} {1:,d}sh @ \u0e3f{2:.2f}  P&L: \u0e3f{3:,.0f}".format(
                r["name"], trade["shares"], r["price"], trade["pnl"]))
            trades_executed.append((trade, r))
            alerted.add(r["ticker"])
            new_state[r["ticker"]]["sell_alerted"] = True
            new_state[r["ticker"]]["buy_alerted"]  = False

    # Pass 3: execute buys — gated by regime (BEAR = no buys)
    if regime == "BEAR":
        if buy_pending:
            print("\n  🔴 BEAR regime — {0} buy signal(s) suppressed".format(
                len(buy_pending)))
        buy_pending = []

    buy_pending.sort(key=lambda x: -x.get("comp_score", 0))
    for r in buy_pending:
        if r["ticker"] in alerted:
            continue
        trade = execute_buy(port, r)
        if trade:
            print("  BUY  {0:12s} {1:,d}sh @ \u0e3f{2:.2f}  "
                  "Tech:{3:+d} Fund:{4:.1f}/10 Comp:{5:.1f}/10".format(
                r["name"], trade["shares"], r["price"],
                r["score"], r.get("fund_score", 5.0), r.get("comp_score", 0)))
            trades_executed.append((trade, r))
            alerted.add(r["ticker"])
            new_state[r["ticker"]]["buy_alerted"]  = True
            new_state[r["ticker"]]["sell_alerted"] = False

    # ── Portfolio rotation ────────────────────────────────────────────────────
    if regime == "BEAR" and ROTATION_ENABLED:
        print("\n  🔴 BEAR regime — rotation suspended")

    if (ROTATION_ENABLED
            and regime == "BULL"                             # no rotation in bear market
            and len(port["holdings"]) >= MAX_POSITIONS
            and len(trades_executed) == 0):      # don't rotate on the same scan as a regular trade

        rot_date  = new_state.get("_rotation_date", "")
        rot_today = new_state.get("_rotation_count_today", 0)
        if rot_date != today_str:
            rot_today = 0                        # new day — reset counter

        if rot_today < ROTATION_MAX_PER_DAY:
            # Build rotation candidate list — lower bar than fresh buy:
            #   • Tech score >= ROTATION_IN_SCORE_MIN (+2, vs +3 for fresh buy)
            #   • Composite >= ROTATION_IN_COMP_MIN (7.0)
            #   • Use stored fund_score from state for accurate comp (not fresh-fetch default)
            rotation_buys = []
            for r in ok:
                if r["ticker"] in port["holdings"]:
                    continue
                if r["score"] < ROTATION_IN_SCORE_MIN:
                    continue
                # Enrich comp_score using stored fund data (more accurate than 5.0 default)
                stored = new_state.get(r["ticker"], {})
                if isinstance(stored, dict) and stored.get("fund_score"):
                    real_fund = stored["fund_score"]
                    r = dict(r)               # don't mutate the original
                    r["fund_score"]  = real_fund
                    r["comp_score"]  = calc_composite_score(r["score"], real_fund)
                    if stored.get("fund"):
                        r["fund"] = stored["fund"]
                if r.get("comp_score", 0) < ROTATION_IN_COMP_MIN:
                    continue
                rotation_buys.append(r)

            if rotation_buys:
                held_info, buy_r = find_rotation_pair(
                    port, rotation_buys, prices, new_state, today_str)

                if held_info and buy_r:
                    print("\n  ROTATION: {0} (score {1}) → {2} (score {3})".format(
                        held_info["h"]["name"], held_info["score"],
                        buy_r["name"], buy_r["score"]))

                    sell_reason = "Rotation → {0} | held {1}d score {2:+d}→{3:+d}".format(
                        buy_r["name"], held_info["days_held"],
                        held_info["score"], buy_r["score"])

                    sell_trade = execute_sell(
                        port, held_info["ticker"], held_info["px"], sell_reason)

                    if sell_trade:
                        buy_trade = execute_buy(port, buy_r)

                        if buy_trade:
                            # Track rotation state
                            rotated_out = dict(new_state.get("_rotated_out", {}))
                            rotated_out[held_info["ticker"]] = today_str
                            new_state["_rotated_out"]           = rotated_out
                            new_state["_rotation_date"]         = today_str
                            new_state["_rotation_count_today"]  = rot_today + 1

                            trades_executed.append((sell_trade, None))
                            trades_executed.append((buy_trade,  buy_r))

                            print("  ✅ Rotation complete: sold {0}, bought {1}".format(
                                sell_trade["name"], buy_trade["name"]))
                        else:
                            # Buy failed — undo the sell by re-adding the holding
                            print("  ⚠ Rotation buy failed — reversing sell")
                            port["cash"] -= sell_trade["shares"] * sell_trade["price"] * (1 - TX_COST)
                            port["holdings"][held_info["ticker"]] = held_info["h"]
                            port["trades"].pop()

    # Save regime info to state (dashboard reads this)
    new_state["_regime"] = {
        "regime":  regime,
        "set_px":  round(set_px, 2) if set_px else None,
        "ma200":   round(ma200,  2) if ma200  else None,
        "gap_pct": round(gap, 2)    if gap    else 0.0,
        "updated": datetime.datetime.now(BKK_OFFSET).strftime("%Y-%m-%d %H:%M"),
    }

    # Save portfolio + state + daily snapshot
    save_portfolio(port, prices)
    save_signal_state(new_state)
    save_daily_snapshot(port, prices)

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
        # Check if this is a rotation pair (sell + buy together)
        is_rotation = (
            len(trades_executed) == 2
            and trades_executed[0][0]["action"] == "SELL"
            and trades_executed[1][0]["action"] == "BUY"
            and "Rotation" in trades_executed[0][0].get("reason", "")
        )
        if is_rotation:
            sell_trade, _   = trades_executed[0]
            buy_trade,  res = trades_executed[1]
            msg    = build_rotation_alert(sell_trade, buy_trade, res, port, prices)
            ok_snd = send_line(msg)
            print("  LINE rotation alert: {0}".format("✅ sent" if ok_snd else "❌ failed"))
            if ok_snd:
                msgs_sent += 1
        else:
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
