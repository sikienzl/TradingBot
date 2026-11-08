import os
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from src.dashboard import app as dashboard

NOW = datetime.now(UTC)
AUTH = ("operator", "secret")


def settings(**overrides):
    values = {
        "user": AUTH[0],
        "password": AUTH[1],
        "database_url": "postgresql://reader:password@localhost/analytics",
        "schema": "trading_analytics",
    }
    return dashboard.Settings(**(values | overrides))


def snapshot(**overrides):
    return {
        "snapshot_ts": NOW,
        "iteration": 5,
        "portfolio_value_base": 102.5,
        "cash_base": 42.5,
        "base_currency": "EUR",
        "open_positions_count": 1,
        "dry_run": True,
        "open_trades_json": {
            "BTC": {
                "amount_coin": 0.01,
                "buy_price": 6000,
                "amount_base": 60,
                "timestamp": "2026-01-01T10:00:00+00:00",
                "signal_source": "rules",
            }
        },
    } | overrides


def event(**overrides):
    return {
        "event_ts": NOW,
        "iteration": 5,
        "coin": "BTC",
        "action": "buy",
        "price": 6000,
        "amount_coin": 0.01,
        "amount_base": 60,
        "pnl_base": 0,
        "pnl_pct": 0,
        "signal_source": "rules",
        "reason": "entry",
        "dry_run": False,
        "base_currency": "EUR",
    } | overrides


def mock_db(monkeypatch, snapshots=None, events=None):
    conn = MagicMock()
    cursor = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cursor
    cursor.fetchall.side_effect = [
        [] if snapshots is None else snapshots,
        [] if events is None else events,
    ]
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(dashboard.psycopg, "connect", connect)
    return connect, conn, cursor


def test_missing_credentials_fails_closed_and_denies_unauthenticated(monkeypatch):
    connect, _, _ = mock_db(monkeypatch)
    client = TestClient(dashboard.create_app(settings(user="", password="")))
    assert client.get("/").status_code == 503
    assert client.get("/", auth=AUTH).status_code == 503
    client = TestClient(dashboard.create_app(settings()))
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("operator", "bad")).status_code == 401
    connect.assert_not_called()


