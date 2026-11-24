import pandas as pd
import pytest

from src.trading_bot import Backtester


def test_backtester_uses_chronological_next_bar_prices_and_costs(tmp_path):
    data_file = tmp_path / "training_data.csv"
    pd.DataFrame(
        [
            {"timestamp": "2026-01-01T00:00:00Z", "coin": "BTC", "close": 10, "signal": "kaufen"},
            {"timestamp": "2026-01-01T00:00:00Z", "coin": "ETH", "close": 100, "signal": "halten"},
            {"timestamp": "2026-01-01T00:01:00Z", "coin": "BTC", "close": 12, "signal": "verkaufen"},
            {"timestamp": "2026-01-01T00:01:00Z", "coin": "ETH", "close": 200, "signal": "halten"},
            {"timestamp": "2026-01-01T00:02:00Z", "coin": "BTC", "close": 13, "signal": "halten"},
            {"timestamp": "2026-01-01T00:02:00Z", "coin": "ETH", "close": 300, "signal": "halten"},
        ]
    ).to_csv(data_file, index=False)

    backtester = Backtester(
        data_file=str(data_file),
        initial_cash=1000,
        fee_per_trade=0.01,
        slippage=0.02,
    )
    backtester.run(lambda row: row["signal"])

    assert [trade[2] for trade in backtester.trades] == ["buy", "sell"]
    assert backtester.trades[0][3] == pytest.approx(12 * 1.02)
    assert backtester.trades[1][3] == pytest.approx(13 * 0.98)
    amount = 1000 / (12 * 1.02 * 1.01)
    expected_cash = amount * (13 * 0.98) * 0.99
    assert backtester.equity_curve[-1] == pytest.approx(expected_cash)


def test_backtester_rejects_non_positive_prices(tmp_path):
    data_file = tmp_path / "training_data.csv"
    pd.DataFrame(
        [{"timestamp": "2026-01-01T00:00:00Z", "coin": "BTC", "close": 0}]
    ).to_csv(data_file, index=False)
    backtester = Backtester(data_file=str(data_file))

    with pytest.raises(ValueError, match="close prices"):
        backtester.run(lambda _: "halten")


def test_backtester_rejects_duplicate_coin_timestamps(tmp_path):
    data_file = tmp_path / "training_data.csv"
    pd.DataFrame(
        [
            {"timestamp": "2026-01-01T00:00:00Z", "coin": "BTC", "close": 10},
            {"timestamp": "2026-01-01T00:00:00Z", "coin": "BTC", "close": 11},
        ]
    ).to_csv(data_file, index=False)
    backtester = Backtester(data_file=str(data_file))

    with pytest.raises(ValueError, match="duplicate coin/timestamp"):
        backtester.run(lambda _: "halten")
