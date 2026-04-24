#!/usr/bin/env python3
"""
SET Portfolio History Backfiller
─────────────────────────────────────────────────────────────────
One-time script: reconstructs set_history.json from the beginning
using real historical closing prices from yfinance.

Run once with:
  ~/set-venv/bin/python set_backfill_history.py

After running, commit and push set_history.json to GitHub.
The dashboard will then show accurate daily portfolio values.
"""

import sys, os, json, datetime

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO_PATH = os.path.join(SCRIPT_DIR, "set_portfolio.json")
HISTORY_PATH   = os.path.join(SCRIPT_DIR, "set_history.json")

def ensure_packages():
    import importlib, subprocess
    for pkg in ["yfinance", "pandas"]:
        try:
            importlib.import_module(pkg)
        except ImportError:
            for cmd in [
                [sys.executable, "-m", "pip", "install", pkg, "-q"],
                [sys.executable, "-m", "pip", "install", pkg, "--user", "-q"],
            ]:
                try:
                    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break
                except subprocess.CalledProcessError:
                    continue

ensure_packages()
import yfinance as yf
import pandas as pd

def main():
    print("=" * 60)
    print("SET Portfolio History Backfiller")
    print("=" * 60)

    with open(PORTFOLIO_PATH) as f:
        port = json.load(f)

    capital    = port["capital"]
    start_date = datetime.date.fromisoformat(port["start_date"])
    today      = datetime.date.today()
    trades     = sorted(port.get("trades", []), key=lambda t: t["date"])

    if not trades:
        print("No trades found in portfolio.")
        return

    # ── Collect all tickers that were ever held ───────────────────────
    ever_held = set()
    for t in trades:
        ever_held.add(t["ticker"])
    ever_held.discard(None)
    print(f"\nFetching historical prices for {len(ever_held)} tickers...")

    # ── Fetch closing price history for each ticker ───────────────────
    price_cache = {}  # ticker -> {date_str -> close_price}
    for i, ticker in enumerate(sorted(ever_held), 1):
        try:
            df = yf.Ticker(ticker).history(
                start=start_date.isoformat(),
                end=(today + datetime.timedelta(days=1)).isoformat(),
                auto_adjust=True
            )
            if df.empty or "Close" not in df.columns:
                print(f"  [{i:2d}] ⚠  {ticker}: no price data")
                price_cache[ticker] = {}
                continue
            closes = {}
            for ts, row in df.iterrows():
                d = ts.date().isoformat()
                closes[d] = float(row["Close"])
            price_cache[ticker] = closes
            print(f"  [{i:2d}] ✅ {ticker}: {len(closes)} days")
        except Exception as e:
            print(f"  [{i:2d}] ⚠  {ticker}: {e}")
            price_cache[ticker] = {}

    # ── Replay trades day by day ──────────────────────────────────────
    # Group trades by date
    by_date = {}
    for t in trades:
        by_date.setdefault(t["date"], []).append(t)

    cash     = capital
    holdings = {}   # ticker -> {shares, avg_cost, name}
    history  = []
    day      = 0
    peak     = capital

    cur = start_date
    while cur <= today:
        # Skip weekends
        if cur.weekday() >= 5:
            cur += datetime.timedelta(days=1)
            continue

        date_str   = cur.isoformat()
        day_trades = by_date.get(date_str, [])
        cnt        = 0

        # Apply trades for this day
        for t in day_trades:
            action = t.get("action", "")
            if action == "BUY":
                cash -= t["shares"] * t["price"] * 1.0025
                holdings[t["ticker"]] = {
                    "shares":   t["shares"],
                    "avg_cost": t["price"],
                    "name":     t.get("name", t["ticker"]),
                }
                cnt += 1
            elif action == "SELL":
                cash += t["shares"] * t["price"] * 0.9975
                holdings.pop(t["ticker"], None)
                cnt += 1
            elif action == "DIVIDEND":
                cash += t.get("value", 0)
                cnt += 1

        # Calculate portfolio value using real closing prices
        hold_val = 0.0
        for ticker, h in holdings.items():
            closes = price_cache.get(ticker, {})
            # Use this day's close, or find nearest earlier close
            px = None
            check = cur
            for _ in range(5):  # look back up to 5 days
                px = closes.get(check.isoformat())
                if px is not None:
                    break
                check -= datetime.timedelta(days=1)
            if px is None:
                px = h["avg_cost"]  # final fallback
            hold_val += h["shares"] * px

        day += 1
        total  = cash + hold_val
        if total > peak:
            peak = total
        pnl    = total - capital
        pnl_pct = pnl / capital * 100
        dd     = (total - peak) / peak * 100

        history.append({
            "day":      day,
            "date":     date_str,
            "value":    round(total, 2),
            "cash":     round(cash, 2),
            "pnl":      round(pnl, 2),
            "pnlPct":   round(pnl_pct, 2),
            "drawdown": round(dd, 2),
            "trades":   cnt,
        })

        cur += datetime.timedelta(days=1)

    # ── Save ─────────────────────────────────────────────────────────
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, default=str)

    print(f"\n✅ Saved {len(history)} daily entries to set_history.json")
    print(f"\n📊 Summary:")
    print(f"   Start : {history[0]['date']}  ฿{history[0]['value']:,.0f}")
    print(f"   End   : {history[-1]['date']}  ฿{history[-1]['value']:,.0f}")
    print(f"   P&L   : {history[-1]['pnl']:+,.0f}  ({history[-1]['pnlPct']:+.2f}%)")
    print(f"\nNow run:")
    print(f"  cd ~/Documents/Claude/Projects/Stock")
    print(f"  git add set_history.json")
    print(f'  git commit -m "Backfill portfolio history"')
    print(f"  git push origin main --force")


if __name__ == "__main__":
    main()