def test_read_only_dashboard_renders_actual_schema_and_modes(monkeypatch):
    connect, conn, cursor = mock_db(
        monkeypatch, [snapshot()], [event(reason="<script>alert(1)</script>")]
    )
    client = TestClient(dashboard.create_app(settings()))
    response = client.get("/", auth=AUTH)
    assert response.status_code == 200
    assert "Modus laut letztem Snapshot: DRY-RUN" in response.text
    assert "Aktuell" in response.text
    assert all(value in response.text for value in ("BTC", "102.5", "42.5", "6000", "LIVE"))
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    queries = [call.args[0] for call in cursor.execute.call_args_list]
    assert queries[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert len(queries) == 3
    assert "trading_analytics.portfolio_snapshots" in queries[1]
    assert "trading_analytics.trade_events" in queries[2]
    assert all("LIMIT 100" in query for query in queries[1:])
    assert connect.call_args.kwargs["connect_timeout"] == 5
    assert connect.call_args.kwargs["autocommit"] is True
    conn.transaction.assert_called_once()
    conn.__exit__.assert_called_once()


def test_empty_stale_and_future_data_are_explicit(monkeypatch):
    mock_db(monkeypatch)
    client = TestClient(dashboard.create_app(settings()))
    response = client.get("/", auth=AUTH)
    assert "Modus: UNBEKANNT" in response.text
    assert "Kein Portfolio-Snapshot" in response.text
    assert "Keine Daten" in response.text

    mock_db(monkeypatch, [snapshot(snapshot_ts=NOW - timedelta(seconds=301), dry_run=False)])
    response = client.get("/", auth=AUTH)
    assert "VERALTET" in response.text
    assert "Modus laut letztem Snapshot: LIVE" in response.text

    mock_db(monkeypatch, [snapshot(snapshot_ts=NOW + timedelta(minutes=5))])
    assert "Zukunft" in client.get("/", auth=AUTH).text


def test_database_failure_is_generic_and_never_logs_credentials(monkeypatch, caplog):
    secret = "secret-in-db-exception"
    def fail(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(dashboard.psycopg, "connect", fail)
    client = TestClient(dashboard.create_app(settings()))
    response = client.get("/", auth=AUTH)
    assert response.status_code == 503
    assert "DB-Fehler" in response.text
    assert secret not in response.text
    assert secret not in caplog.text
    assert "password" not in repr(settings())


def test_database_not_configured_and_no_write_endpoints(monkeypatch):
    connect, _, _ = mock_db(monkeypatch)
    client = TestClient(dashboard.create_app(settings(database_url="")))
    assert client.get("/", auth=AUTH).status_code == 503
    assert "nicht konfiguriert" in client.get("/", auth=AUTH).text
    for path in ("/", "/orders", "/openapi.json", "/docs"):
        assert client.post(path, auth=AUTH).status_code in (404, 405)
    assert client.get("/docs", auth=AUTH).status_code == 404
    connect.assert_not_called()


@pytest.mark.parametrize("schema", ["public;drop table x", "public.foo", '"public"', "PUBLIC", ""])
def test_rejects_unsafe_schema(schema):
    with pytest.raises(ValueError):
        settings(schema=schema)


def test_invalid_snapshot_returns_safe_error_and_closes_connection(monkeypatch):
    _, conn, _ = mock_db(monkeypatch, [snapshot(open_trades_json=[])])
    response = TestClient(dashboard.create_app(settings())).get("/", auth=AUTH)
    assert response.status_code == 503
    assert "DB-Fehler" in response.text
    conn.__exit__.assert_called_once()


def chart_elements(html):
    return [ET.fromstring(svg) for svg in re.findall(r"<svg\b.*?</svg>", html, re.DOTALL)]


def test_portfolio_charts_separate_modes_and_currencies_and_scale_time(monkeypatch):
    snapshots = [
        snapshot(snapshot_ts=NOW, dry_run=True, base_currency="EUR", portfolio_value_base=130),
        snapshot(snapshot_ts=NOW - timedelta(seconds=20), dry_run=False, base_currency="EUR", portfolio_value_base=1000),
        snapshot(snapshot_ts=NOW - timedelta(seconds=30), dry_run=True, base_currency="USD", portfolio_value_base=500),
        snapshot(snapshot_ts=NOW - timedelta(seconds=40), dry_run=True, base_currency="EUR", portfolio_value_base=110),
        snapshot(snapshot_ts=NOW - timedelta(seconds=100), dry_run=True, base_currency="EUR", portfolio_value_base=100),
    ]
    mock_db(monkeypatch, snapshots)
    response = TestClient(dashboard.create_app(settings())).get("/", auth=AUTH)
    assert response.status_code == 200
    charts = chart_elements(response.text)
    assert len(charts) == 3
    by_label = {chart.attrib["aria-label"]: chart for chart in charts}
    assert set(by_label) == {
        "LIVE · EUR: Portfolioverlauf mit 1 Datenpunkten",
        "DRY-RUN · EUR: Portfolioverlauf mit 3 Datenpunkten",
        "DRY-RUN · USD: Portfolioverlauf mit 1 Datenpunkten",
    }
    chart = by_label["DRY-RUN · EUR: Portfolioverlauf mit 3 Datenpunkten"]
    labels = [point.find("title").text for point in chart.findall("circle")]
    assert [label.split(": ")[-1] for label in labels] == ["100.0 EUR", "110.0 EUR", "130.0 EUR"]
    coords = [(float(point.attrib["cx"]), float(point.attrib["cy"])) for point in chart.findall("circle")]
    assert coords[0][0] < coords[1][0] < coords[2][0]
    assert coords[1][0] == pytest.approx(397.6)
    assert coords[0][1] > coords[1][1] > coords[2][1]


def test_portfolio_chart_handles_single_flat_and_duplicate_timestamps():
    charts = chart_elements(dashboard._portfolio_charts([
        snapshot(snapshot_ts=NOW, portfolio_value_base=7),
        snapshot(snapshot_ts=NOW, portfolio_value_base=7),
        snapshot(snapshot_ts=NOW, dry_run=False, portfolio_value_base=3),
    ]))
    assert len(charts) == 2
    for chart in charts:
        circles = chart.findall("circle")
        assert circles
        assert all(circle.attrib["cx"] == "342.00" and circle.attrib["cy"] == "110.00"
                   for circle in circles)
        assert chart.find("polyline").attrib["points"]


def test_flat_values_across_time_form_a_horizontal_line():
    chart, = chart_elements(dashboard._portfolio_charts([
        snapshot(snapshot_ts=NOW, portfolio_value_base=7),
        snapshot(snapshot_ts=NOW - timedelta(hours=1), portfolio_value_base=7),
    ]))
    circles = chart.findall("circle")
    assert [point.attrib["cx"] for point in circles] == ["64.00", "620.00"]
    assert [point.attrib["cy"] for point in circles] == ["110.00", "110.00"]


def test_portfolio_chart_skips_invalid_points_and_escapes_labels(monkeypatch):
    currency = '<img src=x onerror="x">'
    data = [
        snapshot(portfolio_value_base=None),
        snapshot(portfolio_value_base=float("nan")),
        snapshot(portfolio_value_base=float("inf")),
        snapshot(snapshot_ts=None),
        snapshot(dry_run=None),
        snapshot(base_currency=""),
        snapshot(base_currency=currency, portfolio_value_base=1),
        snapshot(base_currency=currency, portfolio_value_base=1e308),
        snapshot(base_currency=currency, portfolio_value_base=-1e308),
    ]
    mock_db(monkeypatch, data)
    response = TestClient(dashboard.create_app(settings())).get("/", auth=AUTH)
    assert response.status_code == 200
    assert "6 unvollständige oder ungültige Snapshots" in response.text
    assert "<img" not in response.text
    chart, = chart_elements(response.text)
    assert currency in chart.attrib["aria-label"]
    for circle in chart.findall("circle"):
        assert 64 <= float(circle.attrib["cx"]) <= 620
        assert 20 <= float(circle.attrib["cy"]) <= 200
    assert len(chart.findall("circle")) == 3


def test_missing_portfolio_chart_data_does_not_break_dashboard(monkeypatch):
    incomplete = snapshot(snapshot_ts=None, portfolio_value_base=None, base_currency=None)
    mock_db(monkeypatch, [incomplete])
    response = TestClient(dashboard.create_app(settings())).get("/", auth=AUTH)
    assert response.status_code == 200
    assert "Aktualität unbekannt" in response.text
    assert "Keine gültigen Daten für den Portfolioverlauf" in response.text
    assert not chart_elements(response.text)
    assert "1 unvollständige oder ungültige Snapshots" in response.text


@pytest.fixture
def postgres_dashboard():
    """Opt in only with a dedicated disposable database; drop only our unique schema."""
    dsn = os.getenv("TRADING_SAFETY_TEST_DSN")
    if not dsn:
        pytest.skip("TRADING_SAFETY_TEST_DSN not supplied (dedicated disposable DB required)")
    schema = "dashboard_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        schema_sql = (Path(__file__).resolve().parents[1] / "scripts/init_postgres_analytics.sql").read_text(
            encoding="utf-8"
        ).replace("trading_analytics", schema)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(schema_sql)
            snapshots = [
                (NOW - timedelta(minutes=3), 1, 100, "EUR", True),
                (NOW - timedelta(minutes=2), 2, 110, "EUR", True),
                (NOW - timedelta(minutes=1), 3, 500, "USD", True),
                (NOW, 4, 1000, "EUR", False),
            ]
            for timestamp, iteration, value, currency, dry_run in snapshots:
                conn.execute(
                    sql.SQL("""
                        INSERT INTO {}.portfolio_snapshots
                        (snapshot_ts, iteration, portfolio_value_base, cash_base,
                         base_currency, open_positions_count, analyzed_coins_count,
                         buy_recommendations_count, dry_run, holdings_json, open_trades_json)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """).format(sql.Identifier(schema)),
                    (timestamp, iteration, value, 40, currency, 1, 1, 0, dry_run,
                     Jsonb({"BTC": 0.01}), Jsonb({"BTC": {
                         "amount_coin": 0.01, "buy_price": 6000, "amount_base": 60,
                         "timestamp": timestamp.isoformat(), "signal_source": "rules",
                     }})),
                )
            conn.execute(
                sql.SQL("""
                    INSERT INTO {}.trade_events
                    (event_ts, iteration, coin, action, price, amount_coin, amount_base,
                     pnl_base, pnl_pct, hold_seconds, signal_source, reason, dry_run, base_currency)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """).format(sql.Identifier(schema)),
                (NOW, 4, "BTC", "buy", 6000, 0.01, 60, 0, 0, 0, "rules", "entry", False, "EUR"),
            )
        yield dsn, schema
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_real_postgres_auth_charts_and_read_only_transaction(postgres_dashboard, monkeypatch):
    dsn, schema = postgres_dashboard
    connect = psycopg.connect
    observed = []
    connection_count = []

    class ObservedCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            result = super().execute(query, params, **kwargs)
            if query == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY":
                super().execute("SHOW transaction_read_only")
                read_only = next(iter(self.fetchone().values()))
                super().execute("SHOW transaction_isolation")
                isolation = next(iter(self.fetchone().values()))
                super().execute("SAVEPOINT dashboard_write_probe")
                try:
                    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                        super().execute(sql.SQL("UPDATE {}.portfolio_snapshots SET cash_base = 0").format(
                            sql.Identifier(schema)
                        ))
                finally:
                    super().execute("ROLLBACK TO SAVEPOINT dashboard_write_probe")
                    super().execute("RELEASE SAVEPOINT dashboard_write_probe")
                observed.append((read_only, isolation))
            return result

    def observed_connect(*args, **kwargs):
        connection_count.append(1)
        return connect(*args, cursor_factory=ObservedCursor, **kwargs)

    monkeypatch.setattr(dashboard.psycopg, "connect", observed_connect)
    client = TestClient(dashboard.create_app(settings(database_url=dsn, schema=schema)))
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("operator", "wrong")).status_code == 401
    assert not connection_count
    response = client.get("/", auth=AUTH)
    assert response.status_code == 200
    assert observed == [("on", "repeatable read")]
    assert "Modus laut letztem Snapshot: LIVE" in response.text
    assert "BTC" in response.text and "entry" in response.text
    charts = chart_elements(response.text)
    by_label = {chart.attrib["aria-label"]: chart for chart in charts}
    assert set(by_label) == {
        "LIVE · EUR: Portfolioverlauf mit 1 Datenpunkten",
        "DRY-RUN · EUR: Portfolioverlauf mit 2 Datenpunkten",
        "DRY-RUN · USD: Portfolioverlauf mit 1 Datenpunkten",
    }
    dry_eur = by_label["DRY-RUN · EUR: Portfolioverlauf mit 2 Datenpunkten"]
    assert [point.find("title").text.split(": ")[-1] for point in dry_eur.findall("circle")] == [
        "100.0 EUR", "110.0 EUR",
    ]
    with connect(dsn) as conn:
        count, total = conn.execute(sql.SQL(
            "SELECT count(*), sum(cash_base) FROM {}.portfolio_snapshots"
        ).format(sql.Identifier(schema))).fetchone()
    assert (count, total) == (4, 160)


