"""
Re-entry cooldown study: re-runs the strategy with 0/7/14/21/30-day cooldowns
after each exit, with full brokerage/STT/stamp/DP charges and next-day-open fills.
Requires the signal cache:  python -m backtest.live_backtest --save-signals
Run from the repo root:     python -m backtest.cooldown_comparison
"""
import sys, os, pickle
from broker import get_fyers
from config import data_path
import pandas as pd, numpy as np
from datetime import datetime
import warnings
warnings.filterwarnings("ignore")

SIM_START     = "2025-05-01"
SIM_END       = "2026-05-08"
TOTAL_CAPITAL = 1_000_000
CPT           = 25000
MAX_POS       = 40
EXT_CACHE     = data_path("signals_extended.pkl")

def to_epoch(d): return int(datetime.strptime(d, "%Y-%m-%d").timestamp())

def stock_charges(buy_value, sell_value):
    brok=40; stt=0.001*sell_value; exc=0.0000335*(buy_value+sell_value)
    sebi=0.000001*(buy_value+sell_value); stamp=0.00015*buy_value; dp=15.93
    gst=0.18*(brok+exc+sebi)
    return round(brok+stt+exc+sebi+stamp+dp+gst, 2)

def etf_charges(v, is_buy):
    brok=20; stt=0.001*v if not is_buy else 0; exc=0.0000335*v
    sebi=0.000001*v; stamp=0.00015*v if is_buy else 0; dp=15.93 if not is_buy else 0
    gst=0.18*(brok+exc+sebi)
    return round(brok+stt+exc+sebi+stamp+dp+gst, 2)

