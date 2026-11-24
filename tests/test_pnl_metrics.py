"""Unit tests for the pure PnL accumulator in :mod:`src.pnl_metrics`."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from src.pnl_metrics import (
    MIN_DRAWDOWN_PCT_BASE_USD,
    PnLAccumulator,
    calculate_pnl_metrics,
    extract_return_pct,
    parse_timestamp,
    sharpe_ratio,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _cutoff(hours: float = 24) -> datetime:
    return _now() - timedelta(hours=hours)


def _sell(pnl, coin="ETH", *, hours_ago=1.0, **extra):
    trade = {
        "timestamp": (_now() - timedelta(hours=hours_ago)).isoformat(),
        "action": "sell",
        "coin": coin,
        "pnl_base": str(pnl),
    }
    trade.update(extra)
    return trade


def _buy(amount_base, coin="ETH", *, hours_ago=1.0):
    return {
        "timestamp": (_now() - timedelta(hours=hours_ago)).isoformat(),
        "action": "buy",
        "coin": coin,
        "amount_base": str(amount_base),
    }


def _fold(trades, cutoff_hours=24):
    accumulator = PnLAccumulator()
    cutoff = _cutoff(cutoff_hours)
    for trade in trades:
        accumulator.record(trade, cutoff)
    return accumulator


# --------------------------------------------------------------------------- #
# timestamps and return extraction
# --------------------------------------------------------------------------- #
def test_parse_timestamp_accepts_microseconds_and_rejects_garbage():
    assert parse_timestamp("2026-01-15T12:00:00.123456+00:00") == datetime(
        2026, 1, 15, 12, 0, 0, 123456, tzinfo=UTC
    )
    assert parse_timestamp("2026-01-15T12:00:00.123456").microsecond == 123456
    assert parse_timestamp("2026-01-15T12:00:00Z").year == 2026
    assert parse_timestamp("") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp("not a timestamp") is None


def test_extract_return_pct_prefers_first_present_column_and_normalises_percent():
    assert extract_return_pct({"pnl_pct": "5"}) == 0.05
    assert extract_return_pct({"pnl_pct": "0.05"}) == 0.05
    assert extract_return_pct({"pnl_pct": "", "return_pct": "-2"}) == -0.02
    assert extract_return_pct({"pnl_pct": "bad", "return": "1.5"}) == 0.015
    assert extract_return_pct({}) == 0.0
    assert extract_return_pct({"return": "nonsense"}) == 0.0


# --------------------------------------------------------------------------- #
# window handling
# --------------------------------------------------------------------------- #
def test_realized_pnl_is_all_time_but_win_loss_counts_are_window_scoped():
    accumulator = _fold(
        [
            _sell(10.0, hours_ago=200),
            _sell(-4.0, hours_ago=1.0),
        ]
    )

    assert accumulator.all_time_realized_pnl == pytest.approx(6.0)
    assert accumulator.total_realized_pnl == pytest.approx(-4.0)
    assert accumulator.total_trades == 1
    assert accumulator.losing_trades == 1
    assert accumulator.coin_closed == {"ETH": 1}


def test_buy_rows_only_feed_volume_counters():
    accumulator = _fold(
        [
            _buy(100.0, "ETH"),
            _buy(50.0, "BTC"),
            _buy(7.0, "ETH"),
            _buy(999.0, "SOL", hours_ago=200),
        ]
    )

    assert accumulator.coin_buy_volume == {"ETH": 107.0, "BTC": 50.0}
    assert accumulator.coin_buy_count == {"ETH": 2, "BTC": 1}
    assert accumulator.total_trades == 0
    assert accumulator.all_time_realized_pnl == 0.0


def test_negative_buy_volume_is_clamped_to_zero():
    accumulator = _fold([_buy(-25.0, "ETH")])

    assert accumulator.coin_buy_volume == {"ETH": 0.0}
    assert accumulator.coin_buy_count == {"ETH": 1}


def test_actions_other_than_buy_or_sell_are_ignored():
    accumulator = _fold(
        [
            {"timestamp": _now().isoformat(), "action": "stop-loss", "coin": "ETH", "pnl_base": "5"},
            {"timestamp": _now().isoformat(), "action": "", "coin": "ETH", "pnl_base": "5"},
        ]
    )

    assert accumulator.metrics()["total_trades"] == 0
    assert accumulator.all_time_realized_pnl == 0.0


def test_action_matching_is_case_insensitive_and_coin_is_upper_cased():
    accumulator = _fold(
        [
            {"timestamp": _now().isoformat(), "action": "Sell", "coin": "eth", "pnl_base": "3"},
            {"timestamp": _now().isoformat(), "action": "BUY", "coin": None, "amount_base": "4"},
        ]
    )

    assert accumulator.coin_pnl == {"ETH": 3.0}
    assert accumulator.coin_buy_volume == {"UNKNOWN": 4.0}


# --------------------------------------------------------------------------- #
# classification and per-coin aggregation
# --------------------------------------------------------------------------- #
def test_win_loss_and_break_even_classification():
    accumulator = _fold([_sell(1.0), _sell(-1.0), _sell(0.0), _sell(-0.0)])

    # -0.0 is neither greater nor less than zero, so it counts as break even
    assert accumulator.winning_trades == 1
    assert accumulator.losing_trades == 1
    assert accumulator.break_even_trades == 2
    assert accumulator.gross_profit == pytest.approx(1.0)
    assert accumulator.gross_loss == pytest.approx(-1.0)


def test_break_even_coins_are_registered_even_without_a_break_even_trade():
    accumulator = _fold([_sell(2.0, "SOL")])

    assert accumulator.coin_break_even == {"SOL": 0}


def test_coin_win_rate_uses_closed_counts_per_coin():
    accumulator = _fold(
        [
            _sell(1.0, "ETH"),
            _sell(1.0, "ETH"),
            _sell(-1.0, "ETH"),
            _sell(-2.0, "BTC"),
        ]
    )

    rates = accumulator.coin_win_rate()
    assert rates["ETH"] == pytest.approx(2 / 3 * 100.0)
    assert rates["BTC"] == 0.0


def test_derived_ratios_on_a_known_sample():
    accumulator = _fold([_sell(4.0), _sell(-2.0), _sell(6.0), _sell(-1.0)])

    assert accumulator.win_rate() == pytest.approx(50.0)
    assert accumulator.average_pnl_per_trade() == pytest.approx(1.75)
    assert accumulator.profit_factor() == pytest.approx(10.0 / 3.0)


def test_profit_factor_falls_back_to_gross_profit_without_losses():
    only_wins = _fold([_sell(2.0), _sell(3.0)])
    assert only_wins.profit_factor() == pytest.approx(5.0)

    nothing = _fold([])
    assert nothing.profit_factor() == 0.0


def test_empty_accumulator_reports_zero_ratios_not_errors():
    metrics = PnLAccumulator().metrics()

    assert metrics["win_rate"] == 0
    assert metrics["avg_pnl_per_trade"] == 0
    assert metrics["sharpe_ratio"] == 0.0
    assert metrics["max_drawdown_pct"] == 0.0
    assert metrics["coin_pnl"] == {}
    assert metrics["coin_win_rate"] == {}


# --------------------------------------------------------------------------- #
# equity curve / drawdown
# --------------------------------------------------------------------------- #
def test_drawdown_tracks_the_worst_peak_to_trough_move():
    accumulator = _fold([_sell(10.0), _sell(-4.0), _sell(8.0), _sell(-20.0)])

    # equity 10 -> 6 -> 14 -> -6, peak 14, worst peak-to-trough move 20 USD
    assert accumulator.max_drawdown_usd == pytest.approx(20.0)
    assert accumulator.max_drawdown_pct == pytest.approx(20.0 / 14.0)
    assert accumulator.metrics()["max_drawdown_pct"] == pytest.approx(20.0 / 14.0 * 100.0)


def test_drawdown_percentage_denominator_is_floored_near_zero_peak():
    accumulator = PnLAccumulator()
    accumulator._record_equity(0.2)
    accumulator._record_equity(-0.2)

    assert accumulator.max_drawdown_usd == pytest.approx(0.2)
    assert accumulator.max_drawdown_pct == pytest.approx(
        0.2 / MIN_DRAWDOWN_PCT_BASE_USD
    )


def test_drawdown_stays_zero_for_a_monotonic_equity_curve():
    accumulator = _fold([_sell(1.0), _sell(2.0), _sell(3.0)])

    assert accumulator.max_drawdown_usd == 0.0
    assert accumulator.max_drawdown_pct == 0.0


# --------------------------------------------------------------------------- #
# sharpe ratio
# --------------------------------------------------------------------------- #
def test_sharpe_ratio_needs_two_samples_and_zero_variance():
    assert sharpe_ratio([]) == 0.0
    assert sharpe_ratio([0.5]) == 0.0
    assert sharpe_ratio([0.25, 0.25, 0.25]) == 0.0

    # mean 0.1, sample std sqrt(0.02) -> (0.1 / sqrt(0.02)) * sqrt(2) == 1.0
    assert sharpe_ratio([0.0, 0.2]) == pytest.approx(1.0)

    # 0.1 is not exactly representable, so the variance of three identical
    # samples is tiny rather than zero and the ratio blows up instead
    assert sharpe_ratio([0.1, 0.1, 0.1]) > 1e12


def test_per_trade_returns_feed_the_sharpe_ratio():
    accumulator = _fold(
        [_sell(1.0, pnl_pct="2"), _sell(-1.0, pnl_pct="-2"), _sell(1.0, pnl_pct="4")]
    )

    assert accumulator.per_trade_returns == [0.02, -0.02, 0.04]
    assert accumulator.metrics()["sharpe_ratio"] == pytest.approx(
        sharpe_ratio([0.02, -0.02, 0.04])
    )


# --------------------------------------------------------------------------- #
# malformed rows
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "trade",
    [
        {"action": "sell", "coin": "ETH", "pnl_base": "5"},
        {"timestamp": "garbage", "action": "sell", "coin": "ETH", "pnl_base": "5"},
        {"timestamp": _now().isoformat(), "action": "sell", "coin": "ETH", "pnl_base": "abc"},
    ],
)
def test_malformed_rows_are_skipped_without_touching_the_counters(trade):
    accumulator = _fold([trade, _sell(3.0)])

    assert accumulator.total_trades == 1
    assert accumulator.all_time_realized_pnl == pytest.approx(3.0)
    assert accumulator.coin_buy_volume == {}


def test_unparseable_buy_volume_counts_the_buy_with_zero_volume():
    accumulator = _fold(
        [{"timestamp": _now().isoformat(), "action": "buy", "coin": "ETH", "amount_base": "abc"}]
    )

    assert accumulator.coin_buy_volume == {"ETH": 0.0}
    assert accumulator.coin_buy_count == {"ETH": 1}


def test_naive_timestamp_still_counts_towards_all_time_pnl():
    """The window comparison raises for a naive journal timestamp, but the
    realized PnL is added first — a quirk of the original implementation that
    the exporters depend on, so it is pinned here."""
    accumulator = PnLAccumulator()
    with pytest.raises(TypeError):
        accumulator.record(
            {"timestamp": "2025-01-01T00:00:00.123456", "action": "Sell", "coin": "", "pnl_base": "inf"},
            _cutoff(),
        )
    assert accumulator.all_time_realized_pnl == math.inf
    assert accumulator.total_trades == 0


# --------------------------------------------------------------------------- #
# calculate_pnl_metrics wrapper
# --------------------------------------------------------------------------- #
def test_calculate_pnl_metrics_skips_rows_that_raise_and_returns_full_report():
    trades = [None, 7, _sell(5.0, hours_ago=2)]

    metrics = calculate_pnl_metrics(trades, time_window_hours=24)

    assert metrics["total_trades"] == 1
    assert metrics["all_time_realized_pnl"] == pytest.approx(5.0)
    assert metrics["coin_closed"] == {"ETH": 1}
    assert set(metrics) == {
        "all_time_realized_pnl",
        "total_realized_pnl",
        "total_trades",
        "winning_trades",
        "losing_trades",
        "break_even_trades",
        "win_rate",
        "avg_pnl_per_trade",
        "profit_factor",
        "sharpe_ratio",
        "max_drawdown_usd",
        "max_drawdown_pct",
        "coin_pnl",
        "coin_closed",
        "coin_win_rate",
        "coin_break_even",
        "coin_buy_volume",
        "coin_buy_count",
    }


def test_calculate_pnl_metrics_honours_a_custom_window():
    trades = [_sell(5.0, hours_ago=3), _sell(-2.0, hours_ago=10)]

    narrow = calculate_pnl_metrics(trades, time_window_hours=4)
    wide = calculate_pnl_metrics(trades, time_window_hours=24)

    assert narrow["total_trades"] == 1
    assert narrow["total_realized_pnl"] == pytest.approx(5.0)
    assert wide["total_trades"] == 2
    assert wide["total_realized_pnl"] == pytest.approx(3.0)


def test_calculate_pnl_metrics_does_not_mutate_the_input_rows():
    trade = _sell(5.0)
    before = dict(trade)

    calculate_pnl_metrics([trade])

    assert trade == before
