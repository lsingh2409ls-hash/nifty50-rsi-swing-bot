"""
Portfolio Benchmark Comparison
================================
Compares portfolio performance vs Nifty 50, Nifty 100, Nifty 500
Tracks from PORTFOLIO_START_DATE (set in .env)

Usage:
    python3 benchmark.py          # print to terminal
    python3 benchmark.py --tg     # also send to Telegram
"""

import json, os, sys, requests, math
from datetime import datetime, date, timedelta
from broker import get_fyers
import config

def _f(val, spec=".2f"):
    """Safe formatter — returns N/A if val is None."""
    return format(val, spec) if val is not None else "N/A"


POSITIONS_FILE = config.data_path('positions.json')
HISTORY_FILE   = config.data_path('portfolio_history.jsonl')
TRADE_LOG      = config.data_path('trade_log.jsonl')
NIFTYBEES      = "NSE:NIFTYBEES-EQ"
START_DATE     = config.PORTFOLIO_START_DATE
START_CAPITAL  = config.PORTFOLIO_START_CAPITAL

TG_TOKEN = config.TELEGRAM_TOKEN
TG_CHAT  = config.TELEGRAM_CHAT_ID

BENCHMARKS = {
    "Nifty 50":  "NSE:NIFTY50-INDEX",
    "Nifty 100": "NSE:NIFTY100-INDEX",
    "Nifty 500": "NSE:NIFTY500-INDEX",
    "Nifty 50 EW": "NSE:NIFTY50EQLWGT-INDEX",
}

def alert(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg}, timeout=10)
    except Exception as e:
        print(f"[Telegram error] {e}")

def fetch_index_candles(fyers, symbol, start, end):
    import time
    for attempt in range(3):
        try:
            resp = fyers.history(data={
                "symbol":      symbol,
                "resolution":  "D",
                "date_format": "1",
                "range_from":  start,
                "range_to":    end,
                "cont_flag":   "1"
            })
            if resp.get("s") == "ok" and resp.get("candles"):
                return resp["candles"]
            msg = resp.get("message", "")
            print(f"[fetch_index_candles] {symbol} attempt {attempt+1}: s={resp.get('s')} msg={msg}")
            if "limit" in msg.lower():
                time.sleep(10)
            else:
                return None
        except Exception as e:
            print(f"[fetch error] {symbol}: {e}")
            return None
    return None

def get_live_price(fyers, symbol):
    try:
        data = fyers.quotes({"symbols": symbol})
        return float(data['d'][0]['v']['lp'])
    except Exception as e:
        print(f"[price error] {symbol}: {e}")
        return None

def get_live_prices_batch(fyers, symbols):
    """Fetch all prices in a single API call."""
    try:
        joined = ",".join(symbols)
        data   = fyers.quotes({"symbols": joined})
        return {item['n']: item['v']['lp'] for item in data.get('d', []) if 'v' in item}
    except Exception as e:
        print(f"[batch price error] {e}")
        return {}

def get_portfolio_value(fyers, positions):
    stock_pos = {k: v for k, v in positions.items() if k != "_meta"}
    meta      = positions.get("_meta", {})
    total     = 0

    # Build LTP fallback map from holdings + positions API (works after market hours)
    ltp_fallback = {}
    try:
        h = fyers.holdings()
        for hld in h.get("holdings", []):
            ltp_fallback[hld["symbol"]] = hld["ltp"]
    except Exception as e:
        print(f"[holdings fallback error] {e}")
    try:
        pos_resp = fyers.positions()
        for p in pos_resp.get("netPositions", []):
            sym = p.get("symbol")
            ltp = p.get("ltp", 0)
            if sym and ltp and sym not in ltp_fallback:
                ltp_fallback[sym] = ltp
    except Exception as e:
        print(f"[positions fallback error] {e}")

    # Batch fetch all live prices in one API call
    all_symbols = list(stock_pos.keys()) + [NIFTYBEES]
    batch_prices = get_live_prices_batch(fyers, all_symbols)

    for symbol, pos in stock_pos.items():
        price = batch_prices.get(symbol) or ltp_fallback.get(symbol)
        if price:
            total += float(price) * pos.get("shares", 0)
        else:
            total += pos.get("entry_price", 0) * pos.get("shares", 0)

    etf_price = batch_prices.get(NIFTYBEES) or ltp_fallback.get(NIFTYBEES)
    if etf_price:
        total += float(etf_price) * meta.get("etf_units", 0)
    else:
        total += meta.get("etf_avg_price", 0) * meta.get("etf_units", 0)
    return total

