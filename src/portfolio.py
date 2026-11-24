"""Portfolio state, holdings and open-position bookkeeping.

Extracted from ``src.trading_bot`` so that persistence and position tracking can
be tested and reused without importing the full trading bot.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from datetime import datetime

logger = logging.getLogger(__name__)


class Portfolio:
    """
    Manages the portfolio, holdings and trading positions.
    Includes simple PnL tracking per open position.
    """

    def __init__(self, base_currency: str = "EUR"):
        self.base_currency = base_currency
        self.cash: float = 0.0  # Available balance in base currency
        # Holdings of other cryptocurrencies (Coin -> quantity)
        self.holdings: dict[str, float] = {}
        # Offene Trades: {'BTC': {'buy_price': 30000, 'amount_coin': 0.001, 'amount_base': 30, 'timestamp': datetime}}
        self.open_trades: dict[str, dict] = {}
        self.last_update: datetime | None = None
        self.initial_portfolio_value: float | None = None
        self.state_file: str = ".portfolio_state.json"  # Persistency file for dry-run
        self.strict_persistence = False
        self.simulation_iteration = 0

    def _state(self) -> dict:
        return {
            'cash': self.cash,
            'holdings': self.holdings,
            'open_trades': self.open_trades,
            'base_currency': self.base_currency,
            'initial_portfolio_value': self.initial_portfolio_value,
            'simulation_iteration': self.simulation_iteration,
            'timestamp': datetime.now().isoformat(),
        }

    def save_state(self, filepath: str | None = None, *, state: dict | None = None) -> bool:
        """Save portfolio state to JSON file (for dry-run persistency)."""
        temporary_path = None
        try:
            state_file = filepath or self.state_file
            if state is None:
                state = self._state()
            with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=os.path.dirname(os.path.abspath(state_file)),
                prefix='.portfolio-state-', delete=False,
            ) as f:
                temporary_path = f.name
                json.dump(state, f, indent=2, default=lambda o: o.isoformat(
                ) if isinstance(o, datetime) else str(o))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_path, state_file)
            temporary_path = None
            directory_fd = os.open(os.path.dirname(os.path.abspath(state_file)), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            logger.debug(f"Portfolio state saved to {state_file}")
            return True
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"Failed to save portfolio state: {e}")
            if self.strict_persistence:
                raise RuntimeError("Cannot persist simulation portfolio; stopping") from e
            return False
        finally:
            if temporary_path is not None and os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def load_state(self, filepath: str | None = None) -> bool:
        """Load portfolio state from JSON file (for dry-run persistency)."""
        try:
            state_file = filepath or self.state_file
            if not os.path.exists(state_file):
                logger.debug(f"Portfolio state file not found: {state_file}")
                return False
            with open(state_file, 'r') as f:
                state = json.load(f)
            if self.strict_persistence and (
                not isinstance(state, dict)
                or state.get('base_currency') != self.base_currency
                or not isinstance(state.get('holdings'), dict)
                or not isinstance(state.get('open_trades'), dict)
                or 'cash' not in state
                or not isinstance(state.get('simulation_iteration', 0), int)
                or state.get('simulation_iteration', 0) < 0
            ):
                raise ValueError("Invalid or incompatible simulation portfolio")
            self.simulation_iteration = state.get('simulation_iteration', 0)
            self.cash = float(state.get('cash', 0.0))
            self.holdings = {k: float(v)
                             for k, v in state.get('holdings', {}).items()}
            self.open_trades = state.get('open_trades', {})
            initial_value = state.get('initial_portfolio_value')
            self.initial_portfolio_value = float(
                initial_value) if initial_value not in (None, '') else None
            if self.strict_persistence and (
                not math.isfinite(self.cash) or self.cash < 0
                or any(not math.isfinite(v) or v < 0 for v in self.holdings.values())
            ):
                raise ValueError("Invalid simulation portfolio balances")
            for trade in self.open_trades.values():
                ts = trade.get('timestamp')
                if isinstance(ts, str):
                    try:
                        trade['timestamp'] = datetime.fromisoformat(ts)
                    except ValueError:
                        trade['timestamp'] = datetime.now()
            logger.info(f"Portfolio state loaded from {state_file}")
            logger.info(f"  Cash: {self.cash:.2f} {self.base_currency}")
            logger.info(f"  Holdings: {self.holdings}")
            if self.open_trades:
                logger.info(
                    f"  Open trades restored: {list(self.open_trades.keys())}")
            return True
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            AttributeError,
            TypeError,
            ValueError,
        ) as e:
            logger.error(f"Failed to load portfolio state: {e}")
            if self.strict_persistence:
                raise RuntimeError("Cannot restore simulation portfolio; stopping") from e
            return False

    def get_value(self, prices: dict[str, float]) -> float:
        """
        Berechnet den Gesamtwert des Portfolios basierend auf aktuellen Preisen.
        """
        value = self.cash
        for coin, amount in self.holdings.items():
            if coin in prices:
                value += amount * prices[coin]
                continue

            # Kraken assets may use 'X' or 'Z' prefixes (e.g. XXBT).
            clean_coin = coin
            if len(clean_coin) > 3 and clean_coin[0] in ('X', 'Z'):
                clean_coin = clean_coin[1:]
            if len(clean_coin) > 3 and clean_coin[0] in ('X', 'Z'):
                clean_coin = clean_coin[1:]

            if clean_coin in prices:
                value += amount * prices[clean_coin]
            else:
                logger.debug(
                    f"Price for {coin}/{clean_coin} not found for portfolio value calculation.")
        return value

    def add_trade(
        self,
        coin: str,
        buy_price: float,
        amount_coin: float,
        amount_base: float,
        signal_source: str = "rules",
        signal_confidence: float | None = None,
        recommendation: str = "HOLD",
    ):
        """Adds a new trade to the open positions."""
        self.open_trades[coin] = {
            'buy_price': buy_price,
            'amount_coin': amount_coin,
            'amount_base': amount_base,  # Invested amount in base currency
            'timestamp': datetime.now(),
            'peak_price': buy_price,
            'trough_price': buy_price,
            'partial_tp_taken': False,
            'partial_tp_timestamp': None,
            'signal_source': signal_source,
            'signal_confidence': signal_confidence,
            'recommendation': recommendation,
        }
        logger.info(
            f"Trade {coin} added to monitoring: buy price {buy_price:.4f}, amount {amount_coin:.4f}.")

    def remove_trade(self, coin: str):
        """Entfernt einen Trade aus den offenen Positionen."""
        if coin in self.open_trades:
            del self.open_trades[coin]
            logger.info(f"Trade {coin} removed from monitoring.")
