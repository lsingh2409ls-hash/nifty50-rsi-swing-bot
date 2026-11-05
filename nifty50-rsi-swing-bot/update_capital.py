"""
Capital Update Script
=====================
Usage: python3 update_capital.py <new_total_capital>
Example: python3 update_capital.py 1000000

What it does:
1. Updates total_capital and recalculates capital_per_trade (total/40)
2. Tops up existing stock positions to new capital_per_trade
3. Rebalances NiftyBEES for remaining idle capital
4. Sends Telegram confirmation
"""

import json, os, sys, requests
from datetime import datetime
from broker import get_fyers
from config import data_path, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

POSITIONS_FILE = data_path('positions.json')
TRADE_LOG      = data_path('trade_log.jsonl')
NIFTYBEES      = "NSE:NIFTYBEES-EQ"
TG_TOKEN       = TELEGRAM_TOKEN
TG_CHAT        = TELEGRAM_CHAT_ID

def alert(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg}, timeout=10)
    except Exception as e:
        print(f"[Telegram error] {e}")
    print(msg)

def load_positions():
    return json.load(open(POSITIONS_FILE))

def save_positions(pos):
    tmp = POSITIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pos, f, indent=2, default=str)
    os.replace(tmp, POSITIONS_FILE)

def get_tick_size(price):
    if price < 250:     return 0.01
    elif price < 1000:  return 0.05
    elif price < 5000:  return 0.10
    elif price < 10000: return 0.50
    elif price < 20000: return 1.00
    else:               return 5.00

def round_to_tick(price, tick):
    return round(round(price / tick) * tick, 2)

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

def get_live_price(fyers, symbol):
    try:
        data = fyers.quotes({"symbols": symbol})
        return float(data['d'][0]['v']['lp'])
    except Exception as e:
        print(f"[error] {e}")
        return None

