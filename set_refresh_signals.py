#!/usr/bin/env python3
"""
Thai SET — Quick Signal Refresher
──────────────────────────────────────────────────────────────────────────────
Fetches OHLCV data for ALL watchlist stocks and recalculates 5-indicator
technical scores using Friday's closing data.

Keeps existing fundamental scores from set_signal_state.json — no slow
fundamental API calls needed (runs in ~1–2 minutes instead of 5+).

Updates:
  • set_signal_state.json  — fresh tech scores + comp scores
  • set_dashboard.html     — live SIGNALS block baked in

Usage:
  python3.12 set_refresh_signals.py
  python3.11 set_refresh_signals.py
"""

import sys, os, json, re, datetime

def ensure_packages():
    import importlib, subprocess
    for pkg in ["yfinance", "pandas"]:
        try:
            importlib.import_module(pkg)
        except ImportError:
            print(f"  Installing {pkg}...")
            for cmd in [
                [sys.executable, "-m", "pip", "install", pkg, "-q"],
                [sys.executable, "-m", "pip", "install", pkg, "--user", "-q"],
                [sys.executable, "-m", "pip", "install", pkg, "--break-system-packages", "-q"],
            ]:
                try:
                    import subprocess as _sp
                    _sp.check_call(cmd, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                    break
                except Exception:
                    continue

ensure_packages()

import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── Paths ────────────────────────────────────────────────────────────────────
DIR            = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH    = os.path.join(DIR, "set_config.json")
STATE_PATH     = os.path.join(DIR, "set_signal_state.json")
DASHBOARD_PATH = os.path.join(DIR, "set_dashboard.html")

# ─── Config ───────────────────────────────────────────────────────────────────
with open(CONFIG_PATH, encoding="utf-8") as f:
    cfg = json.load(f)

INSTRUMENTS  = [(i["name"], i["ticker"]) for i in cfg.get("instruments", [])]
RSI_PERIOD   = cfg.get("rsi_period", 14)
RSI_OB       = cfg.get("rsi_overbought", 70)
RSI_OS       = cfg.get("rsi_oversold", 30)
SMA_SHORT    = cfg.get("sma_short_period", 20)
SMA_LONG     = cfg.get("sma_long_period",  50)
ADX_PERIOD   = cfg.get("adx_period", 14)
VOL_SURGE_R  = cfg.get("volume_surge_ratio", 1.5)
MAX_WORKERS  = cfg.get("download_threads", 10)

_sw          = cfg.get("scoring_weights", {})
W_TECH       = _sw.get("technical",   0.6)
W_FUND       = _sw.get("fundamental", 0.4)
TECH_MAX     = 5
_FUND_MAX    = 19.0

BKK = datetime.timezone(datetime.timedelta(hours=7))

# ─── Indicators ───────────────────────────────────────────────────────────────
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
    high, low, close = df["High"], df["Low"], df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    up, down = high.diff().clip(lower=0), (-low.diff()).clip(lower=0)
    dm_p = up.where(up > down, 0.0)
    dm_m = down.where(down > up, 0.0)
    atr_s = tr.ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    di_p  = 100 * dm_p.ewm(alpha=1/n, min_periods=n, adjust=False).mean() / atr_s.replace(0, 1e-10)
    di_m  = 100 * dm_m.ewm(alpha=1/n, min_periods=n, adjust=False).mean() / atr_s.replace(0, 1e-10)
    dx    = 100 * (di_p - di_m).abs() / (di_p + di_m).replace(0, 1e-10)
    adx   = dx.ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    return float(adx.iloc[-1]), float(di_p.iloc[-1]), float(di_m.iloc[-1])

def calc_vol_20d(close):
    ret = close.pct_change().dropna()
    return float(ret.tail(20).std()) if len(ret) >= 20 else 0.02

def score_signals(rsi, close, ema_short, ema_long, macd, msig,
                  adx=0, di_plus=0, di_minus=0, vol_score=0):
    rs    = 1 if rsi < RSI_OS else (-1 if rsi > RSI_OB else 0)
    ms    = 1 if close > ema_long  else -1
    et    = 1 if ema_short > ema_long else -1
    trend = (1 if ms > 0 and et > 0 else (-1 if ms < 0 and et < 0 else 0))
    mc    = 1 if macd > msig else -1
    ad    = (1 if di_plus > di_minus else -1) if adx > 20 else 0
    sc    = max(-5, min(5, rs + trend + mc + ad + vol_score))
    sigs  = {
         5:"STRONG BUY", 4:"STRONG BUY", 3:"BUY",
         2:"BUY",        1:"WATCH",      0:"HOLD",
        -1:"WATCH",     -2:"SELL",      -3:"SELL",
        -4:"STRONG SELL",-5:"STRONG SELL",
    }
    return sc, sigs.get(sc, "HOLD"), rs, trend, mc, ad, vol_score

def calc_composite(tech_score, fund_score):
    tech_norm = (tech_score + TECH_MAX) / (2.0 * TECH_MAX) * 10.0
    return round(tech_norm * W_TECH + fund_score * W_FUND, 2)

# ─── Fetch + analyse one ticker ───────────────────────────────────────────────
def analyse(name, ticker, existing_state):
    try:
        df = yf.Ticker(ticker).history(period="6mo", auto_adjust=True)
        if df.empty or len(df) < SMA_LONG + 10:
            return None, f"{name}: not enough data"

        c      = df["Close"].dropna()
        price  = float(c.iloc[-1])
        prev   = float(c.iloc[-2])

        rsi_v            = calc_rsi(c, RSI_PERIOD)
        ema_s            = calc_sma(c, SMA_SHORT)
        ema_l            = calc_sma(c, SMA_LONG)
        macd_v, msig_v   = calc_macd(c)
        adx_v, dip, dim  = calc_adx(df)
        vol_20d          = calc_vol_20d(c)

        avg_vol   = float(df["Volume"].rolling(20).mean().iloc[-1]) if "Volume" in df.columns else 0.0
        today_vol = float(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0.0
        vol_s = 0
        if avg_vol > 0 and today_vol > VOL_SURGE_R * avg_vol:
            vol_s = 1 if price > prev else -1

        sc, label, rs, trend, mc, ad, vs = score_signals(
            rsi_v, price, ema_s, ema_l, macd_v, msig_v, adx_v, dip, dim, vol_s)

        # Keep existing fundamental data
        prev_s     = existing_state.get(ticker, {})
        fund_score = prev_s.get("fund_score", 5.0) if isinstance(prev_s, dict) else 5.0
        fund       = prev_s.get("fund", {})        if isinstance(prev_s, dict) else {}
        comp_score = calc_composite(sc, fund_score)

        return {
            "ticker":     ticker,
            "name":       name,
            "score":      sc,
            "signal":     label,
            "price":      round(price, 4),
            "rsi":        round(rsi_v, 1),
            "ema_short":  round(ema_s, 4),
            "ema_long":   round(ema_l, 4),
            "macd":       round(macd_v, 4),
            "macd_sig":   round(msig_v, 4),
            "adx":        round(adx_v, 1),
            "di_plus":    round(dip, 1),
            "di_minus":   round(dim, 1),
            "vol_20d":    round(vol_20d, 4),
            "avg_volume": round(avg_vol, 0),
            "rsi_sig":    rs,
            "trend_sig":  trend,
            "mc_sig":     mc,
            "adx_sig":    ad,
            "vol_sig":    vs,
            "fund_score": fund_score,
            "comp_score": comp_score,
            "fund":       fund,
            "buy_alerted":  prev_s.get("buy_alerted",  False) if isinstance(prev_s, dict) else False,
            "sell_alerted": prev_s.get("sell_alerted", False) if isinstance(prev_s, dict) else False,
        }, None
    except Exception as e:
        return None, f"{name}: {e}"


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    now = datetime.datetime.now(BKK)
    print("=" * 65)
    print("  Thai SET — Signal Refresh (Friday close)")
    print(f"  Run: {now.strftime('%Y-%m-%d %H:%M')} Bangkok")
    print("=" * 65)

    existing = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            existing = json.load(f)

    total   = len(INSTRUMENTS)
    results = {}
    errors  = []

    print(f"\nFetching {total} tickers (parallel, ~1–2 min)...\n")

    def fetch(args):
        name, ticker = args
        return ticker, *analyse(name, ticker, existing)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch, inst): inst for inst in INSTRUMENTS}
        done = 0
        for fut in as_completed(futures):
            done += 1
            ticker, result, err = fut.result()
            if result:
                results[ticker] = result
                name   = result["name"]
                sc     = result["score"]
                signal = result["signal"]
                comp   = result["comp_score"]
                rsi    = result["rsi"]
                adx    = result["adx"]
                trend_a = "▲" if result["trend_sig"] > 0 else ("▼" if result["trend_sig"] < 0 else "─")
                rsi_a   = "▲" if result["rsi_sig"]   > 0 else ("▼" if result["rsi_sig"]   < 0 else "─")
                macd_a  = "▲" if result["mc_sig"]    > 0 else ("▼" if result["mc_sig"]    < 0 else "─")
                adx_a   = "▲" if result["adx_sig"]   > 0 else ("▼" if result["adx_sig"]   < 0 else "─")
                vol_a   = "▲" if result["vol_sig"]   > 0 else ("▼" if result["vol_sig"]   < 0 else "─")
                print(f"  [{done:3d}/{total}] {name:10s} {sc:+d}/5  [{signal:11s}]  "
                      f"Comp:{comp:.1f}  RSI{rsi_a}{rsi:.0f} EMA{trend_a} MACD{macd_a} ADX{adx_a} Vol{vol_a}")
            else:
                errors.append(err)
                print(f"  [{done:3d}/{total}] ⚠  {err}")

    print(f"\n✅ Scored {len(results)}/{total} tickers  ({len(errors)} errors)")

    # ─── Update signal state ──────────────────────────────────────────────────
    new_state = dict(existing)
    for ticker, r in results.items():
        entry = new_state.get(ticker, {})
        if not isinstance(entry, dict):
            entry = {}
        entry.update({
            "score":      r["score"],
            "signal":     r["signal"],
            "price":      r["price"],
            "fund_score": r["fund_score"],
            "comp_score": r["comp_score"],
            "fund":       r["fund"],
            "vol_20d":    r["vol_20d"],
            "avg_volume": r["avg_volume"],
            "buy_alerted":  r["buy_alerted"],
            "sell_alerted": r["sell_alerted"],
        })
        new_state[ticker] = entry

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(new_state, f, indent=2, default=str)
    print("✅ Updated set_signal_state.json")

    # ─── Update dashboard HTML ────────────────────────────────────────────────
    if not os.path.exists(DASHBOARD_PATH):
        print("⚠  set_dashboard.html not found — skipping")
    else:
        with open(DASHBOARD_PATH, encoding="utf-8") as f:
            html = f.read()

        embed = {}
        for ticker, r in results.items():
            embed[ticker] = {
                "score":      r["score"],
                "signal":     r["signal"],
                "price":      r["price"],
                "fund_score": r["fund_score"],
                "comp_score": r["comp_score"],
                "fund":       {k: round(v, 4) if isinstance(v, float) else v
                               for k, v in r["fund"].items()} if r["fund"] else {},
                "buy_alerted":  r["buy_alerted"],
                "sell_alerted": r["sell_alerted"],
            }

        signals_json = json.dumps(embed, ensure_ascii=False, separators=(",", ":"))
        new_html, n = re.subn(
            r'const SIGNALS\s*=\s*\{.*?\};',
            f'const SIGNALS = {signals_json};',
            html, flags=re.DOTALL)

        # Also embed regime from state
        regime_data = existing.get("_regime", {})
        if regime_data:
            regime_json = json.dumps(regime_data, ensure_ascii=False, separators=(",", ":"))
            new_html = re.sub(
                r'const REGIME\s*=\s*\{.*?\};',
                f'const REGIME = {regime_json};',
                new_html, flags=re.DOTALL)

        if n == 0:
            print("⚠  Could not find SIGNALS block in HTML")
        else:
            ts = now.strftime("%Y-%m-%d %H:%M")
            new_html = new_html.replace("// Updated:", f"// Updated: {ts} Bangkok ·")
            with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
                f.write(new_html)
            print("✅ Updated set_dashboard.html")

    # ─── Summary table ────────────────────────────────────────────────────────
    portfolio_tickers = {
        "TISCO.BK","SIRI.BK","TU.BK","BAY.BK","KTC.BK",
        "TQM.BK","WHA.BK","JASIF.BK","CK.BK","AMATA.BK"
    }

    held   = [(t, r) for t, r in results.items() if t in portfolio_tickers]
    others = [(t, r) for t, r in results.items() if t not in portfolio_tickers]

    held.sort(key=lambda x: -x[1]["comp_score"])
    others.sort(key=lambda x: -x[1]["score"])

    def arrow(v):
        return "▲" if v > 0 else ("▼" if v < 0 else "─")

    def sig_bar(r):
        return (f"RSI{arrow(r['rsi_sig'])} "
                f"EMA{arrow(r['trend_sig'])} "
                f"MACD{arrow(r['mc_sig'])} "
                f"ADX{arrow(r['adx_sig'])} "
                f"Vol{arrow(r['vol_sig'])}")

    SEP = "─" * 80

    print(f"\n{'─'*80}")
    print("  YOUR PORTFOLIO — Friday close signals")
    print(SEP)
    print(f"  {'Stock':8}  {'Score':>6}  {'Signal':13}  {'Comp':>5}  {'RSI':>5}  Indicators")
    print(SEP)

    avg_costs = {
        "TISCO.BK":113.5,"SIRI.BK":1.41,"TU.BK":11.4,"BAY.BK":26.0,"KTC.BK":29.25,
        "TQM.BK":13.7,"WHA.BK":4.4,"JASIF.BK":6.7,"CK.BK":17.2,"AMATA.BK":20.5,
    }

    for ticker, r in held:
        name  = r["name"]
        sc    = r["score"]
        sig   = r["signal"]
        comp  = r["comp_score"]
        rsi   = r["rsi"]
        price = r["price"]
        avg   = avg_costs.get(ticker, price)
        pnl   = (price - avg) / avg * 100
        bar   = sig_bar(r)
        rot   = " ← ROT?" if (sc <= 1 or comp < 7.2) else ""
        sell  = " ← SELL!" if sc <= -2 else ""
        print(f"  {name:8}  {sc:+5d}/5  {sig:13}  {comp:5.2f}  {rsi:5.1f}  {bar}  {pnl:+.1f}%{sell}{rot}")

    # Rotation candidates
    candidates = [(t, r) for t, r in others if r["score"] >= 3]
    candidates.sort(key=lambda x: -x[1]["comp_score"])

    print(f"\n{SEP}")
    print("  ROTATION CANDIDATES — score ≥+3, not currently held")
    print(SEP)
    if candidates:
        print(f"  {'Stock':12}  {'Score':>6}  {'Signal':13}  {'Comp':>5}  {'RSI':>5}  Indicators")
        print(SEP)
        for ticker, r in candidates[:15]:
            name = r["name"]
            sc   = r["score"]
            sig  = r["signal"]
            comp = r["comp_score"]
            rsi  = r["rsi"]
            bar  = sig_bar(r)
            print(f"  {name:12}  {sc:+5d}/5  {sig:13}  {comp:5.2f}  {rsi:5.1f}  {bar}")
    else:
        print("  None — no watchlist stock reached ≥+3 on Friday")

    # Near-candidates at +2
    near = [(t, r) for t, r in others if r["score"] == 2]
    near.sort(key=lambda x: -x[1]["comp_score"])
    if near:
        print(f"\n  Near-candidates at +2 (one indicator away):")
        for ticker, r in near[:10]:
            print(f"    {r['name']:12}  comp:{r['comp_score']:.2f}  RSI:{r['rsi']:.0f}  {sig_bar(r)}")

    print(f"\n{SEP}")
    print("  SELL / STRONG SELL alerts across full watchlist")
    print(SEP)
    sells = [(t, r) for t, r in results.items() if r["score"] <= -2]
    sells.sort(key=lambda x: x[1]["score"])
    if sells:
        for ticker, r in sells:
            held_flag = " ← IN PORTFOLIO" if ticker in portfolio_tickers else ""
            print(f"  {r['name']:12}  {r['score']:+d}/5  {r['signal']:13}  {sig_bar(r)}{held_flag}")
    else:
        print("  None — no watchlist stock at ≤−2")

    print(f"\n{'='*65}")
    print("  Open set_dashboard.html for the full visual dashboard.")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
