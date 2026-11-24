import pandas as pd
import pytest

from src.historical_backtest import (
    HistoricalRuleBacktester,
    ReplaySettings,
    load_ohlcv,
)
from src.strategy_rules import rule_recommendation


def test_rule_recommendation_matches_live_rules():
    assert rule_recommendation(25, 12, 10, 13) == ("STRONG BUY", 90)
    assert rule_recommendation(35, 12, 10, 11) == ("BUY", 75)
    assert rule_recommendation(75, 8, 10, 7) == ("SELL", 10)
    assert rule_recommendation(65, 8, 10, 9) == ("WEAK SELL", 25)
    assert rule_recommendation(float("nan"), 8, 10, 9) == ("HOLD", 50)


def test_replay_executes_on_next_bar_open_and_applies_costs():
    times = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
    data = pd.DataFrame([
        {
            "timestamp": times[0], "coin": "ETH", "open": 10, "high": 10,
            "low": 10, "close": 10, "volume": 100, "recommendation": "HOLD",
            "score": 50, "quote_volume_24h": 100_000,
        },
        {
            "timestamp": times[1], "coin": "ETH", "open": 11, "high": 11,
            "low": 11, "close": 11, "volume": 100, "recommendation": "BUY",
            "score": 75, "quote_volume_24h": 100_000,
        },
        {
            "timestamp": times[2], "coin": "ETH", "open": 12, "high": 13,
            "low": 12, "close": 13, "volume": 100, "recommendation": "SELL",
            "score": 10, "quote_volume_24h": 100_000,
        },
    ])
    settings = ReplaySettings(
        initial_cash=100,
        fee_rate=0.01,
        slippage=0.02,
        risk_pct=1.0,
        trade_amount=50,
        min_trade_amount=5,
        max_open_trades=1,
        min_volume_base=0,
        top_n=1,
        stop_loss_pct=0.5,
        take_profit_pct=0.5,
        excluded_coins=frozenset(),
    )

    result = HistoricalRuleBacktester(data, settings).run()

    assert [(trade["action"], trade["timestamp"]) for trade in result["trades"]] == [
        ("buy", times[2]),
        ("sell", times[2]),
    ]
    assert result["trades"][0]["price"] == pytest.approx(12 * 1.02)
    assert result["trades"][1]["price"] == pytest.approx(13 * 0.98)
    amount = 50 / (12 * 1.02 * 1.01)
    expected_cash = 50 + amount * (13 * 0.98) * 0.99
    assert result["end_cash"] == pytest.approx(expected_cash)


def test_holdout_replay_uses_pre_split_signal_but_starts_with_cash():
    times = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
    data = pd.DataFrame([
        {
            "timestamp": times[0], "coin": "ETH", "open": 10, "high": 10,
            "low": 10, "close": 10, "volume": 100, "recommendation": "BUY",
            "score": 75, "quote_volume_24h": 100_000,
        },
        {
            "timestamp": times[1], "coin": "ETH", "open": 11, "high": 11,
            "low": 11, "close": 11, "volume": 100, "recommendation": "HOLD",
            "score": 50, "quote_volume_24h": 100_000,
        },
        {
            "timestamp": times[2], "coin": "ETH", "open": 12, "high": 12,
            "low": 12, "close": 12, "volume": 100, "recommendation": "HOLD",
            "score": 50, "quote_volume_24h": 100_000,
        },
    ])
    settings = ReplaySettings(
        initial_cash=100, trade_amount=50, min_trade_amount=5,
        risk_pct=1.0,
        min_volume_base=0, excluded_coins=frozenset(),
        stop_loss_pct=0.5, take_profit_pct=0.5,
    )

    result = HistoricalRuleBacktester(data, settings).run(start_time=times[1])

    assert result["trades"][0]["timestamp"] == times[1]
    assert result["trades"][0]["price"] == pytest.approx(11 * 1.001)
    assert result["start_cash"] == 100


def test_replay_selects_universe_using_only_previous_candle_volume():
    times = pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")
    data = pd.DataFrame([
        {
            "timestamp": times[0], "coin": "ETH", "open": 10, "high": 10,
            "low": 10, "close": 10, "volume": 100, "recommendation": "BUY",
            "score": 75, "quote_volume_24h": 100_000,
        },
        {
            "timestamp": times[0], "coin": "SOL", "open": 10, "high": 10,
            "low": 10, "close": 10, "volume": 100, "recommendation": "BUY",
            "score": 75, "quote_volume_24h": 1,
        },
        {
            "timestamp": times[1], "coin": "ETH", "open": 11, "high": 11,
            "low": 11, "close": 11, "volume": 100, "recommendation": "HOLD",
            "score": 50, "quote_volume_24h": 1,
        },
        {
            "timestamp": times[1], "coin": "SOL", "open": 11, "high": 11,
            "low": 11, "close": 11, "volume": 100, "recommendation": "HOLD",
            "score": 50, "quote_volume_24h": 100_000,
        },
    ])
    settings = ReplaySettings(
        initial_cash=100, risk_pct=1, trade_amount=10,
        min_trade_amount=5, min_volume_base=50_000, top_n=1,
        excluded_coins=frozenset(), stop_loss_pct=0.5, take_profit_pct=0.5,
    )

    result = HistoricalRuleBacktester(data, settings).run()

    assert result["trades"][0]["coin"] == "ETH"


def test_replay_honors_recent_pnl_guard():
    times = pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")
    data = pd.DataFrame([
        {
            "timestamp": timestamp, "coin": "ETH", "open": 10, "high": 10,
            "low": 10, "close": 10, "volume": 100, "recommendation": "BUY",
            "score": 75, "quote_volume_24h": 100_000,
        }
        for timestamp in times
    ])
    settings = ReplaySettings(
        initial_cash=100, min_volume_base=0, excluded_coins=frozenset(),
        recent_pnl_guard_min_trades=1,
    )
    backtester = HistoricalRuleBacktester(data, settings)

    assert backtester._recent_pnl_guard_active([-1.0])
    assert not backtester._recent_pnl_guard_active([1.0])


def test_holdout_carries_training_guard_history_without_carrying_positions():
    times = pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")
    data = pd.DataFrame([
        {
            "timestamp": timestamp, "coin": "ETH", "open": 10, "high": 10,
            "low": 10, "close": 10, "volume": 100, "recommendation": "BUY",
            "score": 75, "quote_volume_24h": 100_000,
        }
        for timestamp in times
    ])
    settings = ReplaySettings(
        initial_cash=100, min_volume_base=0, excluded_coins=frozenset(),
        recent_pnl_guard_min_trades=1,
    )

    result = HistoricalRuleBacktester(data, settings).run(
        start_time=times[1], prior_closed_pnls=[-1.0]
    )

    assert result["trades"] == []
    assert result["end_cash"] == 100


def test_load_ohlcv_rejects_duplicate_rows(tmp_path):
    path = tmp_path / "ohlcv.csv"
    pd.DataFrame([
        {"timestamp": "2026-01-01T00:00:00Z", "coin": "ETH", "open": 1,
         "high": 1, "low": 1, "close": 1, "volume": 1},
        {"timestamp": "2026-01-01T00:00:00Z", "coin": "ETH", "open": 1,
         "high": 1, "low": 1, "close": 1, "volume": 1},
    ]).to_csv(path, index=False)

    with pytest.raises(ValueError, match="duplicate"):
        load_ohlcv(path, ReplaySettings())
