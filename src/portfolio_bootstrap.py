"""Portfolio bootstrap for :mod:`src.trading_bot`.

The bot decides *when* to (re)initialise its portfolio; the functions here do
the work. Everything they need from the bot — config, portfolio, exchange, the
iteration counter and the two paper-trade hooks — is handed in explicitly, so
the whole startup path is unit-testable without an exchange or a live bot.

The bodies are a verbatim move from ``CryptoTradingBot``. The only rewrites are
``self.config`` → ``config``, ``self.portfolio`` → ``portfolio``,
``self.exchange`` → ``exchange``, ``self.iteration = …`` →
``hooks.set_iteration(…)``, ``self._public_paper_mode()`` →
``hooks.public_paper_mode()``, ``self._recover_paper_trade()`` →
``hooks.recover_paper_trade()``, the four sibling method calls → direct module
calls, and ``logger`` → ``log``.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import ccxt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PortfolioBootstrapContext:
    """Bot-owned state the portfolio bootstrap reads and mutates."""

    config: Any
    portfolio: Any
    exchange: Any


@dataclass(frozen=True)
class PortfolioBootstrapHooks:
    """Bot callbacks the portfolio bootstrap calls."""

    public_paper_mode: Callable[[], bool]
    recover_paper_trade: Callable[[], None]
    set_iteration: Callable[[int], None]


def currency_aliases(currency: str) -> list[str]:
    base = currency.upper().strip()
    aliases = [base, f"Z{base}", f"X{base}"]
    if base == 'BTC':
        aliases.extend(['XBT', 'XXBT'])
    return list(dict.fromkeys(aliases))


def extract_cash_and_holdings(free_balances: dict[str, float], base_currency: str) -> tuple[float, dict[str, float]]:
    cash = 0.0
    aliases = currency_aliases(base_currency)
    for key in aliases:
        value = free_balances.get(key)
        if value is not None:
            cash = float(value)
            break

    holdings: dict[str, float] = {}
    for currency, amount in free_balances.items():
        if amount <= 0:
            continue
        if currency in aliases:
            continue
        holdings[currency] = float(amount)
    return cash, holdings


def update_portfolio_balance(
    context: PortfolioBootstrapContext,
    hooks: PortfolioBootstrapHooks,
    *,
    log: logging.Logger = logger,
) -> None:
    """Updates the portfolio with current balances from the exchange."""
    config = context.config
    if config.simulation_initial_balance_from_kraken:
        initialize_simulation_portfolio_from_kraken(context, hooks, log=log)
        return

    if config.simulate_data or context.exchange is None:
        initialize_simulated_portfolio(context, log=log)
        return

    # In dry-run mode:
    # 1. Load initial balance from exchange exactly once
    # 2. Then restore from persistent state file (portfolio changes from previous runs)
    # 3. Never overwrite simulated portfolio changes
    if config.dry_run:
        initialize_dry_run_portfolio(context, log=log)
        return

    refresh_live_portfolio_balance(context, log=log)


def initialize_simulation_portfolio_from_kraken(
    context: PortfolioBootstrapContext,
    hooks: PortfolioBootstrapHooks,
    *,
    log: logging.Logger = logger,
) -> None:
    """Bootstrap the simulation portfolio once from the Kraken EUR balance."""
    config = context.config
    portfolio = context.portfolio
    if config.simulation_initial_balance_from_kraken:
        if portfolio.last_update is not None:
            return
        if hooks.public_paper_mode():
            hooks.recover_paper_trade()
        if os.path.lexists(portfolio.state_file):
            if not portfolio.load_state():
                raise RuntimeError("Cannot restore existing simulation portfolio")
            hooks.set_iteration(portfolio.simulation_iteration + 1)
            log.warning(
                "Simulation restarted: restored the last saved portfolio; "
                "Kraken balance was not fetched again."
            )
        else:
            if not config.simulate_data:
                raise RuntimeError(
                    "Initialize the paper portfolio separately before using public market data"
                )
            if not config.api_key or not config.api_secret:
                raise RuntimeError("Kraken read-only API credentials are required for initial balance")
            exchange = ccxt.kraken({
                'apiKey': config.api_key,
                'secret': config.api_secret,
                'enableRateLimit': True,
                'timeout': config.exchange_timeout_ms,
            })
            try:
                balance = exchange.fetch_balance()
                free = balance['free']
                value = next((free[key] for key in ('EUR', 'ZEUR', 'XEUR')
                              if key in free and free[key] is not None), None)
                cash = float(cast(Any, value))
            except (ccxt.BaseError, KeyError, TypeError, ValueError):
                raise RuntimeError("Could not obtain free EUR balance from Kraken") from None
            if not math.isfinite(cash) or cash < 0:
                raise ValueError("Kraken returned an invalid free EUR balance")
            portfolio.cash = cash
            portfolio.initial_portfolio_value = cash
            portfolio.save_state()
            log.info("Simulation initialized once from Kraken free EUR balance: %.2f EUR", cash)
        portfolio.last_update = datetime.now()


def initialize_simulated_portfolio(
    context: PortfolioBootstrapContext,
    *,
    log: logging.Logger = logger,
) -> None:
    """Restore or seed the portfolio for simulated / offline data mode."""
    config = context.config
    portfolio = context.portfolio
    if config.simulate_data or context.exchange is None:
        needs_initialization = (
            portfolio.last_update is None
            and portfolio.initial_portfolio_value is None
            and math.isclose(portfolio.cash, 0.0, abs_tol=1e-12)
            and not portfolio.holdings
        )
        if needs_initialization and not portfolio.load_state():
            portfolio.cash = 1000.0
            portfolio.initial_portfolio_value = portfolio.cash
            log.info(
                f"Simulated starting capital: {portfolio.cash:.2f} {config.base_currency}")
        portfolio.last_update = datetime.now()
        return


def initialize_dry_run_portfolio(
    context: PortfolioBootstrapContext,
    *,
    log: logging.Logger = logger,
) -> None:
    """Restore the dry-run portfolio from state, else seed it from the exchange."""
    config = context.config
    portfolio = context.portfolio
    if config.dry_run:
        if portfolio.last_update is not None:
            return
        # First time: try to load persistent state
        if portfolio.load_state():
            # Restore from file and mark as initialized
            portfolio.last_update = datetime.now()
            return
        # No persistent state, load initial balance from exchange
        try:
            balance = context.exchange.fetch_balance()
            free_balances = balance.get('free', {})
            portfolio.cash, portfolio.holdings = extract_cash_and_holdings(
                free_balances, config.base_currency)
            if not portfolio.holdings:
                portfolio.initial_portfolio_value = portfolio.cash
            portfolio.last_update = datetime.now()
            log.info(
                f"Portfolio initialized from exchange (dry-run mode). Cash: {portfolio.cash:.2f} {config.base_currency}, Holdings: {portfolio.holdings}")
        except ccxt.BaseError as e:
            log.warning(
                f"Failed to load initial balance from exchange in dry-run mode: {e}")


def refresh_live_portfolio_balance(  # noqa: C901
    context: PortfolioBootstrapContext,
    *,
    log: logging.Logger = logger,
) -> None:
    """Fetch the current balance from the exchange (live trading mode)."""
    config = context.config
    portfolio = context.portfolio
    initial_reconciliation = portfolio.last_update is None
    state_exists = os.path.lexists(portfolio.state_file)
    state_restored = portfolio.load_state() if initial_reconciliation and state_exists else False
    if initial_reconciliation and state_exists and not state_restored:
        raise RuntimeError("Cannot restore live portfolio state; refusing to continue")
    expected = (
        {'cash': portfolio.cash, **portfolio.holdings}
        if state_restored else None
    )

    if initial_reconciliation:
        fetch_open_orders = getattr(context.exchange, 'fetch_open_orders', None)
        if not callable(fetch_open_orders):
            raise RuntimeError("Cannot reconcile Kraken open orders; refusing to continue")
        try:
            open_orders = fetch_open_orders()
        except ccxt.BaseError as e:
            raise RuntimeError(
                "Cannot reconcile Kraken open orders; refusing to continue"
            ) from e
        if not isinstance(open_orders, list) or open_orders:
            raise RuntimeError(
                "Kraken has open or unrecognized orders; refusing to continue"
            )

    # Live trading mode: always fetch current balance
    try:
        balance = context.exchange.fetch_balance()
        free_balances = balance.get('free', {})
        if not isinstance(free_balances, dict):
            raise TypeError("Kraken returned an invalid free-balance payload")
        actual_cash, actual_holdings = extract_cash_and_holdings(
            free_balances, config.base_currency)
        balances = [actual_cash, *actual_holdings.values()]
        if any(not math.isfinite(value) or value < 0 for value in balances):
            raise ValueError("Kraken returned invalid live balances")
        if initial_reconciliation:
            if state_restored and expected is not None:
                actual = {'cash': actual_cash, **actual_holdings}
                if not _balances_match(expected, actual):
                    raise RuntimeError(
                        "Persisted portfolio does not match Kraken balances; refusing to continue"
                    )
            elif actual_holdings:
                raise RuntimeError(
                    "Kraken has crypto balances but no reconciled live portfolio state; "
                    "refusing to continue"
                )
        portfolio.cash = actual_cash
        portfolio.holdings = actual_holdings
        portfolio.last_update = datetime.now()
        if portfolio.strict_persistence:
            portfolio.save_state()
        log.info(
            f"Portfolio balance updated. Cash: {portfolio.cash:.2f} {config.base_currency}, Holdings: {portfolio.holdings}")
    except RuntimeError:
        raise
    except ccxt.BaseError as e:
        raise RuntimeError(
            "Cannot verify live Kraken balances; refusing to continue"
        ) from e
    except (AttributeError, TypeError, ValueError) as e:
        raise RuntimeError(
            "Kraken returned invalid live balances; refusing to continue"
        ) from e


def _balances_match(expected: dict[str, float], actual: dict[str, float]) -> bool:
    if expected.keys() != actual.keys():
        return False
    return all(
        math.isclose(expected[key], actual[key], rel_tol=1e-6, abs_tol=1e-8)
        for key in expected
    )
