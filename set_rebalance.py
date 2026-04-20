#!/usr/bin/env python3
"""
Thai SET — Fundamental Rebalance
─────────────────────────────────
Checks all current holdings against fundamental criteria.
Sells any stock that fails P/E, PBV, ROE, or dividend check.
Run from Terminal: /usr/bin/python3 set_rebalance.py
"""

import sys, os, json, datetime

def ensure_packages():
    import importlib, subprocess
    for pkg in ["yfinance", "pandas", "requests"]:
        try:
            importlib.import_module(pkg)
        except ImportError:
            for args in [
                [sys.executable, "-m", "pip", "install", pkg, "-q"],
                [sys.executable, "-m", "pip", "install", pkg, "--user", "-q"],
            ]:
                try:
                    subprocess.check_call(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break
                except: continue

ensure_packages()
import requests, pandas as pd, yfinance as yf

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH    = os.path.join(SCRIPT_DIR, "set_config.json")
PORTFOLIO_PATH = os.path.join(SCRIPT_DIR, "set_portfolio.json")

with open(CONFIG_PATH) as f: cfg = json.load(f)

LINE_TOKEN   = os.environ.get("LINE_TOKEN", cfg.get("line_channel_access_token", ""))
LINE_USER_ID = os.environ.get("LINE_USER_ID", cfg.get("line_user_id", ""))

ff         = cfg.get("fundamental_filter", {})
MAX_PE     = ff.get("max_pe", 15)
MAX_PBV    = ff.get("max_pbv", 3)
MIN_ROE    = ff.get("min_roe", 0.08)
REQ_DIV    = ff.get("require_dividend", True)
TX_COST    = 0.0025
BKK_OFFSET = datetime.timezone(datetime.timedelta(hours=7))

def now_bkk():
    return datetime.datetime.now(BKK_OFFSET)

def send_line(msg):
    if not LINE_TOKEN or not LINE_USER_ID:
        print("[LINE] No credentials — skipping")
        return
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": "Bearer " + LINE_TOKEN,
                     "Content-Type": "application/json"},
            json={"to": LINE_USER_ID,
                  "messages": [{"type": "text", "text": msg}]},
            timeout=10)
        print("[LINE] " + ("✅ Sent" if r.status_code == 200 else "❌ Error " + str(r.status_code)))
    except Exception as e:
        print("[LINE] ❌ " + str(e))

def get_price(ticker):
    try:
        df = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
        if not df.empty and "Close" in df.columns:
            return float(df["Close"].dropna().iloc[-1])
    except: pass
    return None

def check_fundamentals(ticker):
    try:
        info    = yf.Ticker(ticker).info
        pe      = info.get("trailingPE") or info.get("forwardPE")
        pbv     = info.get("priceToBook")
        roe     = info.get("returnOnEquity")
        div_yld = info.get("dividendYield") or 0.0
        try:
            divs    = yf.Ticker(ticker).dividends
            cutoff  = pd.Timestamp.now(tz="UTC") - pd.DateOffset(years=3)
            has_div = len(divs[divs.index >= cutoff]) > 0
        except:
            has_div = div_yld > 0

        fails  = []
        checks = []

        if pe is not None:
            if pe < 0:    fails.append("P/E negative (loss)")
            elif pe > MAX_PE: fails.append("P/E {:.1f} > {}".format(pe, MAX_PE))
            else: checks.append("P/E {:.1f} ✅".format(pe))
        else: checks.append("P/E N/A")

        if pbv is not None:
            if pbv > MAX_PBV: fails.append("PBV {:.2f} > {}".format(pbv, MAX_PBV))
            else: checks.append("PBV {:.2f} ✅".format(pbv))
        else: checks.append("PBV N/A")

        if roe is not None:
            if roe < MIN_ROE: fails.append("ROE {:.0%} < {:.0%}".format(roe, MIN_ROE))
            else: checks.append("ROE {:.0%} ✅".format(roe))
        else: checks.append("ROE N/A")

        if REQ_DIV:
            if not has_div: fails.append("No dividend")
            else: checks.append("DIV ✅")

        return {
            "pe": pe, "pbv": pbv, "roe": roe,
            "has_div": has_div, "div_yld": div_yld,
            "passes": len(fails) == 0,
            "fails": fails, "checks": checks,
        }
    except Exception as e:
        return {"passes": True, "fails": [], "checks": ["Data unavailable"], "error": str(e)}

