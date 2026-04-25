#!/usr/bin/env python3
"""
Thai SET — Strategy Backtester
──────────────────────────────────────────────────────────────────────────────
Simulates the full set_realtime_monitor.py strategy on 5 years of historical
daily data and reports performance metrics vs the SET Index (buy-and-hold).

Usage:
    python set_backtest.py                   # full 5-year test
    python set_backtest.py --years 3         # last 3 years only
    python set_backtest.py --no-regime       # disable regime filter

Output:
    - Console: performance table + top/worst trades
    - set_backtest_results.json: full trade log + daily equity curve

Key simplifications vs live trading:
    * One signal check per day (at daily close)
    * Buys execute at next day's open + 0.3% slippage
    * Stops and take-profits checked at daily close
    * Fundamentals fetched once at start (current, not point-in-time historical)
"""

import sys, os, json, datetime, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

def ensure_packages():
    import importlib, subprocess
    for pkg in ["yfinance", "pandas", "numpy"]:
        try:
            importlib.import_module(pkg)
        except ImportError:
            for args in [
                [sys.executable, "-m", "pip", "install", pkg, "-q"],
                [sys.executable, "-m", "pip", "install", pkg, "--user", "-q"],
                [sys.executable, "-m", "pip", "install", pkg, "--break-system-packages", "-q"],
            ]:
                try:
                    subprocess.check_call(args, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
                    break
                except subprocess.CalledProcessError:
                    continue

ensure_packages()
import yfinance as yf
import pandas as pd
import numpy as np

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "set_config.json")

with open(CONFIG_PATH) as f:
    cfg = json.load(f)

# ── Parameters (mirror set_realtime_monitor.py) ───────────────────────────────
INSTRUMENTS      = [(i["name"], i["ticker"]) for i in cfg.get("instruments", [])]
SECTOR_MAP       = cfg.get("sector_map", {})
_sw              = cfg.get("scoring_weights", {})
WEIGHT_TECH      = _sw.get("technical",   0.6)
WEIGHT_FUND      = _sw.get("fundamental", 0.4)
TECH_MAX         = 5
RSI_PERIOD       = cfg.get("rsi_period", 14)
RSI_OB           = cfg.get("rsi_overbought", 70)
RSI_OS           = cfg.get("rsi_oversold", 30)
SMA_SHORT        = cfg.get("sma_short_period", 20)
SMA_LONG         = cfg.get("sma_long_period",  50)
ADX_PERIOD       = cfg.get("adx_period", 14)
VOL_SURGE_R      = cfg.get("volume_surge_ratio", 1.5)
_ff              = cfg.get("fundamental_filter", {})
FUND_ENABLED     = _ff.get("enabled", True)
MAX_PE           = _ff.get("max_pe", 20)
MAX_PBV          = _ff.get("max_pbv", 3)
MIN_ROE          = _ff.get("min_roe", 0.08)
REQ_DIVIDEND     = _ff.get("require_dividend", False)
INITIAL_CAPITAL  = 300_000.0
LOT_SIZE         = 100
TX_COST          = 0.0025
MAX_POSITIONS    = 10
CASH_FLOOR_PCT   = 0.05
BUY_SCORE_MIN    = 3
SELL_SCORE_MAX   = -2
BUY_PREV_MAX     = 0
SELL_PREV_MIN    = 0
TAKE_PROFIT_PCT  = 0.25
ATR_PERIOD       = 14
ATR_MULTIPLIER   = 2.0
ATR_FALLBACK_PCT = 0.08
REGIME_TICKER    = "^SET.BK"
REGIME_MA_PERIOD = 200
SLIPPAGE         = 0.003   # 0.3% next-day open slippage
_sc              = cfg.get("sector_concentration", {})
SECTOR_ENABLED   = _sc.get("enabled", True)
SECTOR_MAX       = _sc.get("max_per_sector", 2)
MAX_WORKERS      = 12
_FUND_MAX_RAW    = 19.0


# ── Technical indicators (vectorised) ─────────────────────────────────────────
def calc_rsi(s, n=14):
    d  = s.diff()
    ag = d.clip(lower=0).ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    al = (-d).clip(lower=0).ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    return 100 - 100 / (1 + ag / al.replace(0, 1e-10))

