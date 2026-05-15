#!/usr/bin/env python3
"""
Export Trading Bot PnL metrics for Prometheus + Grafana
Reads trade_journal.csv and exports current balance status as Prometheus metrics
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


MIN_DRAWDOWN_PCT_BASE_USD = 1.0
BOT_LOG_PATH = '/opt/trading_2/logs/bot.log'
ENV_PATH = '/opt/trading_2/.env'
AI_COPILOT_STATE_PATH = '/opt/trading_2/ai_copilot_state.json'
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


def read_trades(journal_path):
    """Read all trades from journal"""
    trades = []
    try:
        with open(journal_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(row)
    except Exception as e:
        print(f"Error reading trades: {e}", file=sys.stderr)
        return []
    return trades


def parse_timestamp(timestamp_str):
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


def extract_return_pct(trade):
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


def calculate_pnl_metrics(trades, time_window_hours=24):
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

    equity = 0.0
    peak_equity = 0.0
    max_drawdown_usd = 0.0
    max_drawdown_pct = 0.0

    for trade in trades:
        try:
            timestamp_str = trade.get('timestamp', '')
            if not timestamp_str:
                continue

            ts = parse_timestamp(timestamp_str)
            if ts is None:
                continue

            action = trade.get('action', '').lower()
            coin = (trade.get('coin') or 'UNKNOWN').upper()

            if action == 'buy':
                if ts < cutoff:
                    continue
                try:
                    buy_volume = float(trade.get('amount_base', '0') or 0.0)
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

            per_trade_returns.append(extract_return_pct(trade))

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

    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
    avg_pnl = total_realized_pnl / total_trades if total_trades > 0 else 0

    if len(per_trade_returns) > 1:
        mean_ret = sum(per_trade_returns) / len(per_trade_returns)
        variance = sum((r - mean_ret) ** 2 for r in per_trade_returns) / \
            (len(per_trade_returns) - 1)
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
        coin_win_rate[coin] = (wins / closed * 100.0) if closed > 0 else 0.0

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


def read_latest_portfolio_snapshot(log_path=BOT_LOG_PATH):
    """Read latest portfolio value and cash from bot log."""
    snapshot = {
        'portfolio_value_eur': 0.0,
        'portfolio_cash_eur': 0.0,
        'holdings_amount_coin': {},
        'holdings_value_eur': {},
        'holdings_cost_basis_eur': {},
        'holdings_unrealized_pnl_eur': {},
        'open_positions_count': 0,
    }

    if not os.path.exists(log_path):
        return snapshot

    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            tail_lines = list(deque(f, maxlen=800))

        current_prices = {}
        open_trade_amount_base = {}
        open_trade_amount_coin = {}

        for line in reversed(tail_lines):
            if snapshot['portfolio_value_eur'] == 0.0 and 'Portfolio value:' in line:
                try:
                    value_part = line.split('Portfolio value:', 1)[1].strip()
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
                            current_prices[current_coin] = float(current_price)
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
    snapshot['open_positions_count'] = len(snapshot['holdings_amount_coin'])

    return snapshot


def read_portfolio_start_value(log_path=BOT_LOG_PATH):
    """Read initial dry-run portfolio cash from first initialization log line."""
    if not os.path.exists(log_path):
        return 0.0

    marker = 'Portfolio initialized from exchange (dry-run mode). Cash:'
    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                if marker not in line:
                    continue
                try:
                    cash_part = line.split('Cash:', 1)[1].strip()
                    return float(cash_part.split(' ')[0])
                except Exception:
                    continue
    except Exception:
        return 0.0
    return 0.0


def read_ai_copilot_usage(env_path=ENV_PATH, state_path=AI_COPILOT_STATE_PATH):
    """Read AI co-pilot caps and monthly usage state for monitoring."""
    result = {
        'ai_copilot_budget_cap_usd': 0.0,
        'ai_copilot_budget_used_usd': 0.0,
        'ai_copilot_calls_used_monthly': 0.0,
        'ai_copilot_calls_cap_monthly': 0.0,
    }

    try:
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8', errors='ignore') as f:
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
                        result['ai_copilot_calls_cap_monthly'] = float(value)
    except Exception:
        pass

    try:
        if os.path.exists(state_path):
            with open(state_path, 'r', encoding='utf-8') as f:
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


def read_ai_shadow_suggestions(log_path=BOT_LOG_PATH):
    result = {'ai_copilot_shadow_suggestions': []}

    if not os.path.exists(log_path):
        return result

    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            tail_lines = list(deque(f, maxlen=4000))

        now = datetime.utcnow()
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
                age_minutes = max(0.0, (now - ts).total_seconds() / 60.0)
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


def format_prometheus_metrics(metrics):
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
    output.append(f'trading_realized_pnl_usd {metrics["total_realized_pnl"]}')

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
    output.append(f'trading_avg_pnl_per_trade {metrics["avg_pnl_per_trade"]}')

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
    output.append(f'trading_max_drawdown_usd {metrics["max_drawdown_usd"]}')

    output.append(
        '# HELP trading_max_drawdown_pct Maximum drawdown in percent over last 24h')
    output.append('# TYPE trading_max_drawdown_pct gauge')
    output.append(f'trading_max_drawdown_pct {metrics["max_drawdown_pct"]}')

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
    output.append('# TYPE trading_current_holding_unrealized_pnl_eur gauge')
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
        '# HELP trading_ai_copilot_shadow_suggestion_value Latest AI shadow suggestion value by parameter and recency rank')
    output.append('# TYPE trading_ai_copilot_shadow_suggestion_value gauge')
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
        '# HELP trading_coin_realized_pnl_usd Realized PnL per coin in USD (last 24h)')
    output.append('# TYPE trading_coin_realized_pnl_usd gauge')
    for coin, pnl in sorted(metrics['coin_pnl'].items()):
        output.append(f'trading_coin_realized_pnl_usd{{coin="{coin}"}} {pnl}')

    output.append(
        '# HELP trading_coin_closed_trades Closed trades per coin (last 24h)')
    output.append('# TYPE trading_coin_closed_trades gauge')
    for coin, count in sorted(metrics['coin_closed'].items()):
        output.append(f'trading_coin_closed_trades{{coin="{coin}"}} {count}')

    output.append(
        '# HELP trading_coin_win_rate Win rate per coin in percent (last 24h)')
    output.append('# TYPE trading_coin_win_rate gauge')
    for coin, rate in sorted(metrics['coin_win_rate'].items()):
        output.append(f'trading_coin_win_rate{{coin="{coin}"}} {rate}')

    output.append(
        '# HELP trading_coin_buy_volume_eur Buy volume per coin in EUR (last 24h)')
    output.append('# TYPE trading_coin_buy_volume_eur gauge')
    for coin, volume in sorted(metrics['coin_buy_volume'].items()):
        output.append(f'trading_coin_buy_volume_eur{{coin="{coin}"}} {volume}')

    output.append(
        '# HELP trading_coin_buy_count Buy count per coin (last 24h)')
    output.append('# TYPE trading_coin_buy_count gauge')
    for coin, count in sorted(metrics['coin_buy_count'].items()):
        output.append(f'trading_coin_buy_count{{coin="{coin}"}} {count}')

    return '\n'.join(output)


if __name__ == '__main__':
    journal_path = sys.argv[1] if len(
        sys.argv) > 1 else '/opt/trading_2/trade_journal.csv'

    trades = read_trades(journal_path)
    metrics = calculate_pnl_metrics(trades, time_window_hours=24)
    metrics.update(read_latest_portfolio_snapshot())
    metrics['portfolio_start_value_eur'] = read_portfolio_start_value()
    metrics.update(read_ai_copilot_usage())
    metrics.update(read_ai_shadow_suggestions())
    output = format_prometheus_metrics(metrics)

    print(output)