def test_real_postgres_select_only_role_cannot_write(postgres_dashboard, monkeypatch):
    dsn, schema = postgres_dashboard
    connect = psycopg.connect
    with connect(dsn, autocommit=True) as conn:
        if not conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pg_read_all_data')"
        ).fetchone()[0]:
            pytest.skip("PostgreSQL role pg_read_all_data is unavailable")
        if not conn.execute("SELECT pg_has_role(current_user, 'pg_read_all_data', 'MEMBER')").fetchone()[0]:
            pytest.skip("Test DB user cannot SET ROLE pg_read_all_data")
        conn.execute("SET ROLE pg_read_all_data")
        for table in ("portfolio_snapshots", "trade_events"):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                allowed = conn.execute(
                    "SELECT has_table_privilege(current_user, %s, %s)",
                    (f"{schema}.{table}", privilege),
                ).fetchone()[0]
                assert allowed is (privilege == "SELECT")
        assert conn.execute(sql.SQL(
            "SELECT count(*) FROM {}.portfolio_snapshots"
        ).format(sql.Identifier(schema))).fetchone()[0] == 4
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(sql.SQL("UPDATE {}.portfolio_snapshots SET cash_base = 0").format(
                sql.Identifier(schema)
            ))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(sql.SQL("DELETE FROM {}.trade_events").format(sql.Identifier(schema)))

    def reader_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        try:
            conn.execute("SET ROLE pg_read_all_data")
        except BaseException:
            conn.close()
            raise
        return conn

    monkeypatch.setattr(dashboard.psycopg, "connect", reader_connect)
    response = TestClient(dashboard.create_app(settings(database_url=dsn, schema=schema))).get("/", auth=AUTH)
    assert response.status_code == 200
    assert len(chart_elements(response.text)) == 3
