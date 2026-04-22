#!/usr/bin/env python3
"""
Thai SET — Dashboard Fundamental Updater
─────────────────────────────────────────────────────────────────────────────
Fetches real P/E, P/BV, ROE and dividend data for every stock in the watchlist
then embeds the computed fund_score and comp_score into set_dashboard.html.

Run this once manually (takes ~3-5 minutes for 80+ stocks).
After running, open set_dashboard.html — every signal card will show real
Fundamental and Composite scores, not estimates.

Usage:
  python set_update_dashboard.py

Also auto-updates the embedded SIGNALS data with latest prices from
set_signal_state.json so the dashboard stays current.
"""

import sys, os, json, re, datetime

def ensure_packages():
    import importlib, subprocess
    for pkg in ["yfinance", "requests"]:
        try:
            importlib.import_module(pkg)
        except ImportError:
            # Try several install strategies for different environments
            strategies = [
                [sys.executable, "-m", "pip", "install", pkg, "-q"],
                [sys.executable, "-m", "pip", "install", pkg, "--user", "-q"],
                [sys.executable, "-m", "pip", "install", pkg, "--break-system-packages", "-q"],
            ]
            installed = False
            for cmd in strategies:
                try:
                    subprocess.check_call(cmd,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
                    installed = True
                    break
                except subprocess.CalledProcessError:
                    continue
            if not installed:
                print(f"⚠  Could not auto-install '{pkg}'. Run manually:")
                print(f"   pip3 install {pkg} --user")
                sys.exit(1)

ensure_packages()
import yfinance as yf

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH    = os.path.join(SCRIPT_DIR, "set_config.json")
STATE_PATH     = os.path.join(SCRIPT_DIR, "set_signal_state.json")
PORTFOLIO_PATH = os.path.join(SCRIPT_DIR, "set_portfolio.json")
DASHBOARD_PATH = os.path.join(SCRIPT_DIR, "set_dashboard.html")

with open(CONFIG_PATH) as f: cfg = json.load(f)
INSTRUMENTS = [(i["name"], i["ticker"]) for i in cfg.get("instruments", [])]
_sw = cfg.get("scoring_weights", {})
W_TECH = _sw.get("technical", 0.6)
W_FUND = _sw.get("fundamental", 0.4)

BKK = datetime.timezone(datetime.timedelta(hours=7))


# ─── Scoring functions (mirrors set_realtime_monitor.py) ─────────────────────
_FUND_MAX_RAW = 13.0   # 3 + 3 + 3 + 4

def calc_fundamental_score(fund):
    if not fund:
        return 5.0
    s = 0.0
    pe, pbv, roe = fund.get("pe"), fund.get("pbv"), fund.get("roe")
    has_div = fund.get("has_div", False)
    div_yld = fund.get("div_yld") or 0.0

    # P/E — max 3 pts
    if pe is None:      s += 1
    elif pe <= 0:       s += 0
    elif pe <= 8:       s += 3
    elif pe <= 12:      s += 2
    elif pe <= 15:      s += 1

    # P/BV — max 3 pts (new: <1 earns extra tier)
    if pbv is None:     s += 1
    elif pbv < 1.0:     s += 3
    elif pbv <= 1.5:    s += 2
    elif pbv <= 3:      s += 1

    # ROE — max 3 pts
    if roe is None:     s += 1
    elif roe >= 0.20:   s += 3
    elif roe >= 0.12:   s += 2
    elif roe >= 0.08:   s += 1

    # Dividend — max 4 pts (graduated yield tiers)
    if has_div:
        if div_yld >= 0.09:    s += 4.0
        elif div_yld >= 0.08:  s += 3.5
        elif div_yld >= 0.065: s += 3.0
        elif div_yld >= 0.05:  s += 2.5
        elif div_yld >= 0.03:  s += 2.0
        else:                  s += 1.0

    return round(min(10.0, s / _FUND_MAX_RAW * 10.0), 1)


def calc_composite_score(tech_score, fund_score):
    tech_norm = (tech_score + 3) / 6.0 * 10.0
    return round(tech_norm * W_TECH + fund_score * W_FUND, 2)


def fetch_fund(ticker):
    try:
        tk      = yf.Ticker(ticker)
        info    = tk.info
        pe      = info.get("trailingPE") or info.get("forwardPE")
        pbv     = info.get("priceToBook")
        roe     = info.get("returnOnEquity")
        div_yld = info.get("dividendYield") or 0.0
        try:
            import pandas as pd
            divs   = tk.dividends
            cutoff = pd.Timestamp.now(tz="UTC") - pd.DateOffset(years=3)
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
        return {"pe": pe, "pbv": pbv, "roe": roe,
                "div_yld": div_yld, "has_div": has_div,
                "ex_div_date": ex_div_date}
    except Exception as e:
        print(f"  ⚠  {ticker}: {e}")
        return {}


def fetch_price(ticker):
    try:
        df = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
        if not df.empty and "Close" in df.columns:
            return float(df["Close"].dropna().iloc[-1])
    except Exception:
        pass
    return None


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("SET Dashboard Fundamental Updater")
    print(f"Run: {datetime.datetime.now(BKK).strftime('%Y-%m-%d %H:%M')} Bangkok")
    print("=" * 60)

    # Load current signal state for existing scores / signals
    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            state = json.load(f)

    # Load portfolio for current prices and holdings
    port = {}
    if os.path.exists(PORTFOLIO_PATH):
        with open(PORTFOLIO_PATH) as f:
            port = json.load(f)

    total = len(INSTRUMENTS)
    print(f"\nFetching fundamentals for {total} stocks...")
    print("(This takes 3–5 minutes — please wait)\n")

    signals = {}
    for i, (name, ticker) in enumerate(INSTRUMENTS, 1):
        # Get existing signal state for this ticker
        st = state.get(ticker, {})
        tech_score = st.get("score", 0) if isinstance(st, dict) else 0
        signal     = st.get("signal", "HOLD") if isinstance(st, dict) else "HOLD"
        price_st   = st.get("price") if isinstance(st, dict) else None

        # Fetch live price if not in state
        price = price_st or fetch_price(ticker) or 0.0

        print(f"  [{i:3d}/{total}] {name:12s} score:{tech_score:+d}  ", end="", flush=True)

        # Fetch fundamentals for this stock
        fund = fetch_fund(ticker)
        fund_score = calc_fundamental_score(fund)
        comp_score = calc_composite_score(tech_score, fund_score)

        print(f"Fund:{fund_score:.1f}/10  Comp:{comp_score:.1f}/10")

        signals[ticker] = {
            "score":      tech_score,
            "signal":     signal,
            "price":      price,
            "fund_score": fund_score,
            "comp_score": comp_score,
            "fund":       fund,
            "buy_alerted":  st.get("buy_alerted",  False) if isinstance(st, dict) else False,
            "sell_alerted": st.get("sell_alerted", False) if isinstance(st, dict) else False,
        }

    print(f"\n✅ Fetched {len(signals)} stocks")

    # Also update signal_state.json with new fund scores
    for ticker, sig in signals.items():
        if ticker in state and isinstance(state[ticker], dict):
            state[ticker]["fund_score"] = sig["fund_score"]
            state[ticker]["comp_score"] = sig["comp_score"]
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)
    print("✅ Updated set_signal_state.json")

    # Embed into dashboard HTML
    if not os.path.exists(DASHBOARD_PATH):
        print(f"⚠  {DASHBOARD_PATH} not found — skipping HTML update")
        return

    with open(DASHBOARD_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    # Build compact JSON for embedding (exclude internal fields)
    embed = {}
    for ticker, sig in signals.items():
        embed[ticker] = {
            "score":      sig["score"],
            "signal":     sig["signal"],
            "price":      round(sig["price"], 4) if sig["price"] else 0,
            "fund_score": sig["fund_score"],
            "comp_score": sig["comp_score"],
            "fund":       {k: round(v, 4) if isinstance(v, float) else v
                           for k, v in sig["fund"].items()} if sig["fund"] else {},
            "buy_alerted":  sig["buy_alerted"],
            "sell_alerted": sig["sell_alerted"],
        }

    signals_json = json.dumps(embed, ensure_ascii=False, separators=(",", ":"))

    # Replace the SIGNALS constant in the HTML
    pattern = r'const SIGNALS\s*=\s*\{.*?\};'
    replacement = f'const SIGNALS = {signals_json};'
    new_html, n = re.subn(pattern, replacement, html, flags=re.DOTALL)

    if n == 0:
        print("⚠  Could not find SIGNALS constant in HTML — check format")
        return

    # Update embedded timestamp comment
    ts = datetime.datetime.now(BKK).strftime("%Y-%m-%d %H:%M")
    new_html = new_html.replace(
        "// Updated:",
        f"// Updated: {ts} Bangkok ·"
    )

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"✅ Updated set_dashboard.html")
    print(f"\nOpen set_dashboard.html in your browser to see real Fund/Comp scores.")
    print("─" * 60)

    # Print top 10 by composite for quick reference
    ranked = sorted(embed.items(), key=lambda x: -x[1]["comp_score"])
    print("\n🏆 Top 10 by Composite Score:")
    for ticker, s in ranked[:10]:
        name = ticker.replace(".BK", "")
        print(f"   {name:10s}  Tech:{s['score']:+d}  Fund:{s['fund_score']:.1f}/10  "
              f"Comp:{s['comp_score']:.1f}/10  [{s['signal']}]")


if __name__ == "__main__":
    main()
