"""Render PnL metric dictionaries as Prometheus exposition text.

The module is pure: it takes the metrics dictionary produced by
:func:`src.export_pnl_metrics.calculate_pnl_metrics` (plus the portfolio,
AI co-pilot and recent-PnL-guard keys that the callers attach) and emits the
``# HELP`` / ``# TYPE`` / sample lines that Prometheus scrapes. Keeping the
rendering separate from the readers makes both sides testable on their own.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "escape_label_value",
    "format_prometheus_metrics",
]


def escape_label_value(value: Any) -> str:
    """Escape a Prometheus label value (backslash, quote, newline)."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _gauge(output: list[str], name: str, help_text: str, value: Any) -> None:
    """Append a single unlabelled gauge with its HELP and TYPE headers."""
    output.append(f'# HELP {name} {help_text}')
    output.append(f'# TYPE {name} gauge')
    output.append(f'{name} {value}')


def _labelled_gauge(
    output: list[str],
    name: str,
    help_text: str,
    entries: Mapping[str, Any],
) -> None:
    """Append one gauge per key of *entries*, sorted, with a ``coin`` label."""
    output.append(f'# HELP {name} {help_text}')
    output.append(f'# TYPE {name} gauge')
    for key, value in sorted(entries.items()):
        output.append(f'{name}{{coin="{key}"}} {value}')


def _trade_metrics(output: list[str], metrics: Mapping[str, Any]) -> None:
    _gauge(
        output,
        'trading_realized_pnl_usd_all_time',
        'Total realized PnL in USD (all-time)',
        metrics['all_time_realized_pnl'],
    )
    _gauge(
        output,
        'trading_realized_pnl_usd',
        'Total realized PnL in USD (last 24h)',
        metrics['total_realized_pnl'],
    )
    _gauge(
        output,
        'trading_closed_trades',
        'Total closed trades (last 24h)',
        metrics['total_trades'],
    )
    _gauge(
        output,
        'trading_winning_trades',
        'Number of winning trades (last 24h)',
        metrics['winning_trades'],
    )
    _gauge(
        output,
        'trading_losing_trades',
        'Number of losing trades (last 24h)',
        metrics['losing_trades'],
    )
    _gauge(
        output,
        'trading_break_even_trades',
        'Number of break-even trades (last 24h)',
        metrics['break_even_trades'],
    )
    _gauge(
        output,
        'trading_win_rate',
        'Win rate percentage (last 24h)',
        metrics['win_rate'],
    )
    _gauge(
        output,
        'trading_avg_pnl_per_trade',
        'Average PnL per trade (last 24h)',
        metrics['avg_pnl_per_trade'],
    )
    _gauge(
        output,
        'trading_profit_factor',
        'Profit factor (gross profits / gross losses) over last 24h',
        metrics['profit_factor'],
    )
    _gauge(
        output,
        'trading_sharpe_ratio',
        'Trade-level Sharpe ratio over last 24h',
        metrics['sharpe_ratio'],
    )
    _gauge(
        output,
        'trading_max_drawdown_usd',
        'Maximum drawdown in USD over last 24h',
        metrics['max_drawdown_usd'],
    )
    _gauge(
        output,
        'trading_max_drawdown_pct',
        'Maximum drawdown in percent over last 24h',
        metrics['max_drawdown_pct'],
    )