def simulate(signals, etf_ret, all_dates, cooldown_days=0):
    cap=CPT; cash=float(TOTAL_CAPITAL); positions,trades,daily_nav={},[],[]
    pending_sells,pending_buys,cooldown={},{},{}; total_charges=0.0

    for dt in all_dates:
        cash *= (1+float(etf_ret.get(dt,0)))

        for ticker,pdata in list(pending_sells.items()):
            if ticker not in positions: continue
            if dt not in signals[ticker].index: continue
            ep=float(signals[ticker].loc[dt]["Open"]); pos=positions[ticker]
            proceeds=ep*pos["shares"]; inr_pnl=proceeds-pos["capital"]
            chg=stock_charges(pos["entry_price"]*pos["shares"],proceeds)+etf_charges(proceeds,True)
            cash+=proceeds-chg; total_charges+=chg
            trades.append({"Ticker":ticker,"Entry Date":pos["entry_date"].date(),
                "Exit Date":dt.date(),"Hold Days":(dt-pos["entry_date"]).days,
                "Entry Price":round(pos["entry_price"],2),"Exit Price":round(ep,2),
                "Gross PnL":round(inr_pnl,2),"Charges":round(chg,2),
                "Net PnL":round(inr_pnl-chg,2),"Exit Reason":pdata["reason"],
                "Result":"Win" if (inr_pnl-chg)>0 else "Loss"})
            del positions[ticker]
            if cooldown_days>0: cooldown[ticker]=dt+pd.Timedelta(days=cooldown_days)
        pending_sells={}

        for ticker in list(pending_buys.keys()):
            if ticker in positions: continue
            if len(positions)>=MAX_POS: break
            if cash<cap: break
            if dt not in signals[ticker].index: continue
            if cooldown_days>0 and ticker in cooldown and dt<cooldown[ticker]: continue
            ep=float(signals[ticker].loc[dt]["Open"])
            chg=etf_charges(cap,False)+stock_charges(cap,0)*0.5
            cash-=cap+chg; total_charges+=chg
            positions[ticker]={"entry_price":ep,"shares":cap/ep,"entry_date":dt,"capital":cap}
        pending_buys={}

        for ticker in list(positions.keys()):
            pos=positions[ticker]; sig=signals[ticker]
            if dt not in sig.index: continue
            row=sig.loc[dt]; price=float(row["Close"])
            rsi=float(row["RSI"]) if not pd.isna(row.get("RSI",np.nan)) else None
            pnl_pct=(price-pos["entry_price"])/pos["entry_price"]; er,ep=None,price
            if   pnl_pct>=0.25:             er,ep="TP Hit",  pos["entry_price"]*1.25
            elif pnl_pct<=-0.20:            er,ep="SL Hit",  pos["entry_price"]*0.80
            elif rsi is not None and rsi>80: er    ="RSI Exit"
            if er: pending_sells[ticker]={"reason":er}

        for ticker,sig in signals.items():
            if ticker in positions or ticker in pending_sells or ticker in pending_buys: continue
            if len(positions)>=MAX_POS: break
            if cash<cap: break
            if dt not in sig.index: continue
            if cooldown_days>0 and ticker in cooldown and dt<cooldown[ticker]: continue
            row=sig.loc[dt]
            rsi=float(row["RSI"])   if not pd.isna(row.get("RSI",  np.nan)) else None
            rsima=float(row["RSIMA"]) if not pd.isna(row.get("RSIMA",np.nan)) else None
            price=float(row["Close"])
            if rsi is None or rsima is None: continue
            if not (rsima>55 and rsi<80): continue
            pending_buys[ticker]={}

        open_val=sum(signals[tk].loc[dt]["Close"]*pos["shares"]
            if dt in signals[tk].index else pos["entry_price"]*pos["shares"]
            for tk,pos in positions.items())
        daily_nav.append({"Date":dt,"NAV":cash+open_val})

    last_dt=all_dates[-1]
    for ticker,pos in positions.items():
        sig=signals[ticker]
        lp=float(sig.loc[last_dt]["Close"]) if last_dt in sig.index else pos["entry_price"]
        pnl=(lp*pos["shares"])-pos["capital"]
        trades.append({"Ticker":ticker,"Entry Date":pos["entry_date"].date(),
            "Exit Date":last_dt.date(),"Hold Days":(last_dt-pos["entry_date"]).days,
            "Entry Price":round(pos["entry_price"],2),"Exit Price":round(lp,2),
            "Gross PnL":round(pnl,2),"Charges":0,"Net PnL":round(pnl,2),
            "Exit Reason":"Open (EOD)","Result":"Open"})

    nav_s=pd.DataFrame(daily_nav).set_index("Date")["NAV"]
    closed=[t for t in trades if t["Result"]!="Open"]
    wins=[t for t in closed if t["Result"]=="Win"]
    losses=[t for t in closed if t["Result"]=="Loss"]
    open_t=[t for t in trades if t["Result"]=="Open"]
    n_years=(nav_s.index[-1]-nav_s.index[0]).days/365.25
    strat_end=float(nav_s.iloc[-1])
    peak=nav_s.cummax(); mon_ret=nav_s.resample("ME").last().pct_change().dropna()
    gross_pnl=sum(t["Gross PnL"] for t in closed)
    return {
        "final":strat_end,"ret":(strat_end-TOTAL_CAPITAL)/TOTAL_CAPITAL*100,
        "cagr":((strat_end/TOTAL_CAPITAL)**(1/n_years)-1)*100,
        "gross_pnl":gross_pnl,"net_pnl":sum(t["Net PnL"] for t in closed),
        "total_charges":total_charges,
        "charges_pct":total_charges/gross_pnl*100 if gross_pnl>0 else 0,
        "unrealised":sum(t["Net PnL"] for t in open_t),
        "closed":len(closed),"wins":len(wins),"losses":len(losses),"open":len(open_t),
        "win_rate":len(wins)/len(closed)*100 if closed else 0,
        "pf":sum(t["Net PnL"] for t in wins)/abs(sum(t["Net PnL"] for t in losses)) if losses else 999,
        "sharpe":mon_ret.mean()/mon_ret.std()*np.sqrt(12) if mon_ret.std()>0 else 0,
        "max_dd":((nav_s-peak)/peak).min()*100,
        "avg_hold":np.mean([t["Hold Days"] for t in closed]) if closed else 0,
        "tp":sum(1 for t in closed if t["Exit Reason"]=="TP Hit"),
        "sl":sum(1 for t in closed if t["Exit Reason"]=="SL Hit"),
        "rsi":sum(1 for t in closed if t["Exit Reason"]=="RSI Exit"),
        "nav":nav_s,"trades":trades,
    }

