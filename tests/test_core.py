"""Unit tests for the pure building blocks: tick rounding, RSI, order generation, charges."""
import numpy as np
import pandas as pd
import pytest

import trading_bot as tb
from backtest.cooldown_comparison import stock_charges


@pytest.mark.parametrize("price,tick", [
    (100, 0.01), (249.99, 0.01), (250, 0.05), (999.9, 0.05), (1000, 0.10),
    (4999, 0.10), (5000, 0.50), (10000, 1.00), (20000, 5.00),
])
def test_tick_size_bands(price, tick):
    assert tb.get_tick_size(price) == tick


@pytest.mark.parametrize("price,tick,expected", [
    (1234.57, 0.10, 1234.6), (512.33, 0.05, 512.35), (512.32, 0.05, 512.3), (25012.4, 5.0, 25010.0),
])
def test_round_to_tick(price, tick, expected):
    out = tb.round_to_tick(price, tick)
    assert out == pytest.approx(expected)
    assert round(out / tick, 6).is_integer()


def test_rsi_bounds_and_extremes():
    up = pd.Series(np.linspace(100, 200, 60))
    rsi_up = tb.compute_rsi(up, 9).dropna()
    assert ((rsi_up >= 0) & (rsi_up <= 100)).all()
    # strictly rising prices have no losses -> RSI undefined (NaN), never a false value
    noisy = pd.Series(100 + np.cumsum(np.random.default_rng(0).normal(0, 1, 200)))
    rsi = tb.compute_rsi(noisy, 9).dropna()
    assert ((rsi > 0) & (rsi < 100)).all()


def _positions(entry_price, capital=25_000, total=1_000_000):
    return {"_meta": {"total_capital": total, "capital_per_trade": capital},
            "NSE:AAA-EQ": {"entry_price": entry_price, "shares": 10, "capital": capital}}


@pytest.mark.parametrize("close,rsi,reason", [
    (79, 50, "SL HIT"), (126, 50, "TP HIT"), (105, 85, "RSI EXIT"),
])
def test_exit_rules(close, rsi, reason):
    signals = {"NSE:AAA-EQ": {"close": close, "rsi": rsi, "rsi_sma": 50, "vol_ok": True}}
    exits, _ = tb.generate_orders(signals, _positions(100))
    assert len(exits) == 1 and reason in exits[0]["reason"]


def test_no_exit_inside_band():
    signals = {"NSE:AAA-EQ": {"close": 105, "rsi": 60, "rsi_sma": 50, "vol_ok": True}}
    exits, _ = tb.generate_orders(signals, _positions(100))
    assert exits == []


def test_entry_filters_and_ranking():
    signals = {
        "NSE:B-EQ": {"close": 100, "rsi": 70, "rsi_sma": 60, "vol_ok": True},   # valid
        "NSE:C-EQ": {"close": 100, "rsi": 70, "rsi_sma": 65, "vol_ok": True},   # valid, stronger
        "NSE:D-EQ": {"close": 100, "rsi": 85, "rsi_sma": 70, "vol_ok": True},   # RSI too high
        "NSE:E-EQ": {"close": 100, "rsi": 60, "rsi_sma": 50, "vol_ok": True},   # SMA too low
        "NSE:F-EQ": {"close": 100, "rsi": 60, "rsi_sma": 60, "vol_ok": False},  # volume filter
    }
    pos = {"_meta": {"total_capital": 1_000_000, "capital_per_trade": 25_000}}
    _, entries = tb.generate_orders(signals, pos)
    assert [e["symbol"] for e in entries] == ["NSE:C-EQ", "NSE:B-EQ"]


def test_entries_capped_by_free_slots():
    signals = {f"NSE:S{i}-EQ": {"close": 100, "rsi": 60, "rsi_sma": 60 + i, "vol_ok": True} for i in range(5)}
    pos = {"_meta": {"total_capital": 50_000, "capital_per_trade": 25_000}}
    _, entries = tb.generate_orders(signals, pos)
    assert len(entries) == 2


def test_round_trip_charges_positive_and_sensible():
    chg = stock_charges(25_000, 26_000)
    assert 50 < chg < 150   # flat brokerage + DP + ~0.1% STT on sell side
