#!/usr/bin/env python3
"""
Siamchart EOD Data Pipeline
────────────────────────────────────────────────────────────────────────────
Downloads End-of-Day price data from siamchart.com, parses the MetaStock
format, and caches per-ticker CSVs for use by set_backtest.py.

Setup (one-time):
  1. Register at http://siamchart.com/stock/
  2. Add your credentials to set_env.sh:
       export SIAMCHART_USER="your_email"
       export SIAMCHART_PASS="your_password"
  3. Run:  python3 set_siamchart.py --download
     Or manually download the EOD zip and run:
       python3 set_siamchart.py --parse /path/to/eod_file.zip

Usage:
  python3 set_siamchart.py --download          # auto login + download + parse
  python3 set_siamchart.py --parse FILE        # parse a manually downloaded file
  python3 set_siamchart.py --check             # show cache status
  python3 set_siamchart.py --ticker PTT.BK     # show cached data for one stock

Cache location:  ./siamchart_cache/  (one CSV per ticker)
"""

import os, sys, json, zipfile, io, datetime, argparse, shutil
from pathlib import Path

SCRIPT_DIR   = Path(__file__).parent
CACHE_DIR    = SCRIPT_DIR / "siamchart_cache"
META_FILE    = CACHE_DIR / "_meta.json"

# ── Siamchart login / download URLs ──────────────────────────────────────────
# These were identified from the siamchart.com/stock/ page.
# If they stop working, log in with your browser, open DevTools → Network,
# click the EOD download button, and copy the request URL here.
LOGIN_URL    = "http://siamchart.com/stock/"
DOWNLOAD_URL = "http://siamchart.com/stock/"   # POST with action=download after login

# Credentials from environment (never hard-code)
SC_USER = os.environ.get("SIAMCHART_USER", "")
SC_PASS = os.environ.get("SIAMCHART_PASS", "")


# ── Helpers ───────────────────────────────────────────────────────────────────
def ensure_packages():
    import importlib, subprocess
    for pkg in ["requests", "pandas"]:
        try:
            importlib.import_module(pkg)
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   pkg, "--break-system-packages", "-q"])

ensure_packages()
import requests
import pandas as pd