def get_portfolio_cost_basis(positions):
    stock_pos = {k: v for k, v in positions.items() if k != "_meta"}
    meta      = positions.get("_meta", {})
    total     = 0
    for pos in stock_pos.values():
        total += pos.get("entry_price", 0) * pos.get("shares", 0)
    total += meta.get("etf_avg_price", 0) * meta.get("etf_units", 0)
    return total

def load_portfolio_history():
    history = []
    if not os.path.exists(HISTORY_FILE):
        return history
    with open(HISTORY_FILE) as f:
        for line in f:
            try:
                history.append(json.loads(line.strip()))
            except:
                pass
    return history

def read_trade_log():
    """
    Read trade_log.jsonl and compute:
    - realised_pnl: total P&L from all closed positions
    - open_positions: dict of symbol -> {qty, avg_buy_price} from unmatched buys
    - trade_count: total number of stock trades (not ETF)
    """
    if not os.path.exists(TRADE_LOG):
        return {"realised_pnl": 0, "open_positions": {}, "trade_count": 0, "win_count": 0, "loss_count": 0}

    trades = []
    with open(TRADE_LOG) as f:
        for line in f:
            try:
                t = json.loads(line.strip())
                if t.get("action") in ("BUY", "SELL", "ETF_SELL") and t.get("symbol") != "NSE:TEST-EQ":
                    trades.append(t)
            except:
                pass

    realised_pnl = 0
    win_count    = 0
    loss_count   = 0
    open_pos     = {}  # symbol -> {qty, cost}

    for t in trades:
        sym    = t["symbol"]
        qty    = t["qty"]
        price  = t["price"]
        action = t["action"]

        if action == "BUY":
            if sym not in open_pos:
                open_pos[sym] = {"qty": 0, "cost": 0}
            open_pos[sym]["qty"]  += qty
            open_pos[sym]["cost"] += qty * price

        elif action == "SELL":
            pnl = t.get("pnl", 0)
            realised_pnl += pnl
            if pnl > 0:
                win_count += 1
            elif pnl < 0:
                loss_count += 1
            # remove from open positions
            if sym in open_pos:
                open_pos[sym]["qty"] -= qty
                if open_pos[sym]["qty"] <= 0:
                    del open_pos[sym]

        elif action == "ETF_SELL":
            realised_pnl += t.get("pnl", 0)

    # compute avg buy price for open positions
    open_positions = {}
    for sym, data in open_pos.items():
        if data["qty"] > 0:
            open_positions[sym] = {
                "qty":           data["qty"],
                "avg_buy_price": round(data["cost"] / data["qty"], 2)
            }

    return {
        "realised_pnl":   round(realised_pnl, 2),
        "open_positions": open_positions,
        "trade_count":    len([t for t in trades if t["action"] == "BUY"]),
        "win_count":      win_count,
        "loss_count":     loss_count,
    }

def append_portfolio_history(live_value, cost_basis):
    today     = str(date.today())
    history   = load_portfolio_history()
    for entry in history:
        if entry.get("date") == today:
            return
    idle_cash     = max(0, START_CAPITAL - cost_basis)
    tlog          = read_trade_log()
    realised_pnl  = tlog["realised_pnl"]
    total         = live_value + idle_cash + realised_pnl
    entry = {
        "date":         today,
        "value":        round(total, 2),
        "cost_basis":   round(cost_basis, 2),
        "realised_pnl": round(realised_pnl, 2),
        "pnl":          round(total - cost_basis, 2),
        "pnl_pct":      round((total - START_CAPITAL) / START_CAPITAL * 100, 4),
    }
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[history] logged {today}: ₹{total:,.0f}")