def main():
    print("="*72)
    print(f"  Cooldown Period Comparison  |  {SIM_START} to {SIM_END}")
    print(f"  Capital: Rs{TOTAL_CAPITAL:,.0f}  |  Rs{CPT:,.0f}/trade  |  {MAX_POS} seats")
    print("="*72)

    with open(EXT_CACHE,"rb") as f: signals=pickle.load(f)
    print(f"Loaded {len(signals)} stocks\n")

    bm_df=pd.read_csv(data_path("nifty50_benchmark.csv"),parse_dates=["Date"]).set_index("Date").sort_index()
    fyers=get_fyers()
    resp=fyers.history(data={"symbol":"NSE:NIFTY50-INDEX","resolution":"D","date_format":"0",
        "range_from":str(to_epoch("2025-02-01")),"range_to":str(to_epoch(SIM_END)),"cont_flag":"1"})
    if resp.get("s")=="ok":
        bm_new=pd.DataFrame(resp["candles"],columns=["ts","Open","High","Low","Close","Volume"])
        bm_new["Date"]=pd.to_datetime(bm_new["ts"],unit="s").dt.normalize()
        bm_new=bm_new.set_index("Date")[["Close"]].apply(pd.to_numeric,errors="coerce")
        bm_full=pd.concat([bm_df[["Close"]],bm_new]).sort_index()
        bm_full=bm_full[~bm_full.index.duplicated(keep="last")]
    else: bm_full=bm_df[["Close"]]

    etf_close=bm_full["Close"]; etf_ret=etf_close.pct_change().fillna(0)
    sim_s=pd.Timestamp(SIM_START); sim_e=pd.Timestamp(SIM_END)
    all_dates=sorted(set(d for df in signals.values() for d in df.index.tolist()))
    all_dates=[d for d in [pd.Timestamp(d) for d in all_dates] if sim_s<=d<=sim_e]

    bm_s=float(etf_close.get(all_dates[0],etf_close.iloc[0]))
    bm_e=float(etf_close.get(all_dates[-1],etf_close.iloc[-1]))
    n_yrs=(all_dates[-1]-all_dates[0]).days/365.25
    bm_ret=(bm_e-bm_s)/bm_s*100; bm_cagr=((bm_e/bm_s)**(1/n_yrs)-1)*100
    print(f"Benchmark: Nifty 50  {bm_ret:.1f}%  |  CAGR {bm_cagr:.2f}%")
    print(f"Simulation: {len(all_dates)} trading days\n")

    COOLDOWNS=[0,7,14,21,30]; results={}
    for cd in COOLDOWNS:
        label=f"{cd}d" if cd>0 else "none"
        print(f"  Cooldown {label:>4}...",end="",flush=True)
        r=simulate(signals,etf_ret,all_dates,cooldown_days=cd); results[cd]=r
        print(f"  trades={r['closed']:>3}  charges=Rs{r['total_charges']:>6,.0f}  "
              f"net alpha={r['cagr']-bm_cagr:>+.2f}%  CAGR={r['cagr']:>+.2f}%")

    print(f"""
+-------+--------+----------+---------+---------+----------+------+-------+--------+--------+
  Cool   Trades  Charges   Chg/Trade  Chg%     Net CAGR   Win   Profit  Sharpe  Alpha
  down                                Gross PnL             Rate  Factor          (net)
+-------+--------+----------+---------+---------+----------+------+-------+--------+--------+""")
    for cd,r in results.items():
        label=f"{cd}d" if cd>0 else "none"; alpha_net=r['cagr']-bm_cagr
        avg_chg=r['total_charges']/r['closed'] if r['closed'] else 0
        flag=" <--current" if cd==0 else ""
        print(f"  {label:<6} {r['closed']:>5}   Rs{r['total_charges']:>6,.0f}   "
              f"Rs{avg_chg:>5,.0f}   {r['charges_pct']:>5.1f}%    "
              f"{r['cagr']:>+7.2f}%   {r['win_rate']:>4.0f}%   "
              f"{r['pf']:>4.2f}    {r['sharpe']:>5.2f}   {alpha_net:>+6.2f}%{flag}")
    print("+-------+--------+----------+---------+---------+----------+------+-------+--------+--------+")

    print("\nTop churning stocks (no cooldown):")
    from collections import Counter
    all_t=[t for t in results[0]["trades"] if t["Result"]!="Open"]
    churn=Counter(t["Ticker"] for t in all_t)
    for ticker,count in churn.most_common(10):
        total_chg=sum(t["Charges"] for t in all_t if t["Ticker"]==ticker)
        total_pnl=sum(t["Net PnL"] for t in all_t if t["Ticker"]==ticker)
        print(f"  {ticker:<14} {count:>2} trades  charges Rs{total_chg:>5,.0f}  net PnL Rs{total_pnl:>+7,.0f}")

    best_cd=max(results.keys(),key=lambda x:results[x]['cagr'])
    best_lbl=f"{best_cd}d" if best_cd>0 else "none"
    best_alpha=results[best_cd]['cagr']-bm_cagr
    curr_alpha=results[0]['cagr']-bm_cagr
    print(f"\nConclusion:")
    print(f"  Current (no cooldown):  net alpha {curr_alpha:>+.2f}%  |  "
          f"charges Rs{results[0]['total_charges']:,.0f}  |  {results[0]['closed']} trades")
    print(f"  Best ({best_lbl} cooldown): net alpha {best_alpha:>+.2f}%  |  "
          f"charges Rs{results[best_cd]['total_charges']:,.0f}  |  {results[best_cd]['closed']} trades")
    print(f"  Alpha improvement:  {best_alpha-curr_alpha:>+.2f}%")
    print(f"  Charges saved:      Rs{results[0]['total_charges']-results[best_cd]['total_charges']:,.0f}")

    print(f"\nMonthly: no cooldown vs best ({best_lbl}):")
    nav_0_m=results[0]['nav'].resample("ME").last()
    nav_b_m=results[best_cd]['nav'].resample("ME").last()
    print(f"  {'Month':<8} {'No cooldown':>12} {best_lbl:>12} {'Diff':>10}")
    print("  "+"-"*48)
    for dt in nav_0_m.index:
        if dt in nav_b_m.index:
            a,b=nav_0_m[dt],nav_b_m[dt]
            print(f"  {str(dt.date())[:7]:<8} Rs{a:>9,.0f}  Rs{b:>9,.0f}  Rs{b-a:>+8,.0f}")

if __name__=="__main__":
    main()
