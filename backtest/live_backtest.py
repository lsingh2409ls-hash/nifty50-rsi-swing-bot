"""
Full-period backtest of the live RSI swing rules on Nifty 50 daily data.
Run from the repo root:  python -m backtest.live_backtest [--save-signals]
"""
import sys, time, csv, os, pickle
from broker import get_fyers
from config import data_path
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings("ignore")

CFG = {
    "rsi_period":         9,
    "rsi_sma_period":     20,
    "entry_rsi_sma_min":  55,
    "entry_rsi_max":      80,
    "exit_rsi":           80,
    "tp_pct":             0.25,
    "sl_pct":            -0.20,
    "capital_per_trade":  25000,
    "max_positions":      40,
}
TOTAL_CAPITAL = 1_000_000
START_DATE    = "2023-03-01"
END_DATE      = "2026-03-10"
CHUNK_DAYS    = 180
NIFTY50 = [
    "ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK",
    "BAJAJ-AUTO","BAJFINANCE","BAJAJFINSV","BEL","BHARTIARTL",
    "BPCL","BRITANNIA","CIPLA","COALINDIA","DRREDDY",
    "EICHERMOT","GRASIM","HCLTECH","HDFCBANK","HDFCLIFE",
    "HEROMOTOCO","HINDALCO","HINDUNILVR","ICICIBANK","INDUSINDBK",
    "ITC","JSWSTEEL","KOTAKBANK","LT","M&M",
    "MARUTI","NESTLEIND","NTPC","ONGC","POWERGRID",
    "RELIANCE","SBIN","SBILIFE","SHRIRAMFIN","SUNPHARMA",
    "TATACONSUM","TATAMOTORS","TATASTEEL","TECHM","TITAN",
    "TRENT","ULTRACEMCO","WIPRO",
]

def to_epoch(d): return int(datetime.strptime(d, "%Y-%m-%d").timestamp())
def to_date(e):  return datetime.fromtimestamp(e).strftime("%Y-%m-%d")

def fetch_ohlc(fyers, symbol, start, end):
    candles = []
    cur = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    while cur < end_dt:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS), end_dt)
        resp = fyers.history(data={
            "symbol":      symbol,
            "resolution":  "D",
            "date_format": "0",
            "range_from":  str(to_epoch(cur.strftime("%Y-%m-%d"))),
            "range_to":    str(to_epoch(chunk_end.strftime("%Y-%m-%d"))),
            "cont_flag":   "1"
        })
        if resp.get("s") == "ok":
            candles.extend(resp.get("candles", []))
        time.sleep(1.0)
        cur = chunk_end + timedelta(days=1)
    seen, out = set(), []
    for c in sorted(candles, key=lambda x: x[0]):
        if c[0] not in seen:
            seen.add(c[0])
            out.append(c)
    return out

def candles_to_df(candles):
    if not candles: return None
    df = pd.DataFrame(candles, columns=["ts","Open","High","Low","Close","Volume"])
    df["Date"] = pd.to_datetime(df["ts"], unit="s").dt.normalize()
    df = df[["Date","Open","High","Low","Close","Volume"]].set_index("Date").sort_index()
    df = df.apply(pd.to_numeric, errors="coerce")
    return df.dropna(subset=["Close"])