def compute_metrics(values):
    if len(values) < 2:
        return None
    returns  = [(values[i] - values[i-1]) / values[i-1] for i in range(1, len(values))]
    n_days   = len(values) - 1
    total_r  = (values[-1] - values[0]) / values[0]
    # max drawdown — always meaningful
    peak   = values[0]
    max_dd = 0
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd
    # annualised metrics — only reliable with 30+ days
    if n_days >= 30:
        ann_ret   = (1 + total_r) ** (252 / n_days) - 1
        mean_r    = sum(returns) / len(returns)
        variance  = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        daily_vol = math.sqrt(variance)
        ann_vol   = daily_vol * math.sqrt(252)
        rf_daily  = 0.065 / 252
        excess    = [r - rf_daily for r in returns]
        mean_ex   = sum(excess) / len(excess)
        sharpe    = (mean_ex / daily_vol * math.sqrt(252)) if daily_vol > 0 else 0
    else:
        ann_ret   = None
        ann_vol   = None
        sharpe    = None
    return {
        "ann_return": round(ann_ret * 100, 2) if ann_ret is not None else None,
        "ann_vol":    round(ann_vol * 100, 2) if ann_vol is not None else None,
        "sharpe":     round(sharpe, 2)         if sharpe  is not None else None,
        "max_dd":     round(max_dd * 100, 2),
        "n_days":     n_days,
    }