def main():
    # ── Validate input ──────────────────────────────────────────
    if len(sys.argv) < 2:
        print("Usage: python3 update_capital.py <new_total_capital>")
        print("Example: python3 update_capital.py 1000000")
        sys.exit(1)

    try:
        new_total = int(sys.argv[1])
    except Exception as e:
        print(f"[error] {e}")
        print("❌ Invalid amount. Use a number like 1000000")
        sys.exit(1)

    # Guardrail 1: market hours check
    from datetime import datetime as dt
    now = dt.now()
    if not (9 <= now.hour < 15 or (now.hour == 15 and now.minute <= 30)):
        print(f"❌ Market closed ({now.strftime('%H:%M')}) — run between 9:15 and 15:30")
        sys.exit(1)
    print(f"✅ Market hours check passed ({now.strftime('%H:%M')})")

    positions = load_positions()
    meta      = positions.get("_meta", {})
    old_total = meta.get("total_capital", 0)
    old_cpt   = meta.get("capital_per_trade", old_total // 40)
    new_cpt   = new_total // 40
    added_cap = new_total - old_total

    # ── Validate ────────────────────────────────────────────────
    if new_total <= old_total:
        print(f"❌ New capital (₹{new_total:,.0f}) must be MORE than current (₹{old_total:,.0f})")
        sys.exit(1)

    stock_pos = {k: v for k, v in positions.items() if k != "_meta"}

    print(f"\n{'='*55}")
    print(f"  Capital Ramp-Up Plan")
    print(f"{'='*55}")
    print(f"  Current Capital:   ₹{old_total:>12,.0f}")
    print(f"  New Capital:       ₹{new_total:>12,.0f}")
    print(f"  Amount Adding:     ₹{added_cap:>12,.0f}")
    print(f"  Old per trade:     ₹{old_cpt:>12,.0f}")
    print(f"  New per trade:     ₹{new_cpt:>12,.0f}")
    print(f"  Open positions:    {len(stock_pos)}")
    print(f"\n  What will happen:")
    print(f"  • Each stock position topped up from ₹{old_cpt:,.0f} → ₹{new_cpt:,.0f}")
    print(f"  • Remaining capital deployed into NiftyBEES")
    print(f"{'='*55}\n")

    if "--yes" in sys.argv:
        print("Auto-confirmed via --yes flag")
    else:
        confirm = input("Confirm? This places real orders. Type 'yes' to proceed: ").strip().lower()
        if confirm != "yes":
            print("Cancelled.")
            sys.exit(0)

    # Guardrail 2: login check
    fyers = get_fyers()
    if not fyers:
        alert("❌ Login failed — update_capital aborted")
        sys.exit(1)
    print("✅ Login check passed")

    etf_price = get_live_price(fyers, NIFTYBEES)
    if not etf_price:
        print("❌ Could not fetch NiftyBEES price. Try again during market hours.")
        sys.exit(1)
    print(f"✅ NiftyBEES price fetched: {etf_price}")

    today             = str(datetime.now().date())
    total_stock_topup = 0
    total_etf_units   = 0

    # Guardrail 3: Telegram alert on start
    alert(f"🔄 Capital Ramp-Up Starting\n"
          f"₹{old_total:,.0f} → ₹{new_total:,.0f}\n"
          f"Per trade: ₹{old_cpt:,.0f} → ₹{new_cpt:,.0f}\n"
          f"NiftyBEES @ ₹{etf_price}\n"
          f"⏰ Started at {datetime.now().strftime('%H:%M:%S')}")

    # ── Step 1: Top up each stock position ──────────────────────
    print(f"\n📈 Topping up {len(stock_pos)} stock positions...")
    for symbol, pos in stock_pos.items():
        live_price    = get_live_price(fyers, symbol)
        if not live_price:
            alert(f"⚠️ No price for {symbol} — skipping")
            continue

        old_cap    = pos.get("capital", old_cpt)
        topup_cap  = new_cpt - old_cap
        if topup_cap <= 0:
            continue

        add_shares = int(topup_cap / live_price)
        if add_shares <= 0:
            continue

        name = symbol.replace("NSE:","").replace("-EQ","")
        resp = place_order(fyers, symbol, 1, add_shares, live_price)

        if resp.get("s") == "ok":
            order_id   = resp.get("id")
            fill_price = live_price  # fallback
            import time; time.sleep(3)
            try:
                ob = fyers.orderbook()
                for o in ob.get("orderBook", []):
                    if str(o.get("id")) == str(order_id) and o.get("tradedPrice", 0) > 0:
                        fill_price = o["tradedPrice"]
                        break
            except Exception as e:
                print(f"[fill price error] {e}")
            # Log top-up buy to trade_log (after fill price is known)
            with open(TRADE_LOG, "a") as _f:
                _f.write(json.dumps({
                    "ts":     datetime.now().isoformat(),
                    "action": "BUY",
                    "symbol": symbol,
                    "qty":    add_shares,
                    "price":  fill_price,
                    "reason": "CAPITAL_TOPUP"
                }) + "\n")
            old_shares    = pos.get("shares", 0)
            new_shares    = old_shares + add_shares
            actual_topup  = add_shares * fill_price
            new_avg_price = ((pos["entry_price"] * old_shares) +
                             (fill_price * add_shares)) / new_shares

            positions[symbol]["shares"]      = new_shares
            positions[symbol]["capital"]     = new_cpt
            positions[symbol]["entry_price"] = round(new_avg_price, 2)
            positions[symbol]["tp_price"]    = round(new_avg_price * 1.25, 2)
            positions[symbol]["sl_price"]    = round(new_avg_price * 0.80, 2)
            total_stock_topup += actual_topup

            alert(f"📈 TOP UP {add_shares} {name} @ ₹{live_price:.2f} | "
                  f"Shares: {old_shares}→{new_shares} | Avg: ₹{new_avg_price:.2f}")
            # Save after each successful top-up — prevents state loss on crash
            positions["_meta"] = meta
            save_positions(positions)
        else:
            alert(f"❌ TOP UP FAILED {name}: {resp.get('message')}")

    # ── Step 2: Deploy remaining into NiftyBEES ─────────────────
    remaining = added_cap - total_stock_topup
    print(f"\n📦 Deploying ₹{remaining:,.0f} into NiftyBEES...")

    if remaining >= etf_price:
        units_to_buy = int(remaining / etf_price)
        if units_to_buy > 0:
            resp = place_order(fyers, NIFTYBEES, 1, units_to_buy, etf_price)
            if resp.get("s") == "ok":
                total_etf_units  = units_to_buy
                with open(TRADE_LOG, "a") as _f:
                    _f.write(json.dumps({
                        "ts":     datetime.now().isoformat(),
                        "action": "BUY",
                        "symbol": "NSE:NIFTYBEES-EQ",
                        "qty":    units_to_buy,
                        "price":  etf_price,
                        "reason": "CAPITAL_TOPUP_ETF"
                    }) + "\n")
                old_units        = meta.get("etf_units", 0)
                old_avg          = meta.get("etf_avg_price", etf_price)
                new_units        = old_units + units_to_buy
                new_avg          = ((old_avg * old_units) +
                                    (etf_price * units_to_buy)) / new_units if new_units > 0 else etf_price

                meta["etf_units"]      = new_units
                meta["etf_avg_price"]  = round(new_avg, 2)
                meta["capital_in_etf"] = meta.get("capital_in_etf", 0) + remaining
                alert(f"📦 BUY {units_to_buy} NIFTYBEES @ ₹{etf_price} | "
                      f"Total units: {new_units} | New avg: ₹{new_avg:.2f}")
            else:
                alert(f"❌ ETF BUY FAILED: {resp.get('message')}")

    # ── Step 3: Update meta ─────────────────────────────────────
    meta["total_capital"]     = new_total
    meta["capital_per_trade"] = new_cpt
    meta["capital_in_stocks"] = sum(
        p.get("capital", new_cpt)
        for p in {k: v for k, v in positions.items() if k != "_meta"}.values()
    )
    meta["last_updated"] = today
    positions["_meta"]   = meta
    save_positions(positions)

    summary = (
        f"✅ Capital Ramp-Up Complete!\n\n"
        f"💰 Total Capital:    ₹{new_total:,.0f}\n"
        f"📊 Per Trade:        ₹{new_cpt:,.0f}\n"
        f"📈 Stocks topped up: {len(stock_pos)}\n"
        f"📦 NiftyBEES bought: {total_etf_units} units\n"
        f"📅 Date:             {today}"
    )
    alert(summary)
    print(f"\n{summary}")

    # Auto-restart trading bot to pick up new capital
    import subprocess
    result = subprocess.run(["systemctl", "restart", "tradingbot"], capture_output=True)
    if result.returncode == 0:
        alert("🔄 Trading bot restarted with new capital settings")
    else:
        alert("⚠️ Bot restart failed — run: systemctl restart tradingbot manually")

if __name__ == "__main__":
    main()