def _portfolio_metrics(output: list[str], metrics: Mapping[str, Any]) -> None:
    _gauge(
        output,
        'trading_portfolio_value_eur',
        'Latest portfolio total value from bot log (EUR)',
        metrics.get('portfolio_value_eur', 0.0),
    )
    _gauge(
        output,
        'trading_portfolio_cash_eur',
        'Latest portfolio cash from bot log (EUR)',
        metrics.get('portfolio_cash_eur', 0.0),
    )
    _gauge(
        output,
        'trading_portfolio_start_value_eur',
        'Initial dry-run portfolio value from startup log (EUR)',
        metrics.get('portfolio_start_value_eur', 0.0),
    )

    # Snapshot freshness metrics are only emitted by the HTTP exporter, which
    # attaches them to its metrics dict; the one-shot script omits them.
    if 'metrics_generated_unixtime' in metrics:
        _gauge(
            output,
            'trading_metrics_generated_unixtime',
            'Unix timestamp when the exporter generated this metrics payload',
            metrics.get('metrics_generated_unixtime', 0.0),
        )
    if 'portfolio_snapshot_timestamp_unixtime' in metrics:
        _gauge(
            output,
            'trading_portfolio_snapshot_unixtime',
            'Unix timestamp of the latest portfolio snapshot parsed from the bot log',
            metrics.get('portfolio_snapshot_timestamp_unixtime', 0.0),
        )
    if 'portfolio_snapshot_age_seconds' in metrics:
        _gauge(
            output,
            'trading_portfolio_snapshot_age_seconds',
            'Age in seconds of the latest portfolio snapshot parsed from the bot log',
            metrics.get('portfolio_snapshot_age_seconds', 0.0),
        )

    _labelled_gauge(
        output,
        'trading_current_holding_value_eur',
        'Current mark-to-market holding value per coin in EUR',
        metrics.get('holdings_value_eur', {}),
    )
    _labelled_gauge(
        output,
        'trading_current_holding_cost_basis_eur',
        'Original buy amount per currently held coin in EUR',
        metrics.get('holdings_cost_basis_eur', {}),
    )
    _labelled_gauge(
        output,
        'trading_current_holding_amount_coin',
        'Current held coin amount per open position',
        metrics.get('holdings_amount_coin', {}),
    )
    _labelled_gauge(
        output,
        'trading_current_holding_unrealized_pnl_eur',
        'Unrealized PnL per currently held coin in EUR',
        metrics.get('holdings_unrealized_pnl_eur', {}),
    )
    _gauge(
        output,
        'trading_current_holdings_unrealized_pnl_total_eur',
        'Total unrealized PnL across current holdings in EUR',
        sum(metrics.get('holdings_unrealized_pnl_eur', {}).values()),
    )
    _gauge(
        output,
        'trading_open_positions_count',
        'Number of currently open positions',
        metrics.get('open_positions_count', 0),
    )


def _ai_copilot_metrics(output: list[str], metrics: Mapping[str, Any]) -> None:
    _gauge(
        output,
        'trading_ai_copilot_budget_cap_usd',
        'Configured AI co-pilot monthly budget cap in USD',
        metrics.get('ai_copilot_budget_cap_usd', 0.0),
    )
    _gauge(
        output,
        'trading_ai_copilot_budget_used_usd',
        'AI co-pilot monthly spend in USD (current month)',
        metrics.get('ai_copilot_budget_used_usd', 0.0),
    )
    _gauge(
        output,
        'trading_ai_copilot_calls_used_monthly',
        'AI co-pilot monthly API calls used (current month)',
        metrics.get('ai_copilot_calls_used_monthly', 0.0),
    )
    _gauge(
        output,
        'trading_ai_copilot_calls_cap_monthly',
        'Configured AI co-pilot monthly API call cap',
        metrics.get('ai_copilot_calls_cap_monthly', 0.0),
    )


def _shadow_suggestion_metrics(output: list[str], metrics: Mapping[str, Any]) -> None:
    suggestions = metrics.get('ai_copilot_shadow_suggestions', [])
    series = (
        (
            'trading_ai_copilot_shadow_suggestion_value',
            'Latest AI shadow suggestion value by parameter and recency rank',
            'value',
        ),
        (
            'trading_ai_copilot_shadow_suggestion_confidence',
            'Latest AI shadow suggestion confidence by parameter and recency rank',
            'confidence',
        ),
        (
            'trading_ai_copilot_shadow_suggestion_age_minutes',
            'Minutes since the latest AI shadow suggestion by parameter and recency rank',
            'age_minutes',
        ),
    )
    for name, help_text, field in series:
        output.append(f'# HELP {name} {help_text}')
        output.append(f'# TYPE {name} gauge')
        for item in suggestions:
            output.append(
                f'{name}{{'
                f'suggestion_id="{item["suggestion_id"]}",'
                f'mode="{item["mode"]}",'
                f'parameter="{item["parameter"]}",'
                f'rank="{item["rank"]}"'
                f'}} {item[field]}'
            )


