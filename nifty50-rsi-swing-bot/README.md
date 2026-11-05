# Nifty 50 RSI Swing Trading Bot

A fully automated, end-of-day swing trading system for the Nifty 50, running live on NSE through the Fyers API since May 2026. It covers the full loop: signal generation, order execution with limit-order repricing, broker reconciliation, idle-cash management, performance benchmarking and Telegram alerting. It also includes the research scripts used to calibrate it.

> Personal project. Not investment advice.

## Strategy

| Rule | Value |
|---|---|
| Universe | Nifty 50 constituents, daily bars |
| Signal | RSI(9) and its 20-day SMA |
| Entry | RSI-SMA > 55 **and** RSI < 80 **and** volume > 10-day average |
| Ranking | Candidates sorted by RSI-SMA when slots are scarce |
| Exit | Take-profit +25%, stop-loss −20%, or RSI > 80 |
| Sizing | Equal weight, 40 slots (capital ÷ 40 per trade), CNC delivery |
| Re-entry | 7-day cooldown after any exit |
| Idle cash | Parked in NiftyBEES ETF, so undeployed capital still tracks the index |

## How it runs

The bot is a single long-running process (`trading_bot.py`, managed by systemd) with an internal schedule (IST):

| Time | Job |
|---|---|
| 10:00 | **Execute**: reconcile with broker, then place sells, buys and the net NiftyBEES adjustment |
| 12:00 | Midday P&L snapshot to Telegram |
| 14:00 | Log portfolio value for benchmarking |
| 14:05 (Fri) | Sweep idle cash into NiftyBEES |
| 15:55 | **Scan**: compute signals, write `pending_signals.json` for next morning |
| 16:15 | Re-check TP/SL against post-close live quotes (candle data can lag) |
| 17:00, 19:00 | Retry the scan if the 15:55 run failed |
| 18:30 | Benchmark report vs Nifty 50 / 100 / 500 / 50 EW |

### Engineering details worth noting
- **Limit-order repricing loop**: orders are re-checked after 30 s, then every 5 min. Unfilled orders are cancelled and repriced at the live quote, with a hard 15:20 cutoff.
- **Tick-size compliance**: limit prices are rounded to NSE price bands (₹0.01 to ₹5.00) using integer-paise arithmetic to avoid float drift.
- **Broker reconciliation** before every execution:
  - Merges settled holdings, T+1 unsettled quantities and same-day positions.
  - Corrects entry prices to actual fills.
  - Flags quantity mismatches and likely corporate actions (cost drift > 15%).
  - Auto-corrects ETF unit drift.
- **Net ETF batching**: only one NiftyBEES order per cycle. It sells upfront when buys exceed sells, and buys the surplus at the end otherwise.
- **Crash safety**: state is written atomically (write to temp file, then `os.replace`). Every fill goes to an append-only trade audit log, and scan status flags make the retries idempotent.
- **Calendar guards**: weekends and NSE holidays are skipped.

## Research and calibration

| Script | Purpose |
|---|---|
| `backtest/live_backtest.py` | Full-period simulation of the live rules on Nifty 50 daily data (Mar 2023 – Mar 2026); exports a trade log |
| `backtest/cooldown_comparison.py` | Re-runs the strategy with 0/7/14/21/30-day re-entry cooldowns, **with full transaction costs** (brokerage, STT, exchange, SEBI, stamp duty, DP, GST) and **next-day-open fills** |

Key findings that shaped the live configuration, from the cost-aware backtest over May 2025 – May 2026:
- The strategy returned a CAGR of **+6.9%** against **−0.3%** for the Nifty 50.
- **Transaction costs consumed about 40% of gross P&L**. With 40 small positions, flat per-order charges dominate.
- A **7-day re-entry cooldown** added about **+1.3% net alpha**, mainly by cutting churn in a few stocks that repeatedly triggered exits and re-entries.
- 40 equal-weight slots beat a concentrated 5-slot version. Most of the edge comes from the diversified, equal-weight structure rather than the RSI timing.

## Validation notes and known limitations

These are documented deliberately. They are the first things to fix before treating the backtest numbers as a reliable estimate.

1. **Survivorship and constituent bias.** The backtest uses one fixed list of stocks, not point-in-time index membership, so stocks that left the index are under-represented.
2. **Backtest and live universes differ.** `live_backtest.py` uses 48 names: it excludes INFY and TCS and includes six stocks the live bot no longer trades. The live bot trades 50.
3. **Rule drift between backtest and live.** The live bot adds a volume filter, ranks candidates by RSI-SMA and applies the 7-day cooldown. `live_backtest.py` has none of these and fills slots in list order. `cooldown_comparison.py` adds the cooldown but not the volume filter.
4. **Optimistic fills in `live_backtest.py`.** Entries fill at the close of the signal bar and no costs are deducted. `cooldown_comparison.py` fixes both (next-day open fills, full charges) and is the more reliable estimate.
5. **TP/SL fill assumption.** Exits are assumed to fill exactly at the +25% / −20% threshold, even when the close gapped past it. This understates gap-down losses on stops.
6. **In-sample parameter choice.** The cooldown length was chosen as the best performer on the same one-year window it was evaluated on. An out-of-sample or walk-forward check is still outstanding.
7. **Inconsistent risk metrics.** The backtests compute Sharpe from monthly returns with no risk-free rate. `benchmark.py` uses daily returns with a 6.5% risk-free rate, so the two are not directly comparable.
8. **Known execution issue.** When buys outnumber sells, NiftyBEES is sold upfront using *estimated* sale proceeds. If a stock sell then fails, buys can go ahead against cash that never arrived. Failed *buys* are rolled back into the ETF, but failed *sells* are not.
9. **Static holiday calendar.** NSE holidays are hard-coded through 2026.

## Repository layout

```
trading_bot.py          live bot: signals, execution, reconciliation, scheduling
benchmark.py            live performance vs Nifty indices (return, vol, Sharpe, max DD)
update_capital.py       safe capital ramp-up: tops up positions, deploys remainder to ETF
broker.py               Fyers session factory (expects an access token in .env)
config.py               paths and environment configuration
backtest/               research scripts
tests/                  unit tests: tick rounding, RSI, entry/exit rules, charges
deploy/                 example systemd unit
data/                   state files (git-ignored) + Nifty 50 benchmark series
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # add Fyers and Telegram credentials
pytest                      # unit tests run offline

python -m backtest.live_backtest --save-signals   # needs Fyers API access
python -m backtest.cooldown_comparison
python trading_bot.py                             # live: places real orders
```

The production deployment refreshes the Fyers access token daily with a separate TOTP-based login module. That module is not included here because it handles account credentials. `broker.py` reads a token from `.env` instead.

## Tech

Python · pandas · NumPy · Fyers API v3 · schedule · systemd · Telegram Bot API · pytest
