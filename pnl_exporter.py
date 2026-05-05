#!/usr/bin/env python3
"""
Simple Prometheus exporter for Trading Bot PnL metrics
Listens on http://localhost:9200/metrics
"""
import csv
import math
import sys
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler

JOURNAL_PATH = '/opt/trading_2/trade_journal.csv'
MIN_DRAWDOWN_PCT_BASE_USD = 1.0


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

        # Equity curve from closed-trade realized PnL over the window.
        equity = 0.0
        peak_equity = 0.0
        max_drawdown_usd = 0.0
        max_drawdown_pct = 0.0

        for trade in trades:
            try:
                timestamp_str = trade.get('timestamp', '')
                action = trade.get('action', '').lower()
                if action != 'sell':
                    continue

                ts = self._parse_timestamp(timestamp_str)
                if ts is None:
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

                coin = (trade.get('coin') or 'UNKNOWN').upper()
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
                    drawdown_pct = drawdown_usd / max(peak_equity, MIN_DRAWDOWN_PCT_BASE_USD)
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

        return '\n'.join(output)

    def log_message(self, format, *args):
        pass  # Suppress logging


if __name__ == '__main__':
    server = HTTPServer(('0.0.0.0', 9200), MetricsHandler)
    print('PnL Metrics Exporter running on http://0.0.0.0:9200/metrics', file=sys.stderr)
    sys.stderr.flush()
    server.serve_forever()