def compute_rsi(close, period):
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    ag    = gain.ewm(com=period-1, min_periods=period).mean()
    al    = loss.ewm(com=period-1, min_periods=period).mean()
    rs    = ag / al.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def main():
    print("=" * 60)
    print("  Live Strategy Backtest")
    print(f"  RSI({CFG['rsi_period']}) | SMA>{CFG['entry_rsi_sma_min']} | TP+{CFG['tp_pct']*100:.0f}% | SL{CFG['sl_pct']*100:.0f}%")
    print(f"  Capital: Rs{TOTAL_CAPITAL:,.0f} | Rs{CFG['capital_per_trade']:,.0f}/trade")
    print("=" * 60)

    fyers = get_fyers()
    if not fyers:
        print("Login failed"); sys.exit(1)
    print("Login OK")

    bm_csv = data_path("nifty50_benchmark.csv")
    if not os.path.exists(bm_csv):
        print(f"ERROR: {bm_csv} not found."); sys.exit(1)
    bm_df = pd.read_csv(bm_csv, parse_dates=["Date"]).set_index("Date").sort_index()
    bm_df.index = pd.to_datetime(bm_df.index).normalize()
    etf_close = bm_df["Close"]
    n_yr = len(etf_close) / 252
    bm_ret  = (etf_close.iloc[-1] / etf_close.iloc[0] - 1) * 100
    bm_cagr = ((etf_close.iloc[-1] / etf_close.iloc[0]) ** (1/n_yr) - 1) * 100
    print(f"Benchmark: Nifty 50 | {bm_ret:.1f}% | CAGR {bm_cagr:.2f}%")

    etf_ret = etf_close.pct_change().fillna(0)
    bh_nav  = (etf_close / etf_close.iloc[0]) * TOTAL_CAPITAL

    print(f"\nFetching {len(NIFTY50)} stocks...")
    signals, failed = {}, []
    for i, ticker in enumerate(NIFTY50, 1):
        sym = f"NSE:{ticker}-EQ"
        print(f"  [{i:>2}/{len(NIFTY50)}] {ticker:<15}", end="", flush=True)
        candles = fetch_ohlc(fyers, sym, START_DATE, END_DATE)
        if not candles or len(candles) < 50:
            print(f"  SKIP ({len(candles)} candles)")
            failed.append(ticker)
            continue
        df = candles_to_df(candles)
        if df is None or len(df) < 50:
            print(f"  SKIP"); failed.append(ticker); continue
        df["RSI"]   = compute_rsi(df["Close"], CFG["rsi_period"])
        df["RSIMA"] = df["RSI"].rolling(CFG["rsi_sma_period"]).mean()
        signals[ticker] = df
        print(f"  OK ({len(df)} days)")

    print(f"\nLoaded: {len(signals)} | Failed: {failed}")
    if "--save-signals" in sys.argv:
        with open(data_path("signals_extended.pkl"), "wb") as f:
            pickle.dump(signals, f)
        print(f"Signal cache saved -> {data_path('signals_extended.pkl')}")
    print("Running simulation...")

    all_dates = sorted(set(d for df in signals.values() for d in df.index.tolist()))
    all_dates = [pd.Timestamp(d) for d in all_dates]

    cash, positions, trades, daily_nav = float(TOTAL_CAPITAL), {}, [], []
    cap = CFG["capital_per_trade"]

    for dt in all_dates:
        cash *= (1 + float(etf_ret.get(dt, 0)))

        for ticker in list(positions.keys()):
            pos = positions[ticker]
            sig = signals[ticker]
            if dt not in sig.index: continue
            row   = sig.loc[dt]
            price = float(row["Close"])
            rsi   = float(row["RSI"]) if not pd.isna(row.get("RSI", np.nan)) else None
            pnl_pct = (price - pos["entry_price"]) / pos["entry_price"]
            hold_d  = (dt - pos["entry_date"]).days
            exit_reason, exit_price = None, price

            if pnl_pct >= CFG["tp_pct"]:
                exit_reason = "TP Hit"
                exit_price  = pos["entry_price"] * (1 + CFG["tp_pct"])
            elif pnl_pct <= CFG["sl_pct"]:
                exit_reason = "SL Hit"
                exit_price  = pos["entry_price"] * (1 + CFG["sl_pct"])
            elif rsi is not None and rsi > CFG["exit_rsi"]:
                exit_reason = "RSI Exit"

            if exit_reason:
                proceeds = exit_price * pos["shares"]
                inr_pnl  = proceeds - pos["capital"]
                cash += proceeds
                trades.append({
                    "Ticker": ticker, "Entry Date": pos["entry_date"],
                    "Exit Date": dt, "Hold Days": hold_d,
                    "Entry Price": round(pos["entry_price"], 2),
                    "Exit Price": round(exit_price, 2),
                    "Capital": pos["capital"],
                    "PnL (Rs)": round(inr_pnl, 2),
                    "PnL (%)": round(pnl_pct * 100, 2),
                    "Exit RSI": round(rsi or 0, 1),
                    "Exit Reason": exit_reason,
                    "Result": "Win" if inr_pnl > 0 else "Loss"
                })
                del positions[ticker]

        for ticker, sig in signals.items():
            if ticker in positions: continue
            if len(positions) >= CFG["max_positions"]: break
            if cash < cap: break
            if dt not in sig.index: continue
            row   = sig.loc[dt]
            rsi   = float(row["RSI"])   if not pd.isna(row.get("RSI",  np.nan)) else None
            rsima = float(row["RSIMA"]) if not pd.isna(row.get("RSIMA",np.nan)) else None
            price = float(row["Close"])
            if rsi is None or rsima is None: continue
            if not (rsima > CFG["entry_rsi_sma_min"] and rsi < CFG["entry_rsi_max"]): continue
            cash -= cap
            positions[ticker] = {
                "entry_price": price, "shares": cap / price,
                "entry_date": dt, "capital": cap
            }

        open_val = sum(
            signals[tk].loc[dt]["Close"] * pos["shares"]
            if dt in signals[tk].index else pos["entry_price"] * pos["shares"]
            for tk, pos in positions.items()
        )
        daily_nav.append({"Date": dt, "NAV": cash + open_val})

    for ticker, pos in positions.items():
        lp   = float(signals[ticker]["Close"].iloc[-1])
        proc = lp * pos["shares"]
        trades.append({
            "Ticker": ticker, "Entry Date": pos["entry_date"],
            "Exit Date": all_dates[-1],
            "Hold Days": (all_dates[-1] - pos["entry_date"]).days,
            "Entry Price": round(pos["entry_price"], 2),
            "Exit Price": round(lp, 2), "Capital": pos["capital"],
            "PnL (Rs)": round(proc - pos["capital"], 2),
            "PnL (%)": round((lp / pos["entry_price"] - 1) * 100, 2),
            "Exit RSI": 0, "Exit Reason": "Open (EOD)", "Result": "Open"
        })

    nav_s   = pd.DataFrame(daily_nav).set_index("Date")["NAV"]
    closed  = [t for t in trades if t["Result"] != "Open"]
    wins    = [t for t in closed if t["Result"] == "Win"]
    losses  = [t for t in closed if t["Result"] == "Loss"]
    n_years = (nav_s.index[-1] - nav_s.index[0]).days / 365.25

    strat_end  = float(nav_s.iloc[-1])
    bm_end_val = float(bh_nav.reindex(nav_s.index, method="ffill").iloc[-1])
    strat_ret  = (strat_end  - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100
    bm_ret_s   = (bm_end_val - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100
    strat_cagr = ((strat_end  / TOTAL_CAPITAL) ** (1/n_years) - 1) * 100
    bm_cagr_s  = ((bm_end_val / TOTAL_CAPITAL) ** (1/n_years) - 1) * 100
    alpha      = strat_cagr - bm_cagr_s
    peak       = nav_s.cummax()
    max_dd     = ((nav_s - peak) / peak).min() * 100
    win_rate   = len(wins) / len(closed) * 100 if closed else 0
    pf         = (sum(t["PnL (Rs)"] for t in wins) /
                  abs(sum(t["PnL (Rs)"] for t in losses)) if losses else 999)
    avg_hold   = np.mean([t["Hold Days"] for t in closed]) if closed else 0
    exp        = np.mean([t["PnL (Rs)"] for t in closed]) if closed else 0
    mon_ret    = nav_s.resample("ME").last().pct_change().dropna()
    sharpe     = mon_ret.mean() / mon_ret.std() * np.sqrt(12) if mon_ret.std() > 0 else 0

    print(f"""
+--------------------------------------------------------------+
  BACKTEST RESULTS
  {nav_s.index[0].date()} to {nav_s.index[-1].date()}
+--------------------------------------------------------------+
  Strategy Return:  {strat_ret:>+8.2f}%   Final: Rs{strat_end:>12,.0f}
  Nifty 50 Return:  {bm_ret_s:>+8.2f}%   Final: Rs{bm_end_val:>12,.0f}
  Strategy CAGR:    {strat_cagr:>+8.2f}%
  Nifty 50 CAGR:    {bm_cagr_s:>+8.2f}%
  Alpha (CAGR):     {alpha:>+8.2f}%
+--------------------------------------------------------------+
  Win Rate:         {win_rate:>7.1f}%
  Profit Factor:    {pf:>7.2f}
  Sharpe Ratio:     {sharpe:>7.2f}
  Avg Hold:         {avg_hold:>7.1f} days
  Expectancy/trade: Rs{exp:>+8,.0f}
  Max Drawdown:     {max_dd:>+8.2f}%
+--------------------------------------------------------------+
  Closed Trades:    {len(closed):>7}  (Wins: {len(wins)} | Losses: {len(losses)})
  RSI Exits:        {sum(1 for t in closed if t['Exit Reason']=='RSI Exit'):>7}
  TP  Exits:        {sum(1 for t in closed if t['Exit Reason']=='TP Hit'):>7}
  SL  Exits:        {sum(1 for t in closed if t['Exit Reason']=='SL Hit'):>7}
  Open (EOD):       {len([t for t in trades if t['Result']=='Open']):>7}
+--------------------------------------------------------------+
""")

    out_file = data_path("backtest_trades.csv")
    keys = ["Ticker","Entry Date","Exit Date","Hold Days",
            "Entry Price","Exit Price","Capital",
            "PnL (Rs)","PnL (%)","Exit RSI","Exit Reason","Result"]
    with open(out_file, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(trades)
    print(f"Trade log saved -> {out_file}")

if __name__ == "__main__":
    main()
