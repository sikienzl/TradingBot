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
        "timestamp",
        "coin",
        "action",
        "amount_base",
        "net_pnl_base",
        "fee_base",
        "slippage_base",
        "execution_cost_mode",
        "entry_cost_mode",
    ]
    with journal.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(
            [
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
            ]
        )
    (data_dir / ".portfolio_state.json").write_text(
        json.dumps(
            {
                "cash": 19.5,
                "holdings": {},
                "open_trades": {},
                "base_currency": "EUR",
                "initial_portfolio_value": 20,
                "simulation_iteration": 8,
                "timestamp": now.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "latest.json").write_text('{"primary": {}}', encoding="utf-8")
    (data_dir / "trading_bot.log").write_text(
        f"🕒 Iteration 7 - {(now - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"🕒 Iteration 8 - {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
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
    monkeypatch.setenv("DASHBOARD_MARKET_DATA_DIR", str(tmp_path / "market-data"))
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
    assert "timeLocal:'Zeit (lokal)'" in page.text
    assert "timeLocal:'Time (local)'" in page.text
    assert "new Intl.DateTimeFormat" in page.text
    assert "localDateTime(x.timestamp)" in page.text
    assert "timeUtc:" not in page.text
    assert "lastCycle:'Letzter Bot-Zyklus'" in page.text
    assert "lastCycle:'Last bot cycle'" in page.text
    assert "d.last_bot_cycle.iteration" in page.text
    assert 'id="market-chart"' in page.text
    assert "api/market-observations" in page.text
    assert "marketNotice" in page.text
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
    assert body["recent_events"][0]["timestamp"].endswith("+00:00")
    assert body["last_bot_cycle"]["iteration"] == 8
    assert body["last_bot_cycle"]["timestamp"].endswith("+00:00")


def test_market_observations_api_is_authenticated_and_reports_missing_files(client):
    response = client.get("/api/market-observations")
    assert response.status_code == 401

    response = client.get(
        "/api/market-observations",
        auth=("viewer", "long-test-password"),
    )
    assert response.status_code == 200
    assert response.json()["available"] is False
    assert "not mounted" in response.json()["reason"]


def test_market_csv_tail_is_bounded_and_validates_original_header(
    tmp_path, monkeypatch
):
    path = tmp_path / "tickers.csv"
    fields = dashboard.MARKET_TICKER_FIELDS
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for index in range(10):
            writer.writerow(
                {
                    "observed_at_utc": f"2026-01-01T00:00:{index:02}+00:00",
                    "symbol": "ADA/EUR",
                    "bid": 1,
                    "ask": 2,
                    "last": 1,
                }
            )
    monkeypatch.setattr(dashboard, "MARKET_DATA_READ_LIMIT", 150)
    row_iterator, truncated = dashboard._read_csv_tail(path, fields)
    rows = list(row_iterator)
    assert truncated
    assert rows[-1]["observed_at_utc"] == "2026-01-01T00:00:09+00:00"
    assert len(rows) < 10
    original = path.read_text(encoding="utf-8")
    path.write_text("invalid,header\n" + original.split("\n", 1)[1], encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected CSV header"):
        dashboard._read_csv_tail(path, fields)


def test_market_observations_api_summarizes_public_ticker_and_candles(client):
    market_dir = Path(os.environ["DASHBOARD_MARKET_DATA_DIR"])
    market_dir.mkdir()
    (market_dir / "metadata.json").write_text(
        json.dumps(
            {
                "exchange": "kraken",
                "symbols": ["ADA/EUR", "ETH/EUR"],
                "ticker_interval_seconds": 10,
                "candle_timeframe": "1m",
                "candle_backfill_limit": 720,
                "created_at_utc": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    now = datetime.now(UTC)
    ticker_rows = [
        {
            "observed_at_utc": (now - timedelta(minutes=10)).isoformat(),
            "exchange_timestamp_utc": "",
            "symbol": "ADA/EUR",
            "bid": "99.9",
            "ask": "100.1",
            "last": "100",
            "base_volume": "",
            "quote_volume": "",
        },
        {
            "observed_at_utc": now.isoformat(),
            "exchange_timestamp_utc": "",
            "symbol": "ADA/EUR",
            "bid": "100.9",
            "ask": "101.1",
            "last": "101",
            "base_volume": "",
            "quote_volume": "",
        },
    ]
    with (market_dir / "tickers.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=dashboard.MARKET_TICKER_FIELDS)
        writer.writeheader()
        writer.writerows(ticker_rows)
    candle_end = now.replace(second=0, microsecond=0)
    candle_rows = [
        {
            "timestamp_utc": (candle_end - timedelta(minutes=1)).isoformat(),
            "symbol": "ADA/EUR",
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "5",
        },
        {
            "timestamp_utc": candle_end.isoformat(),
            "symbol": "ADA/EUR",
            "open": "100",
            "high": "102",
            "low": "100",
            "close": "101",
            "volume": "6",
        },
    ]
    with (market_dir / "candles_1m.csv").open(
        "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(file, fieldnames=dashboard.MARKET_CANDLE_FIELDS)
        writer.writeheader()
        writer.writerows(candle_rows)

    response = client.get(
        "/api/market-observations?symbol=ADA%2FEUR",
        auth=("viewer", "long-test-password"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["selected_symbol"] == "ADA/EUR"
    assert body["freshness_status"] == "fresh"
    market = body["markets"][0]
    assert market["latest_last"] == 101
    assert market["latest_spread_bps"] == pytest.approx(19.8, abs=0.1)
    assert market["price_change_pct_observed"] == 1
    assert market["ticker_samples_24h"] == 2
    assert market["candle_count_24h"] == 2
    assert market["candle_coverage_pct"] == 100
    assert market["candle_gap_minutes"] == 0
    assert len(body["ticker_series"]) == 2
    assert len(body["candle_series"]) == 2
    assert (
        client.get(
            "/api/market-observations?symbol=../../etc/passwd",
            auth=("viewer", "long-test-password"),
        ).status_code
        == 404
    )


def test_dashboard_reports_no_last_bot_cycle_when_log_has_no_iteration(client):
    log_path = Path(os.environ["DASHBOARD_LOG_FILE"])
    log_path.write_text("No iteration has been logged yet\n", encoding="utf-8")
    dashboard._snapshot_cache = None

    response = client.get("/api/dashboard", auth=("viewer", "long-test-password"))

    assert response.status_code == 200
    assert response.json()["last_bot_cycle"] is None


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
