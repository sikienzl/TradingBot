import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src import pnl_exporter
from src.export_pnl_metrics import calculate_pnl_metrics, read_latest_portfolio_snapshot
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


@pytest.fixture(params=["legacy", "http"])
def read_snapshot(request, monkeypatch, tmp_path):
    log_path = tmp_path / "bot.log"
    if request.param == "legacy":
        return log_path, lambda: read_latest_portfolio_snapshot(log_path)
    monkeypatch.setattr(pnl_exporter, "BOT_LOG_PATH", log_path)
    handler = object.__new__(MetricsHandler)
    return log_path, handler.read_latest_portfolio_snapshot


@pytest.mark.parametrize(
    ("value", "cash"),
    [(0.0, 0.0), (0.0, 5.0), (10.0, 0.0), (1e-12, 1e-12), (10.0, 5.0)],
)
def test_snapshot_uses_latest_value_and_cash_even_when_zero(read_snapshot, value, cash):
    log_path, read = read_snapshot
    log_path.write_text(
        "2025-01-01 00:00:00,000 - Portfolio value: 100 EUR\n"
        "2025-01-01 00:00:00,000 -   - Cash: 50 EUR\n"
        f"2025-01-02 00:00:00,000 - Portfolio value: {value} EUR\n"
        f"2025-01-02 00:00:00,000 -   - Cash: {cash} EUR\n",
        encoding="utf-8",
    )

    snapshot = read()

    assert snapshot["portfolio_value_eur"] == pytest.approx(value, abs=1e-15)
    assert snapshot["portfolio_cash_eur"] == pytest.approx(cash, abs=1e-15)
    if "portfolio_snapshot_timestamp_unixtime" in snapshot:
        expected = datetime(2025, 1, 2).timestamp()
        assert snapshot["portfolio_snapshot_timestamp_unixtime"] == pytest.approx(expected)


def test_snapshot_falls_back_to_older_valid_values(read_snapshot, capsys):
    log_path, read = read_snapshot
    log_path.write_text(
        "Portfolio value: 100 EUR\n"
        "  - Cash: 50 EUR\n"
        "Portfolio value: invalid EUR\n"
        "  - Cash: invalid EUR\n",
        encoding="utf-8",
    )

    snapshot = read()

    assert snapshot["portfolio_value_eur"] == pytest.approx(100.0)
    assert snapshot["portfolio_cash_eur"] == pytest.approx(50.0)
    errors = capsys.readouterr().err
    assert "Error parsing portfolio value" in errors
    assert "Error parsing portfolio cash" in errors


def test_snapshot_keeps_holdings_with_zero_cash(read_snapshot):
    log_path, read = read_snapshot
    log_path.write_text(
        "Portfolio value: 100 EUR\n"
        "  - Cash: 50 EUR\n"
        "Portfolio value: 10 EUR\n"
        "  - Cash: 0 EUR\n"
        "  - Holdings: {'ETH': 2}\n"
        "  - Open trades details: {'ETH': {'amount_base': 10, 'amount_coin': 2}}\n",
        encoding="utf-8",
    )

    snapshot = read()

    assert snapshot["portfolio_value_eur"] == pytest.approx(10.0)
    assert snapshot["portfolio_cash_eur"] == pytest.approx(0.0)
    assert snapshot["holdings_amount_coin"] == {"ETH": pytest.approx(2.0)}
    assert snapshot["holdings_value_eur"] == {"ETH": pytest.approx(10.0)}
    assert snapshot["open_positions_count"] == 1


def test_snapshot_defaults_when_log_is_missing(read_snapshot):
    _, read = read_snapshot

    snapshot = read()

    assert snapshot["portfolio_value_eur"] == pytest.approx(0.0)
    assert snapshot["portfolio_cash_eur"] == pytest.approx(0.0)


def test_http_snapshot_tracks_latest_valid_timestamp_and_age(monkeypatch, tmp_path):
    log_path = tmp_path / "bot.log"
    log_path.write_text(
        "2025-01-01 00:00:00,000 - Portfolio value: 100 EUR\n"
        "2025-01-02 00:00:00,000 - Portfolio value: invalid EUR\n"
        "2025-01-02 00:00:00,000 -   - Cash: 0 EUR\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(pnl_exporter, "BOT_LOG_PATH", log_path)
    snapshot = object.__new__(MetricsHandler).read_latest_portfolio_snapshot()

    timestamp = datetime(2025, 1, 1).timestamp()
    assert snapshot["portfolio_snapshot_timestamp_unixtime"] == pytest.approx(timestamp)
    assert snapshot["portfolio_snapshot_age_seconds"] == pytest.approx(
        max(0.0, snapshot["metrics_generated_unixtime"] - timestamp)
    )


def test_http_exporter_imports_as_src_package_module():
    repo_root = Path(pnl_exporter.__file__).resolve().parent.parent
    subprocess.run(
        [sys.executable, "-c", "import src.pnl_exporter"],
        cwd=repo_root,
        check=True,
    )
