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
from collections import deque
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

JOURNAL_PATH = '/opt/trading_2/trade_journal.csv'
BOT_LOG_PATH = '/opt/trading_2/logs/bot.log'
ENV_PATH = '/opt/trading_2/.env'
AI_COPILOT_STATE_PATH = '/opt/trading_2/ai_copilot_state.json'
MIN_DRAWDOWN_PCT_BASE_USD = 1.0
START_VALUE_CACHE = {
    'log_mtime': None,
    'value': 0.0,
}
CURRENT_PRICE_RE = re.compile(
    r'📊\s+([A-Z0-9]+):\s+Buy\s+[0-9.]+\s+→\s+Current\s+([0-9.]+)'
)
OPEN_TRADE_AMOUNT_BASE_RE = re.compile(
    r"'([A-Z0-9]+)'\s*:\s*\{[^}]*'amount_base'\s*:\s*([0-9.eE+-]+)"
)


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/metrics':
            metrics = self.get_metrics()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4')
            self.end_headers()
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
        metrics_dict['portfolio_start_value_eur'] = self.read_portfolio_start_value()
        metrics_dict.update(self.read_ai_copilot_usage())
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

    def _parse_timestamp(self, timestamp_str):
        """Parse ISO timestamps with or without microseconds."""
        ts = (timestamp_str or '').strip()
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            try:
                return datetime.fromisoformat(ts.split('.')[0])
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

    def read_latest_portfolio_snapshot(self):
        """Read latest portfolio value and cash from bot log."""
        snapshot = {
            'portfolio_value_eur': 0.0,
            'portfolio_cash_eur': 0.0,
            'holdings_amount_coin': {},
            'holdings_value_eur': {},
        }

        if not os.path.exists(BOT_LOG_PATH):
            return snapshot

        try:
            with open(BOT_LOG_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                tail_lines = list(deque(f, maxlen=800))

            current_prices = {}
            open_trade_amount_base = {}

            for line in reversed(tail_lines):
                if snapshot['portfolio_value_eur'] == 0.0 and 'Portfolio value:' in line:
                    try:
                        value_part = line.split(
                            'Portfolio value:', 1)[1].strip()
                        snapshot['portfolio_value_eur'] = float(
                            value_part.split(' ')[0])
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

        return snapshot

    def read_portfolio_start_value(self):
        """Read initial dry-run portfolio cash from first initialization log line."""
        if not os.path.exists(BOT_LOG_PATH):
            return 0.0

        try:
            mtime = os.path.getmtime(BOT_LOG_PATH)
            if START_VALUE_CACHE['log_mtime'] == mtime:
                return START_VALUE_CACHE['value']

            value = 0.0
            marker = 'Portfolio initialized from exchange (dry-run mode). Cash:'
            with open(BOT_LOG_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if marker not in line:
                        continue
                    try:
                        cash_part = line.split('Cash:', 1)[1].strip()
                        value = float(cash_part.split(' ')[0])
                        break
                    except Exception:
                        continue

            START_VALUE_CACHE['log_mtime'] = mtime
            START_VALUE_CACHE['value'] = value
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
            if os.path.exists(ENV_PATH):
                with open(ENV_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#') or '=' not in line:
                            continue
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key == 'AI_COPILOT_MAX_BUDGET_USD_PER_MONTH':
                            result['ai_copilot_budget_cap_usd'] = float(value)
                        elif key == 'AI_COPILOT_MAX_CALLS_PER_MONTH':
                            result['ai_copilot_calls_cap_monthly'] = float(
                                value)
        except Exception:
            pass

        try:
            if os.path.exists(AI_COPILOT_STATE_PATH):
                with open(AI_COPILOT_STATE_PATH, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                now = datetime.utcnow()
                month_key = f"{now.year:04d}-{now.month:02d}"
                if state.get('month_key') == month_key:
                    result['ai_copilot_budget_cap_usd'] = float(
                        state.get('budget_cap_usd', result['ai_copilot_budget_cap_usd']) or result['ai_copilot_budget_cap_usd'])
                    result['ai_copilot_calls_cap_monthly'] = float(
                        state.get('calls_cap_monthly', result['ai_copilot_calls_cap_monthly']) or result['ai_copilot_calls_cap_monthly'])
                    result['ai_copilot_budget_used_usd'] = float(
                        state.get('monthly_spend_usd', 0.0) or 0.0)
                    result['ai_copilot_calls_used_monthly'] = float(
                        state.get('monthly_calls', 0) or 0)
        except Exception:
            pass

        return result

    def calculate_pnl_metrics(self, trades, time_window_hours=24):
        """Calculate PnL metrics from trades"""
        now = datetime.utcnow()
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

        output.append(
            '# HELP trading_portfolio_cash_eur Latest portfolio cash from bot log (EUR)')
        output.append('# TYPE trading_portfolio_cash_eur gauge')
        output.append(
            f'trading_portfolio_cash_eur {metrics.get("portfolio_cash_eur", 0.0)}')

        output.append(
            '# HELP trading_portfolio_start_value_eur Initial dry-run portfolio value from startup log (EUR)')
        output.append('# TYPE trading_portfolio_start_value_eur gauge')
        output.append(
            f'trading_portfolio_start_value_eur {metrics.get("portfolio_start_value_eur", 0.0)}')

        output.append(
            '# HELP trading_current_holding_value_eur Current mark-to-market holding value per coin in EUR')
        output.append('# TYPE trading_current_holding_value_eur gauge')
        for coin, value in sorted(metrics.get('holdings_value_eur', {}).items()):
            output.append(
                f'trading_current_holding_value_eur{{coin="{coin}"}} {value}')

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