def run_benchmark(send_telegram=False):
    fyers     = get_fyers()
    positions = json.load(open(POSITIONS_FILE))
    today     = str(date.today())

    print(f"\n{'='*55}")
    print(f"  Portfolio vs Benchmarks | {today}")
    print(f"  Tracking from: {START_DATE} | Capital: ₹{START_CAPITAL:,}")
    print(f"{'='*55}\n")

    # Portfolio
    live_value = get_portfolio_value(fyers, positions)
    cost_basis = get_portfolio_cost_basis(positions)

    # Idle cash = undeployed capital due to integer share rounding
    idle_cash = max(0, START_CAPITAL - cost_basis)

    total_port_value = live_value + idle_cash
    port_return      = (total_port_value - START_CAPITAL) / START_CAPITAL * 100
    port_pnl         = total_port_value - START_CAPITAL

    history      = load_portfolio_history()
    hist_values  = [h["value"] for h in history]
    port_metrics = compute_metrics(hist_values)

    # Trade log stats
    tlog = read_trade_log()
    total_trades = tlog["trade_count"]
    wins         = tlog["win_count"]
    losses       = tlog["loss_count"]
    realised_pnl = tlog["realised_pnl"]
    win_rate     = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

    unrealised_pnl = live_value - cost_basis
    total_pnl      = unrealised_pnl + realised_pnl
    port_return    = total_pnl / START_CAPITAL * 100
    port_pnl       = total_pnl
    print(f"📊 PORTFOLIO")
    print(f"   Cost basis:     ₹{cost_basis:>12,.0f}")
    print(f"   Idle cash:      ₹{idle_cash:>12,.0f}")
    print(f"   Live value:     ₹{live_value:>12,.0f}")
    print(f"   Total value:    ₹{total_port_value:>12,.0f}")
    print(f"   Return:         {port_return:>+.2f}%  (₹{port_pnl:>+,.0f})")
    print(f"   Unrealised P&L: ₹{unrealised_pnl:>+,.0f}")
    print(f"   Realised P&L:   ₹{realised_pnl:>+,.0f}")
    print(f"   Total P&L:      ₹{total_pnl:>+,.0f}")
    print(f"   Trades:         {total_trades} total | {wins}W {losses}L | {win_rate:.0f}% win rate")
    if port_metrics:
        if port_metrics['ann_return'] is not None:
            print(f"   Ann. Return:    {port_metrics['ann_return']:>+.2f}%")
            print(f"   Volatility:     {_f(port_metrics['ann_vol'])}% p.a.")
            print(f"   Sharpe:         {_f(port_metrics['sharpe'])}")
        else:
            days = port_metrics['n_days']
            print(f"   Ann. Return:    — ({days} days, need 30+)")
            print(f"   Volatility:     — ({days} days, need 30+)")
            print(f"   Sharpe:         — ({days} days, need 30+)")
        print(f"   Max Drawdown:   {_f(port_metrics['max_dd'])}%")
    else:
        print(f"   (Need 2+ trading days for metrics)")
    print()

    # Benchmarks
    benchmark_results = {}
    for name, symbol in BENCHMARKS.items():
        candles = fetch_index_candles(fyers, symbol, START_DATE, today)
        if not candles:
            print(f"📈 {name}: No data")
            continue
        if len(candles) == 1:
            # tracking started today — use open as start, close as current
            start_val = candles[0][1]
            end_val   = candles[0][4]
        else:
            start_val = candles[0][4]
            end_val   = candles[-1][4]
        b_return  = (end_val - start_val) / start_val * 100
        b_pnl     = (b_return / 100) * START_CAPITAL
        closes    = [c[4] for c in candles]
        b_metrics = compute_metrics(closes)
        benchmark_results[name] = {"return": b_return, "metrics": b_metrics}

        print(f"📈 {name}")
        print(f"   Start ({START_DATE}): {start_val:>10,.2f}")
        print(f"   Latest:            {end_val:>10,.2f}")
        print(f"   Return:            {b_return:>+.2f}%  (equiv ₹{b_pnl:>+,.0f})")
        if b_metrics:
            if b_metrics['ann_return'] is not None:
                print(f"   Ann. Return:  {b_metrics['ann_return']:>+.2f}%")
                print(f"   Volatility:   {_f(b_metrics['ann_vol'])}% p.a.")
                print(f"   Sharpe:       {_f(b_metrics['sharpe'])}")
            else:
                print(f"   Ann. Return:  — (need 30+ days)")
                print(f"   Volatility:   — (need 30+ days)")
                print(f"   Sharpe:       — (need 30+ days)")
            print(f"   Max Drawdown: {_f(b_metrics['max_dd'])}%")
        print()

    invested_pct     = live_value / total_port_value if total_port_value > 0 else 1
    raw_invested_ret = (live_value - cost_basis + realised_pnl) / cost_basis * 100 if cost_basis > 0 else 0
    # cash drag = % of capital sitting idle earning nothing
    cash_drag        = (idle_cash / START_CAPITAL) * 100 if START_CAPITAL > 0 else 0

    print(f"{'='*55}")
    print(f"  ALPHA (Portfolio vs Benchmark)")
    print(f"{'='*55}")
    print(f"  Invested:        {invested_pct*100:.1f}% of capital in market")
    print(f"  Cash drag:       {_f(cash_drag)}% (idle cash penalty)")
    print(f"  Portfolio return (total):    {port_return:>+.2f}%")
    print(f"  Portfolio return (invested): {raw_invested_ret:>+.2f}%")
    print()
    for name, data in benchmark_results.items():
        alpha_total    = port_return - data["return"]
        alpha_invested = raw_invested_ret - data["return"]
        emoji = "🟢" if alpha_total >= 0 else "🔴"
        print(f"  {emoji} vs {name}:")
        print(f"     Alpha (total capital):    {alpha_total:>+.2f}%")
        print(f"     Alpha (invested capital): {alpha_invested:>+.2f}%")
    print()

    # Telegram
    if send_telegram:
        msg  = f"📊 Benchmark Report — {today}\n"
        msg += f"Tracking from {START_DATE} | ₹{START_CAPITAL:,}\n\n"
        msg += f"💼 Portfolio: {_f(port_return, '+.2f')}% (₹{int(port_pnl):+,} )\n"
        msg += f"   Invested: {_f(invested_pct*100 if invested_pct is not None else None, '.1f')}% | Cash drag: {_f(cash_drag)}%\n\n"
        if not benchmark_results:
            msg += "⚠️ Benchmark data unavailable (API hiccup)\n"
        for name, data in benchmark_results.items():
            b_ret = data.get("return")
            alpha = (port_return - b_ret) if (port_return is not None and b_ret is not None) else None
            emoji = "🟢" if (alpha is not None and alpha >= 0) else "🔴"
            msg += f"{emoji} {name}: {_f(b_ret, '+.2f')}% | Alpha: {_f(alpha, '+.2f')}%\n"
        if port_metrics:
            msg += f"\nSharpe: {_f(port_metrics['sharpe'])} | "
            msg += f"MaxDD: {_f(port_metrics['max_dd'])}% | "
            msg += f"Vol: {_f(port_metrics['ann_vol'])}%"
        alert(msg)
        print("✅ Sent to Telegram")

if __name__ == "__main__":
    send_tg = "--tg" in sys.argv
    run_benchmark(send_telegram=send_tg)
