#!/usr/bin/env python3
"""
Thai SET — Historical Fundamental Data Fetcher
──────────────────────────────────────────────────────────────────────────────
Downloads historical financial data (EPS, DPS, P/E, P/BV, ROE, dividend yield,
book value per share) for every instrument in set_config.json using the
`thaifin` library, which pulls from Finnomena / SET / Settrade.

Run ONCE to build the cache, then re-run quarterly to keep it fresh:

    pip install thaifin
    python3 set_fetch_fundamentals.py

Output: set_fundamental_cache.json
    {
        "SCB.BK": {
            "symbol": "SCB",
            "name": "SCB",
            "fetched": "2026-04-26",
            "yearly": {
                "2021": {
                    "eps": 7.50, "dps": 5.00,
                    "pe": 9.2,   "pbv": 0.85,
                    "roe": 0.095,"div_yield": 0.062,
                    "bvps": 89.5,"de_ratio": null,
                    "eps_growth": 0.12
                },
                ...
            },
            "quarterly": {
                "2021Q2": {"dps": 2.50, "eps": 3.20},
                ...
            }
        },
        ...
    }

The backtest (set_backtest.py) uses this for point-in-time fundamental scoring.
The monitor  (set_realtime_monitor.py) uses DPS history for dividend crediting.
"""

import sys, os, json, datetime

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "set_config.json")
CACHE_PATH  = os.path.join(SCRIPT_DIR, "set_fundamental_cache.json")

