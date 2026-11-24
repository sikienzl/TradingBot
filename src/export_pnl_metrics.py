#!/usr/bin/env python3
"""
Export Trading Bot PnL metrics for Prometheus + Grafana
Reads trade_journal.csv and exports current balance status as Prometheus metrics
"""
import ast
import csv
import json
import math
import os
import re
import sys
from collections import deque
from datetime import UTC, datetime, timedelta

from .pnl_prometheus_format import format_prometheus_metrics

MIN_DRAWDOWN_PCT_BASE_USD = 1.0
BOT_LOG_PATH = '/opt/trading_2/logs/bot.log'
ENV_PATH = '/opt/trading_2/.env'
AI_COPILOT_STATE_PATH = '/opt/trading_2/ai_copilot_state.json'
PORTFOLIO_STATE_PATH = '/opt/trading_2/.portfolio_state.json'
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


def read_trades(journal_path, *, quiet=False):
    """Read all trades from journal.

    ``quiet`` suppresses the stderr diagnostic so the HTTP exporter, which
    polls on a timer and intentionally stays silent, can reuse this helper.
    """
    trades = []
    try:
        with open(journal_path, 'r') as f:
            reader = csv.DictReader(f)
            trades = list(reader)
    except (OSError, UnicodeError, csv.Error) as e:
        if not quiet:
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
        except ValueError:
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
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=time_window_hours)

    all_time_realized_pnl = 0.0
    total_realized_pnl = 0.0
    total_trades = 0
    winning_trades = 0
    losing_trades = 0
    break_even_trades = 0
    gross_profit = 0.0
    gross_loss = 0.0
    per_trade_returns = []

    coin_pnl = {}
    coin_closed = {}
    coin_wins = {}
    coin_break_even = {}
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
            coin_break_even.setdefault(coin, 0)

            if pnl > 0:
                winning_trades += 1
                gross_profit += pnl
                coin_wins[coin] = coin_wins.get(coin, 0) + 1
            elif pnl < 0:
                losing_trades += 1
                gross_loss += pnl
            else:
                break_even_trades += 1
                coin_break_even[coin] = coin_break_even.get(coin, 0) + 1

            per_trade_returns.append(extract_return_pct(trade))

            equity += pnl
            peak_equity = max(peak_equity, equity)
            drawdown_usd = peak_equity - equity
            max_drawdown_usd = max(max_drawdown_usd, drawdown_usd)
            if peak_equity > 0:
                # Prevent unrealistic percentages when peak equity is near zero.
                drawdown_pct = drawdown_usd / \
                    max(peak_equity, MIN_DRAWDOWN_PCT_BASE_USD)
                max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError, OverflowError):
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
        'break_even_trades': break_even_trades,
        'win_rate': win_rate,
        'avg_pnl_per_trade': avg_pnl,
        'profit_factor': profit_factor,
        'sharpe_ratio': sharpe_ratio,
        'max_drawdown_usd': max_drawdown_usd,
        'max_drawdown_pct': max_drawdown_pct * 100.0,
        'coin_pnl': coin_pnl,
        'coin_closed': coin_closed,
        'coin_win_rate': coin_win_rate,
        'coin_break_even': coin_break_even,
        'coin_buy_volume': coin_buy_volume,
        'coin_buy_count': coin_buy_count,
    }


def read_latest_portfolio_snapshot(log_path=BOT_LOG_PATH, *, on_portfolio_value=None):
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
        current_prices, open_trade_amount_base, open_trade_amount_coin = (
            _scan_portfolio_tail(tail_lines, snapshot, on_portfolio_value))
    except (OSError, UnicodeError):
        return snapshot

    _aggregate_portfolio_holdings(
        snapshot, current_prices, open_trade_amount_base,
        open_trade_amount_coin)

    return snapshot