def main():
    with open(PORTFOLIO_PATH) as f:
        port = json.load(f)

    if not port["holdings"]:
        print("No holdings to check.")
        return

    now = now_bkk()
    print("=" * 60)
    print("  Fundamental Rebalance Check")
    print("  Time: {}  Bangkok".format(now.strftime("%Y-%m-%d %H:%M")))
    print("  Criteria: P/E<{}  PBV<{}  ROE>{:.0%}  Dividend required".format(
        MAX_PE, MAX_PBV, MIN_ROE))
    print("=" * 60)

    pass_list = []
    fail_list = []

    for ticker, h in port["holdings"].items():
        print("\nChecking {} ({})...".format(h["name"], ticker))
        fund = check_fundamentals(ticker)
        price = get_price(ticker)

        status = "✅ PASS" if fund["passes"] else "❌ FAIL"
        print("  Status  : {}".format(status))
        print("  Checks  : {}".format(" | ".join(fund["checks"])))
        if fund["fails"]:
            print("  Failed  : {}".format(", ".join(fund["fails"])))
        if price:
            pnl = (price - h["avg_cost"]) * h["shares"]
            pct = (price - h["avg_cost"]) / h["avg_cost"] * 100
            print("  Price   : ฿{:.2f}  (entry ฿{:.2f}  P&L: {:+.0f} {:+.1f}%)".format(
                price, h["avg_cost"], pnl, pct))

        entry = {"ticker": ticker, "holding": h, "fund": fund, "price": price}
        if fund["passes"]:
            pass_list.append(entry)
        else:
            fail_list.append(entry)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("  KEEP ({}) : {}".format(len(pass_list),
          ", ".join(e["holding"]["name"] for e in pass_list) or "None"))
    print("  SELL ({}) : {}".format(len(fail_list),
          ", ".join(e["holding"]["name"] for e in fail_list) or "None"))
    print("=" * 60)

    if not fail_list:
        print("\nAll holdings pass fundamental checks. No action needed.")
        return

    # Execute sells
    print("\nSelling {} failing stock(s)...".format(len(fail_list)))
    today     = datetime.date.today().isoformat()
    sold_info = []

    for e in fail_list:
        ticker  = e["ticker"]
        h       = e["holding"]
        price   = e["price"] or h["avg_cost"]
        shares  = h["shares"]
        proceed = shares * price * (1 - TX_COST)
        pnl     = proceed - shares * h["avg_cost"]

        port["cash"] += proceed
        del port["holdings"][ticker]
        trade = {
            "date":     today,
            "action":   "SELL",
            "ticker":   ticker,
            "name":     h["name"],
            "shares":   shares,
            "price":    round(price, 2),
            "value":    round(proceed, 2),
            "avg_cost": h["avg_cost"],
            "pnl":      round(pnl, 2),
            "reason":   "Fundamental filter: " + ", ".join(e["fund"]["fails"]),
            "time":     now_bkk().strftime("%H:%M"),
        }
        port["trades"].append(trade)
        sold_info.append(trade)
        ps = "+" if pnl >= 0 else ""
        print("  SOLD {} {}sh @ ฿{:.2f}  P&L: {}{:.0f}".format(
            h["name"], shares, price, ps, pnl))

    # Save portfolio
    with open(PORTFOLIO_PATH, "w") as f:
        json.dump(port, f, indent=2, default=str, ensure_ascii=False)
    print("\nPortfolio saved.")

    # Build LINE message
    total_val = port["cash"] + sum(
        port["holdings"][t]["shares"] * (get_price(t) or port["holdings"][t]["avg_cost"])
        for t in port["holdings"])

    lines = [
        "⚖️ FUNDAMENTAL REBALANCE — {}".format(now.strftime("%d %b %Y %H:%M")),
        "─" * 32,
        "Sold {} stock(s) that failed quality filter:".format(len(sold_info)),
        "",
    ]
    for t in sold_info:
        ps = "+" if t["pnl"] >= 0 else ""
        lines.append("🔴 SELL {}".format(t["name"]))
        lines.append("   {}sh @ ฿{:.2f}  P&L: {}฿{:.0f}".format(
            t["shares"], t["price"], ps, t["pnl"]))
        lines.append("   Reason: {}".format(t["reason"]))
        lines.append("")

    lines += [
        "💼 Portfolio after rebalance",
        "   Holdings : {}/10".format(len(port["holdings"])),
        "   Cash     : ฿{:,.0f}".format(port["cash"]),
        "   Kept     : {}".format(", ".join(
            port["holdings"][t]["name"] for t in port["holdings"])),
    ]

    msg = "\n".join(lines)
    print("\nSending LINE notification...")
    send_line(msg)
    print("\nDone! Cash freed up: ฿{:,.0f}".format(
        sum(t["value"] for t in sold_info)))
    print("New positions available for better-quality stocks tomorrow.")

if __name__ == "__main__":
    main()