# ── Auto-install thaifin ──────────────────────────────────────────────────────
def ensure_thaifin():
    try:
        import thaifin
        return True
    except ImportError:
        print("Installing thaifin...")
        import subprocess
        for args in [
            [sys.executable, "-m", "pip", "install", "thaifin", "-q"],
            [sys.executable, "-m", "pip", "install", "thaifin", "--user", "-q"],
            [sys.executable, "-m", "pip", "install", "thaifin",
             "--break-system-packages", "-q"],
        ]:
            try:
                subprocess.check_call(args,
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
                return True
            except subprocess.CalledProcessError:
                continue
        print("❌ Could not install thaifin. Run: pip install thaifin")
        return False

if not ensure_thaifin():
    sys.exit(1)

from thaifin import Stock
import pandas as pd

# ── Load config ───────────────────────────────────────────────────────────────
with open(CONFIG_PATH, encoding="utf-8") as f:
    cfg = json.load(f)

instruments = [(i["name"], i["ticker"]) for i in cfg.get("instruments", [])]
today_str   = datetime.date.today().isoformat()

print("=" * 60)
print("SET Historical Fundamental Data Fetcher")
print(f"Period   : up to 10+ years (via thaifin / Finnomena)")
print(f"Stocks   : {len(instruments)}")
print(f"Output   : set_fundamental_cache.json")
print("=" * 60)


# ── Column name discovery helpers ─────────────────────────────────────────────
# thaifin column names changed between versions.  We try multiple candidates
# and take the first that exists and has a non-null value.

def _pick(row, *candidates):
    """Return first non-null value from the candidate column names, or None."""
    for col in candidates:
        if col in row.index:
            v = row[col]
            if v is not None and not (isinstance(v, float) and v != v):  # not NaN
                try:
                    return round(float(v), 6)
                except (TypeError, ValueError):
                    pass
    return None


# ── Column name maps (try multiple possible names per metric) ─────────────────
YEARLY_COLS = {
    "eps":        ["earning_per_share",     "EPS",             "eps"],
    # thaifin has no dividend_per_share column — computed below from div_yield × close
    "dps":        ["dividend_per_share",    "DPS",             "dps",
                   "dividend",              "Dividend"],
    "pe":         ["price_earning_ratio",   "PE",              "pe",
                   "p_e_ratio",             "price_to_earning"],
    # thaifin actual column: price_book_value (not price_book_value_ratio)
    "pbv":        ["price_book_value",      "price_book_value_ratio", "PBV", "pbv",
                   "price_to_book_value",   "p_bv_ratio"],
    "roe":        ["roe",                   "return_on_equity",       "ROE"],
    "div_yield":  ["dividend_yield",        "DividendYield",          "div_yield"],
    "bvps":       ["book_value_per_share",  "BookValuePerShare",      "bvps",
                   "book_value"],
    "de_ratio":   ["debt_to_equity",        "DE",              "de_ratio",
                   "d_e_ratio"],
    "eps_growth": ["earning_per_share_yoy", "EPSGrowth",       "eps_growth",
                   "earning_per_share_growth"],
    # thaifin actual column: npm (net profit margin)
    "net_margin": ["npm",                   "net_profit_margin","NetProfitMargin",
                   "net_margin"],
    "roa":        ["roa",                   "return_on_asset",  "ROA"],
    # store year-end close for DPS computation
    "_close":     ["close"],
}

QUARTER_COLS = {
    "dps": ["dividend_per_share", "DPS", "dps", "dividend", "Dividend"],
    "eps": ["earning_per_share",  "EPS", "eps"],
}


def _extract_yearly(df):
    """Convert yearly DataFrame to {year: metrics_dict}."""
    result = {}
    for idx, row in df.iterrows():
        # Index may be an integer year, a Period, or a date string
        try:
            year = str(int(str(idx)[:4]))
            if len(year) != 4 or not year.isdigit():
                continue
        except Exception:
            continue
        metrics = {
            key: _pick(row, *cols)
            for key, cols in YEARLY_COLS.items()
        }
        # thaifin has no dividend_per_share column — derive from div_yield × close.
        # dividend_yield in thaifin is a ratio (e.g. 0.05 = 5%), close is the year-end price.
        if metrics.get("dps") is None:
            dy    = metrics.get("div_yield")
            close = metrics.get("_close")
            if dy and close and dy > 0 and close > 0:
                metrics["dps"] = round(dy * close, 4)
        # Drop internal helper key
        metrics.pop("_close", None)
        result[year] = metrics
    return result


def _extract_quarterly(df):
    """Convert quarterly DataFrame to {period: metrics_dict}."""
    result = {}
    for idx, row in df.iterrows():
        try:
            period = str(idx).upper()   # e.g. "2021Q2"
            if "Q" not in period:
                continue
        except Exception:
            continue
        result[period] = {
            key: _pick(row, *cols)
            for key, cols in QUARTER_COLS.items()
        }
    return result


# ── Fetch loop ────────────────────────────────────────────────────────────────
cache      = {}
ok_count   = 0
fail_count = 0
_cols_shown = False

for name, ticker_yf in instruments:
    # Skip index tickers (^SET.BK etc.)
    if ticker_yf.startswith("^") or "=" in ticker_yf:
        print(f"  ⏭  {name:12s} — index ticker, skipped")
        continue

    # thaifin uses bare symbol, e.g. "SCB" not "SCB.BK"
    symbol = ticker_yf.replace(".BK", "").upper()

    try:
        stock = Stock(symbol)

        # ── Yearly data ──────────────────────────────────────────────────────
        try:
            ydf = stock.yearly_dataframe
        except Exception as e:
            raise RuntimeError(f"yearly_dataframe failed: {e}")

        if ydf is None or ydf.empty:
            print(f"  ⚠  {name:12s} ({symbol:8s}) — no yearly data")
            fail_count += 1
            continue

        # Show column names once so operator can verify
        if not _cols_shown:
            print(f"\n📋 yearly_dataframe columns ({len(ydf.columns)} total):")
            print("   " + "  |  ".join(ydf.columns.tolist()))
            print()
            _cols_shown = True

        yearly = _extract_yearly(ydf)

        # ── Quarterly data (bonus — used for dividend timing) ────────────────
        quarterly = {}
        try:
            qdf = stock.quarter_dataframe
            if qdf is not None and not qdf.empty:
                quarterly = _extract_quarterly(qdf)
        except Exception:
            pass   # quarterly is optional

        cache[ticker_yf] = {
            "symbol":    symbol,
            "name":      name,
            "fetched":   today_str,
            "yearly":    yearly,
            "quarterly": quarterly,
        }

        years = sorted(yearly.keys())
        yr_range = f"{years[0]}–{years[-1]}" if years else "no data"
        dps_years = sum(1 for d in yearly.values() if d.get("dps") is not None)
        print(f"  ✅ {name:12s} ({symbol:8s})  {len(yearly):2d} years [{yr_range}]"
              f"  DPS in {dps_years} years")
        ok_count += 1

    except Exception as e:
        err = str(e)[:70]
        print(f"  ❌ {name:12s} ({symbol:8s})  {err}")
        fail_count += 1

# ── Save cache ────────────────────────────────────────────────────────────────
with open(CACHE_PATH, "w", encoding="utf-8") as f:
    json.dump(cache, f, indent=2, ensure_ascii=False, default=str)

size_kb = os.path.getsize(CACHE_PATH) / 1024
print()
print(f"{'='*60}")
print(f"✅ Saved {ok_count} stocks → set_fundamental_cache.json ({size_kb:.0f} KB)")
if fail_count:
    print(f"⚠  {fail_count} stocks failed (not listed or symbol mismatch)")
print()
print("Next step: run  python3 set_backtest.py")
print("Re-run this script quarterly to refresh fundamental data.")