def _scan_portfolio_tail(
    tail_lines: list[str],
    snapshot: dict,
    on_portfolio_value,
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Scan the log tail for value, cash, holdings, open trades and prices.

    Mutates *snapshot* in place and returns (current_prices,
    open_trade_amount_base, open_trade_amount_coin).
    """
    current_prices: dict[str, float] = {}
    open_trade_amount_base: dict[str, float] = {}
    open_trade_amount_coin: dict[str, float] = {}
    portfolio_value_found = False
    portfolio_cash_found = False

    for line in reversed(tail_lines):
        if not portfolio_value_found and 'Portfolio value:' in line:
            portfolio_value_found = _parse_portfolio_value_line(
                line, snapshot, on_portfolio_value)

        if not portfolio_cash_found and '  - Cash:' in line:
            portfolio_cash_found = _parse_cash_line(line, snapshot)

        if not snapshot['holdings_amount_coin'] and '  - Holdings:' in line:
            _parse_holdings_line(line, snapshot)

        if '  - Open trades details:' in line:
            _parse_open_trades_line(
                line, open_trade_amount_base, open_trade_amount_coin)

        if '→ Current ' in line:
            _parse_current_price_line(line, current_prices)

        if _portfolio_scan_complete(
            snapshot, current_prices, open_trade_amount_base,
            portfolio_value_found, portfolio_cash_found,
        ):
            break
    return current_prices, open_trade_amount_base, open_trade_amount_coin


def _parse_portfolio_value_line(
    line: str, snapshot: dict, on_portfolio_value
) -> bool:
    """Parse a 'Portfolio value:' line into *snapshot*. True if parsed."""
    found = False
    try:
        value_part = line.split('Portfolio value:', 1)[1].strip()
        snapshot['portfolio_value_eur'] = float(
            value_part.split(' ')[0])
        found = True
        if on_portfolio_value is not None:
            on_portfolio_value(line)
    except (IndexError, ValueError) as e:
        print(f"Error parsing portfolio value: {e}", file=sys.stderr)
    return found


def _parse_cash_line(line: str, snapshot: dict) -> bool:
    """Parse a 'Cash:' line into *snapshot*. True if parsed."""
    try:
        cash_part = line.split('Cash:', 1)[1].strip()
        snapshot['portfolio_cash_eur'] = float(
            cash_part.split(' ')[0])
        return True
    except (IndexError, ValueError) as e:
        print(f"Error parsing portfolio cash: {e}", file=sys.stderr)
    return False


def _parse_holdings_line(line: str, snapshot: dict) -> None:
    """Parse a 'Holdings:' line into *snapshot*."""
    try:
        holdings_part = line.split('Holdings:', 1)[1].strip()
        parsed = ast.literal_eval(holdings_part)
        if isinstance(parsed, dict):
            snapshot['holdings_amount_coin'] = {
                str(coin).upper(): float(amount)
                for coin, amount in parsed.items()
                if float(amount) > 0.0
            }
    except (SyntaxError, TypeError, ValueError):
        pass


def _parse_open_trades_line(
    line: str,
    open_trade_amount_base: dict[str, float],
    open_trade_amount_coin: dict[str, float],
) -> None:
    """Parse an 'Open trades details:' line into the amount dicts."""
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


def _parse_current_price_line(
    line: str, current_prices: dict[str, float]
) -> None:
    """Parse a current-price line into *current_prices*."""
    match = CURRENT_PRICE_RE.search(line)
    if match:
        coin, current_price = match.groups()
        current_coin = coin.upper()
        if current_coin not in current_prices:
            try:
                current_prices[current_coin] = float(current_price)
            except ValueError:
                pass


def _portfolio_scan_complete(
    snapshot: dict,
    current_prices: dict[str, float],
    open_trade_amount_base: dict[str, float],
    portfolio_value_found: bool,
    portfolio_cash_found: bool,
) -> bool:
    """Return True once the tail scan has collected everything needed."""
    if not (portfolio_value_found and portfolio_cash_found):
        return False
    if not snapshot['holdings_amount_coin']:
        return True
    return all(
        coin in current_prices or coin in open_trade_amount_base
        for coin in snapshot['holdings_amount_coin']
    )


def _aggregate_portfolio_holdings(
    snapshot: dict,
    current_prices: dict[str, float],
    open_trade_amount_base: dict[str, float],
    open_trade_amount_coin: dict[str, float],
) -> None:
    """Fill holdings value, cost basis and unrealized PnL into *snapshot*."""
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


def _scan_portfolio_start_value_from_log(log_path):
    """Return the cash value from the first portfolio-initialisation log line.

    Returns ``None`` when the marker line is absent or unparseable so callers
    can apply their own fallback (direct read vs. cached read).
    """
    marker = 'Portfolio initialized from exchange (dry-run mode). Cash:'
    if not os.path.exists(log_path):
        return None
    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if marker not in line:
                continue
            try:
                cash_part = line.split('Cash:', 1)[1].strip()
                return float(cash_part.split(' ')[0])
            except (IndexError, ValueError):
                continue
    return None


def read_portfolio_start_value(log_path=BOT_LOG_PATH):
    """Read initial dry-run portfolio cash from first initialization log line."""
    try:
        value = _scan_portfolio_start_value_from_log(log_path)
        if value is not None:
            return value

        if os.path.exists(PORTFOLIO_STATE_PATH):
            with open(PORTFOLIO_STATE_PATH, 'r', encoding='utf-8', errors='ignore') as f:
                state = json.load(f)
            return float(state.get('initial_portfolio_value') or 0.0)
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return 0.0
    return 0.0


def read_ai_copilot_usage(
    env_path=ENV_PATH,
    state_path=AI_COPILOT_STATE_PATH,
    *,
    quiet=False,
    backup_state_path=None,
):
    """Read AI co-pilot caps and monthly usage state for monitoring.

    ``quiet`` suppresses the stderr diagnostics so the HTTP exporter, which
    polls on a timer and intentionally stays silent, can reuse this reader.
    ``backup_state_path`` is an optional ``.bak`` fallback that is tried when
    the primary state file is missing or stale.
    """
    result = {
        'ai_copilot_budget_cap_usd': 0.0,
        'ai_copilot_budget_used_usd': 0.0,
        'ai_copilot_calls_used_monthly': 0.0,
        'ai_copilot_calls_cap_monthly': 0.0,
    }

    try:
        budget_cap = _read_env_value(
            'AI_COPILOT_MAX_BUDGET_USD_PER_MONTH', env_path, quiet=quiet)
        calls_cap = _read_env_value(
            'AI_COPILOT_MAX_CALLS_PER_MONTH', env_path, quiet=quiet)
        if budget_cap:
            result['ai_copilot_budget_cap_usd'] = float(budget_cap)
        if calls_cap:
            result['ai_copilot_calls_cap_monthly'] = float(calls_cap)
    except (TypeError, ValueError) as exc:
        if not quiet:
            print(f"Error reading AI co-pilot environment: {exc}", file=sys.stderr)

    state_candidates = [state_path] if backup_state_path is None else [
        state_path, backup_state_path]
    try:
        for candidate in state_candidates:
            if not os.path.exists(candidate):
                continue
            with open(candidate, 'r', encoding='utf-8') as f:
                state = json.load(f)
            now = datetime.now(UTC)
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
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
        if not quiet:
            print(f"Error reading AI co-pilot state: {exc}", file=sys.stderr)

    return result


def read_ai_shadow_suggestions(log_path=BOT_LOG_PATH):
    result = {'ai_copilot_shadow_suggestions': []}

    if not os.path.exists(log_path):
        return result

    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            tail_lines = list(deque(f, maxlen=4000))

        now = datetime.now(UTC)
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
            except (IndexError, SyntaxError, ValueError):
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
    except (OSError, UnicodeError) as exc:
        print(f"Error reading AI co-pilot suggestions: {exc}", file=sys.stderr)

    return result


def _read_env_value(env_key, env_path=ENV_PATH, *, quiet=False):
    try:
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    if key.strip() != env_key:
                        continue
                    return value.strip().strip('"').strip("'")
    except (OSError, UnicodeError) as exc:
        if not quiet:
            print(f"Error reading environment file: {exc}", file=sys.stderr)
    return ''


def _env_bool(env_key, default=False, env_path=ENV_PATH):
    value = _read_env_value(env_key, env_path)
    if value == '':
        return bool(default)
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _safe_int_env(env_key, default, env_path=ENV_PATH):
    value = _read_env_value(env_key, env_path)
    if value == '':
        return int(default)
    try:
        return int(value)
    except ValueError:
        return int(default)


def _safe_float_env(env_key, default, env_path=ENV_PATH):
    value = _read_env_value(env_key, env_path)
    if value == '':
        return float(default)
    try:
        return float(value)
    except ValueError:
        return float(default)


def compute_recent_pnl_guard_state(trades, env_path=ENV_PATH):
    enabled = _env_bool('RECENT_PNL_GUARD_ENABLED', True, env_path=env_path)
    window = max(1, _safe_int_env(
        'RECENT_PNL_GUARD_WINDOW', 120, env_path=env_path))
    min_trades = max(1, _safe_int_env(
        'RECENT_PNL_GUARD_MIN_TRADES', 60, env_path=env_path))
    min_realized_pnl = _safe_float_env(
        'RECENT_PNL_GUARD_MIN_REALIZED_PNL', 0.0, env_path=env_path)
    max_profit_factor = _safe_float_env(
        'RECENT_PNL_GUARD_MAX_PROFIT_FACTOR', 1.0, env_path=env_path)

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


if __name__ == '__main__':
    journal_path = sys.argv[1] if len(
        sys.argv) > 1 else '/opt/trading_2/trade_journal.csv'

    trades = read_trades(journal_path)
    metrics = calculate_pnl_metrics(trades, time_window_hours=24)
    metrics.update(compute_recent_pnl_guard_state(trades))
    metrics.update(read_latest_portfolio_snapshot())
    metrics['portfolio_start_value_eur'] = read_portfolio_start_value()
    metrics.update(read_ai_copilot_usage())
    metrics.update(read_ai_shadow_suggestions())
    output = format_prometheus_metrics(metrics)

    print(output)
