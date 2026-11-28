#!/usr/bin/env python3
"""
Export Trading Bot PnL metrics for Prometheus + Grafana
Reads trade_journal.csv and exports current balance status as Prometheus metrics
"""
import ast
import csv
import json
import os
import re
import sys
from collections import deque
from datetime import UTC, datetime

from .ai_copilot_log import read_ai_shadow_suggestions
from .env_file import read_env_value as _read_env_value
from .exporter_paths import (
    AI_COPILOT_STATE_PATH,
    BOT_LOG_PATH,
    ENV_PATH,
    PORTFOLIO_STATE_PATH,
)
from .pnl_metrics import calculate_pnl_metrics
from .pnl_prometheus_format import format_prometheus_metrics
from .recent_pnl_guard import compute_recent_pnl_guard_state

CURRENT_PRICE_RE = re.compile(
    r'📊\s+([A-Z0-9]+):\s+Buy\s+[0-9.]+\s+→\s+Current\s+([0-9.]+)'
)
OPEN_TRADE_AMOUNT_BASE_RE = re.compile(
    r"'([A-Z0-9]+)'\s*:\s*\{[^}]*'amount_base'\s*:\s*([0-9.eE+-]+)"
)
OPEN_TRADE_AMOUNT_COIN_RE = re.compile(
    r"'([A-Z0-9]+)'\s*:\s*\{[^}]*'amount_coin'\s*:\s*([0-9.eE+-]+)"
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


def read_ai_copilot_usage(  # noqa: C901
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