def calc_adx_series(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr  = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    up  = h.diff().clip(lower=0); down = (-l.diff()).clip(lower=0)
    dmp = up.where(up>down, 0.0); dmm  = down.where(down>up, 0.0)
    atr = tr.ewm(alpha=1/n, min_periods=n, adjust=False).mean()
    dip = 100*dmp.ewm(alpha=1/n,min_periods=n,adjust=False).mean()/atr.replace(0,1e-10)
    dim = 100*dmm.ewm(alpha=1/n,min_periods=n,adjust=False).mean()/atr.replace(0,1e-10)
    dx  = 100*(dip-dim).abs()/(dip+dim).replace(0,1e-10)
    return dx.ewm(alpha=1/n,min_periods=n,adjust=False).mean(), dip, dim

def calc_atr_series(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, min_periods=n, adjust=False).mean()

def compute_signals(df):
    c    = df["Close"]
    ema_s = c.ewm(span=SMA_SHORT, adjust=False).mean()
    ema_l = c.ewm(span=SMA_LONG,  adjust=False).mean()
    ml   = c.ewm(span=12,adjust=False).mean() - c.ewm(span=26,adjust=False).mean()
    msig = ml.ewm(span=9, adjust=False).mean()
    rsi  = calc_rsi(c, RSI_PERIOD)
    adx, dip, dim = calc_adx_series(df, ADX_PERIOD)
    atr  = calc_atr_series(df, ATR_PERIOD)
    avg_vol = df["Volume"].rolling(20).mean()
    vsurge  = ((df["Volume"] > VOL_SURGE_R*avg_vol)&(c>c.shift())).astype(int) - \
              ((df["Volume"] > VOL_SURGE_R*avg_vol)&(c<c.shift())).astype(int)
    rs    = rsi.apply(lambda v: 1 if v<RSI_OS else (-1 if v>RSI_OB else 0))
    ms    = (c>ema_l).astype(int)*2-1
    et    = (ema_s>ema_l).astype(int)*2-1
    mc    = (ml>msig).astype(int)*2-1
    trend = ms.where(ms==et, 0)
    adx_d = adx.apply(lambda v: 1 if v>20 else 0) * ((dip>dim).astype(int)*2-1)
    score = (rs+trend+mc+adx_d+vsurge).clip(-5,5).round().astype(int)
    return pd.DataFrame({"close":c,"open":df["Open"],"score":score,"atr":atr})


# ── Fundamental scoring ───────────────────────────────────────────────────────
def _f(v):
    if v is None: return None
    try:
        x = float(v); return x if x==x else None
    except: return None

def calc_fund_score(fund):
    if not fund: return 5.0
    s=0.0
    pe=_f(fund.get("pe")); pbv=_f(fund.get("pbv")); roe=_f(fund.get("roe"))
    has_d=fund.get("has_div",False); dyld=_f(fund.get("div_yld")) or 0.0
    de=_f(fund.get("de_ratio")); eg=_f(fund.get("eps_growth")); fcf=_f(fund.get("fcf_yield"))
    if pe is None:     s+=1
    elif pe<=0:        s+=0
    elif pe<=8:        s+=3
    elif pe<=12:       s+=2
    elif pe<=15:       s+=1
    if pbv is None:    s+=1
    elif pbv<1.0:      s+=3
    elif pbv<=1.5:     s+=2
    elif pbv<=3:       s+=1
    if roe is None:    s+=1
    elif roe>=0.20:    s+=3
    elif roe>=0.12:    s+=2
    elif roe>=0.08:    s+=1
    if has_d:
        if dyld>=0.09:   s+=4.0
        elif dyld>=0.08: s+=3.5
        elif dyld>=0.065:s+=3.0
        elif dyld>=0.05: s+=2.5
        elif dyld>=0.03: s+=2.0
        else:            s+=1.0
    if de is None:     s+=0
    elif de<0.5:       s+=2
    elif de<1.0:       s+=1
    if eg is None:     s+=0
    elif eg>=0.15:     s+=2
    elif eg>0.0:       s+=1
    if fcf is None:    s+=0
    elif fcf>=0.06:    s+=2
    elif fcf>=0.03:    s+=1
    return round(min(10.0, s/_FUND_MAX_RAW*10.0), 1)

def comp_score(tech, fund):
    return round((tech+TECH_MAX)/(2.0*TECH_MAX)*10.0*WEIGHT_TECH + fund*WEIGHT_FUND, 2)

def fund_ok(fund):
    if not FUND_ENABLED or not fund: return True
    pe=fund.get("pe"); pbv=fund.get("pbv"); roe=fund.get("roe")
    has_d=fund.get("has_div",True)
    if pe is not None and pe>0 and pe>MAX_PE: return False
    if pe is not None and pe<0: return False
    if pbv is not None and pbv>MAX_PBV: return False
    if roe is not None and roe<MIN_ROE: return False
    if REQ_DIVIDEND and not has_d: return False
    return True


# ── Data fetching ─────────────────────────────────────────────────────────────
def fetch_hist(ticker, years):
    try:
        df = yf.Ticker(ticker).history(period=f"{years+1}y", auto_adjust=True)
        if df is None or df.empty: return None
        df.index = pd.to_datetime(df.index).tz_localize(None)
        return df[["Open","High","Low","Close","Volume"]].dropna()
    except Exception as e:
        return None

def fetch_fund(ticker):
    try:
        tk=yf.Ticker(ticker); info=tk.info
        pe=info.get("trailingPE") or info.get("forwardPE")
        pbv=info.get("priceToBook"); roe=info.get("returnOnEquity")
        dyld=info.get("dividendYield") or 0.0
        try:
            divs=tk.dividends; cutoff=pd.Timestamp.now(tz="UTC")-pd.DateOffset(years=3)
            has_div=len(divs[divs.index>=cutoff])>0
        except: has_div=dyld>0
        de_raw=info.get("debtToEquity")
        de=float(de_raw)/100.0 if de_raw is not None else None
        eg=info.get("earningsGrowth")
        fcf=info.get("freeCashflow"); mc=info.get("marketCap")
        fcfy=float(fcf)/float(mc) if fcf and mc and mc>0 else None
        return {"pe":pe,"pbv":pbv,"roe":roe,"div_yld":dyld,"has_div":has_div,
                "de_ratio":de,"eps_growth":eg,"fcf_yield":fcfy}
    except: return {}


# ── Main backtest ─────────────────────────────────────────────────────────────
def run_backtest(years=5, regime_enabled=True):
    print("="*60)
    print("SET Strategy Backtester")
    print(f"Period: {years} years  |  Capital: ฿{INITIAL_CAPITAL:,.0f}")
    print(f"Regime filter: {'ON' if regime_enabled else 'OFF'}")
    print("="*60)

    # Fetch history
    print(f"\n[1/4] Fetching {len(INSTRUMENTS)} instruments...")
    all_data = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(fetch_hist,tk,years):(nm,tk) for nm,tk in INSTRUMENTS}
        done=0
        for future in as_completed(futs):
            nm,tk=futs[future]; df=future.result(); done+=1
            if df is not None and len(df)>SMA_LONG+10:
                all_data[tk]=(nm,df)
            sys.stdout.write(f"\r  {done}/{len(INSTRUMENTS)} fetched..."); sys.stdout.flush()
    print(f"\r  {len(all_data)}/{len(INSTRUMENTS)} instruments loaded.     ")

    # SET regime
    print("[2/4] Fetching SET Index for regime...")
    set_df = fetch_hist(REGIME_TICKER, years)
    if set_df is not None:
        sc = set_df["Close"]
        regime_bull = (sc > sc.rolling(REGIME_MA_PERIOD).mean()).reindex(
            method="ffill")
        print(f"  {len(set_df)} days of SET data loaded.")
    else:
        regime_bull = None
        print("  Could not load SET — regime disabled.")

    # Fundamentals
    print(f"[3/4] Fetching fundamentals (takes ~3 min)...")
    fund_cache = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(fetch_fund,tk):tk for tk in all_data}
        done=0
        for future in as_completed(futs):
            tk=futs[future]; fund_cache[tk]=future.result(); done+=1
            sys.stdout.write(f"\r  {done}/{len(all_data)} done..."); sys.stdout.flush()
    print(f"\r  {len(fund_cache)} fundamentals loaded.     ")
    fs_map  = {tk: calc_fund_score(f) for tk,f in fund_cache.items()}
    fok_map = {tk: fund_ok(f)         for tk,f in fund_cache.items()}

    # Compute signals
    print("[4/4] Computing daily signals...")
    sig = {}
    for tk,(nm,df) in all_data.items():
        try: sig[tk]=compute_signals(df)
        except: pass
    print(f"  Done. {len(sig)} tickers with signals.\n")

    # Trading days
    cutoff = (datetime.date.today()-datetime.timedelta(days=years*365)).isoformat()
    all_dates = sorted({d for tk in sig for d in sig[tk].index
                        if str(d.date())>=cutoff})
    print(f"Simulating {len(all_dates)} trading days...")

    # ── Simulation loop ───────────────────────────────────────────────────────
    cash=INITIAL_CAPITAL; holdings={}; trades=[]; equity_curve=[]
    prev_scores={}

    def pval(px): return cash+sum(h["shares"]*px.get(tk,h["avg_cost"])
                                  for tk,h in holdings.items())

    for date in all_dates:
        prices = {tk: float(sig[tk].loc[date,"close"])
                  for tk in sig if date in sig[tk].index}

        is_bull = True
        if regime_enabled and regime_bull is not None and date in regime_bull.index:
            is_bull = bool(regime_bull.loc[date])

        equity_curve.append({"date":str(date.date()),"value":round(pval(prices),2),
                             "cash":round(cash,2),"n":len(holdings),
                             "regime":"BULL" if is_bull else "BEAR"})

        # Take-profit + ATR stop
        for tk in list(holdings.keys()):
            h=holdings[tk]; px=prices.get(tk,h["avg_cost"])
            gain=(px-h["avg_cost"])/h["avg_cost"]
            if gain>=TAKE_PROFIT_PCT:
                proceeds=h["shares"]*px*(1-TX_COST)
                pnl=proceeds-h["shares"]*h["avg_cost"]*(1+TX_COST)
                cash+=proceeds
                trades.append({"date":str(date.date()),"action":"SELL","ticker":tk,
                    "name":h["name"],"shares":h["shares"],"price":round(px,2),
                    "avg_cost":round(h["avg_cost"],2),"pnl":round(pnl,2),
                    "reason":f"Take-profit +{gain*100:.1f}%",
                    "hold_days":(date.date()-h["entry_date"]).days})
                del holdings[tk]; continue
            atr_stop=h.get("atr_stop",h["avg_cost"]*(1-ATR_FALLBACK_PCT))
            if px<=atr_stop:
                drop=(1-px/h["avg_cost"])*100
                proceeds=h["shares"]*px*(1-TX_COST)
                pnl=proceeds-h["shares"]*h["avg_cost"]*(1+TX_COST)
                cash+=proceeds
                trades.append({"date":str(date.date()),"action":"SELL","ticker":tk,
                    "name":h["name"],"shares":h["shares"],"price":round(px,2),
                    "avg_cost":round(h["avg_cost"],2),"pnl":round(pnl,2),
                    "reason":f"ATR stop -{drop:.1f}%",
                    "hold_days":(date.date()-h["entry_date"]).days})
                del holdings[tk]

        # Signal-based sells
        for tk in list(holdings.keys()):
            if tk not in sig or date not in sig[tk].index: continue
            sc=int(sig[tk].loc[date,"score"]); ps=prev_scores.get(tk,0)
            if sc<=SELL_SCORE_MAX and ps>=SELL_PREV_MIN:
                h=holdings[tk]; px=prices.get(tk,h["avg_cost"])
                proceeds=h["shares"]*px*(1-TX_COST)
                pnl=proceeds-h["shares"]*h["avg_cost"]*(1+TX_COST)
                cash+=proceeds
                trades.append({"date":str(date.date()),"action":"SELL","ticker":tk,
                    "name":h["name"],"shares":h["shares"],"price":round(px,2),
                    "avg_cost":round(h["avg_cost"],2),"pnl":round(pnl,2),
                    "reason":f"SELL score {sc:+d}",
                    "hold_days":(date.date()-h["entry_date"]).days})
                del holdings[tk]

        # Buys (BULL only)
        if is_bull and len(holdings)<MAX_POSITIONS:
            buys=[]
            for tk,(nm,_) in all_data.items():
                if tk in holdings or SECTOR_MAP.get(tk)=="INDEX": continue
                if tk not in sig or date not in sig[tk].index: continue
                if not fok_map.get(tk,True): continue
                sc=int(sig[tk].loc[date,"score"]); ps=prev_scores.get(tk,0)
                if sc>=BUY_SCORE_MIN and ps<=BUY_PREV_MAX:
                    atr=float(sig[tk].loc[date,"atr"])
                    buys.append({"ticker":tk,"name":nm,"score":sc,
                                 "comp":comp_score(sc,fs_map.get(tk,5.0)),"atr":atr})
            buys.sort(key=lambda x:-x["comp"])
            for bc in buys:
                if len(holdings)>=MAX_POSITIONS: break
                tk=bc["ticker"]
                if SECTOR_ENABLED:
                    sec=SECTOR_MAP.get(tk,"OTHER")
                    if sec not in ("OTHER","INDEX"):
                        if sum(1 for t in holdings if SECTOR_MAP.get(t,"OTHER")==sec)>=SECTOR_MAX:
                            continue
                next_ds=[d for d in sig[tk].index if d>date]
                if not next_ds: continue
                nd=next_ds[0]
                buy_px=float(sig[tk].loc[nd,"open"])*(1+SLIPPAGE)
                if buy_px<=0: continue
                avail=cash-INITIAL_CAPITAL*CASH_FLOOR_PCT
                n_free=max(1,MAX_POSITIONS-len(holdings))
                alloc=min(avail/n_free,INITIAL_CAPITAL/MAX_POSITIONS*1.5)
                shares=int(alloc/buy_px/LOT_SIZE)*LOT_SIZE
                if shares<=0: shares=LOT_SIZE
                cost=shares*buy_px*(1+TX_COST)
                if cost>avail: continue
                atr_v=bc["atr"]
                atr_stop=round(buy_px-ATR_MULTIPLIER*atr_v,2) if atr_v>0 \
                         else round(buy_px*(1-ATR_FALLBACK_PCT),2)
                cash-=cost
                holdings[tk]={"name":bc["name"],"shares":shares,
                              "avg_cost":round(buy_px,2),"entry_date":nd.date(),
                              "atr_stop":atr_stop,"atr":round(atr_v,4)}
                trades.append({"date":str(nd.date()),"action":"BUY","ticker":tk,
                    "name":bc["name"],"shares":shares,"price":round(buy_px,2),
                    "avg_cost":round(buy_px,2),"pnl":0,
                    "reason":f"BUY score {bc['score']:+d} comp {bc['comp']:.1f}","hold_days":0})

        for tk in sig:
            if date in sig[tk].index:
                prev_scores[tk]=int(sig[tk].loc[date,"score"])

    # Close remaining positions
    last_date=str(all_dates[-1].date())
    last_px={tk:float(sig[tk]["close"].iloc[-1]) for tk in sig if not sig[tk].empty}
    for tk,h in list(holdings.items()):
        px=last_px.get(tk,h["avg_cost"])
        proceeds=h["shares"]*px*(1-TX_COST)
        pnl=proceeds-h["shares"]*h["avg_cost"]*(1+TX_COST)
        cash+=proceeds
        trades.append({"date":last_date,"action":"SELL","ticker":tk,"name":h["name"],
            "shares":h["shares"],"price":round(px,2),"avg_cost":round(h["avg_cost"],2),
            "pnl":round(pnl,2),"reason":"End of backtest",
            "hold_days":(all_dates[-1].date()-h["entry_date"]).days})

    # ── Performance ───────────────────────────────────────────────────────────
    final_val    = cash
    total_ret    = (final_val-INITIAL_CAPITAL)/INITIAL_CAPITAL*100
    ann_ret      = ((final_val/INITIAL_CAPITAL)**(1/years)-1)*100
    eq           = np.array([e["value"] for e in equity_curve],dtype=float)
    dr           = np.diff(eq)/eq[:-1]
    sharpe       = dr.mean()/dr.std()*np.sqrt(252) if dr.std()>0 else 0
    peak         = np.maximum.accumulate(eq)
    max_dd       = float(((eq-peak)/peak*100).min())
    sells        = [t for t in trades if t["action"]=="SELL"
                    and t["reason"]!="End of backtest"]
    wins         = [t for t in sells if t["pnl"]>0]
    losses       = [t for t in sells if t["pnl"]<=0]
    win_rate     = len(wins)/len(sells)*100 if sells else 0
    avg_win      = sum(t["pnl"] for t in wins)/len(wins)     if wins   else 0
    avg_loss     = sum(t["pnl"] for t in losses)/len(losses) if losses else 0
    pf           = abs(avg_win/avg_loss) if avg_loss!=0 else float("inf")
    avg_hold     = sum(t.get("hold_days",0) for t in sells)/len(sells) if sells else 0
    set_ret=None
    if set_df is not None:
        dates_set={e["date"] for e in equity_curve}
        s_filt=set_df[[str(d.date()) in dates_set for d in set_df.index]]
        if not s_filt.empty:
            set_ret=(float(s_filt["Close"].iloc[-1])-float(s_filt["Close"].iloc[0]))/float(s_filt["Close"].iloc[0])*100

    print("\n"+"="*60)
    print("BACKTEST RESULTS")
    print("="*60)
    print(f"Period         : {str(all_dates[0].date())} → {str(all_dates[-1].date())}")
    print(f"Initial capital: ฿{INITIAL_CAPITAL:>12,.0f}")
    print(f"Final value    : ฿{final_val:>12,.0f}")
    print(f"Total return   : {total_ret:>+.1f}%")
    print(f"Annual return  : {ann_ret:>+.1f}%")
    if set_ret is not None:
        print(f"SET B&H return : {set_ret:>+.1f}%  (same period)")
        print(f"Alpha vs SET   : {total_ret-set_ret:>+.1f}%")
    print(f"Sharpe ratio   : {sharpe:.2f}")
    print(f"Max drawdown   : {max_dd:.1f}%")
    print("─"*60)
    print(f"Total trades   : {len(sells)}")
    print(f"Win rate       : {win_rate:.1f}%  ({len(wins)} wins / {len(losses)} losses)")
    print(f"Avg win        : ฿{avg_win:>+,.0f}")
    print(f"Avg loss       : ฿{avg_loss:>+,.0f}")
    print(f"Profit factor  : {pf:.2f}x")
    print(f"Avg hold       : {avg_hold:.0f} days")
    print("─"*60)
    sells.sort(key=lambda x:-x["pnl"])
    print("\nTop 5 winning trades:")
    for t in sells[:5]:
        print(f"  {t['date']}  {t['name']:10s}  +฿{t['pnl']:,.0f}  [{t['reason'][:28]}]  {t['hold_days']}d")
    print("\nTop 5 losing trades:")
    for t in sells[-5:][::-1]:
        print(f"  {t['date']}  {t['name']:10s}   ฿{t['pnl']:,.0f}  [{t['reason'][:28]}]  {t['hold_days']}d")

    # Save
    results={"meta":{"run_date":datetime.date.today().isoformat(),
                      "years":years,"regime":regime_enabled,
                      "capital":INITIAL_CAPITAL},
             "performance":{"final_value":round(final_val,2),
                            "total_return":round(total_ret,2),
                            "annual_return":round(ann_ret,2),
                            "set_bah_return":round(set_ret,2) if set_ret else None,
                            "alpha":round(total_ret-(set_ret or 0),2),
                            "sharpe":round(sharpe,2),
                            "max_drawdown":round(max_dd,2),
                            "n_trades":len(sells),"win_rate":round(win_rate,1),
                            "profit_factor":round(pf,2),
                            "avg_hold_days":round(avg_hold,1)},
             "trades":trades,"equity_curve":equity_curve}
    out=os.path.join(SCRIPT_DIR,"set_backtest_results.json")
    with open(out,"w",encoding="utf-8") as f:
        json.dump(results,f,indent=2,default=str)
    print(f"\n✅ Results saved → set_backtest_results.json")
    print(f"   To view the equity curve, open set_dashboard.html\n")
    return results


if __name__=="__main__":
    parser=argparse.ArgumentParser(description="SET Strategy Backtester")
    parser.add_argument("--years",    type=int,  default=5)
    parser.add_argument("--no-regime",action="store_true")
    args=parser.parse_args()
    run_backtest(years=args.years, regime_enabled=not args.no_regime)
