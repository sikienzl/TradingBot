import csv
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import public_paper_dashboard as dashboard


@pytest.fixture
def client(monkeypatch, tmp_path):
    now = datetime.now(UTC)
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    data_dir.mkdir()
    reports_dir.mkdir()
    journal = data_dir / "trade_journal.csv"
    fields = [
        "timestamp", "coin", "action", "amount_base", "net_pnl_base",
        "fee_base", "slippage_base", "execution_cost_mode", "entry_cost_mode",
    ]
    with journal.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows([
            {
                "timestamp": (now - timedelta(hours=2)).isoformat(),
                "coin": "BTC",
                "action": "buy",
                "amount_base": "10",
                "fee_base": "0.1",
                "slippage_base": "0.02",
                "execution_cost_mode": "net_costs",
                "entry_cost_mode": "net_costs",
            },
            {
                "timestamp": (now - timedelta(hours=1)).isoformat(),
                "coin": "BTC",
                "action": "sell",
                "amount_base": "10",
                "net_pnl_base": "-0.5",
                "fee_base": "0.1",
                "slippage_base": "0.02",
                "execution_cost_mode": "net_costs",
                "entry_cost_mode": "net_costs",
            },
        ])
    (data_dir / ".portfolio_state.json").write_text(json.dumps({
        "cash": 19.5,
        "holdings": {},
        "open_trades": {},
        "base_currency": "EUR",
        "initial_portfolio_value": 20,
        "simulation_iteration": 8,
        "timestamp": now.isoformat(),
    }), encoding="utf-8")
    (reports_dir / "latest.json").write_text('{"primary": {}}', encoding="utf-8")
    (data_dir / "trading_bot.log").write_text(
        f"{now.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - INFO - "
        "⛔ Entry withheld: ATR(14)/price volatility proxy is unavailable\n"
        f"{now.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - INFO - "
        "⛔ Confident tabular SELL vetoed new entry for BTC\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DASHBOARD_ALLOWED_NETWORKS", "10.1.0.0/24,10.2.0.0/24")
    monkeypatch.setenv("DASHBOARD_USERNAME", "viewer")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "long-test-password")
    monkeypatch.setenv("DASHBOARD_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DASHBOARD_REPORT_FILE", str(reports_dir / "latest.json"))
    monkeypatch.setenv("DASHBOARD_LOG_FILE", str(data_dir / "trading_bot.log"))
    dashboard._snapshot_cache = None
    with TestClient(
        dashboard.app,
        base_url="https://testserver",
        client=("10.1.0.10", 12345),
    ) as test_client:
        yield test_client


def test_dashboard_requires_https_and_authentication(client):
    response = client.get("/", headers={"host": "dashboard"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"
    assert response.headers["cache-control"] == "no-store"
    page = client.get("/", auth=("viewer", "long-test-password"))
    assert '<time id="local-clock"' in page.text
    assert "setInterval(updateClock,1000)" in page.text
    assert "hour:'2-digit',minute:'2-digit',second:'2-digit'" in page.text
    assert 'id="clock-hour"' not in page.text
    assert 'id="language-de"' in page.text
    assert 'id="language-en"' in page.text
    assert "Next update in" in page.text
    assert "Aktualisierung in" in page.text
    assert "paper-dashboard-language" in page.text
    assert "setInterval(updateCountdown,1000)" in page.text
    assert "setTimeout(refresh,REFRESH_INTERVAL_MS)" in page.text

    response = client.get("/", auth=("viewer", "wrong-password"))
    assert response.status_code == 401

    with TestClient(
        dashboard.app,
        base_url="http://testserver",
        client=("10.1.0.10", 12345),
    ) as insecure_client:
        response = insecure_client.get("/")
        assert response.status_code == 426


def test_dashboard_rejects_clients_outside_configured_networks(client):
    with TestClient(
        dashboard.app,
        base_url="https://testserver",
        client=("10.3.0.10", 12345),
    ) as outside_client:
        response = outside_client.get(
            "/api/dashboard",
            auth=("viewer", "long-test-password"),
        )
        assert response.status_code == 403


def test_dashboard_data_is_read_only_and_reports_paper_metrics(client):
    response = client.get("/api/dashboard", auth=("viewer", "long-test-password"))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["portfolio"]["cash"] == 19.5
    assert body["portfolio"]["initial_value"] == 20
    assert body["activity"]["today_buys"] == 1
    assert body["activity"]["today_sell_events"] == 1
    assert body["activity"]["today_net_pnl"] == -0.5
    assert body["activity"]["today_fees"] == 0.2
    assert body["activity"]["today_slippage"] == 0.04
    assert body["filters"]["atr_guard"] == 1
    assert body["filters"]["tabular_sell_veto"] == 1
    assert body["daily_report"] == {"primary": {}}
    assert len(body["daily_net_pnl"]) == 14
    assert body["recent_events"][0]["action"] == "sell"


def test_dashboard_does_not_claim_net_pnl_for_legacy_cost_data(client, monkeypatch):
    data_dir = Path(os.environ["DASHBOARD_DATA_DIR"])
    journal = data_dir / "trade_journal.csv"
    with journal.open("a", encoding="utf-8") as file:
        file.write(
            f"{datetime.now(UTC).isoformat()},ETH,sell,5,1,0.1,0.1,gross_legacy,net_costs\n"
        )

    response = client.get("/api/dashboard", auth=("viewer", "long-test-password"))
    assert response.status_code == 200
    assert response.json()["activity"]["today_net_pnl"] is None


@pytest.mark.parametrize("network", ["", "not-a-cidr", "8.8.8.0/24", "0.0.0.0/0"])
def test_private_networks_are_required_for_dashboard(client, monkeypatch, network):
    monkeypatch.setenv("DASHBOARD_ALLOWED_NETWORKS", network)
    with pytest.raises(dashboard.DashboardConfigurationError):
        dashboard._allowed_networks()