def _recent_pnl_guard_metrics(output: list[str], metrics: Mapping[str, Any]) -> None:
    gauges = (
        (
            'trading_recent_pnl_guard_enabled',
            'Whether recent PnL guard is enabled (1=true, 0=false)',
            metrics.get('recent_pnl_guard_enabled', 0.0),
        ),
        (
            'trading_recent_pnl_guard_active',
            'Whether recent PnL guard is currently active (1=true, 0=false)',
            metrics.get('recent_pnl_guard_active', 0.0),
        ),
        (
            'trading_recent_pnl_guard_window',
            'Number of recent sells considered by the guard',
            metrics.get('recent_pnl_guard_window', 0.0),
        ),
        (
            'trading_recent_pnl_guard_min_trades',
            'Minimum recent closed trades required for guard evaluation',
            metrics.get('recent_pnl_guard_min_trades', 0.0),
        ),
        (
            'trading_recent_pnl_guard_recent_closed_trades',
            'Recent closed sells currently evaluated by the guard',
            metrics.get('recent_pnl_guard_recent_closed_trades', 0.0),
        ),
        (
            'trading_recent_pnl_guard_recent_realized_pnl',
            'Recent realized PnL in base currency used by the guard',
            metrics.get('recent_pnl_guard_recent_realized_pnl', 0.0),
        ),
        (
            'trading_recent_pnl_guard_recent_profit_factor',
            'Recent profit factor used by the guard',
            metrics.get('recent_pnl_guard_recent_profit_factor', 0.0),
        ),
        (
            'trading_recent_pnl_guard_min_realized_pnl',
            'Minimum recent realized PnL threshold configured for the guard',
            metrics.get('recent_pnl_guard_min_realized_pnl', 0.0),
        ),
        (
            'trading_recent_pnl_guard_max_profit_factor',
            'Maximum recent profit factor threshold configured for the guard',
            metrics.get('recent_pnl_guard_max_profit_factor', 0.0),
        ),
    )
    for name, help_text, value in gauges:
        _gauge(output, name, help_text, value)

    _gauge(
        output,
        'trading_recent_pnl_guard_reason',
        'Recent PnL guard status reason (labelled gauge set to 1 for active reason)',
        '',
    )
    output[-1] = (
        'trading_recent_pnl_guard_reason{'
        f'reason="{escape_label_value(metrics.get("recent_pnl_guard_reason", "unknown"))}"'
        '} 1'
    )


def _coin_metrics(output: list[str], metrics: Mapping[str, Any]) -> None:
    _labelled_gauge(
        output,
        'trading_coin_realized_pnl_usd',
        'Realized PnL per coin in USD (last 24h)',
        metrics['coin_pnl'],
    )
    _labelled_gauge(
        output,
        'trading_coin_closed_trades',
        'Closed trades per coin (last 24h)',
        metrics['coin_closed'],
    )
    _labelled_gauge(
        output,
        'trading_coin_win_rate',
        'Win rate per coin in percent (last 24h)',
        metrics['coin_win_rate'],
    )
    _labelled_gauge(
        output,
        'trading_coin_break_even_trades',
        'Break-even trades per coin (last 24h)',
        metrics['coin_break_even'],
    )
    _labelled_gauge(
        output,
        'trading_coin_buy_volume_eur',
        'Buy volume per coin in EUR (last 24h)',
        metrics['coin_buy_volume'],
    )
    _labelled_gauge(
        output,
        'trading_coin_buy_count',
        'Buy count per coin (last 24h)',
        metrics['coin_buy_count'],
    )


def format_prometheus_metrics(metrics: Mapping[str, Any]) -> str:
    """Format a metrics dictionary as Prometheus exposition text."""
    output: list[str] = []
    _trade_metrics(output, metrics)
    _portfolio_metrics(output, metrics)
    _ai_copilot_metrics(output, metrics)
    _shadow_suggestion_metrics(output, metrics)
    _recent_pnl_guard_metrics(output, metrics)
    _coin_metrics(output, metrics)
    return '\n'.join(output)
