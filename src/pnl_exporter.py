#!/usr/bin/env python3
"""
Simple Prometheus exporter for Trading Bot PnL metrics
Listens on http://localhost:9200/metrics
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from .export_pnl_metrics import (
    _read_env_value as _shared_read_env_value,
)
from .export_pnl_metrics import (
    _scan_portfolio_start_value_from_log as _shared_scan_portfolio_start_value,
)
from .export_pnl_metrics import (
    calculate_pnl_metrics as _shared_calculate_pnl_metrics,
)
from .export_pnl_metrics import (
    compute_recent_pnl_guard_state as _shared_compute_recent_pnl_guard_state,
)
from .export_pnl_metrics import (
    format_prometheus_metrics as _shared_format_prometheus_metrics,
)
from .export_pnl_metrics import (
    read_ai_copilot_usage as _shared_read_ai_copilot_usage,
)
from .export_pnl_metrics import (
    read_ai_shadow_suggestions as _shared_read_ai_shadow_suggestions,
)
from .export_pnl_metrics import (
    read_latest_portfolio_snapshot as _shared_read_latest_portfolio_snapshot,
)
from .export_pnl_metrics import (
    read_trades as _shared_read_trades,
)

JOURNAL_PATH = '/opt/trading_2/trade_journal.csv'
BOT_LOG_PATH = '/opt/trading_2/logs/bot.log'
ENV_PATH = '/opt/trading_2/.env'
AI_COPILOT_STATE_PATH = '/opt/trading_2/ai_copilot_state.json'
PORTFOLIO_STATE_PATH = '/opt/trading_2/.portfolio_state.json'
START_VALUE_CACHE = {
    'log_mtime': None,
    'value': 0.0,
}
AI_SPEND_CACHE = {
    'value': None,
    'expires_at': 0.0,
}
AI_SPEND_CACHE_TTL_SECONDS = 300
AI_SPEND_API_TIMEOUT_SECONDS = 2


class MetricsHandler(BaseHTTPRequestHandler):



    def _compute_recent_pnl_guard_state(self, trades):
        return _shared_compute_recent_pnl_guard_state(trades)

    def _read_env_value(self, env_key):
        return _shared_read_env_value(env_key, ENV_PATH, quiet=True)

    def _read_ai_spend_from_api(self):
        now = time.time()
        if AI_SPEND_CACHE['expires_at'] > now:
            return AI_SPEND_CACHE['value']

        api_key = self._read_env_value('MAMMOUTH_API_KEY')
        api_url = self._read_env_value(
            'AI_COPILOT_API_URL') or 'https://api.mammouth.ai/v1/chat/completions'
        if not api_key:
            AI_SPEND_CACHE['value'] = None
            AI_SPEND_CACHE['expires_at'] = now + AI_SPEND_CACHE_TTL_SECONDS
            return None

        api_root = api_url.split('/v1/', 1)[0].rstrip('/')
        req = urllib.request.Request(
            f'{api_root}/key/info',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Accept': 'application/json',
                'User-Agent': 'trading-bot-pnl-exporter/1.0',
            },
            method='GET',
        )

        try:
            with urllib.request.urlopen(req, timeout=AI_SPEND_API_TIMEOUT_SECONDS) as resp:
                payload = json.loads(
                    resp.read().decode('utf-8', errors='replace'))
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
            AI_SPEND_CACHE['expires_at'] = now + AI_SPEND_CACHE_TTL_SECONDS
            return AI_SPEND_CACHE['value']

        info = payload.get('info', {}) if isinstance(payload, dict) else {}
        spend = info.get('spend')
        max_budget = info.get('max_budget')
        result = {}
        if spend is not None:
            result['ai_copilot_budget_used_usd'] = float(spend)
        if max_budget is not None:
            result['ai_copilot_budget_cap_usd'] = float(max_budget)
        AI_SPEND_CACHE['value'] = result or None
        AI_SPEND_CACHE['expires_at'] = now + AI_SPEND_CACHE_TTL_SECONDS
        return AI_SPEND_CACHE['value']

    def do_GET(self):
        if self.path == '/metrics':
            metrics = self.get_metrics()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4')
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(metrics.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def get_metrics(self):
        trades = self.read_trades()
        metrics_dict = self.calculate_pnl_metrics(trades)
        snapshot = self.read_latest_portfolio_snapshot()
        metrics_dict['portfolio_value_eur'] = snapshot['portfolio_value_eur']
        metrics_dict['portfolio_cash_eur'] = snapshot['portfolio_cash_eur']
        metrics_dict['holdings_value_eur'] = snapshot['holdings_value_eur']
        metrics_dict['holdings_amount_coin'] = snapshot['holdings_amount_coin']
        metrics_dict['holdings_cost_basis_eur'] = snapshot['holdings_cost_basis_eur']
        metrics_dict['holdings_unrealized_pnl_eur'] = snapshot['holdings_unrealized_pnl_eur']
        metrics_dict['open_positions_count'] = snapshot['open_positions_count']
        metrics_dict['portfolio_snapshot_timestamp_unixtime'] = snapshot['portfolio_snapshot_timestamp_unixtime']
        metrics_dict['portfolio_snapshot_age_seconds'] = snapshot['portfolio_snapshot_age_seconds']
        metrics_dict['metrics_generated_unixtime'] = snapshot['metrics_generated_unixtime']
        metrics_dict['portfolio_start_value_eur'] = self.read_portfolio_start_value()
        metrics_dict.update(self.read_ai_copilot_usage())
        metrics_dict.update(self.read_ai_shadow_suggestions())
        metrics_dict.update(self._compute_recent_pnl_guard_state(trades))
        return self.format_prometheus_metrics(metrics_dict)

    def read_trades(self):
        """Read all trades from journal"""
        return _shared_read_trades(JOURNAL_PATH, quiet=True)


    def _extract_log_line_timestamp(self, line):
        """Extract a datetime from standard bot log lines like 'YYYY-MM-DD HH:MM:SS,mmm - ...'."""
        try:
            timestamp_raw = line.split(' - ', 1)[0].strip()
            return datetime.strptime(timestamp_raw, '%Y-%m-%d %H:%M:%S,%f')
        except ValueError:
            return None


    def read_latest_portfolio_snapshot(self):
        """Read latest portfolio value and cash from bot log."""
        generated_at = datetime.now(UTC).timestamp()
        timestamp = 0.0

        def capture_timestamp(line):
            nonlocal timestamp
            line_ts = self._extract_log_line_timestamp(line)
            if line_ts is not None:
                timestamp = line_ts.timestamp()

        snapshot = _shared_read_latest_portfolio_snapshot(
            BOT_LOG_PATH, on_portfolio_value=capture_timestamp)
        snapshot['portfolio_snapshot_timestamp_unixtime'] = timestamp
        snapshot['portfolio_snapshot_age_seconds'] = 0.0
        snapshot['metrics_generated_unixtime'] = generated_at
        if snapshot['portfolio_snapshot_timestamp_unixtime'] > 0.0:
            snapshot['portfolio_snapshot_age_seconds'] = max(
                0.0,
                snapshot['metrics_generated_unixtime'] -
                snapshot['portfolio_snapshot_timestamp_unixtime'],
            )

        return snapshot

    def read_portfolio_start_value(self):
        """Read initial dry-run portfolio cash from first initialization log line."""
        try:
            mtime = os.path.getmtime(BOT_LOG_PATH) if os.path.exists(
                BOT_LOG_PATH) else None
            if START_VALUE_CACHE['log_mtime'] == mtime:
                return START_VALUE_CACHE['value']

            scanned = _shared_scan_portfolio_start_value(BOT_LOG_PATH)
            value = scanned if scanned is not None else 0.0

            if value <= 0.0 and os.path.exists(PORTFOLIO_STATE_PATH):
                try:
                    with open(PORTFOLIO_STATE_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                        state = json.load(f)
                    value = float(state.get('initial_portfolio_value') or 0.0)
                except (TypeError, ValueError):
                    pass

            START_VALUE_CACHE['log_mtime'] = mtime
            START_VALUE_CACHE['value'] = value
            return value
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
            return 0.0

    def read_ai_copilot_usage(self):
        """Read AI co-pilot caps and monthly usage state for monitoring."""
        result = _shared_read_ai_copilot_usage(
            ENV_PATH,
            AI_COPILOT_STATE_PATH,
            quiet=True,
            backup_state_path=f'{AI_COPILOT_STATE_PATH}.bak',
        )

        try:
            api_usage = self._read_ai_spend_from_api()
            if api_usage:
                result.update(api_usage)
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
            pass

        return result

    def read_ai_shadow_suggestions(self):
        return _shared_read_ai_shadow_suggestions(BOT_LOG_PATH)

    def calculate_pnl_metrics(self, trades, time_window_hours=24):
        return _shared_calculate_pnl_metrics(trades, time_window_hours=time_window_hours)

    def format_prometheus_metrics(self, metrics):
        return _shared_format_prometheus_metrics(metrics)

    def log_message(self, format, *args):
        pass  # Suppress logging


if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', 9200), MetricsHandler)
    print('PnL Metrics Exporter running on http://0.0.0.0:9200/metrics', file=sys.stderr)
    sys.stderr.flush()
    server.serve_forever()
