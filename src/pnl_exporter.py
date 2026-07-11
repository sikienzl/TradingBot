#!/usr/bin/env python3
"""
Simple Prometheus exporter for Trading Bot PnL metrics
Listens on http://localhost:9200/metrics
"""
import ast
import csv
import math
import os
import sys
import json
import re
import time
import base64
import hashlib
import hmac
import urllib.parse
import urllib.request
import urllib.error
from contextlib import suppress
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    import ccxt
except Exception:  # pragma: no cover - optional dependency in test shells
    ccxt = None

JOURNAL_PATH = '/opt/trading_2/trade_journal.csv'
BOT_LOG_PATH = '/opt/trading_2/logs/bot.log'
ENV_PATH = '/opt/trading_2/.env'
AI_COPILOT_STATE_PATH = '/opt/trading_2/ai_copilot_state.json'
AI_COPILOT_BENCHMARK_STATE_PATH = '/opt/trading_2/ai_copilot_benchmark_state.json'
PORTFOLIO_STATE_PATH = '/opt/trading_2/.portfolio_state.json'
SCORECARD_PROM_PATH = '/opt/trading_2/results/scorecards/textfile/trading_scorecard.prom'
MIN_DRAWDOWN_PCT_BASE_USD = 1.0
START_VALUE_CACHE = {
    'expires_at': 0.0,
    'value': 0.0,
}
LAST_KRAKEN_NONCE = 0
START_VALUE_CACHE_TTL_SECONDS = 300
AI_SPEND_CACHE = {
    'value': None,
    'expires_at': 0.0,
}
AI_SPEND_CACHE_TTL_SECONDS = 300
AI_SPEND_API_TIMEOUT_SECONDS = 2
CURRENT_PRICE_RE = re.compile(
    r'📊\s+([A-Z0-9]+):\s+Buy\s+[0-9.]+\s+→\s+Current\s+([0-9.]+)'
)
OPEN_TRADE_AMOUNT_BASE_RE = re.compile(
    r"'([A-Z0-9]+)'\s*:\s*\{[^}]*'amount_base'\s*:\s*([0-9.eE+-]+)"
)
OPEN_TRADE_AMOUNT_COIN_RE = re.compile(
    r"'([A-Z0-9]+)'\s*:\s*\{[^}]*'amount_coin'\s*:\s*([0-9.eE+-]+)"
)
SHADOW_SUGGESTION_META_RE = re.compile(
    r'^([a-z]+),\s+confidence=([0-9.]+),\s+reason='
)


