"""
Nifty 50 RSI Swing Trading Bot
================================
Strategy: RSI(9) | RSI SMA(20) > 55 | TP +25% | SL -20% | RSI Exit > 80
Capital: Dynamic (set via update_capital.py) | Ratio: total/40 per trade
Idle Capital: NiftyBEES ETF
Schedule (IST): 10:00 execute | 12:00 P&L | 14:00 history | 14:05 Fri ETF sweep |
          15:55 scan | 16:15 exit recheck | 17:00 & 19:00 scan retries | 18:30 benchmark

FIXES APPLIED
  FIX 1   Sells use live price, not entry_price
  FIX 2   ETF rollback improved
  FIX 3   Replacement order monitored for 15 more mins
  FIX 4   Rollback uses buy_etf() return value
  FIX 5   ETF avg_price weighted average on every buy
  FIX 6   NSE holiday + weekend guard
  FIX 7   Failed buy signals dropped, not retried
  FIX 8   Zero-qty skip sends Telegram alert
  FIX 9   Conservative free_slots only
  FIX 10  get_fyers() checked for None
  FIX 11  ETF price refreshed before each transaction
  FIX 12  Atomic file writes
  FIX 13  Bare excepts replaced
  FIX 14  Trade audit log (data/trade_log.jsonl)
"""

import json, os, requests, schedule, time
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
from broker import get_fyers
from config import data_path, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, SHARED_ETF_STATE_FILE

CFG = {
    "rsi_period":        9,
    "rsi_sma_period":    20,
    "entry_rsi_sma_min": 55,
    "entry_rsi_max":     80,
    "sl_pct":           -0.20,
    "tp_pct":            0.25,
    "exit_rsi":          80,
    "default_capital":   3_00_000,
    "vol_filter_period": 10,
}

NIFTYBEES = "NSE:NIFTYBEES-EQ"

NIFTY50 = [
    "NSE:RELIANCE-EQ","NSE:TCS-EQ","NSE:HDFCBANK-EQ","NSE:BHARTIARTL-EQ",
    "NSE:ICICIBANK-EQ","NSE:INFY-EQ","NSE:SBIN-EQ","NSE:HINDUNILVR-EQ",
    "NSE:ITC-EQ","NSE:LT-EQ","NSE:KOTAKBANK-EQ","NSE:AXISBANK-EQ",
    "NSE:BAJFINANCE-EQ","NSE:MARUTI-EQ","NSE:HCLTECH-EQ","NSE:SUNPHARMA-EQ",
    "NSE:ASIANPAINT-EQ","NSE:ADANIENT-EQ","NSE:ADANIPORTS-EQ","NSE:TITAN-EQ",
    "NSE:NTPC-EQ","NSE:POWERGRID-EQ","NSE:ONGC-EQ",
    "NSE:ULTRACEMCO-EQ","NSE:JSWSTEEL-EQ","NSE:TMPV-EQ","NSE:TATASTEEL-EQ",
    "NSE:NESTLEIND-EQ","NSE:TECHM-EQ","NSE:M&M-EQ","NSE:BAJAJFINSV-EQ",
    "NSE:HINDALCO-EQ",
"NSE:GRASIM-EQ","NSE:COALINDIA-EQ",
"NSE:DRREDDY-EQ","NSE:HDFCLIFE-EQ",
    "NSE:SBILIFE-EQ","NSE:CIPLA-EQ","NSE:APOLLOHOSP-EQ","NSE:BAJAJ-AUTO-EQ",
    "NSE:EICHERMOT-EQ",
"NSE:TATACONSUM-EQ",
    "NSE:SHRIRAMFIN-EQ","NSE:BEL-EQ",
    "NSE:ETERNAL-EQ",
    "NSE:INDIGO-EQ",
    "NSE:JIOFIN-EQ",
    "NSE:MAXHEALTH-EQ",
    "NSE:TRENT-EQ",
    "NSE:BSE-EQ",
]

POSITIONS_FILE = data_path('positions.json')
PENDING_FILE   = data_path('pending_signals.json')
TRADE_LOG      = data_path('trade_log.jsonl')
TG_TOKEN       = TELEGRAM_TOKEN
TG_CHAT        = TELEGRAM_CHAT_ID

NSE_HOLIDAYS = {
    date(2025, 1, 26), date(2025, 3, 14), date(2025, 3, 31),
    date(2025, 4, 10), date(2025, 4, 14), date(2025, 4, 18),
    date(2025, 5, 1),  date(2025, 8, 15), date(2025, 8, 27),
    date(2025, 10, 2), date(2025, 10, 24), date(2025, 11, 5),
    date(2025, 12, 25),
    date(2026, 1, 15), date(2026, 1, 26), date(2026, 3, 3),
    date(2026, 3, 26), date(2026, 3, 31), date(2026, 4, 3),
    date(2026, 4, 14), date(2026, 5, 1),  date(2026, 5, 28),
    date(2026, 6, 26), date(2026, 8, 15), date(2026, 9, 14),
    date(2026, 10, 2), date(2026, 10, 20), date(2026, 11, 10),
    date(2026, 11, 24), date(2026, 12, 25),
}

def is_trading_day():
    today = date.today()
    if today.weekday() >= 5:
        return False
    return today not in NSE_HOLIDAYS

def alert(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg},
            timeout=10
        )
    except Exception as e:
        print(f"[Telegram error] {e}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def log_trade(action, symbol, qty, price, reason="", pnl=None, entry_price=None):
    entry = {
        "ts":     datetime.now().isoformat(),
        "action": action,
        "symbol": symbol,
        "qty":    qty,
        "price":  price,
        "reason": reason,
    }
    if pnl is not None:
        entry["pnl"] = round(pnl, 2)
    if entry_price is not None:
        entry["entry_price"] = round(entry_price, 2)
    try:
        with open(TRADE_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[TradeLog error] {e}")

def load_positions():
    if not os.path.exists(POSITIONS_FILE):
        total = CFG["default_capital"]
        cpt   = total // 40
        default = {"_meta": {
            "total_capital":     total,
            "capital_per_trade": cpt,
            "capital_in_stocks": 0,
            "capital_in_etf":    total,
            "etf_units":         0,
            "etf_avg_price":     0,
            "last_updated":      None
        }}
        save_positions(default)
        return default
    return json.load(open(POSITIONS_FILE))

def save_positions(pos):
    tmp = POSITIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pos, f, indent=2, default=str)
    os.replace(tmp, POSITIONS_FILE)

def load_pending():
    if not os.path.exists(PENDING_FILE):
        return {"buys": [], "sells": []}
    return json.load(open(PENDING_FILE))

def save_pending(p):
    tmp = PENDING_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p, f, indent=2, default=str)
    os.replace(tmp, PENDING_FILE)

