import json
import pytest

import export_pnl_metrics
import pnl_exporter


def test_export_pnl_metrics_uses_kraken_balance_first(tmp_path, monkeypatch):
    log_path = tmp_path / "bot.log"
    state_path = tmp_path / ".portfolio_state.json"

    log_path.write_text(
        "2024-01-01 00:00:00,000 - INFO - Portfolio initialized from exchange (dry-run mode). Cash: 1000.00 EUR, Holdings: {}\n"
        "2024-01-01 00:00:01,000 - INFO - 📈 Portfolio value: 1000.00 EUR\n",
        encoding="utf-8",
    )
    state_path.write_text(json.dumps(
        {"initial_portfolio_value": 1234.56}), encoding="utf-8")

    monkeypatch.setenv("KRAKEN_API_KEY", "test-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "test-secret")
    export_pnl_metrics.START_VALUE_CACHE["expires_at"] = 0.0
    export_pnl_metrics.START_VALUE_CACHE["value"] = 0.0
    monkeypatch.setattr(export_pnl_metrics, "ccxt", None)
    monkeypatch.setattr(
        export_pnl_metrics,
        "_kraken_private_request",
        lambda path, payload, api_key, api_secret: {
            "error": [], "result": {"eb": "19.42"}},
    )

    value = export_pnl_metrics.read_portfolio_start_value(
        log_path=str(log_path))

    assert value == pytest.approx(19.42)


def test_pnl_exporter_uses_kraken_balance_first(tmp_path, monkeypatch):
    log_path = tmp_path / "bot.log"
    state_path = tmp_path / ".portfolio_state.json"

    log_path.write_text(
        "2024-01-01 00:00:00,000 - INFO - Portfolio initialized from exchange (dry-run mode). Cash: 1000.00 EUR, Holdings: {}\n"
        "2024-01-01 00:00:01,000 - INFO - 📈 Portfolio value: 1000.00 EUR\n",
        encoding="utf-8",
    )
    state_path.write_text(json.dumps(
        {"initial_portfolio_value": 1234.56}), encoding="utf-8")

    monkeypatch.setenv("KRAKEN_API_KEY", "test-key")
    monkeypatch.setenv("KRAKEN_API_SECRET", "test-secret")
    monkeypatch.setattr(pnl_exporter, "ccxt", None)
    monkeypatch.setattr(
        pnl_exporter.MetricsHandler,
        "_read_env_value",
        lambda self, key: "EUR" if key == "BASE_CURRENCY" else "",
    )
    monkeypatch.setattr(
        pnl_exporter.MetricsHandler,
        "_kraken_private_request",
        lambda self, path, payload, api_key, api_secret: {
            "error": [], "result": {"eb": "19.42"}},
    )
    pnl_exporter.START_VALUE_CACHE["expires_at"] = 0.0
    pnl_exporter.START_VALUE_CACHE["value"] = 0.0

    handler = object.__new__(pnl_exporter.MetricsHandler)
    value = pnl_exporter.MetricsHandler.read_portfolio_start_value(handler)

    assert value == pytest.approx(19.42)
