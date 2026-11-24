"""Aggregate trade journal rows into the PnL metrics exposed to Prometheus.

The counters produced here back
:func:`src.pnl_prometheus_format.format_prometheus_metrics` and are shared by the
one-shot ``export_pnl_metrics`` script and the long-running ``pnl_exporter`` HTTP
service. The module performs no I/O: callers pass the rows they read from
``trade_journal.csv``.

A row is only counted when it is well formed. Realized PnL is tracked over all
time, while the win/loss, per-coin and drawdown statistics cover only the trades
closed inside the reporting window.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = [
    "MIN_DRAWDOWN_PCT_BASE_USD",
    "PnLAccumulator",
    "calculate_pnl_metrics",
    "extract_return_pct",
    "parse_timestamp",
]

# Drawdown percentages are meaningless when the equity peak is a rounding
# error away from zero, so the denominator is floored at this many USD.
MIN_DRAWDOWN_PCT_BASE_USD = 1.0

# Journal columns that carry the trade return, in preference order.
RETURN_PCT_KEYS = ('pnl_pct', 'pnl_percent', 'return_pct', 'return')


def parse_timestamp(timestamp_str: str | None) -> datetime | None:
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


def extract_return_pct(trade: Mapping[str, Any]) -> float:
    """Extract trade return in decimal form when available."""
    for key in RETURN_PCT_KEYS:
        raw = trade.get(key)
        if raw in (None, ''):
            continue
        try:
            val = float(raw)
            return val / 100.0 if abs(val) > 1.0 else val
        except ValueError:
            continue
    return 0.0


class PnLAccumulator:
    """Running counters for one PnL report.

    :meth:`record` accepts a single journal row and decides what to do with it;
    :meth:`metrics` renders the finished report. Malformed rows raise so the
    caller can skip them.
    """

    def __init__(self) -> None:
        self.all_time_realized_pnl = 0.0
        self.total_realized_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.break_even_trades = 0
        self.gross_profit = 0.0
        self.gross_loss = 0.0
        self.per_trade_returns: list[float] = []

        self.coin_pnl: dict[str, float] = {}
        self.coin_closed: dict[str, int] = {}
        self.coin_wins: dict[str, int] = {}
        self.coin_break_even: dict[str, int] = {}
        self.coin_buy_volume: dict[str, float] = {}
        self.coin_buy_count: dict[str, int] = {}

        self.equity = 0.0
        self.peak_equity = 0.0
        self.max_drawdown_usd = 0.0
        self.max_drawdown_pct = 0.0

    def record(self, trade: Mapping[str, Any], cutoff: datetime) -> None:
        """Fold one journal row into the counters.

        Buys only feed the per-coin volume counters, sells feed everything, and
        rows outside the window only contribute their realized PnL.
        """
        timestamp_str = trade.get('timestamp', '')
        if not timestamp_str:
            return

        ts = parse_timestamp(timestamp_str)
        if ts is None:
            return

        action = trade.get('action', '').lower()
        coin = (trade.get('coin') or 'UNKNOWN').upper()

        if action == 'buy':
            self._record_buy(coin, trade, ts, cutoff)
            return
        if action != 'sell':
            return
        self._record_sell(coin, trade, ts, cutoff)

    def _record_buy(
        self, coin: str, trade: Mapping[str, Any], ts: datetime, cutoff: datetime
    ) -> None:
        if ts < cutoff:
            return
        try:
            buy_volume = float(trade.get('amount_base', '0') or 0.0)
        except ValueError:
            buy_volume = 0.0
        self.coin_buy_volume[coin] = self.coin_buy_volume.get(
            coin, 0.0) + max(buy_volume, 0.0)
        self.coin_buy_count[coin] = self.coin_buy_count.get(coin, 0) + 1

    def _record_sell(
        self, coin: str, trade: Mapping[str, Any], ts: datetime, cutoff: datetime
    ) -> None:
        pnl_str = trade.get('pnl_base', '0')
        try:
            pnl = float(pnl_str)
        except ValueError:
            return

        # The all-time total is accumulated before the window comparison on
        # purpose: comparing a naive journal timestamp with the aware cutoff
        # raises, and the row must still count towards the all-time PnL.
        self.all_time_realized_pnl += pnl
        if ts < cutoff:
            return
        self._record_closed_trade(coin, pnl, trade)

    def _record_closed_trade(
        self, coin: str, pnl: float, trade: Mapping[str, Any]
    ) -> None:
        self.total_trades += 1
        self.total_realized_pnl += pnl

        self.coin_pnl[coin] = self.coin_pnl.get(coin, 0.0) + pnl
        self.coin_closed[coin] = self.coin_closed.get(coin, 0) + 1
        self.coin_break_even.setdefault(coin, 0)

        if pnl > 0:
            self.winning_trades += 1
            self.gross_profit += pnl
            self.coin_wins[coin] = self.coin_wins.get(coin, 0) + 1
        elif pnl < 0:
            self.losing_trades += 1
            self.gross_loss += pnl
        else:
            self.break_even_trades += 1
            self.coin_break_even[coin] = self.coin_break_even.get(coin, 0) + 1

        self.per_trade_returns.append(extract_return_pct(trade))
        self._record_equity(pnl)

    def _record_equity(self, pnl: float) -> None:
        self.equity += pnl
        self.peak_equity = max(self.peak_equity, self.equity)
        drawdown_usd = self.peak_equity - self.equity
        self.max_drawdown_usd = max(self.max_drawdown_usd, drawdown_usd)
        if self.peak_equity > 0:
            drawdown_pct = drawdown_usd / \
                max(self.peak_equity, MIN_DRAWDOWN_PCT_BASE_USD)
            self.max_drawdown_pct = max(self.max_drawdown_pct, drawdown_pct)

    def metrics(self) -> dict[str, Any]:
        """Render the accumulated counters as the PnL metric dictionary."""
        return {
            'all_time_realized_pnl': self.all_time_realized_pnl,
            'total_realized_pnl': self.total_realized_pnl,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'break_even_trades': self.break_even_trades,
            'win_rate': self.win_rate(),
            'avg_pnl_per_trade': self.average_pnl_per_trade(),
            'profit_factor': self.profit_factor(),
            'sharpe_ratio': sharpe_ratio(self.per_trade_returns),
            'max_drawdown_usd': self.max_drawdown_usd,
            'max_drawdown_pct': self.max_drawdown_pct * 100.0,
            'coin_pnl': self.coin_pnl,
            'coin_closed': self.coin_closed,
            'coin_win_rate': self.coin_win_rate(),
            'coin_break_even': self.coin_break_even,
            'coin_buy_volume': self.coin_buy_volume,
            'coin_buy_count': self.coin_buy_count,
        }

    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0
        return self.winning_trades / self.total_trades * 100

    def average_pnl_per_trade(self) -> float:
        if self.total_trades == 0:
            return 0
        return self.total_realized_pnl / self.total_trades

    def profit_factor(self) -> float:
        if self.gross_loss < 0:
            return self.gross_profit / abs(self.gross_loss)
        return self.gross_profit if self.gross_profit > 0 else 0.0

    def coin_win_rate(self) -> dict[str, float]:
        rates: dict[str, float] = {}
        for coin, closed in self.coin_closed.items():
            wins = self.coin_wins.get(coin, 0)
            rates[coin] = (wins / closed * 100.0) if closed > 0 else 0.0
        return rates


def sharpe_ratio(returns: Iterable[float]) -> float:
    """Sample-standard-deviation Sharpe ratio; ``0.0`` for fewer than two trades."""
    samples = list(returns)
    if len(samples) <= 1:
        return 0.0
    mean_ret = sum(samples) / len(samples)
    variance = sum((r - mean_ret) ** 2 for r in samples) / (len(samples) - 1)
    std_dev = math.sqrt(variance)
    if std_dev <= 0:
        return 0.0
    return (mean_ret / std_dev) * math.sqrt(len(samples))


def calculate_pnl_metrics(
    trades: Iterable[Mapping[str, Any]], time_window_hours: float = 24
) -> dict[str, Any]:
    """Calculate PnL metrics from trades"""
    cutoff = datetime.now(UTC) - timedelta(hours=time_window_hours)
    accumulator = PnLAccumulator()
    for trade in trades:
        try:
            accumulator.record(trade, cutoff)
        except (
            AttributeError,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            continue
    return accumulator.metrics()
