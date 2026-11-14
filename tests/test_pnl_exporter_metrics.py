from datetime import UTC, datetime

from src.export_pnl_metrics import calculate_pnl_metrics
from src.pnl_exporter import MetricsHandler


def _closed_trade(pnl: float) -> dict[str, str]:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "action": "sell",
        "coin": "ETH",
        "pnl_base": str(pnl),
    }


def test_exporter_metrics_classify_positive_negative_and_break_even_trades():
    trades = [_closed_trade(1.0), _closed_trade(-1.0), _closed_trade(0.0)]

    legacy_metrics = calculate_pnl_metrics(trades)
    handler = object.__new__(MetricsHandler)
    exporter_metrics = handler.calculate_pnl_metrics(trades)

    for metrics in (legacy_metrics, exporter_metrics):
        assert metrics["winning_trades"] == 1
        assert metrics["losing_trades"] == 1
        assert metrics["break_even_trades"] == 1
        assert metrics["win_rate"] == (1 / 3) * 100.0
        assert metrics["coin_break_even"] == {"ETH": 1}