class MetricsHandler(BaseHTTPRequestHandler):
    def _env_bool(self, env_key, default=False):
        # Prefer process environment variables, fall back to .env file
        value = os.getenv(env_key, '').strip() or self._read_env_value(env_key)
        if value == '':
            return bool(default)
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}

    def _safe_int_env(self, env_key, default):
        # Prefer process environment variables, fall back to .env file
        value = os.getenv(env_key, '').strip() or self._read_env_value(env_key)
        if value == '':
            return int(default)
        try:
            return int(value)
        except ValueError:
            return int(default)

    def _safe_float_env(self, env_key, default):
        # Prefer process environment variables, fall back to .env file
        value = os.getenv(env_key, '').strip() or self._read_env_value(env_key)
        if value == '':
            return float(default)
        try:
            return float(value)
        except ValueError:
            return float(default)

    def _compute_recent_pnl_guard_state(self, trades):
        enabled = self._env_bool('RECENT_PNL_GUARD_ENABLED', True)
        window = max(1, self._safe_int_env('RECENT_PNL_GUARD_WINDOW', 120))
        min_trades = max(1, self._safe_int_env(
            'RECENT_PNL_GUARD_MIN_TRADES', 60))
        min_realized_pnl = self._safe_float_env(
            'RECENT_PNL_GUARD_MIN_REALIZED_PNL', 0.0)
        max_profit_factor = self._safe_float_env(
            'RECENT_PNL_GUARD_MAX_PROFIT_FACTOR', 1.0)

        state = {
            'recent_pnl_guard_enabled': 1.0 if enabled else 0.0,
            'recent_pnl_guard_active': 0.0,
            'recent_pnl_guard_window': float(window),
            'recent_pnl_guard_min_trades': float(min_trades),
            'recent_pnl_guard_recent_closed_trades': 0.0,
            'recent_pnl_guard_recent_realized_pnl': 0.0,
            'recent_pnl_guard_recent_profit_factor': 0.0,
            'recent_pnl_guard_min_realized_pnl': float(min_realized_pnl),
            'recent_pnl_guard_max_profit_factor': float(max_profit_factor),
            'recent_pnl_guard_reason': 'disabled' if not enabled else 'insufficient_recent_trades',
        }
        if not enabled:
            return state

        sells = []
        for trade in trades:
            action = (trade.get('action') or '').strip().lower()
            if action != 'sell':
                continue
            try:
                pnl = float(trade.get('pnl_base', '0') or 0.0)
            except ValueError:
                continue
            sells.append(pnl)

        if not sells:
            return state

        recent = sells[-window:]
        recent_closed = len(recent)
        state['recent_pnl_guard_recent_closed_trades'] = float(recent_closed)
        if recent_closed < min_trades:
            return state

        realized_pnl = float(sum(recent))
        gross_profit = float(sum(p for p in recent if p > 0.0))
        gross_loss_abs = float(sum(-p for p in recent if p < 0.0))
        if gross_loss_abs > 0:
            recent_pf = gross_profit / gross_loss_abs
        elif gross_profit > 0:
            recent_pf = float('inf')
        else:
            recent_pf = 0.0

        state['recent_pnl_guard_recent_realized_pnl'] = realized_pnl
        state['recent_pnl_guard_recent_profit_factor'] = recent_pf

        guard_active = realized_pnl < min_realized_pnl and recent_pf <= max_profit_factor
        state['recent_pnl_guard_active'] = 1.0 if guard_active else 0.0
        state['recent_pnl_guard_reason'] = (
            'negative_recent_pnl_and_low_pf' if guard_active else 'healthy_recent_window'
        )
        return state

    def _read_env_value(self, env_key):
        try:
            if os.path.exists(ENV_PATH):
                with open(ENV_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#') or '=' not in line:
                            continue
                        key, value = line.split('=', 1)
                        if key.strip() != env_key:
                            continue
                        return value.strip().strip('"').strip("'")
        except Exception:
            pass
        return ''

    def _read_first_env_value(self, *env_keys):
        for env_key in env_keys:
            value = os.getenv(env_key, '').strip(
            ) or self._read_env_value(env_key)
            if value:
                return value.strip().strip('"').strip("'")
        return ''

    def _currency_aliases(self, currency):
        base = (currency or 'EUR').upper().strip()
        aliases = [base, f'Z{base}', f'X{base}']
        if base == 'BTC':
            aliases.extend(['XBT', 'XXBT'])
        return list(dict.fromkeys(aliases))

    def _kraken_trade_balance_assets(self, currency):
        base = (currency or 'EUR').upper().strip()
        aliases = [base]
        if base in {'EUR', 'USD', 'GBP', 'CHF', 'AUD', 'CAD', 'JPY'}:
            aliases.append(f'Z{base}')
        elif base == 'BTC':
            aliases.extend(['XBT', 'XXBT'])
        else:
            aliases.extend([f'Z{base}', f'X{base}'])
        return list(dict.fromkeys(aliases))

    def _kraken_nonce(self):
        global LAST_KRAKEN_NONCE
        now_ms = int(time.time() * 1000)
        if now_ms <= LAST_KRAKEN_NONCE:
            now_ms = LAST_KRAKEN_NONCE + 1
        LAST_KRAKEN_NONCE = now_ms
        return str(now_ms)

    def _kraken_sign(self, path, payload, api_secret):
        encoded_payload = urllib.parse.urlencode(payload)
        nonce = payload['nonce']
        message = path.encode() + hashlib.sha256((nonce + encoded_payload).encode()).digest()
        signature = hmac.new(
            base64.b64decode(api_secret),
            message,
            hashlib.sha512,
        )
        return base64.b64encode(signature.digest()).decode()

    def _kraken_private_request(self, path, payload, api_key, api_secret):
        encoded_payload = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            f'https://api.kraken.com{path}',
            data=encoded_payload,
            headers={
                'API-Key': api_key,
                'API-Sign': self._kraken_sign(path, payload, api_secret),
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'trading-bot-pnl-exporter/1.0',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode('utf-8', errors='replace'))

    def _read_kraken_trade_balance(self, base_currency, api_key, api_secret):
        path = '/0/private/TradeBalance'
        for asset in self._kraken_trade_balance_assets(base_currency):
            payload = {
                'nonce': self._kraken_nonce(),
                'asset': asset,
            }
            try:
                response = self._kraken_private_request(
                    path, payload, api_key, api_secret)
            except Exception:
                continue
            errors = response.get('error', []) if isinstance(
                response, dict) else []
            if errors:
                continue
            result = response.get('result', {}) if isinstance(
                response, dict) else {}
            for key in ('eb', 'tb'):
                raw_value = result.get(key)
                if raw_value in (None, ''):
                    continue
                try:
                    return float(raw_value)
                except (TypeError, ValueError):
                    continue
        return None

    def _read_kraken_balance(self, api_key, api_secret):
        payload = {
            'nonce': self._kraken_nonce(),
        }
        try:
            response = self._kraken_private_request(
                '/0/private/Balance', payload, api_key, api_secret)
        except Exception:
            return {}
        errors = response.get('error', []) if isinstance(
            response, dict) else []
        if errors:
            return {}
        result = response.get('result', {}) if isinstance(
            response, dict) else {}
        return result if isinstance(result, dict) else {}

    def _read_kraken_asset_pairs(self):
        req = urllib.request.Request(
            'https://api.kraken.com/0/public/AssetPairs',
            headers={
                'Accept': 'application/json',
                'User-Agent': 'trading-bot-pnl-exporter/1.0',
            },
            method='GET',
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                payload = json.loads(
                    response.read().decode('utf-8', errors='replace'))
        except Exception:
            return {}
        errors = payload.get('error', []) if isinstance(payload, dict) else []
        if errors:
            return {}
        result = payload.get('result', {}) if isinstance(payload, dict) else {}
        return result if isinstance(result, dict) else {}

    def _read_kraken_ticker_last(self, pair):
        req = urllib.request.Request(
            f'https://api.kraken.com/0/public/Ticker?pair={urllib.parse.quote(pair)}',
            headers={
                'Accept': 'application/json',
                'User-Agent': 'trading-bot-pnl-exporter/1.0',
            },
            method='GET',
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                payload = json.loads(
                    response.read().decode('utf-8', errors='replace'))
        except Exception:
            return None
        errors = payload.get('error', []) if isinstance(payload, dict) else []
        if errors:
            return None
        result = payload.get('result', {}) if isinstance(payload, dict) else {}
        if not isinstance(result, dict) or not result:
            return None
        first = next(iter(result.values()))
        if not isinstance(first, dict):
            return None
        close_arr = first.get('c')
        if not isinstance(close_arr, list) or not close_arr:
            return None
        try:
            return float(close_arr[0])
        except (TypeError, ValueError):
            return None

    def _read_kraken_balance_portfolio_value(self, base_currency, api_key, api_secret):
        balances = self._read_kraken_balance(api_key, api_secret)
        if not balances:
            return None

        base_aliases = {self._normalize_asset_code(
            alias) for alias in self._currency_aliases(base_currency)}
        total_value = 0.0
        non_base_assets = {}

        for asset_code, raw_amount in balances.items():
            try:
                amount = float(raw_amount)
            except (TypeError, ValueError):
                continue
            if amount <= 0.0:
                continue

            normalized_asset = self._normalize_asset_code(asset_code)
            if normalized_asset in base_aliases:
                total_value += amount
            else:
                non_base_assets[normalized_asset] = non_base_assets.get(
                    normalized_asset, 0.0) + amount

        if not non_base_assets:
            return total_value

        asset_pairs = self._read_kraken_asset_pairs()
        if not asset_pairs:
            return total_value if total_value > 0.0 else None

        pair_map = {}
        normalized_quote = self._normalize_asset_code(base_currency)
        for pair_name, pair_info in asset_pairs.items():
            if '.d' in pair_name:
                continue
            if not isinstance(pair_info, dict):
                continue
            base_asset = self._normalize_asset_code(
                pair_info.get('base') or '')
            quote_asset = self._normalize_asset_code(
                pair_info.get('quote') or '')
            if not base_asset or quote_asset != normalized_quote:
                continue
            altname = pair_info.get('altname') or pair_name
            pair_map.setdefault(base_asset, altname)

        for asset, amount in non_base_assets.items():
            pair = pair_map.get(asset)
            if not pair:
                continue
            last_price = self._read_kraken_ticker_last(pair)
            if last_price is None:
                continue
            total_value += amount * last_price

        return total_value if total_value > 0.0 else None

    def _normalize_asset_code(self, asset_code):
        asset = (asset_code or '').upper().strip()
        if not asset:
            return ''

        while len(asset) > 3 and asset[:1] in {'X', 'Z'}:
            asset = asset[1:]

        if asset == 'XBT':
            return 'BTC'
        if asset == 'XDG':
            return 'DOGE'
        return asset

    def _build_base_symbol_map(self, markets, base_currency):
        symbol_map = {}
        normalized_quote = self._normalize_asset_code(base_currency)
        for symbol, market in (markets or {}).items():
            base_asset = self._normalize_asset_code(market.get('base') or '')
            quote_asset = self._normalize_asset_code(market.get('quote') or '')
            if not base_asset or quote_asset != normalized_quote:
                continue
            if market.get('active', True) is False:
                continue
            symbol_map.setdefault(base_asset, symbol)
        return symbol_map

    def _extract_portfolio_value_from_balance(self, exchange, balance, base_currency):
        totals = balance.get('total', {}) if isinstance(balance, dict) else {}
        if not totals:
            totals = balance.get('free', {}) if isinstance(
                balance, dict) else {}

        portfolio_value = 0.0
        held_assets = {}
        base_aliases = {self._normalize_asset_code(
            alias) for alias in self._currency_aliases(base_currency)}

        for asset_code, raw_amount in totals.items():
            try:
                amount = float(raw_amount)
            except (TypeError, ValueError):
                continue
            if amount <= 0.0:
                continue

            normalized_asset = self._normalize_asset_code(asset_code)
            if normalized_asset in base_aliases:
                portfolio_value += amount
                continue
            held_assets[normalized_asset] = held_assets.get(
                normalized_asset, 0.0) + amount

        if not held_assets:
            return portfolio_value

        try:
            markets = exchange.load_markets()
        except Exception:
            markets = {}
        symbol_map = self._build_base_symbol_map(markets, base_currency)

        for asset, amount in held_assets.items():
            symbol = symbol_map.get(asset)
            if not symbol:
                continue
            try:
                ticker = exchange.fetch_ticker(symbol)
            except Exception:
                continue
            last_price = ticker.get('last') if isinstance(
                ticker, dict) else None
            if last_price is None:
                last_price = ticker.get('close') if isinstance(
                    ticker, dict) else None
            try:
                portfolio_value += amount * float(last_price)
            except (TypeError, ValueError):
                continue

        return portfolio_value

    def _read_kraken_start_value(self):
        api_key = self._read_first_env_value('KRAKEN_API_KEY')
        api_secret = self._read_first_env_value(
            'KRAKEN_API_SECRET', 'KRAKEN_SECRET_KEY')
        if not api_key or not api_secret:
            return None

        base_currency = self._read_env_value('BASE_CURRENCY') or 'EUR'

        # Prefer direct REST valuation from current balances for exact live portfolio value.
        balance_value = self._read_kraken_balance_portfolio_value(
            base_currency, api_key, api_secret)
        if balance_value is not None:
            return balance_value

        api_value = self._read_kraken_trade_balance(
            base_currency, api_key, api_secret)
        if api_value is not None:
            return api_value

        if ccxt is None:
            return None

        try:
            exchange = ccxt.kraken({
                'apiKey': api_key,
                'secret': api_secret,
                'enableRateLimit': True,
                'timeout': 10000,
                'options': {
                    'adjustForTimeDifference': True,
                    'verbose': False,
                },
            })
            balance = exchange.fetch_balance()
            return float(self._extract_portfolio_value_from_balance(exchange, balance, base_currency))
        except Exception:
            return None

        return 0.0

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
        except Exception:
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
        # If snapshot reports no open positions but the trade journal contains
        # unmatched BUYs (dry-run / monitoring entries), derive open positions
        # from the journal as a best-effort fallback so the exporter reports
        # simulated opens immediately.
        try:
            if (snapshot.get('open_positions_count', 0) == 0) and trades:
                derived = self._derive_open_positions_from_trades(trades)
                if derived:
                    # populate holdings and counts
                    holdings_amount = {c: v['net_coin']
                                       for c, v in derived.items()}
                    holdings_cost = {c: v['cost_eur']
                                     for c, v in derived.items()}
                    snapshot['holdings_amount_coin'] = holdings_amount
                    snapshot['holdings_cost_basis_eur'] = holdings_cost

                    # Try to mark-to-market derived holdings using ccxt if available.
                    holdings_value = {}
                    holdings_unrealized = {}
                    try:
                        if ccxt is not None and holdings_amount:
                            exchange_name = self._read_first_env_value(
                                'EXCHANGE_NAME') or 'kraken'
                            try:
                                exchange_cls = getattr(
                                    ccxt, exchange_name.lower(), None)
                                if exchange_cls is None:
                                    exchange = ccxt.__dict__.get(
                                        exchange_name, None)
                                    if exchange is not None:
                                        exchange = exchange(
                                            {'enableRateLimit': True})
                                else:
                                    exchange = exchange_cls(
                                        {'enableRateLimit': True})
                            except Exception:
                                exchange = None

                            symbol_map = {}
                            if exchange is not None:
                                try:
                                    markets = exchange.load_markets()
                                except Exception:
                                    markets = {}
                                symbol_map = self._build_base_symbol_map(
                                    markets, self._read_env_value('BASE_CURRENCY') or 'EUR')

                            for coin, amt in holdings_amount.items():
                                valued = None
                                symbol = symbol_map.get(coin)
                                if symbol and exchange is not None:
                                    try:
                                        ticker = exchange.fetch_ticker(symbol)
                                        last_price = ticker.get('last') if isinstance(
                                            ticker, dict) else None
                                        if last_price is None:
                                            last_price = ticker.get('close') if isinstance(
                                                ticker, dict) else None
                                        if last_price is not None:
                                            valued = float(
                                                amt) * float(last_price)
                                    except Exception:
                                        valued = None
                                if valued is None:
                                    # fallback to cost-basis if price unavailable
                                    valued = float(
                                        holdings_cost.get(coin, 0.0))
                                holdings_value[coin] = float(valued)
                                holdings_unrealized[coin] = float(
                                    valued) - float(holdings_cost.get(coin, 0.0) or 0.0)
                        else:
                            # ccxt not available: use cost-basis as value
                            for coin in holdings_amount.keys():
                                holdings_value[coin] = float(
                                    holdings_cost.get(coin, 0.0))
                                holdings_unrealized[coin] = 0.0
                    except Exception:
                        for coin in holdings_amount.keys():
                            holdings_value[coin] = float(
                                holdings_cost.get(coin, 0.0))
                            holdings_unrealized[coin] = 0.0

                    snapshot['holdings_value_eur'] = holdings_value
                    snapshot['holdings_unrealized_pnl_eur'] = holdings_unrealized
                    snapshot['open_positions_count'] = int(len(derived))
                    # mark that this snapshot was derived from the trade journal
                    snapshot['_derived_from_journal'] = True
        except Exception:
            pass
        # Recompute portfolio value from cash + mark-to-market holdings so
        # Grafana reflects current buys/sells immediately.
        try:
            cash = float(snapshot.get('portfolio_cash_eur', 0.0) or 0.0)
        except Exception:
            cash = 0.0
        try:
            holdings_values = snapshot.get('holdings_value_eur') or {}
            total_holdings = float(sum(float(v or 0.0) for v in (holdings_values.values() if isinstance(holdings_values, dict) else [])))
        except Exception:
            total_holdings = 0.0
        snapshot['portfolio_value_eur'] = cash + total_holdings
        metrics_dict['portfolio_value_eur'] = snapshot['portfolio_value_eur']
        metrics_dict['portfolio_cash_eur'] = cash
        metrics_dict['holdings_value_eur'] = snapshot['holdings_value_eur']
        metrics_dict['holdings_amount_coin'] = snapshot['holdings_amount_coin']
        metrics_dict['holdings_cost_basis_eur'] = snapshot['holdings_cost_basis_eur']
        metrics_dict['holdings_unrealized_pnl_eur'] = snapshot['holdings_unrealized_pnl_eur']
        metrics_dict['open_positions_count'] = snapshot['open_positions_count']
        metrics_dict['portfolio_snapshot_timestamp_unixtime'] = snapshot['portfolio_snapshot_timestamp_unixtime']
        metrics_dict['portfolio_snapshot_age_seconds'] = snapshot['portfolio_snapshot_age_seconds']
        metrics_dict['metrics_generated_unixtime'] = snapshot['metrics_generated_unixtime']
        metrics_dict['portfolio_start_value_eur'] = self.read_portfolio_start_value()
        # If we're running in DRY_RUN but using the real Kraken API (not simulated data),
        # force the primary exported portfolio value to the Kraken-derived start value
        # so dashboards reflect the live account rather than any synthetic snapshot.
        try:
            start_val = metrics_dict['portfolio_start_value_eur']
            if (
                start_val
                and float(start_val) > 0.0
                and self._env_bool('DRY_RUN', False)
                and not self._env_bool('SIMULATE_DATA', False)
                and not snapshot.get('_derived_from_journal', False)
            ):
                # Only override with Kraken-derived start value when we are
                # running DRY_RUN + using the live Kraken API *and* the
                # current snapshot was not derived from the local trade
                # journal (i.e. prefer locally-derived simulated holdings).
                metrics_dict['portfolio_value_eur'] = float(start_val)
        except Exception:
            pass

        # Ensure the primary exported portfolio value reflects cash + MTM holdings
        # so simulated buys/sells immediately show in dashboards. Compute again
        # here to avoid earlier overrides masking the live snapshot.
        try:
            cash_final = float(snapshot.get('portfolio_cash_eur', 0.0) or 0.0)
        except Exception:
            cash_final = 0.0
        try:
            hv = snapshot.get('holdings_value_eur') or {}
            holdings_sum = float(sum(float(v or 0.0) for v in (hv.values() if isinstance(hv, dict) else [])))
        except Exception:
            holdings_sum = 0.0
        metrics_dict['portfolio_value_eur'] = cash_final + holdings_sum
        metrics_dict['portfolio_cash_eur'] = cash_final

        metrics_dict['portfolio_target_eur'] = self._safe_float_env(
            'PORTFOLIO_TARGET_EUR', 25.0)
        metrics_dict.update(self.read_ai_copilot_usage())
        metrics_dict.update(self.read_ai_model_info())
        metrics_dict.update(self.read_ai_shadow_suggestions())
        metrics_dict.update(self._compute_recent_pnl_guard_state(trades))
        return self.format_prometheus_metrics(metrics_dict)

    def read_trades(self):
        """Read all trades from journal"""
        trades = []
        try:
            with open(JOURNAL_PATH, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    trades.append(row)
        except Exception:
            pass
        return trades

    def _derive_open_positions_from_trades(self, trades):
        """Derive open (net) positions per coin from the trade journal.

        Returns a dict coin -> { 'net_coin': float, 'cost_eur': float }
        Only returns coins with positive net amount (open long positions).
        """
        per = {}
        if not trades:
            return {}

        for t in trades:
            action = (t.get('action') or t.get('side') or '').strip().lower()
            if not action or action not in {'buy', 'sell'}:
                continue
            coin = (t.get('coin') or t.get('symbol') or '').upper().strip()
            if not coin:
                continue

            # try several common amount keys
            amt_coin = None
            for k in ('amount_coin', 'amount', 'qty', 'coin_amount'):
                raw = t.get(k)
                if raw in (None, ''):
                    continue
                try:
                    amt_coin = float(raw)
                    break
                except Exception:
                    continue
            if amt_coin is None:
                continue

            # determine euros spent/received for this trade if present
            amount_base = None
            for k in ('amount_base', 'amount_eur', 'trade_amount', 'cost_eur'):
                raw = t.get(k)
                if raw in (None, ''):
                    continue
                try:
                    amount_base = float(raw)
                    break
                except Exception:
                    continue

            price = None
            for k in ('price', 'price_eur', 'entry_price'):
                raw = t.get(k)
                if raw in (None, ''):
                    continue
                try:
                    price = float(raw)
                    break
                except Exception:
                    continue

            rec = per.setdefault(coin, {'net_coin': 0.0, 'cost_eur': 0.0})
            if action == 'buy':
                rec['net_coin'] += amt_coin
                rec['cost_eur'] += (amount_base if amount_base is not None else (
                    price * amt_coin if price is not None else 0.0))
            else:
                rec['net_coin'] -= amt_coin
                rec['cost_eur'] -= (amount_base if amount_base is not None else (
                    price * amt_coin if price is not None else 0.0))

        # keep only positive net positions
        result = {c: v for c, v in per.items() if v.get(
            'net_coin', 0.0) > 1e-12}
        return result

    def _parse_timestamp(self, timestamp_str):
        """Parse ISO timestamps with or without microseconds, treating naive timestamps as UTC."""
        ts = (timestamp_str or '').strip()
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            try:
                dt = datetime.fromisoformat(ts.split('.')[0])
            except Exception:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _extract_log_line_timestamp(self, line):
        """Extract a datetime from standard bot log lines like 'YYYY-MM-DD HH:MM:SS,mmm - ...'."""
        try:
            timestamp_raw = line.split(' - ', 1)[0].strip()
            return datetime.strptime(timestamp_raw, '%Y-%m-%d %H:%M:%S,%f')
        except Exception:
            return None

    def _extract_return_pct(self, trade, pnl_value):
        """Extract trade return in decimal form when available."""
        for key in ('pnl_pct', 'pnl_percent', 'return_pct', 'return'):
            raw = trade.get(key)
            if raw in (None, ''):
                continue
            try:
                val = float(raw)
                return val / 100.0 if abs(val) > 1.0 else val
            except ValueError:
                continue
        return 0.0

    def _read_live_portfolio_snapshot_from_kraken(self):
        """Fallback snapshot from Kraken when bot log data is unavailable."""
        base_currency = self._read_env_value('BASE_CURRENCY') or 'EUR'
        api_key = self._read_first_env_value('KRAKEN_API_KEY')
        api_secret = self._read_first_env_value(
            'KRAKEN_API_SECRET', 'KRAKEN_SECRET_KEY')
        if not api_key or not api_secret:
            return None

        balances = self._read_kraken_balance(api_key, api_secret)
        if not balances:
            return None

        base_aliases = {self._normalize_asset_code(
            alias) for alias in self._currency_aliases(base_currency)}
        portfolio_cash = 0.0
        open_positions_count = 0

        for asset_code, raw_amount in balances.items():
            try:
                amount = float(raw_amount)
            except (TypeError, ValueError):
                continue
            if amount <= 0.0:
                continue
            normalized_asset = self._normalize_asset_code(asset_code)
            if normalized_asset in base_aliases:
                portfolio_cash += amount
            else:
                open_positions_count += 1

        portfolio_value = self._read_kraken_balance_portfolio_value(
            base_currency, api_key, api_secret)
        if portfolio_value is None:
            portfolio_value = portfolio_cash

        return {
            'portfolio_value_eur': float(max(portfolio_value, 0.0)),
            'portfolio_cash_eur': float(max(portfolio_cash, 0.0)),
            'open_positions_count': int(max(open_positions_count, 0)),
            'portfolio_snapshot_timestamp_unixtime': datetime.now(timezone.utc).timestamp(),
            'portfolio_snapshot_age_seconds': 0.0,
        }

    def read_latest_portfolio_snapshot(self):
        """Read latest portfolio value and cash from bot log."""
        snapshot = {
            'portfolio_value_eur': 0.0,
            'portfolio_cash_eur': 0.0,
            'holdings_amount_coin': {},
            'holdings_value_eur': {},
            'holdings_cost_basis_eur': {},
            'holdings_unrealized_pnl_eur': {},
            'open_positions_count': 0,
            'portfolio_snapshot_timestamp_unixtime': 0.0,
            'portfolio_snapshot_age_seconds': 0.0,
            'metrics_generated_unixtime': datetime.now(timezone.utc).timestamp(),
        }

        # Prefer a persisted portfolio snapshot file when available (written by the bot)
        try:
            if os.path.exists(PORTFOLIO_STATE_PATH):
                with open(PORTFOLIO_STATE_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                    state = json.load(f)
                now_ts = datetime.now(timezone.utc).timestamp()
                cash = float(state.get('cash', 0.0) or 0.0)
                holdings_raw = state.get('holdings', {}) or {}
                holdings = {str(k).upper(): float(v) for k, v in holdings_raw.items() if (
                    v not in (None, '') and float(v) != 0.0)}
                open_trades_raw = state.get('open_trades', {}) or {}
                cost_basis = {}
                for coin, trade in (open_trades_raw.items()):
                    try:
                        amount_base = float(
                            trade.get('amount_base', 0.0) or 0.0)
                    except Exception:
                        amount_base = 0.0
                    if amount_base > 0.0:
                        cost_basis[str(coin).upper()] = amount_base

                snapshot.update({
                    # Compute a live portfolio value if possible: prefer cash + current market value
                    # of holdings (when ccxt is available) falling back to stored initial value.
                    'portfolio_value_eur': float(state.get('initial_portfolio_value') or 0.0) or 0.0,
                    'portfolio_cash_eur': cash,
                    'holdings_amount_coin': holdings,
                    'holdings_cost_basis_eur': cost_basis,
                    'open_positions_count': int(max(len(holdings), len(open_trades_raw))),
                    'portfolio_snapshot_timestamp_unixtime': float(datetime.fromisoformat(state.get('timestamp')).timestamp()) if state.get('timestamp') else now_ts,
                    'portfolio_snapshot_age_seconds': max(0.0, now_ts - (float(datetime.fromisoformat(state.get('timestamp')).timestamp()) if state.get('timestamp') else now_ts)),
                    'metrics_generated_unixtime': now_ts,
                })
                # Do a best-effort mapping for holdings_value: try to value each holding
                # using live market prices via ccxt (preferred). If ccxt is unavailable
                # or pricing fails, fall back to the stored cost basis.
                try:
                    if ccxt is not None and snapshot['holdings_amount_coin']:
                        exchange_name = self._read_first_env_value(
                            'EXCHANGE_NAME') or 'kraken'
                        try:
                            exchange_cls = getattr(
                                ccxt, exchange_name.lower(), None)
                            if exchange_cls is None:
                                # Some CCXT ids differ from common names (kraken -> kraken)
                                exchange = ccxt.__dict__.get(
                                    exchange_name, None)
                            else:
                                exchange = exchange_cls(
                                    {'enableRateLimit': True})
                        except Exception:
                            exchange = None

                        symbol_map = {}
                        try:
                            if exchange is not None:
                                try:
                                    markets = exchange.load_markets()
                                except Exception:
                                    markets = {}
                                symbol_map = self._build_base_symbol_map(
                                    markets, self._read_env_value('BASE_CURRENCY') or 'EUR')
                        except Exception:
                            symbol_map = {}

                        for coin, amt in snapshot['holdings_amount_coin'].items():
                            valued = None
                            symbol = symbol_map.get(coin)
                            if symbol and exchange is not None:
                                try:
                                    ticker = exchange.fetch_ticker(symbol)
                                    last_price = ticker.get('last') if isinstance(
                                        ticker, dict) else None
                                    if last_price is None:
                                        last_price = ticker.get('close') if isinstance(
                                            ticker, dict) else None
                                    if last_price is not None:
                                        valued = float(amt) * float(last_price)
                                except Exception:
                                    valued = None
                            if valued is None:
                                # Fallback: use cost basis as current value when available
                                if coin in snapshot['holdings_cost_basis_eur']:
                                    valued = float(
                                        snapshot['holdings_cost_basis_eur'][coin])
                                else:
                                    valued = 0.0
                            snapshot['holdings_value_eur'][coin] = float(
                                valued)
                except Exception:
                    # On any failure above, fall back to cost-basis mapping
                    for coin, amt in snapshot['holdings_amount_coin'].items():
                        if coin in snapshot['holdings_cost_basis_eur']:
                            snapshot['holdings_value_eur'][coin] = float(
                                snapshot['holdings_cost_basis_eur'][coin])
                        else:
                            snapshot['holdings_value_eur'][coin] = 0.0
                # If possible and we're in DRY_RUN but not SIMULATE_DATA,
                # prefer the live Kraken start value over the synthetic
                # snapshot so dashboards reflect the real account value.
                try:
                    start_val = self.read_portfolio_start_value()
                    if (
                        start_val
                        and float(start_val) > 0.0
                        and self._env_bool('DRY_RUN', False)
                        and not self._env_bool('SIMULATE_DATA', False)
                    ):
                        snapshot['portfolio_value_eur'] = float(start_val)
                except Exception:
                    pass
                return snapshot
        except Exception:
            # If snapshot file is unreadable, continue to bot log parsing fallback
            pass

        if not os.path.exists(BOT_LOG_PATH):
            fallback = self._read_live_portfolio_snapshot_from_kraken()
            if fallback is not None:
                snapshot.update(fallback)
            return snapshot

        try:
            with open(BOT_LOG_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                tail_lines = list(deque(f, maxlen=800))

            current_prices = {}
            open_trade_amount_base = {}
            open_trade_amount_coin = {}

            for line in reversed(tail_lines):
                if snapshot['portfolio_value_eur'] == 0.0 and 'Portfolio value:' in line:
                    try:
                        value_part = line.split(
                            'Portfolio value:', 1)[1].strip()
                        snapshot['portfolio_value_eur'] = float(
                            value_part.split(' ')[0])
                        line_ts = self._extract_log_line_timestamp(line)
                        if line_ts is not None:
                            snapshot['portfolio_snapshot_timestamp_unixtime'] = line_ts.timestamp(
                            )
                    except Exception:
                        pass

                if snapshot['portfolio_cash_eur'] == 0.0 and '  - Cash:' in line:
                    try:
                        cash_part = line.split('Cash:', 1)[1].strip()
                        snapshot['portfolio_cash_eur'] = float(
                            cash_part.split(' ')[0])
                    except Exception:
                        pass

                if not snapshot['holdings_amount_coin'] and '  - Holdings:' in line:
                    try:
                        holdings_part = line.split('Holdings:', 1)[1].strip()
                        parsed = ast.literal_eval(holdings_part)
                        if isinstance(parsed, dict):
                            snapshot['holdings_amount_coin'] = {
                                str(coin).upper(): float(amount)
                                for coin, amount in parsed.items()
                                if float(amount) > 0.0
                            }
                    except Exception:
                        pass

                if '  - Open trades details:' in line:
                    for coin, amount_coin in OPEN_TRADE_AMOUNT_COIN_RE.findall(line):
                        current_coin = coin.upper()
                        if current_coin not in open_trade_amount_coin:
                            try:
                                open_trade_amount_coin[current_coin] = float(
                                    amount_coin)
                            except ValueError:
                                pass
                    for coin, amount_base in OPEN_TRADE_AMOUNT_BASE_RE.findall(line):
                        current_coin = coin.upper()
                        if current_coin not in open_trade_amount_base:
                            try:
                                open_trade_amount_base[current_coin] = float(
                                    amount_base)
                            except ValueError:
                                pass

                if '→ Current ' in line:
                    match = CURRENT_PRICE_RE.search(line)
                    if match:
                        coin, current_price = match.groups()
                        current_coin = coin.upper()
                        if current_coin not in current_prices:
                            try:
                                current_prices[current_coin] = float(
                                    current_price)
                            except ValueError:
                                pass

                if snapshot['portfolio_value_eur'] != 0.0 and snapshot['portfolio_cash_eur'] != 0.0:
                    if not snapshot['holdings_amount_coin']:
                        break
                    if all(
                        coin in current_prices or coin in open_trade_amount_base
                        for coin in snapshot['holdings_amount_coin']
                    ):
                        break
        except Exception:
            return snapshot

        for coin, amount_coin in snapshot['holdings_amount_coin'].items():
            if coin in current_prices:
                snapshot['holdings_value_eur'][coin] = amount_coin * \
                    current_prices[coin]
            elif coin in open_trade_amount_base:
                snapshot['holdings_value_eur'][coin] = open_trade_amount_base[coin]
            if coin in open_trade_amount_base:
                snapshot['holdings_cost_basis_eur'][coin] = open_trade_amount_base[coin]

        for coin, amount_coin in open_trade_amount_coin.items():
            snapshot['holdings_amount_coin'].setdefault(coin, amount_coin)
        for coin, amount_base in open_trade_amount_base.items():
            snapshot['holdings_cost_basis_eur'].setdefault(coin, amount_base)
        for coin, current_value in snapshot['holdings_value_eur'].items():
            cost_basis = snapshot['holdings_cost_basis_eur'].get(coin, 0.0)
            snapshot['holdings_unrealized_pnl_eur'][coin] = current_value - cost_basis
        snapshot['open_positions_count'] = len(
            snapshot['holdings_amount_coin'])
        if snapshot['portfolio_snapshot_timestamp_unixtime'] > 0.0:
            snapshot['portfolio_snapshot_age_seconds'] = max(
                0.0,
                snapshot['metrics_generated_unixtime'] -
                snapshot['portfolio_snapshot_timestamp_unixtime'],
            )

        return snapshot

    def read_portfolio_start_value(self):
        """Read the current session's portfolio start value from Kraken first."""
        try:
            now = time.time()
            if START_VALUE_CACHE['expires_at'] > now:
                return START_VALUE_CACHE['value']

            api_value = self._read_kraken_start_value()
            if api_value is not None:
                START_VALUE_CACHE['value'] = api_value
                START_VALUE_CACHE['expires_at'] = now + \
                    START_VALUE_CACHE_TTL_SECONDS
                return api_value

            value = 0.0
            if os.path.exists(BOT_LOG_PATH):
                with open(BOT_LOG_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        if 'Portfolio value:' not in line:
                            continue
                        try:
                            value_part = line.split(
                                'Portfolio value:', 1)[1].strip()
                            value = float(value_part.split(' ')[0])
                            break
                        except Exception:
                            continue

            if value <= 0.0 and os.path.exists(PORTFOLIO_STATE_PATH):
                try:
                    with open(PORTFOLIO_STATE_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                        state = json.load(f)
                    value = float(state.get('initial_portfolio_value') or 0.0)
                except Exception:
                    pass

            # Final fallback: reuse the scorecard runtime start value metric.
            if value <= 0.0 and os.path.exists(SCORECARD_PROM_PATH):
                try:
                    with open(SCORECARD_PROM_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                        for line in f:
                            if not line.startswith('trading_runtime_portfolio_start_value'):
                                continue
                            parts = line.strip().split()
                            if len(parts) >= 2:
                                value = float(parts[-1])
                                break
                except Exception:
                    pass

            START_VALUE_CACHE['value'] = value
            START_VALUE_CACHE['expires_at'] = now + \
                START_VALUE_CACHE_TTL_SECONDS
            return value
        except Exception:
            return 0.0

    def read_ai_copilot_usage(self):
        """Read AI co-pilot caps and monthly usage state for monitoring."""
        result = {
            'ai_copilot_budget_cap_usd': 0.0,
            'ai_copilot_budget_used_usd': 0.0,
            'ai_copilot_calls_used_monthly': 0.0,
            'ai_copilot_calls_cap_monthly': 0.0,
        }

        try:
            budget_cap = self._read_env_value(
                'AI_COPILOT_MAX_BUDGET_USD_PER_MONTH')
            calls_cap = self._read_env_value('AI_COPILOT_MAX_CALLS_PER_MONTH')
            if budget_cap:
                result['ai_copilot_budget_cap_usd'] = float(budget_cap)
            if calls_cap:
                result['ai_copilot_calls_cap_monthly'] = float(calls_cap)
        except Exception:
            pass

        try:
            for candidate in (AI_COPILOT_STATE_PATH, f'{AI_COPILOT_STATE_PATH}.bak'):
                if not os.path.exists(candidate):
                    continue
                with open(candidate, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                now = datetime.now(timezone.utc)
                month_key = f"{now.year:04d}-{now.month:02d}"
                if state.get('month_key') == month_key:
                    result['ai_copilot_budget_cap_usd'] = float(
                        state.get('budget_cap_usd', result['ai_copilot_budget_cap_usd']) or result['ai_copilot_budget_cap_usd'])
                    result['ai_copilot_calls_cap_monthly'] = float(
                        state.get('calls_cap_monthly', result['ai_copilot_calls_cap_monthly']) or result['ai_copilot_calls_cap_monthly'])
                    if result['ai_copilot_budget_used_usd'] <= 0.0:
                        result['ai_copilot_budget_used_usd'] = float(
                            state.get('monthly_spend_usd', 0.0) or 0.0)
                    result['ai_copilot_calls_used_monthly'] = float(
                        state.get('monthly_calls', 0) or 0)
                    break
        except Exception:
            pass

        try:
            api_usage = self._read_ai_spend_from_api()
            if api_usage:
                result.update(api_usage)
        except Exception:
            pass

        return result

    def read_ai_model_info(self):
        """Read current primary and benchmark AI model names for dashboard display."""
        result = {'ai_copilot_model_info': []}

        def _read_state_model(path):
            for candidate in (path, f'{path}.bak'):
                if not os.path.exists(candidate):
                    continue
                try:
                    with open(candidate, 'r', encoding='utf-8') as f:
                        state = json.load(f)
                    model = str(state.get('model', '') or '').strip()
                    if model:
                        return model
                except Exception:
                    continue
            return ''

        primary_model = _read_state_model(
            AI_COPILOT_STATE_PATH) or self._read_env_value('AI_COPILOT_MODEL')
        benchmark_model = _read_state_model(
            AI_COPILOT_BENCHMARK_STATE_PATH) or self._read_env_value('AI_COPILOT_BENCHMARK_MODEL')
        benchmark_enabled = self._env_bool(
            'AI_COPILOT_BENCHMARK_ENABLED', False)

        if primary_model:
            result['ai_copilot_model_info'].append(
                {'role': 'primary', 'model': primary_model})

        if benchmark_model:
            role = 'benchmark' if benchmark_enabled else 'benchmark-disabled'
            result['ai_copilot_model_info'].append(
                {'role': role, 'model': benchmark_model})

        return result

    def read_ai_shadow_suggestions(self):
        """Read the most recent AI suggestion outcomes from the bot log."""
        result = {'ai_copilot_shadow_suggestions': []}

        if not os.path.exists(BOT_LOG_PATH):
            return result

        try:
            with open(BOT_LOG_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                tail_lines = list(deque(f, maxlen=4000))

            now = datetime.now(timezone.utc)
            suggestions = []
            rank = 1

            for line in reversed(tail_lines):
                if 'AI co-pilot' not in line:
                    continue

                try:
                    timestamp_raw, remainder = line.split(' - ', 1)
                    confidence = 0.0
                    mode = 'shadow'

                    if 'AI co-pilot shadow suggestion:' in remainder:
                        payload = remainder.split(
                            'AI co-pilot shadow suggestion:', 1)[1].strip()
                        changes_text, meta_text = payload.split(' (risk=', 1)
                        changes = ast.literal_eval(changes_text.strip())
                        meta_text = meta_text.rstrip(')')
                        meta_match = SHADOW_SUGGESTION_META_RE.match(meta_text)
                        confidence = float(meta_match.group(2)
                                           ) if meta_match else 0.0
                    elif 'AI co-pilot suggested unchanged values:' in remainder:
                        payload = remainder.split(
                            'AI co-pilot suggested unchanged values:', 1)[1].strip()
                        changes = ast.literal_eval(payload)
                        mode = 'unchanged'
                    elif 'AI co-pilot suggestion: no change' in remainder:
                        payload = remainder.split(
                            'AI co-pilot suggestion:', 1)[1].strip()
                        meta_text = payload.split(' (risk=', 1)[1].rstrip(')')
                        meta_match = SHADOW_SUGGESTION_META_RE.match(meta_text)
                        confidence = float(meta_match.group(2)
                                           ) if meta_match else 0.0
                        changes = {'no_change': 0.0}
                        mode = 'no_change'
                    else:
                        continue

                    ts = datetime.strptime(
                        timestamp_raw.strip(), '%Y-%m-%d %H:%M:%S,%f')
                    age_minutes = max(
                        0.0, (now - ts).total_seconds() / 60.0)
                except Exception:
                    continue

                if not isinstance(changes, dict):
                    continue

                for parameter, value in changes.items():
                    try:
                        numeric_value = float(value)
                    except (TypeError, ValueError):
                        continue
                    suggestions.append({
                        'rank': rank,
                        'suggestion_id': f'{rank}_{parameter}',
                        'mode': mode,
                        'parameter': str(parameter),
                        'value': numeric_value,
                        'confidence': confidence,
                        'age_minutes': round(age_minutes, 2),
                    })
                rank += 1
                if rank > 3:
                    break

            result['ai_copilot_shadow_suggestions'] = suggestions
        except Exception:
            pass

        return result

    def calculate_pnl_metrics(self, trades, time_window_hours=24):
        """Calculate PnL metrics from trades"""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=time_window_hours)

        all_time_realized_pnl = 0.0
        total_realized_pnl = 0.0
        total_trades = 0
        winning_trades = 0
        losing_trades = 0
        gross_profit = 0.0
        gross_loss = 0.0
        per_trade_returns = []

        coin_pnl = {}
        coin_closed = {}
        coin_wins = {}
        coin_buy_volume = {}
        coin_buy_count = {}

        # Equity curve from closed-trade realized PnL over the window.
        equity = 0.0
        peak_equity = 0.0
        max_drawdown_usd = 0.0
        max_drawdown_pct = 0.0

        for trade in trades:
            try:
                timestamp_str = trade.get('timestamp', '')
                ts = self._parse_timestamp(timestamp_str)
                if ts is None:
                    continue

                action = trade.get('action', '').lower()
                coin = (trade.get('coin') or 'UNKNOWN').upper()

                if action == 'buy':
                    if ts < cutoff:
                        continue
                    try:
                        buy_volume = float(
                            trade.get('amount_base', '0') or 0.0)
                    except ValueError:
                        buy_volume = 0.0
                    coin_buy_volume[coin] = coin_buy_volume.get(
                        coin, 0.0) + max(buy_volume, 0.0)
                    coin_buy_count[coin] = coin_buy_count.get(coin, 0) + 1
                    continue

                if action != 'sell':
                    continue

                pnl_str = trade.get('pnl_base', '0')
                try:
                    pnl = float(pnl_str)
                except ValueError:
                    continue

                all_time_realized_pnl += pnl

                if ts < cutoff:
                    continue

                total_trades += 1
                total_realized_pnl += pnl

                coin_pnl[coin] = coin_pnl.get(coin, 0.0) + pnl
                coin_closed[coin] = coin_closed.get(coin, 0) + 1

                if pnl > 0:
                    winning_trades += 1
                    gross_profit += pnl
                    coin_wins[coin] = coin_wins.get(coin, 0) + 1
                elif pnl < 0:
                    losing_trades += 1
                    gross_loss += pnl

                ret = self._extract_return_pct(trade, pnl)
                per_trade_returns.append(ret)

                equity += pnl
                if equity > peak_equity:
                    peak_equity = equity
                drawdown_usd = peak_equity - equity
                if drawdown_usd > max_drawdown_usd:
                    max_drawdown_usd = drawdown_usd
                if peak_equity > 0:
                    # Prevent unrealistic percentages when peak equity is near zero.
                    drawdown_pct = drawdown_usd / \
                        max(peak_equity, MIN_DRAWDOWN_PCT_BASE_USD)
                    if drawdown_pct > max_drawdown_pct:
                        max_drawdown_pct = drawdown_pct
            except Exception:
                continue

        win_rate = (winning_trades / total_trades *
                    100) if total_trades > 0 else 0
        avg_pnl = total_realized_pnl / total_trades if total_trades > 0 else 0

        if len(per_trade_returns) > 1:
            mean_ret = sum(per_trade_returns) / len(per_trade_returns)
            variance = sum(
                (r - mean_ret) ** 2 for r in per_trade_returns) / (len(per_trade_returns) - 1)
            std_dev = math.sqrt(variance)
            sharpe_ratio = (mean_ret / std_dev) * \
                math.sqrt(len(per_trade_returns)) if std_dev > 0 else 0.0
        else:
            sharpe_ratio = 0.0

        if gross_loss < 0:
            profit_factor = gross_profit / abs(gross_loss)
        else:
            profit_factor = gross_profit if gross_profit > 0 else 0.0

        coin_win_rate = {}
        for coin, closed in coin_closed.items():
            wins = coin_wins.get(coin, 0)
            coin_win_rate[coin] = (
                wins / closed * 100.0) if closed > 0 else 0.0

        return {
            'all_time_realized_pnl': all_time_realized_pnl,
            'total_realized_pnl': total_realized_pnl,
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': win_rate,
            'avg_pnl_per_trade': avg_pnl,
            'profit_factor': profit_factor,
            'sharpe_ratio': sharpe_ratio,
            'max_drawdown_usd': max_drawdown_usd,
            'max_drawdown_pct': max_drawdown_pct * 100.0,
            'coin_pnl': coin_pnl,
            'coin_closed': coin_closed,
            'coin_win_rate': coin_win_rate,
            'coin_buy_volume': coin_buy_volume,
            'coin_buy_count': coin_buy_count,
        }

    def format_prometheus_metrics(self, metrics):
        """Format metrics as Prometheus lines"""
        output = []
        output.append(
            '# HELP trading_realized_pnl_usd_all_time Total realized PnL in USD (all-time)')
        output.append('# TYPE trading_realized_pnl_usd_all_time gauge')
        output.append(
            f'trading_realized_pnl_usd_all_time {metrics["all_time_realized_pnl"]}')

        output.append(
            '# HELP trading_realized_pnl_usd Total realized PnL in USD (last 24h)')
        output.append('# TYPE trading_realized_pnl_usd gauge')
        output.append(
            f'trading_realized_pnl_usd {metrics["total_realized_pnl"]}')

        output.append(
            '# HELP trading_closed_trades Total closed trades (last 24h)')
        output.append('# TYPE trading_closed_trades gauge')
        output.append(f'trading_closed_trades {metrics["total_trades"]}')

        output.append(
            '# HELP trading_winning_trades Number of winning trades (last 24h)')
        output.append('# TYPE trading_winning_trades gauge')
        output.append(f'trading_winning_trades {metrics["winning_trades"]}')

        output.append(
            '# HELP trading_losing_trades Number of losing trades (last 24h)')
        output.append('# TYPE trading_losing_trades gauge')
        output.append(f'trading_losing_trades {metrics["losing_trades"]}')

        output.append('# HELP trading_win_rate Win rate percentage (last 24h)')
        output.append('# TYPE trading_win_rate gauge')
        output.append(f'trading_win_rate {metrics["win_rate"]}')

        output.append(
            '# HELP trading_avg_pnl_per_trade Average PnL per trade (last 24h)')
        output.append('# TYPE trading_avg_pnl_per_trade gauge')
        output.append(
            f'trading_avg_pnl_per_trade {metrics["avg_pnl_per_trade"]}')

        output.append(
            '# HELP trading_profit_factor Profit factor (gross profits / gross losses) over last 24h')
        output.append('# TYPE trading_profit_factor gauge')
        output.append(f'trading_profit_factor {metrics["profit_factor"]}')

        output.append(
            '# HELP trading_sharpe_ratio Trade-level Sharpe ratio over last 24h')
        output.append('# TYPE trading_sharpe_ratio gauge')
        output.append(f'trading_sharpe_ratio {metrics["sharpe_ratio"]}')

        output.append(
            '# HELP trading_max_drawdown_usd Maximum drawdown in USD over last 24h')
        output.append('# TYPE trading_max_drawdown_usd gauge')
        output.append(
            f'trading_max_drawdown_usd {metrics["max_drawdown_usd"]}')

        output.append(
            '# HELP trading_max_drawdown_pct Maximum drawdown in percent over last 24h')
        output.append('# TYPE trading_max_drawdown_pct gauge')
        output.append(
            f'trading_max_drawdown_pct {metrics["max_drawdown_pct"]}')

        output.append(
            '# HELP trading_portfolio_value_eur Latest portfolio total value from bot log (EUR)')
        output.append('# TYPE trading_portfolio_value_eur gauge')
        output.append(
            f'trading_portfolio_value_eur {metrics.get("portfolio_value_eur", 0.0)}')

        # Portfolio return (absolute and percent) relative to start value
        try:
            start_val = float(metrics.get(
                'portfolio_start_value_eur', 0.0) or 0.0)
            curr_val = float(metrics.get('portfolio_value_eur', 0.0) or 0.0)
            return_eur = curr_val - start_val
            return_pct = (return_eur / start_val *
                          100.0) if start_val > 0.0 else 0.0
        except Exception:
            return_eur = 0.0
            return_pct = 0.0

        output.append(
            '# HELP trading_portfolio_return_eur Absolute portfolio return since start (EUR)')
        output.append('# TYPE trading_portfolio_return_eur gauge')
        output.append(f'trading_portfolio_return_eur {return_eur}')

        output.append(
            '# HELP trading_portfolio_return_pct Portfolio return since start in percent (%%)')
        output.append('# TYPE trading_portfolio_return_pct gauge')
        output.append(f'trading_portfolio_return_pct {return_pct}')

        output.append(
            '# HELP trading_portfolio_cash_eur Latest portfolio cash from bot log (EUR)')
        output.append('# TYPE trading_portfolio_cash_eur gauge')
        output.append(
            f'trading_portfolio_cash_eur {metrics.get("portfolio_cash_eur", 0.0)}')

        output.append(
            '# HELP trading_portfolio_start_value_eur Portfolio start value from Kraken API or local fallback (EUR)')
        output.append('# TYPE trading_portfolio_start_value_eur gauge')
        output.append(
            f'trading_portfolio_start_value_eur {metrics.get("portfolio_start_value_eur", 0.0)}')

        output.append(
            '# HELP trading_portfolio_target_eur Portfolio target value in EUR (configurable via PORTFOLIO_TARGET_EUR)')
        output.append('# TYPE trading_portfolio_target_eur gauge')
        output.append(
            f'trading_portfolio_target_eur {metrics.get("portfolio_target_eur", 0.0)}')

        output.append(
            '# HELP trading_metrics_generated_unixtime Unix timestamp when the exporter generated this metrics payload')
        output.append('# TYPE trading_metrics_generated_unixtime gauge')
        output.append(
            f'trading_metrics_generated_unixtime {metrics.get("metrics_generated_unixtime", 0.0)}')

        output.append(
            '# HELP trading_portfolio_snapshot_unixtime Unix timestamp of the latest portfolio snapshot parsed from the bot log')
        output.append('# TYPE trading_portfolio_snapshot_unixtime gauge')
        output.append(
            f'trading_portfolio_snapshot_unixtime {metrics.get("portfolio_snapshot_timestamp_unixtime", 0.0)}')

        output.append(
            '# HELP trading_portfolio_snapshot_age_seconds Age in seconds of the latest portfolio snapshot parsed from the bot log')
        output.append('# TYPE trading_portfolio_snapshot_age_seconds gauge')
        output.append(
            f'trading_portfolio_snapshot_age_seconds {metrics.get("portfolio_snapshot_age_seconds", 0.0)}')

        output.append(
            '# HELP trading_current_holding_value_eur Current mark-to-market holding value per coin in EUR')
        output.append('# TYPE trading_current_holding_value_eur gauge')
        for coin, value in sorted(metrics.get('holdings_value_eur', {}).items()):
            output.append(
                f'trading_current_holding_value_eur{{coin="{coin}"}} {value}')

        output.append(
            '# HELP trading_current_holding_cost_basis_eur Original buy amount per currently held coin in EUR')
        output.append('# TYPE trading_current_holding_cost_basis_eur gauge')
        for coin, value in sorted(metrics.get('holdings_cost_basis_eur', {}).items()):
            output.append(
                f'trading_current_holding_cost_basis_eur{{coin="{coin}"}} {value}')

        output.append(
            '# HELP trading_current_holding_amount_coin Current held coin amount per open position')
        output.append('# TYPE trading_current_holding_amount_coin gauge')
        for coin, value in sorted(metrics.get('holdings_amount_coin', {}).items()):
            output.append(
                f'trading_current_holding_amount_coin{{coin="{coin}"}} {value}')

        output.append(
            '# HELP trading_current_holding_unrealized_pnl_eur Unrealized PnL per currently held coin in EUR')
        output.append(
            '# TYPE trading_current_holding_unrealized_pnl_eur gauge')
        for coin, value in sorted(metrics.get('holdings_unrealized_pnl_eur', {}).items()):
            output.append(
                f'trading_current_holding_unrealized_pnl_eur{{coin="{coin}"}} {value}')

        output.append(
            '# HELP trading_current_holdings_unrealized_pnl_total_eur Total unrealized PnL across current holdings in EUR')
        output.append(
            '# TYPE trading_current_holdings_unrealized_pnl_total_eur gauge')
        output.append(
            f'trading_current_holdings_unrealized_pnl_total_eur {sum(metrics.get("holdings_unrealized_pnl_eur", {}).values())}')

        output.append(
            '# HELP trading_open_positions_count Number of currently open positions')
        output.append('# TYPE trading_open_positions_count gauge')
        output.append(
            f'trading_open_positions_count {metrics.get("open_positions_count", 0)}')

        output.append(
            '# HELP trading_ai_copilot_budget_cap_usd Configured AI co-pilot monthly budget cap in USD')
        output.append('# TYPE trading_ai_copilot_budget_cap_usd gauge')
        output.append(
            f'trading_ai_copilot_budget_cap_usd {metrics.get("ai_copilot_budget_cap_usd", 0.0)}')

        output.append(
            '# HELP trading_ai_copilot_budget_used_usd AI co-pilot monthly spend in USD (current month)')
        output.append('# TYPE trading_ai_copilot_budget_used_usd gauge')
        output.append(
            f'trading_ai_copilot_budget_used_usd {metrics.get("ai_copilot_budget_used_usd", 0.0)}')

        output.append(
            '# HELP trading_ai_copilot_calls_used_monthly AI co-pilot monthly API calls used (current month)')
        output.append('# TYPE trading_ai_copilot_calls_used_monthly gauge')
        output.append(
            f'trading_ai_copilot_calls_used_monthly {metrics.get("ai_copilot_calls_used_monthly", 0.0)}')

        output.append(
            '# HELP trading_ai_copilot_calls_cap_monthly Configured AI co-pilot monthly API call cap')
        output.append('# TYPE trading_ai_copilot_calls_cap_monthly gauge')
        output.append(
            f'trading_ai_copilot_calls_cap_monthly {metrics.get("ai_copilot_calls_cap_monthly", 0.0)}')

        output.append(
            '# HELP trading_ai_copilot_model_info Current AI co-pilot model names by role')
        output.append('# TYPE trading_ai_copilot_model_info gauge')
        for item in metrics.get('ai_copilot_model_info', []):
            role = str(item.get('role', '')).replace(
                '\\', '\\\\').replace('"', '\\"')
            model = str(item.get('model', '')).replace(
                '\\', '\\\\').replace('"', '\\"')
            if not role or not model:
                continue
            output.append(
                f'trading_ai_copilot_model_info{{role="{role}",model="{model}"}} 1')

        output.append(
            '# HELP trading_ai_copilot_shadow_suggestion_value Latest AI shadow suggestion value by parameter and recency rank')
        output.append(
            '# TYPE trading_ai_copilot_shadow_suggestion_value gauge')
        for item in metrics.get('ai_copilot_shadow_suggestions', []):
            output.append(
                'trading_ai_copilot_shadow_suggestion_value{'
                f'suggestion_id="{item["suggestion_id"]}",'
                f'mode="{item["mode"]}",'
                f'parameter="{item["parameter"]}",'
                f'rank="{item["rank"]}"'
                f'}} {item["value"]}'
            )

        output.append(
            '# HELP trading_ai_copilot_shadow_suggestion_confidence Latest AI shadow suggestion confidence by parameter and recency rank')
        output.append(
            '# TYPE trading_ai_copilot_shadow_suggestion_confidence gauge')
        for item in metrics.get('ai_copilot_shadow_suggestions', []):
            output.append(
                'trading_ai_copilot_shadow_suggestion_confidence{'
                f'suggestion_id="{item["suggestion_id"]}",'
                f'mode="{item["mode"]}",'
                f'parameter="{item["parameter"]}",'
                f'rank="{item["rank"]}"'
                f'}} {item["confidence"]}'
            )

        output.append(
            '# HELP trading_ai_copilot_shadow_suggestion_age_minutes Minutes since the latest AI shadow suggestion by parameter and recency rank')
        output.append(
            '# TYPE trading_ai_copilot_shadow_suggestion_age_minutes gauge')
        for item in metrics.get('ai_copilot_shadow_suggestions', []):
            output.append(
                'trading_ai_copilot_shadow_suggestion_age_minutes{'
                f'suggestion_id="{item["suggestion_id"]}",'
                f'mode="{item["mode"]}",'
                f'parameter="{item["parameter"]}",'
                f'rank="{item["rank"]}"'
                f'}} {item["age_minutes"]}'
            )

        output.append(
            '# HELP trading_recent_pnl_guard_enabled Whether recent PnL guard is enabled (1=true, 0=false)')
        output.append('# TYPE trading_recent_pnl_guard_enabled gauge')
        output.append(
            f'trading_recent_pnl_guard_enabled {metrics.get("recent_pnl_guard_enabled", 0.0)}')

        output.append(
            '# HELP trading_recent_pnl_guard_active Whether recent PnL guard is currently active (1=true, 0=false)')
        output.append('# TYPE trading_recent_pnl_guard_active gauge')
        output.append(
            f'trading_recent_pnl_guard_active {metrics.get("recent_pnl_guard_active", 0.0)}')

        output.append(
            '# HELP trading_recent_pnl_guard_window Number of recent sells considered by the guard')
        output.append('# TYPE trading_recent_pnl_guard_window gauge')
        output.append(
            f'trading_recent_pnl_guard_window {metrics.get("recent_pnl_guard_window", 0.0)}')

        output.append(
            '# HELP trading_recent_pnl_guard_min_trades Minimum recent closed trades required for guard evaluation')
        output.append('# TYPE trading_recent_pnl_guard_min_trades gauge')
        output.append(
            f'trading_recent_pnl_guard_min_trades {metrics.get("recent_pnl_guard_min_trades", 0.0)}')

        output.append(
            '# HELP trading_recent_pnl_guard_recent_closed_trades Recent closed sells currently evaluated by the guard')
        output.append(
            '# TYPE trading_recent_pnl_guard_recent_closed_trades gauge')
        output.append(
            f'trading_recent_pnl_guard_recent_closed_trades {metrics.get("recent_pnl_guard_recent_closed_trades", 0.0)}')

        output.append(
            '# HELP trading_recent_pnl_guard_recent_realized_pnl Recent realized PnL in base currency used by the guard')
        output.append(
            '# TYPE trading_recent_pnl_guard_recent_realized_pnl gauge')
        output.append(
            f'trading_recent_pnl_guard_recent_realized_pnl {metrics.get("recent_pnl_guard_recent_realized_pnl", 0.0)}')

        output.append(
            '# HELP trading_recent_pnl_guard_recent_profit_factor Recent profit factor used by the guard')
        output.append(
            '# TYPE trading_recent_pnl_guard_recent_profit_factor gauge')
        output.append(
            f'trading_recent_pnl_guard_recent_profit_factor {metrics.get("recent_pnl_guard_recent_profit_factor", 0.0)}')

        output.append(
            '# HELP trading_recent_pnl_guard_min_realized_pnl Minimum recent realized PnL threshold configured for the guard')
        output.append('# TYPE trading_recent_pnl_guard_min_realized_pnl gauge')
        output.append(
            f'trading_recent_pnl_guard_min_realized_pnl {metrics.get("recent_pnl_guard_min_realized_pnl", 0.0)}')

        output.append(
            '# HELP trading_recent_pnl_guard_max_profit_factor Maximum recent profit factor threshold configured for the guard')
        output.append(
            '# TYPE trading_recent_pnl_guard_max_profit_factor gauge')
        output.append(
            f'trading_recent_pnl_guard_max_profit_factor {metrics.get("recent_pnl_guard_max_profit_factor", 0.0)}')

        output.append(
            '# HELP trading_recent_pnl_guard_reason Recent PnL guard status reason (labelled gauge set to 1 for active reason)')
        output.append('# TYPE trading_recent_pnl_guard_reason gauge')
        output.append(
            f'trading_recent_pnl_guard_reason{{reason="{str(metrics.get("recent_pnl_guard_reason", "unknown")).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"}} 1')

        output.append(
            '# HELP trading_coin_realized_pnl_usd Realized PnL per coin in USD (last 24h)')
        output.append('# TYPE trading_coin_realized_pnl_usd gauge')
        for coin, pnl in sorted(metrics['coin_pnl'].items()):
            output.append(
                f'trading_coin_realized_pnl_usd{{coin="{coin}"}} {pnl}')

        output.append(
            '# HELP trading_coin_closed_trades Closed trades per coin (last 24h)')
        output.append('# TYPE trading_coin_closed_trades gauge')
        for coin, count in sorted(metrics['coin_closed'].items()):
            output.append(
                f'trading_coin_closed_trades{{coin="{coin}"}} {count}')

        output.append(
            '# HELP trading_coin_win_rate Win rate per coin in percent (last 24h)')
        output.append('# TYPE trading_coin_win_rate gauge')
        for coin, rate in sorted(metrics['coin_win_rate'].items()):
            output.append(f'trading_coin_win_rate{{coin="{coin}"}} {rate}')

        output.append(
            '# HELP trading_coin_buy_volume_eur Buy volume per coin in EUR (last 24h)')
        output.append('# TYPE trading_coin_buy_volume_eur gauge')
        for coin, volume in sorted(metrics['coin_buy_volume'].items()):
            output.append(
                f'trading_coin_buy_volume_eur{{coin="{coin}"}} {volume}')

        output.append(
            '# HELP trading_coin_buy_count Buy count per coin (last 24h)')
        output.append('# TYPE trading_coin_buy_count gauge')
        for coin, count in sorted(metrics['coin_buy_count'].items()):
            output.append(f'trading_coin_buy_count{{coin="{coin}"}} {count}')

        return '\n'.join(output)

    def log_message(self, format, *args):
        pass  # Suppress logging


if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', 9200), MetricsHandler)
    print('PnL Metrics Exporter running on http://0.0.0.0:9200/metrics', file=sys.stderr)
    sys.stderr.flush()
    server.serve_forever()
