#!/usr/bin/env python3
"""
Thai SET — Quick Test Script
────────────────────────────
Tests signals + LINE alerts on 5 popular SET stocks.
Does NOT execute real trades or modify portfolio/state files.
Run from Terminal: python3 set_test_run.py
"""

import sys, os, json, datetime
from concurrent.futures import ThreadPoolExecutor

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
                    import subprocess as sp
                    sp.check_call(args, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
                    break
                except:
                    continue

ensure_packages()

import requests
import pandas as pd
import yfinance as yf

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "set_config.json")

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = json.load(f)

LINE_TOKEN   = cfg.get("line_channel_access_token", "")
LINE_USER_ID = cfg.get("line_user_id", "")

BKK_OFFSET = datetime.timezone(datetime.timedelta(hours=7))

TEST_STOCKS = [
    ("PTT",      "PTT.BK"),
    ("SCB",      "SCB.BK"),
    ("ADVANC",   "ADVANC.BK"),
    ("CPALL",    "CPALL.BK"),
    ("AOT",      "AOT.BK"),
]

RSI_PERIOD  = 14
SMA_PERIOD  = 50
LOOKBACK    = 200

# ─── Indicators ───────────────────────────────────────────────────────────────

def calc_rsi(closes, period=14):
    delta = closes.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    alpha = 1.0 / period
    avg_gain = gain.ewm(alpha=alpha, adjust=False).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])

def calc_sma(closes, period=50):
    if len(closes) < period:
        return None, None
    sma = closes.rolling(period).mean()
    return float(sma.iloc[-1]), float(closes.iloc[-1])

def calc_macd(closes):
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd_line   = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1])

def analyse(name, ticker):
    try:
        df = yf.download(ticker, period="1y", interval="1d", progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 60:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        closes = df["Close"].dropna()
        price  = float(closes.iloc[-1])

        rsi = calc_rsi(closes, RSI_PERIOD)
        sma, last = calc_sma(closes, SMA_PERIOD)
        macd_val, macd_sig = calc_macd(closes)

        score = 0
        rsi_sig = sma_sig = macd_sig_str = ""

        if rsi < 30:
            score += 1; rsi_sig = "OVERSOLD ✅"
        elif rsi > 70:
            score -= 1; rsi_sig = "OVERBOUGHT ⚠️"
        else:
            rsi_sig = "NEUTRAL"

        if sma and last:
            if last > sma:
                score += 1; sma_sig = "ABOVE SMA ✅"
            else:
                score -= 1; sma_sig = "BELOW SMA ⚠️"

        if macd_val > macd_sig:
            score += 1; macd_sig_str = "BULLISH ✅"
        else:
            score -= 1; macd_sig_str = "BEARISH ⚠️"

        if score >= 3:
            signal = "STRONG BUY 🚀"
        elif score >= 2:
            signal = "BUY 📈"
        elif score >= 1:
            signal = "WEAK BUY"
        elif score == 0:
            signal = "HOLD ➡️"
        elif score >= -2:
            signal = "SELL 📉"
        else:
            signal = "STRONG SELL 🔴"

        return {
            "name": name, "ticker": ticker, "price": price,
            "rsi": rsi, "sma": sma, "macd": macd_val, "macd_signal": macd_sig,
            "rsi_sig": rsi_sig, "sma_sig": sma_sig, "macd_sig_str": macd_sig_str,
            "score": score, "signal": signal,
        }
    except Exception as e:
        print(f"  ERROR {ticker}: {e}")
        return None

# ─── LINE ─────────────────────────────────────────────────────────────────────

def send_line(msg):
    if not LINE_TOKEN or not LINE_USER_ID:
        print("[LINE] No token/user ID configured — skipping LINE send")
        return
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": "Bearer " + LINE_TOKEN,
                     "Content-Type": "application/json"},
            json={"to": LINE_USER_ID,
                  "messages": [{"type": "text", "text": msg}]},
            timeout=10,
        )
        if r.status_code == 200:
            print("[LINE] ✅ Message sent!")
        else:
            print(f"[LINE] ❌ Error {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[LINE] ❌ Exception: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    now = datetime.datetime.now(BKK_OFFSET)
    print("=" * 60)
    print("  Thai SET — TEST RUN")
    print("  Time (Bangkok): {}".format(now.strftime("%Y-%m-%d %H:%M")))
    print("  Stocks: {}".format(", ".join(t for _, t in TEST_STOCKS)))
    print("=" * 60)
    print()

    print("Fetching prices and calculating signals...")
    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(analyse, name, ticker): name for name, ticker in TEST_STOCKS}
        for fut in futures:
            r = fut.result()
            if r:
                results.append(r)
                print("  {:8s}  ฿{:.2f}  Score: {:+d}  {}".format(
                    r["name"], r["price"], r["score"], r["signal"]))

    if not results:
        print("No data retrieved — check internet connection.")
        return

    # Build LINE test message
    lines = [
        "🧪 SET SIGNAL TEST — {}".format(now.strftime("%d %b %Y %H:%M")),
        "─" * 30,
    ]
    for r in sorted(results, key=lambda x: -x["score"]):
        lines.append("{} {}".format(r["signal"], r["name"]))
        lines.append("  Price : ฿{:.2f}".format(r["price"]))
        lines.append("  Score : {:+d}/3".format(r["score"]))
        lines.append("  RSI   : {:.1f} — {}".format(r["rsi"], r["rsi_sig"]))
        lines.append("  SMA50 : {:.2f} — {}".format(r["sma"] or 0, r["sma_sig"]))
        lines.append("  MACD  : {}".format(r["macd_sig_str"]))
        lines.append("")

    lines.append("✅ LINE connection test PASSED" if LINE_TOKEN else "⚠️  No LINE token configured")
    msg = "\n".join(lines)

    print()
    print("─" * 60)
    print("LINE message preview:")
    print(msg)
    print("─" * 60)
    print()
    print("Sending to LINE...")
    send_line(msg)
    print()
    print("Done! Check your LINE app.")

if __name__ == "__main__":
    main()
