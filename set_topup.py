#!/usr/bin/env python3
"""
set_topup.py
────────────────────────────────────────────────────────────────────────────────
Monthly DCA top-up script.  Adds ฿50,000 to the portfolio's cash balance,
logs it as a DEPOSIT entry in the trade history, and refreshes the dashboard.

Run once on the 1st trading day of each month:
    python3 set_topup.py

Optional override amount:
    python3 set_topup.py --amount 30000
"""

import json, os, datetime, argparse, sys, subprocess

SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO_PATH = os.path.join(SCRIPT_DIR, "set_portfolio.json")
DASHBOARD_UPD  = os.path.join(SCRIPT_DIR, "set_dashboard_update.py")

DEFAULT_TOPUP  = 50_000.0

# ── CLI ───────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Monthly DCA top-up")
parser.add_argument("--amount", type=float, default=DEFAULT_TOPUP,
                    help=f"Top-up amount in ฿ (default: {DEFAULT_TOPUP:,.0f})")
parser.add_argument("--dry-run", action="store_true",
                    help="Preview the change without writing to disk")
args = parser.parse_args()

amount = args.amount
if amount <= 0:
    print("❌  Amount must be positive. Aborting.")
    sys.exit(1)

# ── Load portfolio ────────────────────────────────────────────────────────────
with open(PORTFOLIO_PATH, encoding="utf-8") as f:
    port = json.load(f)

now_bkk = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)
today   = now_bkk.strftime("%Y-%m-%d")
time_str= now_bkk.strftime("%H:%M")

cash_before = port.get("cash", 0)
cash_after  = cash_before + amount

# Calculate current portfolio value for reference
holdings_val = sum(
    h["shares"] * h.get("last_price", h["avg_cost"])
    for h in port.get("holdings", {}).values()
)
port_val_before = cash_before + holdings_val
port_val_after  = cash_after  + holdings_val

total_deposited_before = sum(
    t["pnl"] for t in port.get("trades", []) if t.get("action") == "DEPOSIT"
) + 300_000   # initial capital

print("=" * 55)
print(f"  Monthly DCA Top-Up — {today}")
print("=" * 55)
print(f"  Top-up amount    : ฿{amount:>12,.0f}")
print(f"  Cash before      : ฿{cash_before:>12,.0f}")
print(f"  Cash after       : ฿{cash_after:>12,.0f}")
print(f"  Holdings value   : ฿{holdings_val:>12,.0f}")
print(f"  Portfolio value  : ฿{port_val_after:>12,.0f}  (was ฿{port_val_before:,.0f})")
print(f"  Total deposited  : ฿{total_deposited_before + amount:>12,.0f}  (was ฿{total_deposited_before:,.0f})")
print(f"  Open positions   : {len(port.get('holdings', {}))}")
print("=" * 55)

if args.dry_run:
    print("\n  🔍  DRY RUN — no changes written.\n")
    sys.exit(0)

# ── Apply top-up ──────────────────────────────────────────────────────────────
port["cash"] = round(cash_after, 2)

# Log as DEPOSIT in trade history
deposit_entry = {
    "date":     today,
    "action":   "DEPOSIT",
    "ticker":   "—",
    "name":     "Monthly Top-up",
    "shares":   0,
    "price":    0,
    "value":    amount,
    "avg_cost": 0,
    "pnl":      amount,
    "reason":   f"DCA deposit ฿{amount:,.0f}",
    "time":     time_str,
}
port.setdefault("trades", []).append(deposit_entry)

# Update peak value if portfolio just hit a new high
new_val = cash_after + holdings_val
if new_val > port.get("peak_value", 0):
    port["peak_value"] = round(new_val, 2)

# ── Save ──────────────────────────────────────────────────────────────────────
with open(PORTFOLIO_PATH, "w", encoding="utf-8") as f:
    json.dump(port, f, indent=2, ensure_ascii=False)

print(f"\n  ✅  Portfolio updated — ฿{amount:,.0f} deposited")

# ── Refresh dashboard ─────────────────────────────────────────────────────────
if os.path.exists(DASHBOARD_UPD):
    print("  🔄  Refreshing dashboard...")
    result = subprocess.run(
        [sys.executable, DASHBOARD_UPD],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        last_line = result.stdout.strip().split("\n")[-1]
        print(f"  {last_line}")
    else:
        print(f"  ⚠️  Dashboard update failed: {result.stderr.strip()}")

print(f"\n  Next: monitor will automatically deploy cash into new signals.\n")
