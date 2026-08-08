"""
Postgres Analytics Reporter.

Reads trade and portfolio data from the analytics database and produces
summary reports that can be consumed by Grafana or printed to the console.

Schema reference: docs/postgres_analytics_schema.md

Environment variables:
  ANALYTICS_DATABASE_URL  – SQLAlchemy-compatible Postgres DSN
                            (e.g. postgresql+psycopg2://user:pass@host:5432/dbname)
  ANALYTICS_DB_HOST       – fallback individual parts when no full DSN given
  ANALYTICS_DB_PORT
  ANALYTICS_DB_USER
  ANALYTICS_DB_PASSWORD
  ANALYTICS_DB_NAME
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def _get_dsn() -> str:
    """Build Postgres DSN from environment."""
    dsn = os.getenv("ANALYTICS_DATABASE_URL")
    if dsn:
        return dsn
    host = os.getenv("ANALYTICS_DB_HOST", "localhost")
    port = os.getenv("ANALYTICS_DB_PORT", "5432")
    user = os.getenv("ANALYTICS_DB_USER", "trading")
    password = os.getenv("ANALYTICS_DB_PASSWORD", "")
    dbname = os.getenv("ANALYTICS_DB_NAME", "trading_analytics")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}"


@contextmanager
def _connection() -> Generator[Any, None, None]:
    """Return a psycopg2 connection as a context manager."""
    try:
        import psycopg2  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError("psycopg2 not installed — run: pip install psycopg2-binary") from exc

    dsn = _get_dsn()
    # Strip SQLAlchemy prefix if present
    dsn_pg = dsn.replace("postgresql+psycopg2://", "postgresql://")
    conn = psycopg2.connect(dsn_pg)
    try:
        yield conn
    finally:
        conn.close()


def get_trade_summary(days: int = 7) -> list[dict[str, Any]]:
    """
    Return per-coin P&L summary for the last *days* days.

    Columns: coin, trades, wins, losses, win_rate, total_pnl, avg_pnl
    """
    sql = """
        SELECT
            symbol                              AS coin,
            COUNT(*)                            AS trades,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END)  AS wins,
            SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END) AS losses,
            ROUND(
                100.0 * SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) / COUNT(*),
                1
            )                                   AS win_rate,
            ROUND(SUM(realized_pnl)::numeric, 4) AS total_pnl,
            ROUND(AVG(realized_pnl)::numeric, 4) AS avg_pnl
        FROM trades
        WHERE closed_at >= NOW() - INTERVAL '%s days'
          AND state = 'closed'
        GROUP BY symbol
        ORDER BY total_pnl DESC
    """
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (days,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_portfolio_snapshot() -> dict[str, Any]:
    """
    Return the latest portfolio snapshot (cash + open positions).

    Uses the *portfolio_snapshots* table (see schema).
    """
    sql = """
        SELECT *
        FROM portfolio_snapshots
        ORDER BY snapshot_at DESC
        LIMIT 1
    """
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
        if row is None:
            return {}
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def get_drawdown_history(days: int = 30) -> list[dict[str, Any]]:
    """
    Return daily max-drawdown series for the last *days* days.

    Columns: date, max_drawdown_pct
    """
    sql = """
        SELECT
            DATE(snapshot_at)             AS date,
            ROUND(MIN(drawdown_pct)::numeric, 4) AS max_drawdown_pct
        FROM portfolio_snapshots
        WHERE snapshot_at >= NOW() - INTERVAL '%s days'
        GROUP BY DATE(snapshot_at)
        ORDER BY date
    """
    with _connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (days,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def print_summary_report(days: int = 7) -> None:
    """Pretty-print a trade-summary report to stdout."""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n=== Trading Analytics Report ({days}d) — {now} ===\n")

    try:
        rows = get_trade_summary(days)
    except (RuntimeError, OSError) as exc:
        print(f"[ERROR] Could not fetch trade summary: {exc}")
        return

    if not rows:
        print("No closed trades in the period.")
        return

    header = f"{'Coin':<12} {'Trades':>6} {'Wins':>5} {'Loss':>5} {'WR%':>6} {'PnL':>10} {'AvgPnL':>10}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['coin']:<12} {r['trades']:>6} {r['wins']:>5} {r['losses']:>5} "
            f"{r['win_rate']:>5.1f}% {r['total_pnl']:>10.4f} {r['avg_pnl']:>10.4f}"
        )

    total_pnl = sum(float(r["total_pnl"]) for r in rows)
    print("-" * len(header))
    print(f"{'TOTAL':<12} {'':>6} {'':>5} {'':>5} {'':>6} {total_pnl:>10.4f}\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Postgres Analytics Reporter")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
    args = parser.parse_args()
    print_summary_report(days=args.days)