# ── Download ──────────────────────────────────────────────────────────────────
def download_eod(save_path: Path) -> bool:
    """
    Log in to Siamchart and download the latest EOD zip file.
    Returns True on success.

    NOTE: If this fails, log into siamchart.com/stock/ in your browser,
    right-click the download button → Copy link address, then use:
        python3 set_siamchart.py --parse <downloaded_file>
    """
    if not SC_USER or not SC_PASS:
        print("❌  SIAMCHART_USER / SIAMCHART_PASS not set in environment.")
        print("    Add them to set_env.sh and run:  source set_env.sh")
        return False

    session = requests.Session()
    session.headers.update({"User-Agent":
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})

    print("[1/3] Logging in to siamchart.com...")
    try:
        # Get login page first (to capture any CSRF token / cookies)
        r = session.get(LOGIN_URL, timeout=30)
        r.raise_for_status()

        # Post credentials
        payload = {
            "username": SC_USER,
            "password": SC_PASS,
            "action":   "login",
        }
        r = session.post(LOGIN_URL, data=payload, timeout=30)
        r.raise_for_status()

        if "logout" not in r.text.lower() and "ออกจากระบบ" not in r.text:
            print("  ⚠️  Login may have failed — page doesn't show logout link.")
            print("  Continuing anyway (some pages don't redirect after login)...")

        print("  ✅ Login OK")
    except Exception as e:
        print(f"  ❌ Login failed: {e}")
        return False

    print("[2/3] Downloading EOD file...")
    try:
        # Request the download — Siamchart uses a POST with action=download
        r = session.post(DOWNLOAD_URL, data={"action": "download"}, timeout=120,
                         stream=True)
        r.raise_for_status()

        content_type = r.headers.get("Content-Type", "")
        if "zip" not in content_type and "octet" not in content_type:
            print(f"  ⚠️  Unexpected content-type: {content_type}")
            print("  The download URL may have changed.")
            print("  Please download manually and use:  --parse <file>")
            return False

        with open(save_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        print(f"  ✅ Saved → {save_path} ({save_path.stat().st_size/1024:.0f} KB)")
    except Exception as e:
        print(f"  ❌ Download failed: {e}")
        return False

    return True


# ── Parser ────────────────────────────────────────────────────────────────────
def _detect_format(content: str) -> str:
    """Detect whether the file is MetaStock ASCII or a plain CSV."""
    first = content.strip().split("\n")[0]
    parts = first.split(",")
    # MetaStock ASCII: TICKER,YYYYMMDD,O,H,L,C,V  (date is 8-digit int)
    if len(parts) >= 6 and len(parts[1].strip()) == 8 and parts[1].strip().isdigit():
        return "metastock"
    # Header row CSV
    if "date" in first.lower() or "open" in first.lower():
        return "csv_header"
    return "unknown"


def parse_eod_file(file_path: Path) -> dict:
    """
    Parse an EOD file (zip or plain text) into a dict of
    { 'PTT.BK': pd.DataFrame(OHLCV, DatetimeIndex), ... }

    Handles:
    - Zip files containing one or more text files
    - MetaStock ASCII: TICKER,YYYYMMDD,O,H,L,C,V
    - Plain CSV with headers
    """
    print(f"[3/3] Parsing {file_path.name}...")

    raw_text = ""

    if zipfile.is_zipfile(file_path):
        with zipfile.ZipFile(file_path) as zf:
            names = zf.namelist()
            print(f"  Zip contains: {names}")
            # Pick the largest text file inside the zip
            txt_files = [n for n in names
                         if n.lower().endswith((".txt", ".csv", ".dat", ".asc"))]
            if not txt_files:
                txt_files = names   # try all
            for name in sorted(txt_files, key=lambda n: -zf.getinfo(n).file_size):
                try:
                    raw_text = zf.read(name).decode("utf-8", errors="replace")
                    if len(raw_text) > 1000:
                        print(f"  Using: {name} ({len(raw_text):,} chars)")
                        break
                except Exception:
                    continue
    else:
        raw_text = file_path.read_text(encoding="utf-8", errors="replace")

    if not raw_text:
        print("  ❌ Could not read any text from the file.")
        return {}

    fmt = _detect_format(raw_text)
    print(f"  Detected format: {fmt}")

    frames = {}  # ticker → list of (date, O, H, L, C, V)

    if fmt == "metastock":
        for line in raw_text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 6:
                continue
            try:
                ticker  = parts[0].strip().upper()
                date_s  = parts[1].strip()
                o,h,l,c = (float(parts[i]) for i in range(2, 6))
                vol     = float(parts[6]) if len(parts) > 6 else 0.0
                dt      = datetime.date(int(date_s[:4]),
                                        int(date_s[4:6]),
                                        int(date_s[6:8]))
                frames.setdefault(ticker, []).append((dt, o, h, l, c, vol))
            except Exception:
                continue

    elif fmt == "csv_header":
        try:
            df_all = pd.read_csv(io.StringIO(raw_text))
            df_all.columns = [c.strip().lower() for c in df_all.columns]
            ticker_col = next((c for c in df_all.columns
                               if c in ("ticker","symbol","stock","name")), None)
            date_col   = next((c for c in df_all.columns
                               if "date" in c), None)
            if ticker_col and date_col:
                for ticker, grp in df_all.groupby(ticker_col):
                    rows = []
                    for _, r in grp.iterrows():
                        try:
                            dt = pd.to_datetime(str(r[date_col])).date()
                            rows.append((dt,
                                         float(r.get("open", r.get("o", 0))),
                                         float(r.get("high", r.get("h", 0))),
                                         float(r.get("low",  r.get("l", 0))),
                                         float(r.get("close",r.get("c", 0))),
                                         float(r.get("volume",r.get("vol",0)))))
                        except Exception:
                            continue
                    frames[str(ticker).upper()] = rows
        except Exception as e:
            print(f"  ❌ CSV parse error: {e}")
            return {}
    else:
        print("  ❌ Unknown file format. Expected MetaStock ASCII (TICKER,YYYYMMDD,...)")
        print("  First line:", raw_text.splitlines()[0][:120])
        return {}

    # Convert to DataFrames
    result = {}
    for ticker, rows in frames.items():
        if not rows:
            continue
        rows.sort(key=lambda x: x[0])
        df = pd.DataFrame(rows, columns=["Date","Open","High","Low","Close","Volume"])
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        # Normalise ticker to .BK suffix (Siamchart uses bare symbols like PTT)
        bk_ticker = ticker if ticker.endswith(".BK") else ticker + ".BK"
        result[bk_ticker] = df

    print(f"  ✅ Parsed {len(result)} tickers, "
          f"{sum(len(v) for v in result.values()):,} total rows")
    return result


# ── Cache ─────────────────────────────────────────────────────────────────────
def save_cache(data: dict):
    """Save parsed DataFrames as per-ticker CSV files."""
    CACHE_DIR.mkdir(exist_ok=True)
    saved = 0
    for ticker, df in data.items():
        path = CACHE_DIR / f"{ticker.replace('.','_')}.csv"
        # Merge with existing cache if present (append new rows)
        if path.exists():
            old = pd.read_csv(path, index_col=0, parse_dates=True)
            df  = pd.concat([old, df]).sort_index()
            df  = df[~df.index.duplicated(keep="last")]
        df.to_csv(path)
        saved += 1
    # Update meta
    meta = {"updated": datetime.datetime.now().isoformat(),
            "tickers": sorted(data.keys()),
            "n_tickers": len(data)}
    META_FILE.write_text(json.dumps(meta, indent=2))
    print(f"  ✅ Cache updated: {saved} tickers → {CACHE_DIR}")


def load_cache(ticker: str, years: int = 5) -> "pd.DataFrame | None":
    """
    Load cached Siamchart data for a ticker.
    Returns a DataFrame with columns Open/High/Low/Close/Volume,
    filtered to the last `years` years.  Returns None if not cached.
    """
    path = CACHE_DIR / f"{ticker.replace('.','_')}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index).tz_localize(None)
        cutoff = pd.Timestamp.today() - pd.DateOffset(years=years)
        df = df[df.index >= cutoff]
        if df.empty:
            return None
        return df[["Open","High","Low","Close","Volume"]].dropna()
    except Exception:
        return None


def cache_status():
    """Print a summary of the local cache."""
    if not META_FILE.exists():
        print("No Siamchart cache found. Run:  python3 set_siamchart.py --download")
        return
    meta = json.loads(META_FILE.read_text())
    print(f"Siamchart cache — last updated: {meta['updated']}")
    print(f"Tickers cached : {meta['n_tickers']}")
    # Show date range of a sample ticker
    if meta["tickers"]:
        sample = meta["tickers"][0]
        df = load_cache(sample, years=20)
        if df is not None:
            print(f"Date range     : {df.index[0].date()} → {df.index[-1].date()}")
            print(f"Sample ticker  : {sample}  ({len(df)} rows)")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Siamchart EOD data pipeline")
    parser.add_argument("--download", action="store_true",
                        help="Login to Siamchart and download the latest EOD file")
    parser.add_argument("--parse", metavar="FILE",
                        help="Parse a manually downloaded EOD zip/txt file")
    parser.add_argument("--check",  action="store_true",
                        help="Show cache status")
    parser.add_argument("--ticker", metavar="TICKER",
                        help="Show cached data for one ticker (e.g. PTT.BK)")
    args = parser.parse_args()

    if args.check:
        cache_status()
        return

    if args.ticker:
        df = load_cache(args.ticker, years=10)
        if df is None:
            print(f"No cached data for {args.ticker}")
        else:
            print(f"{args.ticker}: {len(df)} rows  "
                  f"({df.index[0].date()} → {df.index[-1].date()})")
            print(df.tail(10).to_string())
        return

    if args.parse:
        path = Path(args.parse)
        if not path.exists():
            print(f"File not found: {path}")
            sys.exit(1)
        data = parse_eod_file(path)
        if data:
            save_cache(data)
        return

    if args.download:
        tmp = SCRIPT_DIR / "_siamchart_eod_tmp.zip"
        ok  = download_eod(tmp)
        if ok:
            data = parse_eod_file(tmp)
            if data:
                save_cache(data)
            tmp.unlink(missing_ok=True)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
