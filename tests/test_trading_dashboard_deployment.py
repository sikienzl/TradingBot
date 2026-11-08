import os
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_deployment_is_loopback_only_and_uses_external_secret():
    overlay = ROOT / "k8s" / "overlays" / "trading-dashboard"
    kustomization = yaml.safe_load((overlay / "kustomization.yaml").read_text())
    assert "deployment-dashboard.yaml" in kustomization["resources"]
    assert not any("service" in item.lower() for item in kustomization["resources"])
    deployment = yaml.safe_load((overlay / "deployment-dashboard.yaml").read_text())
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False
    assert container["envFrom"] == [{"secretRef": {"name": "trading-dashboard-credentials"}}]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert {item["name"]: item["value"] for item in container["env"]} == {
        "DASHBOARD_DB_SCHEMA": "trading_analytics",
    }
    assert "volumeMounts" not in container
    assert "volumes" not in pod
    dockerfile = (ROOT / "docker" / "trading-dashboard.Dockerfile").read_text()
    assert '"--host", "127.0.0.1"' in dockerfile


def test_roles_script_scopes_reader_grants_and_has_no_passwords():
    sql = (ROOT / "k8s/overlays/trading-dashboard/analytics-roles.sql").read_text().lower()
    assert "grant select on trading_analytics.trade_events, trading_analytics.portfolio_snapshots to trading_dashboard_reader" in sql
    assert "grant usage on schema trading_analytics to trading_dashboard_reader" in sql
    assert "alter role trading_dashboard_reader set default_transaction_read_only = on" in sql
    assert "password '" not in sql


def test_real_postgres_role_setup_and_writer_schema_initialization():
    """Run only against an explicitly supplied disposable superuser database."""
    dsn = os.getenv("TRADING_DASHBOARD_SETUP_TEST_DSN")
    if not dsn:
        pytest.skip("TRADING_DASHBOARD_SETUP_TEST_DSN not supplied")
    import psycopg

    suffix = uuid4().hex[:12]
    schema = f"dashboard_setup_{suffix}"
    writer = f"dashboard_writer_{suffix}"
    reader = f"dashboard_reader_{suffix}"
    schema_sql = (ROOT / "scripts/init_postgres_analytics.sql").read_text().replace("trading_analytics", schema)
    roles_sql = (ROOT / "k8s/overlays/trading-dashboard/analytics-roles.sql").read_text()
    roles_sql = roles_sql.replace("trading_analytics_writer", writer)
    roles_sql = roles_sql.replace("trading_dashboard_reader", reader)
    roles_sql = roles_sql.replace("trading_analytics", schema)
    # All DDL, role creation and inserted data are rolled back, even on failure.
    with psycopg.connect(dsn) as conn:
        try:
            conn.execute(schema_sql)
            conn.execute(roles_sql)
            conn.execute(f"SET ROLE {writer}")
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            conn.execute(f"CREATE TABLE IF NOT EXISTS {schema}.trade_events (id bigserial primary key)")
            conn.execute(f"CREATE INDEX IF NOT EXISTS trade_events_event_ts_idx ON {schema}.trade_events (event_ts DESC)")
            conn.execute(f"""INSERT INTO {schema}.portfolio_snapshots
                (snapshot_ts, iteration, portfolio_value_base, cash_base, base_currency,
                 open_positions_count, analyzed_coins_count, buy_recommendations_count,
                 dry_run, holdings_json, open_trades_json)
                VALUES (now(), 1, 100, 100, 'EUR', 0, 0, 0, true, '{{}}', '{{}}')""")
            conn.execute("RESET ROLE")
            conn.execute(f"SET ROLE {reader}")
            for table in ("portfolio_snapshots", "trade_events"):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    allowed = conn.execute(
                        "SELECT has_table_privilege(current_user, %s, %s)",
                        (f"{schema}.{table}", privilege),
                    ).fetchone()[0]
                    assert allowed is (privilege == "SELECT")
            assert conn.execute(f"SELECT count(*) FROM {schema}.portfolio_snapshots").fetchone()[0] == 1
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(f"UPDATE {schema}.portfolio_snapshots SET cash_base = 0")
        finally:
            conn.rollback()