def get_capital_per_trade(positions):
    meta  = positions.get("_meta", {})
    total = meta.get("total_capital", CFG["default_capital"])
    return meta.get("capital_per_trade", total // 40)

def compute_rsi(close, period=9):
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    ag    = gain.ewm(com=period-1, min_periods=period).mean()
    al    = loss.ewm(com=period-1, min_periods=period).mean()
    rs    = ag / al.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def get_live_price(fyers, symbol):
    try:
        data = fyers.quotes({"symbols": symbol})
        return float(data['d'][0]['v']['lp'])
    except Exception as e:
        print(f"[get_live_price error] {symbol}: {e}")
        return None

def get_fresh_etf_price(fyers):
    price = get_live_price(fyers, NIFTYBEES)
    if not price:
        alert("⚠️ Could not fetch fresh NiftyBEES price")
    return price

def get_tick_size(price):
    if price < 250:     return 0.01
    elif price < 1000:  return 0.05
    elif price < 5000:  return 0.10
    elif price < 10000: return 0.50
    elif price < 20000: return 1.00
    else:               return 5.00

def round_to_tick(price, tick):
    # Use integer paise arithmetic to avoid float precision errors
    tick_p = int(round(tick * 100))
    price_p = int(round(price * 100))
    return round(round(price_p / tick_p) * tick_p / 100, 2)

def fetch_candles(fyers, symbol, days=90):
    end   = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    resp  = fyers.history(data={
        "symbol":      symbol,
        "resolution":  "D",
        "date_format": "1",
        "range_from":  start,
        "range_to":    end,
        "cont_flag":   "1"
    })
    if resp.get("s") != "ok" or not resp.get("candles"):
        return None
    df = pd.DataFrame(resp["candles"],
        columns=["timestamp","open","high","low","close","volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")
    df.set_index("timestamp", inplace=True)
    return df

def get_signals(fyers):
    alert("📡 Fetching Nifty 50 data from Fyers...")
    signals = {}
    for symbol in NIFTY50:
        try:
            time.sleep(0.5)
            df = fetch_candles(fyers, symbol, days=90)
            if df is None or len(df) < 30:
                continue
            rsi_s   = compute_rsi(df["close"], CFG["rsi_period"])
            rsi_sma = rsi_s.rolling(CFG["rsi_sma_period"]).mean()
            rsi     = float(rsi_s.iloc[-1])
            rsima   = float(rsi_sma.iloc[-1])
            close   = float(df["close"].iloc[-1])
            vol      = float(df["volume"].iloc[-1])
            vol_avg  = float(df["volume"].rolling(CFG["vol_filter_period"]).mean().iloc[-1])
            vol_ok   = vol > vol_avg and not np.isnan(vol_avg)
            if np.isnan(rsi) or np.isnan(rsima):
                continue
            signals[symbol] = {
                "symbol":  symbol,
                "close":   round(close, 2),
                "rsi":     round(rsi, 2),
                "rsi_sma": round(rsima, 2),
                "vol_ok":  vol_ok,
            }
        except Exception as e:
            print(f"[get_signals error] {symbol}: {e}")
    alert(f"✅ Signals ready for {len(signals)} stocks")
    return signals

def generate_orders(signals, positions, live_prices=None):
    """live_prices: {symbol: ltp} — overrides candle close for exit evaluation.
    Also covers held symbols removed from NIFTY50 (not in signals)."""
    if live_prices is None:
        live_prices = {}
    cpt        = get_capital_per_trade(positions)
    meta       = positions.get("_meta", {})
    stock_pos  = {k: v for k, v in positions.items() if k != "_meta"}
    cap_stocks = sum(p.get("capital", cpt) for p in stock_pos.values())
    free_cap   = meta.get("total_capital", CFG["default_capital"]) - cap_stocks
    free_slots = int(free_cap / cpt)

    # Pre-scan exits to add their slots to free_slots
    exiting_cap = 0
    for symbol, pos in stock_pos.items():
        ep    = pos.get("entry_price", 0)
        sig   = signals.get(symbol)
        close = live_prices.get(symbol) or (sig["close"] if sig else 0)
        rsi   = sig["rsi"] if sig else 0
        if not close:
            continue
        pct = (close - ep) / ep if ep else 0
        if pct <= CFG["sl_pct"] or pct >= CFG["tp_pct"] or rsi > CFG["exit_rsi"]:
            exiting_cap += pos.get("capital", cpt)
    free_slots += int(exiting_cap / cpt)

    exits = []
    for symbol, pos in stock_pos.items():
        ep    = pos.get("entry_price", 0)
        sig   = signals.get(symbol)
        close = live_prices.get(symbol) or (sig["close"] if sig else 0)
        rsi   = sig["rsi"] if sig else 0
        if not close:
            continue
        pct = (close - ep) / ep if ep else 0
        if pct <= CFG["sl_pct"]:
            exits.append({"symbol": symbol,
                "reason": f"🔴 SL HIT ({pct:.1%})", "urgency": "URGENT"})
        elif pct >= CFG["tp_pct"]:
            exits.append({"symbol": symbol,
                "reason": f"🟢 TP HIT ({pct:.1%})", "urgency": "SELL AT OPEN"})
        elif rsi > CFG["exit_rsi"]:
            exits.append({"symbol": symbol,
                "reason": f"🟡 RSI EXIT (RSI={rsi:.1f})", "urgency": "SELL AT OPEN"})

    already_held = set(stock_pos.keys())
    today_str    = str(datetime.now().date())
    cooldown     = meta.get("cooldown", {})
    cooldown     = {k: v for k, v in cooldown.items() if today_str < v}
    candidates   = []
    for symbol, sig in signals.items():
        if symbol in already_held:
            continue
        if symbol in cooldown:
            continue  # within 7-day re-entry cooldown
        if (sig["rsi_sma"] > CFG["entry_rsi_sma_min"] and
                sig["rsi"] < CFG["entry_rsi_max"] and
                sig.get("vol_ok", True)):
            candidates.append({
                "symbol":       symbol,
                "signal_price": sig["close"],
                "rsi":          sig["rsi"],
                "rsi_sma":      sig["rsi_sma"],
                "tp_price":     round(sig["close"] * (1 + CFG["tp_pct"]), 2),
                "sl_price":     round(sig["close"] * (1 + CFG["sl_pct"]), 2),
                "capital":      cpt,
                "signal_date":  str(datetime.now().date()),
            })

    candidates.sort(key=lambda x: x["rsi_sma"], reverse=True)
    entries = candidates[:free_slots]
    return exits, entries

def place_order(fyers, symbol, side, qty, price):
    tick        = get_tick_size(price)
    raw         = price * 1.005 if side == 1 else price * 0.995
    limit_price = round_to_tick(raw, tick)
    return fyers.place_order(data={
        "symbol":       symbol,
        "qty":          qty,
        "type":         1,
        "side":         side,
        "productType":  "CNC",
        "limitPrice":   limit_price,
        "stopPrice":    0,
        "validity":     "DAY",
        "disclosedQty": 0,
        "offlineOrder": False
    })

def is_order_filled(fyers, order_id):
    try:
        orderbook = fyers.orderbook()
        for o in orderbook.get("orderBook", []):
            if str(o.get("id")) == str(order_id):
                return o.get("status")
        return None
    except Exception as e:
        print(f"[is_order_filled error] {e}")
        return None

def place_and_ensure_filled(fyers, symbol, side, qty, price,
                             check_every_mins=5, cutoff_hour=15, cutoff_min=20):
    """
    Places a limit order. Every check_every_mins:
    - If filled -> done
    - If cancelled/rejected -> done
    - If still pending -> cancel, fetch live price, reprice
    - If past 3:20 PM cutoff -> cancel and abort
    Fixes Bug 3 (time awareness) and Bug 4 (stale signal price) simultaneously.
    """
    name = symbol.replace("NSE:", "").replace("-EQ", "")

    def past_cutoff():
        now = datetime.now()
        return now.hour > cutoff_hour or (now.hour == cutoff_hour and now.minute >= cutoff_min)

    def place_fresh(limit_price):
        tick = get_tick_size(limit_price)
        raw  = limit_price * 1.005 if side == 1 else limit_price * 0.995
        lp   = round_to_tick(raw, tick)
        r = fyers.place_order(data={
            "symbol":       symbol,
            "qty":          qty,
            "type":         1,
            "side":         side,
            "productType":  "CNC",
            "limitPrice":   lp,
            "stopPrice":    0,
            "validity":     "DAY",
            "disclosedQty": 0,
            "offlineOrder": False
        })
        return r, lp

    # Initial order
    if past_cutoff():
        alert(f"⚠️ {name} skipped — past 3:20 PM cutoff")
        return {"s": "error", "message": "Past cutoff time"}

    resp, lp = place_fresh(price)
    if resp.get("s") != "ok":
        alert(f"❌ {name} initial order failed: {resp.get('message')}")
        return resp

    order_id   = resp.get("id")
    start_time = datetime.now()
    attempt    = 1
    alert(f"⏳ {name} order placed @ ₹{lp} | ID: {order_id}")

    # Reprice loop — first check after 30 seconds, then every check_every_mins
    first_check = True
    while True:
        time.sleep(30 if first_check else check_every_mins * 60)
        first_check = False

        if past_cutoff():
            alert(f"⚠️ {name} past 3:20 PM — cancelling")
            try:
                fyers.cancel_order(data={"id": order_id})
            except Exception as e:
                print(f"[cancel error] {e}")
            return {"s": "error", "message": "Cancelled — past cutoff"}

        status  = is_order_filled(fyers, order_id)
        elapsed = (datetime.now() - start_time).seconds // 60

        if status == 2:
            fill_price = lp  # fallback to limit price placed
            try:
                ob = fyers.orderbook()
                for o in ob.get("orderBook", []):
                    if str(o.get("id")) == str(order_id):
                        fill_price = o.get("tradedPrice") or lp
                        break
            except Exception as e:
                print(f"[fill price fetch error] {e}")
            resp["fill_price"] = fill_price
            alert(f"✅ {name} FILLED @ ₹{fill_price} after {elapsed} mins (attempt {attempt})")
            return resp

        elif status in (1, 5):
            alert(f"❌ {name} cancelled/rejected (status={status})")
            return {"s": "error", "message": "Order cancelled/rejected"}

        else:
            alert(f"⏳ {name} pending after {elapsed} mins — repricing...")
            try:
                fyers.cancel_order(data={"id": order_id})
                time.sleep(2)

                live_price = get_live_price(fyers, symbol)
                if not live_price:
                    alert(f"❌ {name} no live price — aborting")
                    return {"s": "error", "message": "No live price for reprice"}

                new_resp, new_lp = place_fresh(live_price)
                if new_resp.get("s") != "ok":
                    alert(f"❌ {name} reprice failed: {new_resp.get('message')}")
                    return new_resp

                order_id = new_resp.get("id")
                resp     = new_resp
                lp       = new_lp
                attempt += 1
                alert(f"🔄 {name} repriced @ ₹{new_lp} | ID: {order_id} | attempt {attempt}")

            except Exception as e:
                alert(f"❌ {name} reprice error: {e}")
                return {"s": "error", "message": str(e)}

def buy_etf(fyers, capital, etf_price, meta, reason=""):
    units = int(capital / etf_price)
    if units <= 0:
        return None, 0
    resp = place_order(fyers, NIFTYBEES, 1, units, etf_price)
    if resp.get("s") == "ok":
        old_units    = meta.get("etf_units", 0)
        old_avg      = meta.get("etf_avg_price", etf_price)
        total_units  = old_units + units
        new_avg      = (old_avg * old_units + etf_price * units) / total_units if total_units > 0 else etf_price
        actual_value = units * etf_price  # use actual value transacted, not requested capital
        meta["etf_avg_price"]  = round(new_avg, 4)
        meta["etf_units"]      = total_units
        meta["capital_in_etf"] = round(total_units * new_avg, 2)
        alert(f"📦 BUY {units} NIFTYBEES @ ₹{etf_price} (₹{actual_value:,.0f}) {reason} | avg ₹{new_avg:.2f}")
        log_trade("ETF_BUY", NIFTYBEES, units, etf_price, reason)
    else:
        alert(f"❌ ETF BUY FAILED: {resp.get('message')}")
    return resp, units

def sell_etf(fyers, capital, etf_price, meta, reason=""):
    units = int(capital / etf_price)
    if units <= 0:
        return None, 0
    resp = place_order(fyers, NIFTYBEES, -1, units, etf_price)
    if resp.get("s") == "ok":
        actual_value  = units * etf_price
        new_units     = max(0, meta.get("etf_units", 0) - units)
        meta["etf_units"]      = new_units
        meta["capital_in_etf"] = round(new_units * meta.get("etf_avg_price", etf_price), 2)
        etf_avg = meta.get("etf_avg_price", etf_price)
        etf_pnl = round((etf_price - etf_avg) * units, 2)
        alert(f"📦 SELL {units} NIFTYBEES @ ₹{etf_price} (₹{capital:,.0f}) {reason} | P&L ₹{etf_pnl:+,.2f}")
        log_trade("ETF_SELL", NIFTYBEES, units, etf_price, reason, etf_pnl)
    else:
        alert(f"❌ ETF SELL FAILED: {resp.get('message')}")
    return resp, units

# ── Broker Reconciliation ────────────────────────────────────────
def reconcile_with_broker(fyers, positions):
    """
    Bug 1 + Bug 10: Compare positions.json against actual broker holdings.
    - Flags qty mismatches (partial fills, manual trades, corporate actions)
    - Auto-fixes ETF unit drift
    - Alerts on stock mismatches for manual review
    """
    meta      = positions.get("_meta", {})
    stock_pos = {k: v for k, v in positions.items() if k != "_meta"}

    try:
        h = fyers.holdings()
        if h.get("s") != "ok":
            alert(f"⚠️ Reconciliation skipped — holdings fetch failed: {h.get('message')}")
            return True
    except Exception as e:
        alert(f"⚠️ Reconciliation skipped — {e}")
        return True

    # Build broker holdings dict using BOTH holdings (settled) + positions (T+1 intraday)
    # holdings() only returns settled shares — T+1 purchases appear in positions() as CNC netQty

    # Step 1: get intraday CNC positions (T+1 unsettled)
    t1_qty   = {}
    pos_resp = {"netPositions": []}
    try:
        pos_resp = fyers.positions()
        for p in pos_resp.get("netPositions", []):
            if p.get("productType") == "CNC":
                sym = p.get("symbol")
                net = p.get("netQty", 0)
                if sym and net > 0:
                    t1_qty[sym] = {"qty": net, "buyAvg": p.get("buyAvg", 0)}
    except Exception as e:
        print(f"[reconcile] positions() error: {e}")

    # Step 2: build broker dict
    # Fyers holdings structure:
    # HLD entry: qty=settled shares, qty_t1=unsettled bought recently
    # T1 entry: duplicate of just the T+1 portion — SKIP these
    # For T1-only stocks (fully unsettled): only T1 entry exists, use T1.qty
    # Correct total = HLD.qty + HLD.qty_t1 for HLD stocks
    #               = T1.qty for T1-only stocks
    broker = {}
    hld_syms = set()
    for holding in h.get("holdings", []):
        sym          = holding.get("symbol")
        holding_type = holding.get("holdingType", "HLD")
        if not sym:
            continue
        if holding_type == "HLD":
            hld_syms.add(sym)
            settled   = holding.get("quantity", 0)
            unsettled = holding.get("qty_t1", 0)
            total     = settled + unsettled
            has_t1    = unsettled > 0
            if sym not in broker:
                broker[sym] = {"qty": settled, "qty_t1": unsettled, "total": total,
                               "costPrice": holding.get("costPrice", 0),
                               "ltp":       holding.get("ltp", 0),
                               "has_t1":    has_t1}
            else:
                # multiple HLD lots — sum them
                broker[sym]["qty"]    += settled
                broker[sym]["qty_t1"] += unsettled
                broker[sym]["total"]  += total
                broker[sym]["has_t1"] = broker[sym]["has_t1"] or has_t1
        elif holding_type == "T1" and sym not in hld_syms:
            # T1-only stock — fully unsettled, no HLD entry
            t1_shares = holding.get("quantity", 0)
            broker[sym] = {"qty": 0, "qty_t1": t1_shares, "total": t1_shares,
                           "costPrice": holding.get("costPrice", 0),
                           "ltp":       holding.get("ltp", 0),
                           "has_t1":    True}

    # Step 3: add symbols only in positions() — bought today, not yet in holdings at all
    for sym, t1 in t1_qty.items():
        if sym not in broker:
            broker[sym] = {"qty": 0, "qty_t1": t1["qty"], "total": t1["qty"],
                           "costPrice": t1["buyAvg"], "ltp": 0, "has_t1": True}

    clean = True

    # Check stock positions
    for symbol, pos in stock_pos.items():
        bot_qty     = pos.get("shares", 0)
        broker_data = broker.get(symbol)

        if not broker_data:
            alert(f"🚨 RECONCILE: {symbol} in positions.json but NOT in broker — manual check needed")
            clean = False
            continue

        broker_qty = broker_data["total"]
        if bot_qty != broker_qty:
            if broker_data.get("has_t1"):
                print(f"[reconcile] {symbol} T+1 pending — skipping mismatch (bot={bot_qty} broker_total={broker_qty})")
            else:
                alert(
                    f"🚨 RECONCILE: {symbol} qty mismatch — "
                    f"bot={bot_qty} broker={broker_qty} | "
                    f"Possible partial fill or corporate action — manual check needed"
                )
                clean = False

        # T+1 entry price correction — update entry price from actual broker fill
        if broker_data.get("has_t1") and broker_data.get("costPrice", 0) > 0:
            actual_price = broker_data["costPrice"]
            entry_price  = pos.get("entry_price", 0)
            if entry_price > 0 and abs(actual_price - entry_price) / entry_price > 0.001:
                pos["entry_price"] = round(actual_price, 2)
                pos["tp_price"]    = round(actual_price * 1.25, 2)
                pos["sl_price"]    = round(actual_price * 0.80, 2)
                positions[symbol]   = pos
                alert(f"📝 RECONCILE: {symbol} entry price updated ₹{entry_price} → ₹{actual_price:.2f} | TP: ₹{pos['tp_price']} SL: ₹{pos['sl_price']}")

        # Corporate action detection — cost price drift > 15% from entry
        entry_price = pos.get("entry_price", 0)
        broker_cost = broker_data["costPrice"]
        if entry_price > 0 and abs(broker_cost - entry_price) / entry_price > 0.15:
            alert(
                f"🚨 RECONCILE: {symbol} cost price discrepancy — "
                f"entry=₹{entry_price} broker_cost=₹{broker_cost:.2f} | "
                f"Possible corporate action (split/bonus) — manual check needed"
            )
            clean = False

    # Check broker holdings not in bot
    for symbol, bdata in broker.items():
        if symbol == NIFTYBEES:
            continue
        if symbol in NIFTY50 and symbol not in stock_pos:
            broker_qty = bdata["total"]
            if broker_qty > 0:
                alert(
                    f"⚠️ RECONCILE: {symbol} in broker but not in positions.json "
                    f"(qty={broker_qty}) — possible missed entry or manual trade"
                )
                clean = False

    # Check ETF units
    # Correct total = HLD.qty + HLD.qty_t1 (yesterday T+1) + positions.netQty (today activity)
    etf_hld_data  = broker.get(NIFTYBEES, {})
    etf_hld_total = etf_hld_data.get("total", 0)

    # today's net ETF activity from positions API
    etf_net_today = 0
    try:
        for p in pos_resp.get("netPositions", []):
            if p.get("symbol") == NIFTYBEES and p.get("productType") == "CNC":
                etf_net_today = p.get("netQty", 0)
    except Exception as e:
        print(f"[reconcile] ETF positions error: {e}")

    # If another strategy holds NiftyBEES in the same broker account, net its units out
    other_etf = 0
    if SHARED_ETF_STATE_FILE:
        try:
            with open(SHARED_ETF_STATE_FILE) as f:
                other_etf = json.load(f).get('holdings', {}).get('NIFTYBEES', 0)
        except Exception as e:
            print(f"[reconcile] shared ETF state unreadable: {e}")
    broker_etf = etf_hld_total + etf_net_today - other_etf
    bot_etf    = meta.get("etf_units", 0)

    print(f"[reconcile] NIFTYBEES: HLD={etf_hld_total} + today={etf_net_today} = {broker_etf} | bot={bot_etf}")

    if broker_etf != bot_etf:
        meta["etf_units"]  = broker_etf
        positions["_meta"] = meta
        save_positions(positions)
        alert(f"⚠️ NIFTYBEES corrected: {bot_etf} → {broker_etf} (HLD={etf_hld_total} + today={etf_net_today})")
    else:
        print(f"[reconcile] NIFTYBEES ✅ match = {broker_etf}")

    # reconcile etf_avg_price from broker costPrice
    broker_avg = etf_hld_data.get("costPrice", 0)
    bot_avg    = meta.get("etf_avg_price", 0)
    if broker_avg > 0 and bot_avg > 0 and abs(broker_avg - bot_avg) / bot_avg > 0.001:
        meta["etf_avg_price"] = round(broker_avg, 4)
        positions["_meta"]    = meta
        save_positions(positions)
        alert(f"📝 RECONCILE: NIFTYBEES avg price ₹{bot_avg:.4f} → ₹{broker_avg:.4f}")
    else:
        print(f"[reconcile] NIFTYBEES avg ✅ bot={bot_avg:.4f} broker={broker_avg:.4f}")

    if clean:
        alert("✅ Reconciliation clean — broker matches positions.json")

    return clean

def update_dynamic_seats(positions):
    """
    Adjust seat count (and capital_per_trade) based on realised P&L.
    - Increase: immediate when realised P&L crosses next ₹25K threshold
    - Decrease: only after 7 consecutive days below threshold
    - Floor: 40 seats | Ceiling: 50 seats
    """
    from benchmark import read_trade_log
    from datetime import date as date_cls

    meta          = positions.get("_meta", {})
    total_capital = meta.get("total_capital", 1000000)
    current_seats = meta.get("seats", 40)
    today         = str(date_cls.today())

    tlog         = read_trade_log()
    realised_pnl = tlog.get("realised_pnl", 0)

    base         = 40
    threshold    = max(25000, total_capital // 40)
    target_seats = min(50, max(base, base + int(realised_pnl / threshold)))

    if target_seats > current_seats:
        meta["seats"]                      = target_seats
        meta["capital_per_trade"]          = total_capital // target_seats
        meta["seats_reduce_pending_since"] = None
        positions["_meta"]                 = meta
        save_positions(positions)
        alert(f"📈 Seats {current_seats} → {target_seats} | "
              f"Per trade: ₹{meta['capital_per_trade']:,.0f} | "
              f"Realised P&L: ₹{realised_pnl:,.0f}")

    elif target_seats < current_seats:
        since = meta.get("seats_reduce_pending_since")
        if since is None:
            meta["seats_reduce_pending_since"] = today
            positions["_meta"]                 = meta
            save_positions(positions)
            alert(f"⚠️ Seat reduction pending: {current_seats} → {target_seats} | "
                  f"Will confirm after 7 days | Realised P&L: ₹{realised_pnl:,.0f}")
        else:
            days_below = (date_cls.today() - date_cls.fromisoformat(since)).days
            if days_below >= 7:
                meta["seats"]                      = target_seats
                meta["capital_per_trade"]          = total_capital // target_seats
                meta["seats_reduce_pending_since"] = None
                positions["_meta"]                 = meta
                save_positions(positions)
                alert(f"📉 Seats {current_seats} → {target_seats} confirmed after 7 days | "
                      f"Per trade: ₹{meta['capital_per_trade']:,.0f}")
            else:
                remaining = 7 - days_below
                print(f"[seats] Reduction pending {current_seats}→{target_seats} | "
                      f"{remaining} days remaining | Realised P&L: ₹{realised_pnl:,.0f}")

    else:
        # Target matches current — cancel any pending reduction if P&L recovered
        if meta.get("seats_reduce_pending_since"):
            meta["seats_reduce_pending_since"] = None
            positions["_meta"]                 = meta
            save_positions(positions)
            print(f"[seats] Reduction cancelled — P&L recovered. Seats stay at {current_seats}")

def _run_evening_scan():
    """Core scan logic. Returns True if successful, False if failed."""
    fyers = get_fyers()
    if not fyers:
        alert("❌ Login failed — evening scan aborted")
        return False
    try:
        signals = get_signals(fyers)
    except Exception as e:
        alert(f"❌ Evening scan failed — get_signals error: {e}")
        return False
    if not signals:
        alert("❌ Evening scan failed — no signals returned")
        return False
    try:
        positions = load_positions()
        update_dynamic_seats(positions)
        positions      = load_positions()  # reload after potential seat update
        cpt            = get_capital_per_trade(positions)
        # Fetch live quotes for all held positions — covers stale candle data at 3:55 PM
        # and symbols removed from NIFTY50 that are still held (e.g. post-reconstitution)
        stock_pos_keys = [k for k in positions if k != "_meta"]
        live_prices    = {}
        try:
            for i in range(0, len(stock_pos_keys), 10):
                batch = stock_pos_keys[i:i+10]
                r     = fyers.quotes({"symbols": ",".join(batch)})
                for item in (r.get("d") or []):
                    sym = item["n"]
                    lp  = item["v"].get("lp") or item["v"].get("prev_close_price") or 0
                    if lp:
                        live_prices[sym] = lp
        except Exception as e:
            print(f"[live quotes warning] {e}")
        exits, entries = generate_orders(signals, positions, live_prices)
        save_pending({"buys": entries, "sells": exits})
        meta  = positions.get("_meta", {})
        total = meta.get("total_capital", CFG["default_capital"])
        msg   = f"📊 Evening Scan — {datetime.now().strftime('%d %b %Y')}\n"
        msg  += f"💰 Capital: ₹{total:,.0f} | Per trade: ₹{cpt:,.0f}\n"
        msg  += f"🔴 SELL signals: {len(exits)}\n"
        msg  += f"🟢 BUY  signals: {len(entries)}\n\n"
        if exits:
            msg += "SELLS:\n"
            for o in exits:
                msg += f"  • {o['symbol'].replace('NSE:','').replace('-EQ','')} — {o['reason']}\n"
        if entries:
            msg += "\nBUYS:\n"
            for e in entries:
                msg += f"  • {e['symbol'].replace('NSE:','').replace('-EQ','')} @ ₹{e['signal_price']} | RSI SMA={e['rsi_sma']:.1f}\n"
        if not exits and not entries:
            msg += "✅ No trades tomorrow"
        alert(msg)



        return True
    except Exception as e:
        alert(f"❌ Evening scan failed — {e}")
        return False

def _get_scan_flag():
    return data_path(f"evening_scan_{date.today()}.status")

def _write_scan_flag(status):
    try:
        with open(_get_scan_flag(), "w") as f:
            f.write(status)
    except Exception as e:
        print(f"[scan flag error] {e}")

def _read_scan_flag():
    # Check flag file first
    try:
        with open(_get_scan_flag()) as f:
            return f.read().strip()
    except:
        pass
    # Flag missing (e.g. after crash/restart) — check if pending has valid signals
    try:
        import json as _json
        pending = _json.load(open(PENDING_FILE))
        if pending.get("buys") or pending.get("sells"):
            # Signals exist from a previous scan — treat as success
            print(f"[scan flag] No flag found but pending_signals.json has signals — treating as success")
            _write_scan_flag("success")
            return "success"
    except:
        pass
    return None

def evening_scan():
    """3:55 PM — primary evening scan."""
    if not is_trading_day():
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Non-trading day — evening scan skipped")
        return
    alert("🌙 Evening scan starting (3:55 PM)...")
    success = _run_evening_scan()
    if success:
        _write_scan_flag("success")
    else:
        _write_scan_flag("failed")
        alert("⚠️ Evening scan FAILED at 3:55 PM — will retry at 5:00 PM")

def evening_scan_retry_1():
    """5:00 PM — first retry, only if the 3:55 PM scan failed."""
    if not is_trading_day():
        return
    if _read_scan_flag() == "success":
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Scan already succeeded — 5 PM retry skipped")
        return
    alert("🌙 Evening scan retry (5:00 PM)...")
    success = _run_evening_scan()
    if success:
        _write_scan_flag("success")
        alert("✅ Evening scan complete (5 PM retry)")
    else:
        _write_scan_flag("failed")
        alert("⚠️ Evening scan FAILED at 5:00 PM — will retry at 7:00 PM")

def evening_scan_retry_2():
    """7:00 PM — second retry, only if both earlier scans failed."""
    if not is_trading_day():
        return
    if _read_scan_flag() == "success":
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Scan already succeeded — 7 PM retry skipped")
        return
    alert("🌙 Evening scan retry (7:00 PM)...")
    success = _run_evening_scan()
    if success:
        _write_scan_flag("success")
        alert("✅ Evening scan complete (7 PM retry)")
    else:
        alert("🚨 Evening scan FAILED all 3 attempts — no signals for tomorrow. Manual check needed.")

def morning_execution():
    if not is_trading_day():
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Non-trading day — execution skipped")
        return
    pending = load_pending()
    if not pending.get("buys") and not pending.get("sells"):
        alert("✅ No pending orders today")
        return
    alert("🌅 Executing orders...")
    fyers = get_fyers()
    if not fyers:
        alert("❌ Login failed — morning execution aborted")
        return
    positions = load_positions()
    cpt       = get_capital_per_trade(positions)
    stock_pos = {k: v for k, v in positions.items() if k != "_meta"}
    meta      = positions.get("_meta", {})
    today     = str(datetime.now().date())

    # Reconcile with broker before executing any orders (Bug 1 + Bug 10)
    reconcile_with_broker(fyers, positions)

    # Reload positions after reconciliation — picks up any auto-corrections
    # Without this, meta and stock_pos are stale if reconciliation fixed anything
    positions = load_positions()
    stock_pos = {k: v for k, v in positions.items() if k != "_meta"}
    meta      = positions.get("_meta", {})

    etf_price = get_fresh_etf_price(fyers)
    if not etf_price:
        alert("⚠️ Could not fetch NiftyBEES price — aborting")
        return

    if meta.get("etf_units", 0) == 0 and meta.get("capital_in_etf", 0) > 0:
        alert(f"🆕 First run! Buying NiftyBEES with ₹{meta['capital_in_etf']:,.0f}...")
        fresh = get_fresh_etf_price(fyers)
        if fresh:
            buy_etf(fyers, meta["capital_in_etf"], fresh, meta, "(initial)")

    n_buys  = len(pending.get("buys",  []))
    n_sells = len(pending.get("sells", []))

    total_sell_proceeds = 0
    total_buy_costs     = 0

    # Pre-calculate buy quantities once for both cases
    buy_orders = []
    for order in pending.get("buys", []):
        price     = order["signal_price"]
        trade_cap = cpt
        qty       = int(trade_cap / price)
        leftover  = trade_cap - qty * price
        if leftover >= 1000:
            overshoot = (qty + 1) * price - trade_cap
            if overshoot <= 1000:
                qty += 1
        name = order["symbol"].replace("NSE:", "").replace("-EQ", "")
        if qty <= 0:
            alert(f"⚠️ {name}: qty=0 at ₹{price} with ₹{trade_cap} capital — skipping")
            continue
        buy_orders.append({**order, "qty": qty})

    if n_buys > n_sells:
        # ── CASE 1: more buys than sells ──────────────────────────────────
        # Step 1: estimate net ETF sell needed upfront using live prices
        estimated_sell_val = 0
        for order in pending.get("sells", []):
            pos = stock_pos.get(order["symbol"])
            if not pos:
                continue
            lp = get_live_price(fyers, order["symbol"]) or 0
            estimated_sell_val += lp * int(pos.get("shares", 0))

        estimated_buy_val = sum(o["qty"] * o["signal_price"] for o in buy_orders)
        net_etf_sell      = estimated_buy_val - estimated_sell_val

        if net_etf_sell > 0:
            etf_price = get_fresh_etf_price(fyers) or etf_price
            sell_etf(fyers, net_etf_sell, etf_price, meta,
                     f"(net {n_buys}B-{n_sells}S)")

        # Step 2: all stock SELLs
        for order in pending.get("sells", []):
            symbol = order["symbol"]
            pos    = stock_pos.get(symbol)
            if not pos:
                continue
            qty  = int(pos.get("shares", 0))
            name = symbol.replace("NSE:", "").replace("-EQ", "")
            if qty <= 0:
                continue
            live_sell_price = get_live_price(fyers, symbol)
            if not live_sell_price:
                alert(f"⚠️ Could not get live price for {name} — skipping sell")
                continue
            resp = place_and_ensure_filled(fyers, symbol, -1, qty, live_sell_price)
            if resp.get("s") == "ok":
                ep         = pos.get("entry_price", 0)
                fill_price = resp.get("fill_price", live_sell_price)
                pnl        = (fill_price - ep) * qty
                total_sell_proceeds += fill_price * qty
                alert(f"🔴 SELL {qty} {name} @ ₹{fill_price} | {order['reason']} | P&L ₹{pnl:+,.0f}")
                log_trade("SELL", symbol, qty, fill_price, order["reason"], pnl, entry_price=ep)
                del positions[symbol]
                # 7-day re-entry cooldown
                from datetime import timedelta
                expiry = (datetime.now().date() + timedelta(days=7)).isoformat()
                meta.setdefault("cooldown", {})[symbol] = expiry
            else:
                alert(f"❌ SELL FAILED {name}: {resp.get('message')}")

        # Step 3: all stock BUYs
        for order in buy_orders:
            symbol    = order["symbol"]
            name      = symbol.replace("NSE:", "").replace("-EQ", "")
            qty       = order["qty"]
            trade_cap = cpt
            resp = place_and_ensure_filled(fyers, symbol, 1, qty, order["signal_price"])
            if resp.get("s") == "ok":
                fill_price = resp.get("fill_price", order["signal_price"])
                tp_price   = round(fill_price * 1.25, 2)
                sl_price   = round(fill_price * 0.80, 2)
                total_buy_costs += fill_price * qty
                alert(f"🟢 BUY {qty} {name} @ ₹{fill_price} | TP: ₹{tp_price} | SL: ₹{sl_price}")
                log_trade("BUY", symbol, qty, fill_price, f"TP={tp_price} SL={sl_price}")
                positions[symbol] = {
                    "entry_date":  today,
                    "entry_price": fill_price,
                    "shares":      qty,
                    "capital":     trade_cap,
                    "tp_price":    tp_price,
                    "sl_price":    sl_price,
                }
            else:
                alert(f"❌ BUY FAILED {name}: {resp.get('message')}")
                fresh_etf = get_fresh_etf_price(fyers) or etf_price
                rb_resp, _ = buy_etf(fyers, order["qty"] * order["signal_price"],
                                     fresh_etf, meta, f"(rollback {name})")
                if not rb_resp or rb_resp.get("s") != "ok":
                    alert(f"🚨 ROLLBACK FAILED for {name} — check manually.")

        # Case 1 surplus: if sell proceeds exceed buy costs, park surplus in ETF
        if net_etf_sell <= 0:
            actual_net = total_sell_proceeds - total_buy_costs
            if actual_net >= 500:
                etf_price = get_fresh_etf_price(fyers) or etf_price
                buy_etf(fyers, actual_net, etf_price, meta,
                        f"(surplus {n_buys}B-{n_sells}S)")

    else:
        # ── CASE 2: sells >= buys ──────────────────────────────────────────
        # Step 1: all stock SELLs
        for order in pending.get("sells", []):
            symbol = order["symbol"]
            pos    = stock_pos.get(symbol)
            if not pos:
                continue
            qty  = int(pos.get("shares", 0))
            name = symbol.replace("NSE:", "").replace("-EQ", "")
            if qty <= 0:
                continue
            live_sell_price = get_live_price(fyers, symbol)
            if not live_sell_price:
                alert(f"⚠️ Could not get live price for {name} — skipping sell")
                continue
            resp = place_and_ensure_filled(fyers, symbol, -1, qty, live_sell_price)
            if resp.get("s") == "ok":
                ep         = pos.get("entry_price", 0)
                fill_price = resp.get("fill_price", live_sell_price)
                pnl        = (fill_price - ep) * qty
                total_sell_proceeds += fill_price * qty
                alert(f"🔴 SELL {qty} {name} @ ₹{fill_price} | {order['reason']} | P&L ₹{pnl:+,.0f}")
                log_trade("SELL", symbol, qty, fill_price, order["reason"], pnl, entry_price=ep)
                del positions[symbol]
                # 7-day re-entry cooldown
                from datetime import timedelta
                expiry = (datetime.now().date() + timedelta(days=7)).isoformat()
                meta.setdefault("cooldown", {})[symbol] = expiry
            else:
                alert(f"❌ SELL FAILED {name}: {resp.get('message')}")

        # Step 2: all stock BUYs
        for order in buy_orders:
            symbol    = order["symbol"]
            name      = symbol.replace("NSE:", "").replace("-EQ", "")
            qty       = order["qty"]
            trade_cap = cpt
            resp = place_and_ensure_filled(fyers, symbol, 1, qty, order["signal_price"])
            if resp.get("s") == "ok":
                fill_price = resp.get("fill_price", order["signal_price"])
                tp_price   = round(fill_price * 1.25, 2)
                sl_price   = round(fill_price * 0.80, 2)
                total_buy_costs += fill_price * qty
                alert(f"🟢 BUY {qty} {name} @ ₹{fill_price} | TP: ₹{tp_price} | SL: ₹{sl_price}")
                log_trade("BUY", symbol, qty, fill_price, f"TP={tp_price} SL={sl_price}")
                positions[symbol] = {
                    "entry_date":  today,
                    "entry_price": fill_price,
                    "shares":      qty,
                    "capital":     trade_cap,
                    "tp_price":    tp_price,
                    "sl_price":    sl_price,
                }
            else:
                alert(f"❌ BUY FAILED {name}: {resp.get('message')}")

        # Step 3: net ETF buy with actual surplus
        net_surplus = total_sell_proceeds - total_buy_costs
        if net_surplus >= 500:
            etf_price = get_fresh_etf_price(fyers) or etf_price
            buy_etf(fyers, net_surplus, etf_price, meta,
                    f"(net {n_sells}S-{n_buys}B)")

    meta["capital_in_stocks"] = sum(
        v.get("entry_price", 0) * v.get("shares", 0)
        for k, v in positions.items() if k != "_meta"
    )

    meta["last_updated"] = today
    positions["_meta"]   = meta
    save_positions(positions)
    save_pending({"buys": [], "sells": []})
    alert("✅ Morning execution complete!")

def midday_check():
    if not is_trading_day():
        return
    positions = load_positions()
    stock_pos = {k: v for k, v in positions.items() if k != "_meta"}
    meta      = positions.get("_meta", {})
    if not stock_pos and meta.get("etf_units", 0) == 0:
        return
    fyers = get_fyers()
    if not fyers:
        alert("❌ Login failed — midday check aborted")
        return
    total_cap = meta.get("total_capital", CFG["default_capital"])
    cpt       = get_capital_per_trade(positions)
    msg       = f"📈 Portfolio — {datetime.now().strftime('%d %b %Y')}\n"
    msg      += f"💰 Total: ₹{total_cap:,.0f} | Per trade: ₹{cpt:,.0f}\n\n"
    total_pnl = 0
    # Batch fetch all prices in one API call
    all_symbols = list(stock_pos.keys()) + [NIFTYBEES]
    try:
        batch_resp   = fyers.quotes({"symbols": ",".join(all_symbols)})
        batch_prices = {item["n"]: item["v"]["lp"] for item in batch_resp.get("d", []) if "v" in item}
    except Exception as e:
        print(f"[batch quotes error] {e}")
        batch_prices = {}
    etf_price = batch_prices.get(NIFTYBEES)
    for symbol, pos in stock_pos.items():
        try:
            price = batch_prices.get(symbol)
            if not price:
                continue
            ep    = pos.get("entry_price", 0)
            qty   = pos.get("shares", 0)
            pnl   = (price - ep) * qty
            pct   = (price - ep) / ep * 100 if ep else 0
            total_pnl += pnl
            name  = symbol.replace("NSE:", "").replace("-EQ", "")
            emoji = "🟢" if pnl > 0 else "🔴"
            msg  += f"{emoji} {name:12} ₹{price:>8.2f} | {pct:+.1f}% | ₹{pnl:+,.0f}\n"
        except Exception as e:
            print(f"[midday quote error] {symbol}: {e}")
    if etf_price and meta.get("etf_units", 0) > 0:
        etf_units = meta["etf_units"]
        etf_avg   = meta.get("etf_avg_price", etf_price)
        etf_pnl   = (etf_price - etf_avg) * etf_units
        etf_pct   = (etf_price - etf_avg) / etf_avg * 100 if etf_avg else 0
        total_pnl += etf_pnl
        emoji      = "🟢" if etf_pnl > 0 else "🔴"
        msg += (f"{emoji} {'NIFTYBEES':12} ₹{etf_price:>8.2f} | "
                f"{etf_pct:+.1f}% | ₹{etf_pnl:+,.0f} ({etf_units} units)\n")
    msg += f"\n💰 Total Unrealised PnL: ₹{total_pnl:+,.0f}"
    alert(msg)

def log_portfolio_history():
    """2:00 PM — log portfolio value to history file for benchmark tracking."""
    if not is_trading_day():
        return
    fyers = get_fyers()
    if not fyers:
        alert("❌ Login failed — portfolio history log aborted")
        return
    try:
        from benchmark import append_portfolio_history, get_portfolio_value, get_portfolio_cost_basis
        positions  = load_positions()
        live_value = get_portfolio_value(fyers, positions)
        cost_basis = get_portfolio_cost_basis(positions)
        append_portfolio_history(live_value, cost_basis)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Portfolio history logged: ₹{live_value:,.0f}")
    except Exception as e:
        print(f"[portfolio history error] {e}")


def weekly_cash_sweep():
    """Friday 2:05 PM — sweep idle cash into NiftyBEES if above threshold."""
    from datetime import date
    if date.today().weekday() != 4:  # 4 = Friday
        return
    if not is_trading_day():
        alert('🧹 Weekly sweep: market holiday — skipping')
        return
    try:
        positions  = load_positions()
        meta       = positions.get('_meta', {})
        fyers      = get_fyers()
        from benchmark import get_portfolio_cost_basis, START_CAPITAL
        cost_basis = get_portfolio_cost_basis(positions)
        idle_cash  = max(0, START_CAPITAL - cost_basis)
        sweep_threshold = max(5000, meta.get("total_capital", 500000) // 100)
        if idle_cash < sweep_threshold:
            alert(f'🧹 Weekly sweep: idle cash ₹{idle_cash:,.0f} below ₹{sweep_threshold:,.0f} threshold — skipping')
            return
        etf_price = get_fresh_etf_price(fyers)
        if not etf_price:
            alert('⚠️ Weekly sweep: could not fetch NiftyBEES price — skipping')
            return
        resp, units = buy_etf(fyers, idle_cash, etf_price, meta, '(weekly cash sweep)')
        if resp and resp.get('s') == 'ok':
            alert(f'🧹 Weekly sweep: ₹{idle_cash:,.0f} idle cash → {units} NiftyBEES @ ₹{etf_price}')
            positions['_meta'] = meta
            save_positions(positions)
        else:
            alert(f'⚠️ Weekly sweep ETF buy failed')
    except Exception as e:
        alert(f'⚠️ Weekly sweep error: {e}')

def benchmark_snapshot():
    """6:30 PM — daily benchmark report to Telegram."""
    try:
        from benchmark import run_benchmark
        run_benchmark(send_telegram=True)
    except Exception as e:
        alert(f'⚠️ Benchmark snapshot error: {e}')


def exit_recheck_415pm():
    """4:15 PM — re-check exits using live quotes after market close.
    Runs regardless of whether 3:55 PM scan succeeded, because candle data
    at 3:55 PM may still show yesterday's close (Fyers settlement lag).
    Only adds exits to pending_signals — never removes existing ones."""
    if not is_trading_day():
        return
    fyers = get_fyers()
    if not fyers:
        alert("⚠️ 4:15 PM exit recheck — login failed")
        return
    try:
        positions  = load_positions()
        stock_pos  = {k: v for k, v in positions.items() if k != "_meta"}
        if not stock_pos:
            return

        # Fetch live quotes for all held symbols
        symbols    = list(stock_pos.keys())
        live       = {}
        for i in range(0, len(symbols), 10):
            batch = symbols[i:i+10]
            r     = fyers.quotes({"symbols": ",".join(batch)})
            for item in (r.get("d") or []):
                sym = item["n"]
                lp  = item["v"].get("lp") or item["v"].get("prev_close_price") or 0
                if lp:
                    live[sym] = lp

        # Load existing pending sells so we don't duplicate
        pending    = load_pending()
        already    = {o["symbol"] for o in pending.get("sells", [])}
        new_exits  = []

        for sym, pos in stock_pos.items():
            if sym in already:
                continue
            ltp    = live.get(sym, 0)
            if not ltp:
                continue
            entry  = pos.get("entry_price", 0)
            tp     = pos.get("tp_price",    entry * 1.25)
            sl     = pos.get("sl_price",    entry * 0.80)
            pct    = (ltp - entry) / entry * 100 if entry else 0
            if ltp <= sl:
                new_exits.append({"symbol": sym, "reason": f"🔴 SL HIT ({pct:.1f}%)", "urgency": "URGENT"})
            elif ltp >= tp:
                new_exits.append({"symbol": sym, "reason": f"🟢 TP HIT (+{pct:.1f}%)", "urgency": "URGENT"})

        if new_exits:
            pending["sells"] = pending.get("sells", []) + new_exits
            save_pending(pending)
            msg = f"⚠️ 4:15 PM Exit Recheck — {len(new_exits)} new exit(s) added:\n"
            for o in new_exits:
                msg += f"  • {o['symbol'].replace('NSE:','').replace('-EQ','')} — {o['reason']}\n"
            alert(msg)
        else:
            print(f"[4:15 recheck] No new exits from live quotes")
    except Exception as e:
        alert(f"⚠️ 4:15 PM exit recheck error: {e}")

def main():
    schedule.every().day.at("10:00").do(morning_execution)
    schedule.every().day.at("12:00").do(midday_check)
    schedule.every().day.at("14:00").do(log_portfolio_history)
    schedule.every().day.at("14:05").do(weekly_cash_sweep)
    schedule.every().day.at("15:55").do(evening_scan)
    schedule.every().day.at("16:15").do(exit_recheck_415pm)
    schedule.every().day.at("18:30").do(benchmark_snapshot)
    schedule.every().day.at("17:00").do(evening_scan_retry_1)
    schedule.every().day.at("19:00").do(evening_scan_retry_2)

    positions = load_positions()
    meta      = positions.get("_meta", {})
    alert(f"🤖 Nifty50 RSI Swing Bot Started!\n"
          f"💰 Capital: ₹{meta.get('total_capital', CFG['default_capital']):,.0f} | "
          f"Per trade: ₹{get_capital_per_trade(positions):,.0f}\n"
          f"📅 10:00 AM → Execute orders\n"
          f"   12:00 PM → P&L check\n"
          f"   3:55 PM  → Signal scan")

    if datetime.now().hour >= 16:
        evening_scan()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
