import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg

from src.trading_bot import PostgresAnalyticsWriter


def _writer():
    config = SimpleNamespace(
        analytics_db_enabled=True,
        analytics_db_schema="trading_analytics",
        analytics_db_connect_kwargs=lambda: {"host": "localhost"},
    )
    writer = PostgresAnalyticsWriter(config)
    writer._psycopg = SimpleNamespace(Error=psycopg.Error, connect=MagicMock())
    return writer


def test_connection_error_is_reported_without_crashing(caplog):
    writer = _writer()
    writer._psycopg.connect.side_effect = psycopg.OperationalError("offline")
    with caplog.at_level(logging.WARNING):
        assert not writer._ensure_connection()
    assert "Failed to connect to analytics Postgres DB" in caplog.text
    assert writer._conn is None


def test_snapshot_database_failure_is_reported_and_connection_closed(caplog):
    writer = _writer()
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value.__enter__.return_value.execute.side_effect = (
        psycopg.OperationalError("offline")
    )
    writer._conn = conn
    with caplog.at_level(logging.WARNING):
        writer.write_snapshot(
            iteration=1, portfolio_value=5, cash_value=5, base_currency="EUR",
            dry_run=True, holdings={}, open_trades={}, analyzed_coins_count=0,
            buy_recommendations_count=0,
        )
    assert "Failed to write portfolio snapshot" in caplog.text
    conn.close.assert_called_once()
    assert writer._conn is None


def test_trade_database_failure_is_reported_and_connection_closed(caplog):
    writer = _writer()
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value.__enter__.return_value.execute.side_effect = (
        psycopg.OperationalError("offline")
    )
    writer._conn = conn
    with caplog.at_level(logging.WARNING):
        writer.write_trade({}, "EUR")
    assert "Failed to write trade event" in caplog.text
    conn.close.assert_called_once()
    assert writer._conn is None


def test_close_database_failure_is_reported(caplog):
    writer = _writer()
    conn = MagicMock()
    conn.close.side_effect = psycopg.OperationalError("close failed")
    writer._conn = conn
    with caplog.at_level(logging.WARNING):
        writer.close()
    assert "Failed to close analytics Postgres connection" in caplog.text
    assert writer._conn is None


def test_recovery_resets_connection_failure_warning(caplog):
    writer = _writer()
    writer._psycopg.connect.side_effect = [
        psycopg.OperationalError("offline"), MagicMock(), psycopg.OperationalError("offline")
    ]
    writer._ensure_schema = MagicMock()
    with caplog.at_level(logging.WARNING):
        assert not writer._ensure_connection()
        assert writer._ensure_connection()
        writer.close()
        assert not writer._ensure_connection()
    assert caplog.text.count("Failed to connect to analytics Postgres DB") == 2
