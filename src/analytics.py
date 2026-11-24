"""Optional Postgres analytics sink for trades and portfolio snapshots.

Extracted from ``src.trading_bot`` so the analytics pipeline can be tested and
reused without importing the full trading bot.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from src.bot_config import BotConfig

logger = logging.getLogger(__name__)

_SAFE_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


class PostgresAnalyticsWriter:
    """Optional Postgres sink for trade and portfolio analytics."""

    def __init__(self, config: BotConfig):
        self.config = config
        self.enabled = config.analytics_db_enabled
        self._psycopg: Any = None
        self._conn: Any = None
        self._disabled_reason_logged = False
        self._schema = self._sanitize_identifier(
            config.analytics_db_schema, 'public')

    def _sanitize_identifier(self, value: str, fallback: str) -> str:
        candidate = (value or '').strip()
        if _SAFE_IDENTIFIER_RE.match(candidate):
            return candidate
        return fallback

    def _qualified_table(self, table_name: str) -> str:
        return f'{self._schema}.{table_name}'

    def _ensure_connection(self) -> bool:
        if not self.enabled:
            return False
        if self._conn is not None and not getattr(self._conn, 'closed', False):
            return True
        try:
            if self._psycopg is None:
                self._psycopg = importlib.import_module('psycopg')
        except ImportError as exc:
            if not self._disabled_reason_logged:
                logger.warning(
                    'Analytics DB enabled but psycopg is unavailable: %s', exc)
                self._disabled_reason_logged = True
            return False

        kwargs = self.config.analytics_db_connect_kwargs()
        if 'conninfo' not in kwargs and not kwargs.get('host'):
            if not self._disabled_reason_logged:
                logger.warning(
                    'Analytics DB enabled but neither ANALYTICS_DB_URL nor ANALYTICS_DB_HOST is configured.')
                self._disabled_reason_logged = True
            return False

        try:
            self._conn = self._psycopg.connect(**kwargs)
            self._conn.autocommit = True
            self._ensure_schema()
            return True
        except (self._psycopg.Error, ValueError) as exc:
            if not self._disabled_reason_logged:
                logger.warning(
                    'Failed to connect to analytics Postgres DB: %s', exc)
                self._disabled_reason_logged = True
            self.close()
            return False

    def _ensure_schema(self) -> None:
        if self._conn is None:
            return
        trades_table = self._qualified_table('trade_events')
        snapshots_table = self._qualified_table('portfolio_snapshots')
        with self._conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS {self._schema}')
            cur.execute(
                f'''
                CREATE TABLE IF NOT EXISTS {trades_table} (
                    id BIGSERIAL PRIMARY KEY,
                    event_ts TIMESTAMPTZ NOT NULL,
                    iteration INTEGER NOT NULL,
                    coin TEXT NOT NULL,
                    action TEXT NOT NULL,
                    price DOUBLE PRECISION NOT NULL,
                    amount_coin DOUBLE PRECISION NOT NULL,
                    amount_base DOUBLE PRECISION NOT NULL,
                    pnl_base DOUBLE PRECISION NOT NULL,
                    pnl_pct DOUBLE PRECISION NOT NULL,
                    hold_seconds DOUBLE PRECISION NOT NULL,
                    signal_source TEXT,
                    signal_confidence DOUBLE PRECISION,
                    recommendation TEXT,
                    reason TEXT,
                    dry_run BOOLEAN NOT NULL,
                    base_currency TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT 'legacy',
                    config_hash TEXT NOT NULL DEFAULT 'legacy',
                    mfe_pct DOUBLE PRECISION,
                    mae_pct DOUBLE PRECISION,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                '''
            )
            cur.execute(
                f'ALTER TABLE {trades_table} ADD COLUMN IF NOT EXISTS '
                "run_id TEXT NOT NULL DEFAULT 'legacy'"
            )
            cur.execute(
                f'ALTER TABLE {trades_table} ADD COLUMN IF NOT EXISTS '
                "config_hash TEXT NOT NULL DEFAULT 'legacy'"
            )
            cur.execute(
                f'ALTER TABLE {trades_table} ADD COLUMN IF NOT EXISTS mfe_pct DOUBLE PRECISION'
            )
            cur.execute(
                f'ALTER TABLE {trades_table} ADD COLUMN IF NOT EXISTS mae_pct DOUBLE PRECISION'
            )
            cur.execute(
                f'''
                CREATE INDEX IF NOT EXISTS trade_events_event_ts_idx
                ON {trades_table} (event_ts DESC)
                '''
            )
            cur.execute(
                f'''
                CREATE INDEX IF NOT EXISTS trade_events_coin_action_idx
                ON {trades_table} (coin, action)
                '''
            )
            cur.execute(
                f'''
                CREATE TABLE IF NOT EXISTS {snapshots_table} (
                    id BIGSERIAL PRIMARY KEY,
                    snapshot_ts TIMESTAMPTZ NOT NULL,
                    iteration INTEGER NOT NULL,
                    portfolio_value_base DOUBLE PRECISION NOT NULL,
                    cash_base DOUBLE PRECISION NOT NULL,
                    base_currency TEXT NOT NULL,
                    open_positions_count INTEGER NOT NULL,
                    analyzed_coins_count INTEGER NOT NULL,
                    buy_recommendations_count INTEGER NOT NULL,
                    dry_run BOOLEAN NOT NULL,
                    holdings_json JSONB NOT NULL,
                    open_trades_json JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                '''
            )
            cur.execute(
                f'''
                CREATE INDEX IF NOT EXISTS portfolio_snapshots_snapshot_ts_idx
                ON {snapshots_table} (snapshot_ts DESC)
                '''
            )

    def write_trade(self, row: dict[str, Any], base_currency: str) -> None:
        if not self._ensure_connection():
            return
        trades_table = self._qualified_table('trade_events')
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f'''
                    INSERT INTO {trades_table} (
                        event_ts, iteration, coin, action, price, amount_coin,
                        amount_base, pnl_base, pnl_pct, hold_seconds,
                        signal_source, signal_confidence, recommendation, reason,
                        dry_run, base_currency, run_id, config_hash, mfe_pct, mae_pct
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    ''',
                    (
                        row.get('timestamp', datetime.now(
                            UTC).isoformat()),
                        int(row.get('iteration', 0)),
                        row.get('coin', ''),
                        row.get('action', ''),
                        float(row.get('price', 0.0)),
                        float(row.get('amount_coin', 0.0)),
                        float(row.get('amount_base', 0.0)),
                        float(row.get('pnl_base', 0.0)),
                        float(row.get('pnl_pct', 0.0)),
                        float(row.get('hold_seconds', 0.0)),
                        row.get('signal_source', ''),
                        None if row.get('signal_confidence', '') in (
                            '', None) else float(row.get('signal_confidence', 0.0)),
                        row.get('recommendation', ''),
                        row.get('reason', ''),
                        bool(row.get('dry_run', False)),
                        base_currency,
                        row.get('run_id', 'legacy'),
                        row.get('config_hash', 'legacy'),
                        None if row.get('mfe_pct', '') in ('', None)
                        else float(row.get('mfe_pct')),
                        None if row.get('mae_pct', '') in ('', None)
                        else float(row.get('mae_pct')),
                    ),
                )
        except self._psycopg.Error as exc:
            logger.warning(
                'Failed to write trade event to analytics DB: %s', exc)
            self.close()

    def write_snapshot(
        self,
        *,
        iteration: int,
        portfolio_value: float,
        cash_value: float,
        base_currency: str,
        dry_run: bool,
        holdings: dict[str, float],
        open_trades: dict[str, dict[str, Any]],
        analyzed_coins_count: int,
        buy_recommendations_count: int,
    ) -> None:
        if not self._ensure_connection():
            return
        snapshots_table = self._qualified_table('portfolio_snapshots')
        serialized_open_trades = json.dumps(
            open_trades,
            default=lambda value: value.isoformat() if isinstance(
                value, datetime) else str(value),
            ensure_ascii=True,
        )
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f'''
                    INSERT INTO {snapshots_table} (
                        snapshot_ts, iteration, portfolio_value_base, cash_base,
                        base_currency, open_positions_count, analyzed_coins_count,
                        buy_recommendations_count, dry_run, holdings_json, open_trades_json
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s::jsonb, %s::jsonb
                    )
                    ''',
                    (
                        datetime.now(UTC),
                        iteration,
                        float(portfolio_value),
                        float(cash_value),
                        base_currency,
                        len(open_trades),
                        analyzed_coins_count,
                        buy_recommendations_count,
                        bool(dry_run),
                        json.dumps(holdings, ensure_ascii=True),
                        serialized_open_trades,
                    ),
                )
        except self._psycopg.Error as exc:
            logger.warning(
                'Failed to write portfolio snapshot to analytics DB: %s', exc)
            self.close()

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            conn.close()
        except self._psycopg.Error:
            logger.warning('Failed to close analytics Postgres connection')
