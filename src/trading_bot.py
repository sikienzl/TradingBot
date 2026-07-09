import os
import sys
import logging
import csv
import json
import shutil
import math
import importlib
from typing import Any, Dict, List, Optional, Set, Tuple
import pandas as pd
import time
from datetime import datetime, timedelta, timezone
import ccxt
import numpy as np
from dotenv import load_dotenv
from collections import Counter, defaultdict, deque
import urllib.request
import urllib.error
import re


logger = logging.getLogger(__name__)


_SAFE_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _compute_dynamic_lossmaker_entry_pairs(
    df: pd.DataFrame,
    window: int,
    min_sells: int,
    max_win_rate_pct: float,
    min_pnl_loss: float,
    min_max_hold_exit_ratio: float,
    max_avg_hold_seconds: float,
) -> Set[Tuple[str, str]]:
    """Returns recent coin/source pairs that should be temporarily suppressed."""
    if df.empty:
        return set()

    required_columns = {"action", "coin", "pnl_base"}
    if not required_columns.issubset(df.columns):
        return set()

    sells = df.loc[
        df["action"].fillna("").astype(str).str.lower() == "sell"
    ].copy()
    if sells.empty:
        return set()

    recent_window = sells.tail(max(1, int(window))).copy()
    if recent_window.empty:
        return set()

    recent_window["coin"] = recent_window["coin"].fillna(
        "").astype(str).str.upper()
    recent_window["signal_source"] = recent_window.get(
        "signal_source",
        pd.Series(index=recent_window.index, dtype=object),
    ).fillna("rules").astype(str).str.lower()
    recent_window["pnl_base"] = pd.to_numeric(
        recent_window["pnl_base"], errors="coerce"
    ).fillna(0.0)
    recent_window["hold_seconds"] = pd.to_numeric(
        recent_window.get(
            "hold_seconds",
            pd.Series(index=recent_window.index, dtype=float),
        ),
        errors="coerce",
    ).fillna(0.0)
    recent_reasons = recent_window.get(
        "reason",
        pd.Series(index=recent_window.index, dtype=object),
    ).fillna("").astype(str)

    blocked_pairs: Set[Tuple[str, str]] = set()
    for (coin, signal_source), group in recent_window.groupby(["coin", "signal_source"], dropna=False):
        if not coin or not signal_source:
            continue
        if len(group) < max(1, int(min_sells)):
            continue

        pnl = pd.to_numeric(group["pnl_base"], errors="coerce").fillna(0.0)
        pnl_sum = float(pnl.sum())
        if pnl_sum > -float(min_pnl_loss):
            continue

        win_rate_pct = float((pnl > 0).mean() * 100.0)
        if win_rate_pct > float(max_win_rate_pct):
            continue

        avg_hold_seconds = float(
            pd.to_numeric(group["hold_seconds"],
                          errors="coerce").fillna(0.0).mean()
        )

        reasons = recent_reasons.loc[group.index]
        max_hold_exit_ratio = float(
            reasons.str.contains("MAX-HOLD-TIME", case=False, na=False).mean()
        )
        if (
            max_hold_exit_ratio < float(min_max_hold_exit_ratio)
            and avg_hold_seconds > float(max_avg_hold_seconds)
        ):
            continue

        blocked_pairs.add((str(coin).upper(), str(signal_source).lower()))

    return blocked_pairs


def _compute_degraded_entry_sources(
    df: pd.DataFrame,
    window: int,
    min_sells: int,
    min_pnl_loss: float,
    max_profit_factor: float,
    max_avg_hold_seconds: float,
) -> Set[str]:
    """Returns signal sources that recently underperform with short-hold churn."""
    if df.empty:
        return set()

    required_columns = {"action", "pnl_base"}
    if not required_columns.issubset(df.columns):
        return set()

    sells = df.loc[
        df["action"].fillna("").astype(str).str.lower() == "sell"
    ].copy()
    if sells.empty:
        return set()

    recent_window = sells.tail(max(1, int(window))).copy()
    if recent_window.empty:
        return set()

    recent_window["signal_source"] = recent_window.get(
        "signal_source",
        pd.Series(index=recent_window.index, dtype=object),
    ).fillna("rules").astype(str).str.lower()
    recent_window["pnl_base"] = pd.to_numeric(
        recent_window["pnl_base"], errors="coerce"
    ).fillna(0.0)
    recent_window["hold_seconds"] = pd.to_numeric(
        recent_window.get(
            "hold_seconds",
            pd.Series(index=recent_window.index, dtype=float),
        ),
        errors="coerce",
    ).fillna(0.0)

    degraded_sources: Set[str] = set()
    for source, group in recent_window.groupby("signal_source", dropna=False):
        if not source:
            continue
        if len(group) < max(1, int(min_sells)):
            continue

        pnl = pd.to_numeric(group["pnl_base"], errors="coerce").fillna(0.0)
        pnl_sum = float(pnl.sum())
        if pnl_sum > -float(min_pnl_loss):
            continue

        gross_profit = float(pnl[pnl > 0].sum())
        gross_loss = float(-pnl[pnl < 0].sum())
        profit_factor = (
            gross_profit / gross_loss) if gross_loss > 0 else 999.0
        if profit_factor > float(max_profit_factor):
            continue

        avg_hold_seconds = float(
            pd.to_numeric(group["hold_seconds"],
                          errors="coerce").fillna(0.0).mean()
        )
        if avg_hold_seconds > float(max_avg_hold_seconds):
            continue

        degraded_sources.add(str(source).lower())

    return degraded_sources


def _compute_recent_pnl_guard_state(
    df: pd.DataFrame,
    window: int,
    min_trades: int,
    min_realized_pnl: float,
    max_profit_factor: float,
) -> Dict[str, Any]:
    """Returns whether recent realized performance is weak enough to tighten entries."""
    result: Dict[str, Any] = {
        "active": False,
        "recent_trades": 0,
        "recent_realized_pnl": 0.0,
        "recent_profit_factor": 999.0,
        "reason": "insufficient_data",
    }

    if df.empty or "action" not in df.columns or "pnl_base" not in df.columns:
        return result

    sells = df.loc[
        df["action"].fillna("").astype(str).str.lower() == "sell"
    ].copy()
    if sells.empty:
        return result

    recent = sells.tail(max(1, int(window))).copy()
    if recent.empty:
        return result

    pnl = pd.to_numeric(recent["pnl_base"], errors="coerce").fillna(0.0)
    recent_trades = int(len(recent))
    recent_realized_pnl = float(pnl.sum())
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = float(-pnl[pnl < 0].sum())
    recent_profit_factor = (
        (gross_profit / gross_loss) if gross_loss > 0 else 999.0
    )

    result["recent_trades"] = recent_trades
    result["recent_realized_pnl"] = recent_realized_pnl
    result["recent_profit_factor"] = recent_profit_factor

    if recent_trades < max(1, int(min_trades)):
        result["reason"] = "below_min_trades"
        return result

    if recent_realized_pnl < float(min_realized_pnl) and recent_profit_factor <= float(max_profit_factor):
        result["active"] = True
        result["reason"] = "negative_recent_pnl_and_low_pf"
    else:
        result["reason"] = "healthy_recent_window"

    return result


def configure_logging(log_file: str = 'trading_bot.log', level: int = logging.INFO) -> None:
    """Configures runtime logging lazily so importing the module has no file-system side effects."""
    root_logger = logging.getLogger()
    if getattr(root_logger, '_trading_bot_logging_configured', False):
        return

    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s')
    handlers: List[logging.Handler] = []

    try:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except OSError as exc:
        fallback_logger = logging.getLogger(__name__)
        fallback_logger.warning(
            "Failed to open log file %s, falling back to console logging only: %s",
            log_file,
            exc,
        )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)

    root_logger.setLevel(level)
    for handler in handlers:
        root_logger.addHandler(handler)
    root_logger._trading_bot_logging_configured = True


def _mask_secret(value: str) -> str:
    """Masks sensitive strings for logging output."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


class Portfolio:
    """
    Manages the portfolio, holdings and trading positions.
    Includes simple PnL tracking per open position.
    """

    def __init__(self, base_currency: str = "EUR"):
        self.base_currency = base_currency
        self.cash: float = 0.0  # Available balance in base currency
        # Holdings of other cryptocurrencies (Coin -> quantity)
        self.holdings: Dict[str, float] = {}
        # Offene Trades: {'BTC': {'buy_price': 30000, 'amount_coin': 0.001, 'amount_base': 30, 'timestamp': datetime}}
        self.open_trades: Dict[str, Dict] = {}
        self.last_update: Optional[datetime] = None
        self.initial_portfolio_value: Optional[float] = None
        self.state_file: str = ".portfolio_state.json"  # Persistency file for dry-run

    def save_state(self, filepath: Optional[str] = None) -> bool:
        """Save portfolio state to JSON file (for dry-run persistency)."""
        try:
            state_file = filepath or self.state_file
            state = {
                'cash': self.cash,
                'holdings': self.holdings,
                'open_trades': self.open_trades,
                'base_currency': self.base_currency,
                'initial_portfolio_value': self.initial_portfolio_value,
                'timestamp': datetime.now().isoformat(),
            }
            with open(state_file, 'w') as f:
                json.dump(state, f, indent=2, default=lambda o: o.isoformat(
                ) if isinstance(o, datetime) else str(o))
            logger.debug(f"Portfolio state saved to {state_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to save portfolio state: {e}")
            return False

    def load_state(self, filepath: Optional[str] = None) -> bool:
        """Load portfolio state from JSON file (for dry-run persistency)."""
        try:
            state_file = filepath or self.state_file
            if not os.path.exists(state_file):
                logger.debug(f"Portfolio state file not found: {state_file}")
                return False
            with open(state_file, 'r') as f:
                state = json.load(f)
            self.cash = float(state.get('cash', 0.0))
            self.holdings = {k: float(v)
                             for k, v in state.get('holdings', {}).items()}
            self.open_trades = state.get('open_trades', {})
            initial_value = state.get('initial_portfolio_value')
            self.initial_portfolio_value = float(
                initial_value) if initial_value not in (None, '') else None
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
        except Exception as e:
            logger.error(f"Failed to load portfolio state: {e}")
            return False

    def get_value(self, prices: Dict[str, float]) -> float:
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
        signal_confidence: Optional[float] = None,
        recommendation: str = "HOLD",
    ):
        """Adds a new trade to the open positions."""
        self.open_trades[coin] = {
            'buy_price': buy_price,
            'amount_coin': amount_coin,
            'amount_base': amount_base,  # Invested amount in base currency
            'timestamp': datetime.now(),
            'peak_price': buy_price,
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


class BotConfig:
    """Configuration class for the trading bot."""

    def __init__(self):
        # Ensure project .env wins over inherited shell/service variables.
        load_dotenv(override=True)

        def _env_bool(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            return raw.strip().lower() in ("1", "true", "yes", "y", "on")

        def _env_str(name: str, default: str = "") -> str:
            raw = os.getenv(name)
            if raw is None:
                return default
            return raw.strip().strip('"').strip("'")

        def _env_str_any(names: List[str], default: str = "") -> str:
            for name in names:
                raw = os.getenv(name)
                if raw is not None:
                    return raw.strip().strip('"').strip("'")
            return default

        def _env_bool_any(names: List[str], default: bool) -> bool:
            for name in names:
                raw = os.getenv(name)
                if raw is not None:
                    return raw.strip().lower() in ("1", "true", "yes", "y", "on")
            return default

        def _env_symbol_int_map(name: str, default: str = "") -> Dict[str, int]:
            raw = _env_str(name, default)
            if not raw:
                return {}

            parsed: Dict[str, int] = {}
            for item in raw.split(','):
                token = item.strip()
                if not token or ':' not in token:
                    continue
                symbol, value = token.split(':', 1)
                symbol = symbol.strip().upper()
                value = value.strip()
                if not symbol or not value:
                    continue
                try:
                    parsed[symbol] = int(value)
                except ValueError:
                    continue
            return parsed

        def _env_symbol_float_map(name: str, default: str = "") -> Dict[str, float]:
            raw = _env_str(name, default)
            if not raw:
                return {}

            parsed: Dict[str, float] = {}
            for item in raw.split(','):
                token = item.strip()
                if not token or ':' not in token:
                    continue
                symbol, value = token.split(':', 1)
                symbol = symbol.strip().upper()
                value = value.strip()
                if not symbol or not value:
                    continue
                try:
                    parsed[symbol] = float(value)
                except ValueError:
                    continue
            return parsed

        def _env_threshold_float_pairs(name: str, default: str = "") -> List[Tuple[float, float]]:
            raw = _env_str(name, default)
            if not raw:
                return []

            parsed: List[Tuple[float, float]] = []
            for item in raw.split(','):
                token = item.strip()
                if not token or ':' not in token:
                    continue
                threshold, value = token.split(':', 1)
                try:
                    parsed.append(
                        (float(threshold.strip()), float(value.strip())))
                except ValueError:
                    continue
            return sorted(parsed, key=lambda entry: entry[0])

        def _env_threshold_int_pairs(name: str, default: str = "") -> List[Tuple[float, int]]:
            raw = _env_str(name, default)
            if not raw:
                return []

            parsed: List[Tuple[float, int]] = []
            for item in raw.split(','):
                token = item.strip()
                if not token or ':' not in token:
                    continue
                threshold, value = token.split(':', 1)
                try:
                    parsed.append(
                        (float(threshold.strip()), int(value.strip())))
                except ValueError:
                    continue
            return sorted(parsed, key=lambda entry: entry[0])

        self.exchange_name = os.getenv('EXCHANGE_NAME', 'kraken').lower()
        self.api_key = os.getenv(
            'KRAKEN_API_KEY', '')
        self.api_secret = os.getenv(
            'KRAKEN_API_SECRET', '')
        self.base_currency = os.getenv('BASE_CURRENCY', 'EUR').upper()
        self.quote_currencies = [c.strip().upper() for c in os.getenv(
            'QUOTE_CURRENCIES', 'BTC,ETH').split(',') if c.strip()]
        configured_excluded_coins = {c.strip().upper() for c in os.getenv(
            'EXCLUDED_COINS', 'USDC,USDT,EURT,DAI,TUSD,USDP,FDUSD,USDE').split(',') if c.strip()}
        lossmaker_excluded_coins = {c.strip().upper() for c in os.getenv(
            'LOSSMAKER_EXCLUDED_COINS', 'ZEC,HYPE,TON,BTC,XRP').split(',') if c.strip()}
        self.excluded_coins = configured_excluded_coins | lossmaker_excluded_coins
        self.dynamic_lossmaker_exclusion_enabled = _env_bool(
            'DYNAMIC_LOSSMAKER_EXCLUSION_ENABLED', True)
        self.dynamic_lossmaker_window = int(
            os.getenv('DYNAMIC_LOSSMAKER_WINDOW', 80))
        self.dynamic_lossmaker_min_sells = int(
            os.getenv('DYNAMIC_LOSSMAKER_MIN_SELLS', 3))
        self.dynamic_lossmaker_max_win_rate_pct = float(
            os.getenv('DYNAMIC_LOSSMAKER_MAX_WIN_RATE_PCT', 45.0))
        self.dynamic_lossmaker_min_pnl_loss = float(
            os.getenv('DYNAMIC_LOSSMAKER_MIN_PNL_LOSS', 0.003))
        self.dynamic_lossmaker_min_max_hold_exit_ratio = float(
            os.getenv('DYNAMIC_LOSSMAKER_MIN_MAX_HOLD_EXIT_RATIO', 0.5))
        self.recent_pnl_guard_enabled = _env_bool(
            'RECENT_PNL_GUARD_ENABLED', True)
        self.recent_pnl_guard_window = int(
            os.getenv('RECENT_PNL_GUARD_WINDOW', 120))
        self.recent_pnl_guard_min_trades = int(
            os.getenv('RECENT_PNL_GUARD_MIN_TRADES', 60))
        self.recent_pnl_guard_min_realized_pnl = float(
            os.getenv('RECENT_PNL_GUARD_MIN_REALIZED_PNL', 0.0))
        self.recent_pnl_guard_max_profit_factor = float(
            os.getenv('RECENT_PNL_GUARD_MAX_PROFIT_FACTOR', 1.0))
        # Amount in base currency per trade
        self.trade_amount = float(os.getenv('TRADE_AMOUNT', 20))
        self.portfolio_trade_amount_multipliers = _env_threshold_float_pairs(
            'PORTFOLIO_TRADE_AMOUNT_MULTIPLIERS',
            '50:1.10,100:1.25,200:1.50,300:1.75,500:2.20,1000:3.00',
        )
        # Minimum amount per trade in base currency
        self.min_trade_amount = float(os.getenv('MIN_TRADE_AMOUNT', 5))
        # Seconds between iterations
        self.check_interval = int(os.getenv('CHECK_INTERVAL', 100))
        self.dry_run = _env_bool('DRY_RUN', True)
        self.simulate_data = _env_bool('SIMULATE_DATA', False)
        self.simulation_seed = int(os.getenv('SIMULATION_SEED', 42))
        self.simulation_regime = _env_str(
            'SIMULATION_REGIME', 'neutral').lower()
        self.stop_loss_pct = float(
            os.getenv('STOP_LOSS_PCT', 0.05))  # 5% loss
        self.take_profit_pct = float(
            os.getenv('TAKE_PROFIT_PCT', 0.10))  # 10% gain
        self.max_open_trades = int(os.getenv('MAX_OPEN_TRADES', 3))
        self.portfolio_max_open_trades_tiers = _env_threshold_int_pairs(
            'PORTFOLIO_MAX_OPEN_TRADES_TIERS',
            '100:3,200:4,500:5,1000:6',
        )
        # Minimum 24h volume in base currency
        self.min_volume_base = float(os.getenv('MIN_VOLUME_BASE', 100000))
        # Number of top coins for analysis
        self.top_n_for_analysis = int(os.getenv('TOP_N_FOR_ANALYSIS', 10))
        self.exchange_timeout_ms = int(os.getenv('EXCHANGE_TIMEOUT_MS', 15000))
        # Split ticker requests into manageable chunks to avoid oversized exchange calls.
        self.ticker_batch_size = int(os.getenv('TICKER_BATCH_SIZE', 80))
        self.ticker_fetch_retries = int(os.getenv('TICKER_FETCH_RETRIES', 2))
        self.ticker_retry_delay_seconds = float(
            os.getenv('TICKER_RETRY_DELAY_SECONDS', 1.0))
        self.ohlcv_fetch_retries = int(os.getenv('OHLCV_FETCH_RETRIES', 1))
        self.ohlcv_retry_delay_seconds = float(
            os.getenv('OHLCV_RETRY_DELAY_SECONDS', 1.0))
        self.slow_analysis_warning_seconds = float(
            os.getenv('SLOW_ANALYSIS_WARNING_SECONDS', 8.0))
        self.rsi_period = int(os.getenv('RSI_PERIOD', 14))
        self.sma_period_short = int(os.getenv('SMA_PERIOD_SHORT', 20))
        self.sma_period_long = int(os.getenv('SMA_PERIOD_LONG', 50))
        # Minimum score for entries (HOLD (Up-Trend) also possible)
        self.min_entry_score = int(os.getenv('MIN_ENTRY_SCORE', 60))
        # If no classic BUY recommendation is available, still trade the strongest candidate
        self.enable_fallback_entry = _env_bool('ENABLE_FALLBACK_ENTRY', True)
        # Minimum score for fallback entries only (can be lower than MIN_ENTRY_SCORE)
        self.fallback_min_score = int(os.getenv('FALLBACK_MIN_SCORE', 45))
        # RSI limit for fallback entries (avoid overbought assets)
        self.fallback_max_rsi = float(os.getenv('FALLBACK_MAX_RSI', 68))
        self.fallback_max_rsi_by_coin = _env_symbol_float_map(
            'FALLBACK_MAX_RSI_BY_COIN', 'TRX:72')
        # Optionally forces filling of free slots with neutral non-SELL candidates
        self.force_fill_slots = _env_bool('FORCE_FILL_SLOTS', False)
        # Minimum score for forced slot filling (without RSI limit)
        self.force_fill_min_score = int(os.getenv('FORCE_FILL_MIN_SCORE', 35))
        self.defensive_entry_mode_enabled = _env_bool(
            'DEFENSIVE_ENTRY_MODE_ENABLED', True)
        # Guard aggressive fallback entries in down-trends behind a stricter reversal filter.
        self.downtrend_reversal_entry_enabled = _env_bool(
            'DOWNTREND_REVERSAL_ENTRY_ENABLED', True)
        self.downtrend_reversal_max_rsi = float(
            os.getenv('DOWNTREND_REVERSAL_MAX_RSI', 30.0))
        self.downtrend_reversal_max_rsi_by_coin = _env_symbol_float_map(
            'DOWNTREND_REVERSAL_MAX_RSI_BY_COIN', '')
        self.downtrend_reversal_min_buy_proba = float(
            os.getenv('DOWNTREND_REVERSAL_MIN_BUY_PROBA', 0.20))
        self.downtrend_reversal_min_buy_proba_by_coin = _env_symbol_float_map(
            'DOWNTREND_REVERSAL_MIN_BUY_PROBA_BY_COIN', '')
        self.downtrend_reversal_max_sell_proba = float(
            os.getenv('DOWNTREND_REVERSAL_MAX_SELL_PROBA', 0.35))
        self.downtrend_reversal_max_sell_proba_by_coin = _env_symbol_float_map(
            'DOWNTREND_REVERSAL_MAX_SELL_PROBA_BY_COIN', '')
        self.downtrend_reversal_min_proba_edge = float(
            os.getenv('DOWNTREND_REVERSAL_MIN_PROBA_EDGE', 0.02))
        self.downtrend_reversal_min_proba_edge_by_coin = _env_symbol_float_map(
            'DOWNTREND_REVERSAL_MIN_PROBA_EDGE_BY_COIN', '')
        self.downtrend_reversal_min_ret_1 = float(
            os.getenv('DOWNTREND_REVERSAL_MIN_RET_1', 0.0))
        self.downtrend_reversal_min_ret_3 = float(
            os.getenv('DOWNTREND_REVERSAL_MIN_RET_3', -0.01))
        self.downtrend_reversal_min_macd_hist = float(
            os.getenv('DOWNTREND_REVERSAL_MIN_MACD_HIST', 0.0))
        self.downtrend_reversal_allowed_coins = {
            c.strip().upper() for c in os.getenv(
                'DOWNTREND_REVERSAL_ALLOWED_COINS', 'ETH').split(',') if c.strip()
        }
        self.downtrend_reversal_fast_exit_enabled = _env_bool(
            'DOWNTREND_REVERSAL_FAST_EXIT_ENABLED', True)
        self.downtrend_reversal_fast_exit_seconds = int(
            os.getenv('DOWNTREND_REVERSAL_FAST_EXIT_SECONDS', 120))
        self.downtrend_reversal_flat_max_profit_pct = float(
            os.getenv('DOWNTREND_REVERSAL_FLAT_MAX_PROFIT_PCT', 0.10))
        self.downtrend_reversal_weak_signal_exit_seconds = int(
            os.getenv('DOWNTREND_REVERSAL_WEAK_SIGNAL_EXIT_SECONDS', 45))
        self.downtrend_reversal_weak_signal_max_profit_pct = float(
            os.getenv('DOWNTREND_REVERSAL_WEAK_SIGNAL_MAX_PROFIT_PCT', 0.02))
        self.downtrend_reversal_max_hold_seconds = int(
            os.getenv('DOWNTREND_REVERSAL_MAX_HOLD_SECONDS', 240))
        self.uptrend_entry_gate_enabled = _env_bool(
            'UPTREND_ENTRY_GATE_ENABLED', True)
        self.uptrend_entry_max_rsi = float(
            os.getenv('UPTREND_ENTRY_MAX_RSI', 72.0))
        self.uptrend_entry_max_rsi_by_coin = _env_symbol_float_map(
            'UPTREND_ENTRY_MAX_RSI_BY_COIN', 'XDC:78,TRX:74')
        self.uptrend_entry_min_buy_proba = float(
            os.getenv('UPTREND_ENTRY_MIN_BUY_PROBA', 0.24))
        self.uptrend_entry_min_buy_proba_by_coin = _env_symbol_float_map(
            'UPTREND_ENTRY_MIN_BUY_PROBA_BY_COIN', 'TRX:0.150')
        self.uptrend_entry_max_sell_proba = float(
            os.getenv('UPTREND_ENTRY_MAX_SELL_PROBA', 0.34))
        self.uptrend_entry_max_sell_proba_by_coin = _env_symbol_float_map(
            'UPTREND_ENTRY_MAX_SELL_PROBA_BY_COIN', 'ONDO:0.445')
        self.uptrend_entry_min_proba_edge = float(
            os.getenv('UPTREND_ENTRY_MIN_PROBA_EDGE', -0.05))
        self.uptrend_entry_min_proba_edge_by_coin = _env_symbol_float_map(
            'UPTREND_ENTRY_MIN_PROBA_EDGE_BY_COIN', 'TRX:-0.13,ONDO:-0.055')
        self.uptrend_rules_fast_exit_enabled = _env_bool(
            'UPTREND_RULES_FAST_EXIT_ENABLED', True)
        self.uptrend_rules_fast_exit_seconds = int(
            os.getenv('UPTREND_RULES_FAST_EXIT_SECONDS', 120))
        self.uptrend_rules_fast_exit_seconds_by_coin = _env_symbol_int_map(
            'UPTREND_RULES_FAST_EXIT_SECONDS_BY_COIN')
        self.uptrend_rules_flat_max_profit_pct = float(
            os.getenv('UPTREND_RULES_FLAT_MAX_PROFIT_PCT', 0.08))
        self.uptrend_rules_flat_max_profit_pct_by_coin = _env_symbol_float_map(
            'UPTREND_RULES_FLAT_MAX_PROFIT_PCT_BY_COIN')
        self.uptrend_rules_max_hold_seconds = int(
            os.getenv('UPTREND_RULES_MAX_HOLD_SECONDS', 300))
        self.uptrend_rules_max_hold_seconds_by_coin = _env_symbol_int_map(
            'UPTREND_RULES_MAX_HOLD_SECONDS_BY_COIN')
        # Optional momentum quality filter for new BUY entries.
        self.entry_momentum_filter_enabled = _env_bool(
            'ENTRY_MOMENTUM_FILTER_ENABLED', True)
        self.entry_min_ret_3 = float(os.getenv('ENTRY_MIN_RET_3', -0.01))
        self.entry_min_ret_3_by_coin = _env_symbol_float_map(
            'ENTRY_MIN_RET_3_BY_COIN')
        self.entry_require_price_above_ema20 = _env_bool(
            'ENTRY_REQUIRE_PRICE_ABOVE_EMA20', False)
        self.entry_sharp_pump_filter_enabled = _env_bool(
            'ENTRY_SHARP_PUMP_FILTER_ENABLED', True)
        self.entry_max_ret_1 = float(os.getenv('ENTRY_MAX_RET_1', 0.04))
        self.entry_max_ret_3 = float(os.getenv('ENTRY_MAX_RET_3', 0.08))
        self.reentry_cooldown_seconds = int(
            os.getenv('REENTRY_COOLDOWN_SECONDS', 900))
        self.reentry_dynamic_enabled = _env_bool(
            'REENTRY_DYNAMIC_ENABLED', True)
        self.reentry_cooldown_min_seconds = int(
            os.getenv('REENTRY_COOLDOWN_MIN_SECONDS', 300))
        self.reentry_cooldown_max_seconds = int(
            os.getenv(
                'REENTRY_COOLDOWN_MAX_SECONDS',
                max(1200, self.reentry_cooldown_seconds),
            ))
        if self.reentry_cooldown_max_seconds < self.reentry_cooldown_min_seconds:
            self.reentry_cooldown_max_seconds = self.reentry_cooldown_min_seconds
        self.reentry_volatility_low_atr_pct = float(
            os.getenv('REENTRY_VOLATILITY_LOW_ATR_PCT', 0.008))
        self.reentry_volatility_high_atr_pct = float(
            os.getenv('REENTRY_VOLATILITY_HIGH_ATR_PCT', 0.018))
        self.reentry_cooldown_mult_low_vol = float(
            os.getenv('REENTRY_COOLDOWN_MULT_LOW_VOL', 0.70))
        self.reentry_cooldown_mult_mid_vol = float(
            os.getenv('REENTRY_COOLDOWN_MULT_MID_VOL', 1.00))
        self.reentry_cooldown_mult_high_vol = float(
            os.getenv('REENTRY_COOLDOWN_MULT_HIGH_VOL', 1.30))
        # Enables additional exit signals from market analysis
        self.enable_signal_exits = _env_bool('ENABLE_SIGNAL_EXITS', True)
        # Close positions also on HOLD (Down-Trend) signal
        self.exit_on_downtrend = _env_bool('EXIT_ON_DOWNTREND', True)
        # ATR multipliers for stop-loss and take-profit
        self.atr_stop_mult = float(os.getenv('ATR_STOP_MULT', 1.0))
        self.atr_tp_mult = float(os.getenv('ATR_TP_MULT', 2.0))
        self.partial_take_profit_enabled = _env_bool(
            'PARTIAL_TAKE_PROFIT_ENABLED', True)
        self.partial_take_profit_atr_mult = float(
            os.getenv('PARTIAL_TAKE_PROFIT_ATR_MULT', 1.0))
        self.partial_take_profit_fraction = float(
            os.getenv('PARTIAL_TAKE_PROFIT_FRACTION', 0.5))
        self.partial_take_profit_remainder_max_hold_seconds = int(
            os.getenv('PARTIAL_TAKE_PROFIT_REMAINDER_MAX_HOLD_SECONDS', 300))
        self.partial_take_profit_exit_on_weak_signal = _env_bool(
            'PARTIAL_TAKE_PROFIT_EXIT_ON_WEAK_SIGNAL', True)
        self.trailing_stop_enabled = _env_bool('TRAILING_STOP_ENABLED', True)
        self.trailing_stop_atr_mult = float(
            os.getenv('TRAILING_STOP_ATR_MULT', 1.0))
        self.break_even_enabled = _env_bool('BREAK_EVEN_ENABLED', True)
        self.break_even_trigger_pct = float(
            os.getenv('BREAK_EVEN_TRIGGER_PCT', 1.0))
        self.break_even_buffer_pct = float(
            os.getenv('BREAK_EVEN_BUFFER_PCT', 0.1))
        # Maximum hold time in seconds (0 = disabled)
        self.max_hold_seconds = int(os.getenv('MAX_HOLD_SECONDS', 120))
        self.coin_max_hold_seconds = _env_symbol_int_map(
            'COIN_MAX_HOLD_SECONDS', 'LINK:420,SUI:420,ONDO:420')
        # Allow smaller trades when cash is below TRADE_AMOUNT
        self.allow_partial_trades = _env_bool('ALLOW_PARTIAL_TRADES', True)
        # Fraction of cash kept in reserve (safety buffer)
        self.cash_reserve_pct = float(os.getenv('CASH_RESERVE_PCT', 0.02))
        # Additional data for calculations
        self.ohlcv_limit = max(self.rsi_period, self.sma_period_long) + 5
        # OHLCV timeframe (e.g. 1h = 60m)
        self.ohlcv_timeframe = os.getenv('OHLCV_TIMEFRAME', '1h')
        # ML-Modell Integration
        self.use_ml_model = _env_bool('USE_ML_MODEL', False)
        self.model_path = os.getenv(
            'MODEL_PATH', './model/fine_tuned_trading_model')
        # Minimum confidence for ML signals (0.0 - 1.0)
        self.ml_min_confidence = float(os.getenv('ML_MIN_CONFIDENCE', 0.5))
        # Optional CatBoost model for fast tabular signals
        self.use_tabular_model = _env_bool('USE_TABULAR_MODEL', False)
        self.tabular_model_path = os.getenv(
            'TABULAR_MODEL_PATH', './model/catboost_trading_model')
        self.tabular_research_signal_path = os.getenv(
            'TABULAR_RESEARCH_SIGNAL_PATH', './data/research_signal_latest.json')
        self.tabular_min_confidence = float(
            os.getenv('TABULAR_MIN_CONFIDENCE', 0.45))
        self.tabular_buy_min_confidence = float(
            os.getenv('TABULAR_BUY_MIN_CONFIDENCE', str(self.tabular_min_confidence)))
        self.tabular_allow_buy_entries = _env_bool(
            'TABULAR_ALLOW_BUY_ENTRIES', False)
        self.tabular_source_gate_enabled = _env_bool(
            'TABULAR_SOURCE_GATE_ENABLED', False)
        self.tabular_override_min_confidence = float(
            os.getenv('TABULAR_OVERRIDE_MIN_CONFIDENCE', 0.60))
        self.tabular_override_margin = float(
            os.getenv('TABULAR_OVERRIDE_MARGIN', 0.15))
        # Optional safety filter: block new entries when CatBoost signals a strong sell
        self.tabular_block_on_sell = _env_bool('TABULAR_BLOCK_ON_SELL', False)
        self.tabular_sell_block_min_proba = float(
            os.getenv('TABULAR_SELL_BLOCK_MIN_PROBA', 0.50))
        # Optional: automatic recommendation/application of confidence threshold from journal data
        self.auto_tune_tabular_confidence = _env_bool(
            'AUTO_TUNE_TABULAR_CONFIDENCE', False)
        self.auto_tune_lookback_trades = int(
            os.getenv('AUTO_TUNE_LOOKBACK_TRADES', 200))
        self.auto_tune_threshold_min = float(
            os.getenv('AUTO_TUNE_THRESHOLD_MIN', 0.45))
        self.auto_tune_threshold_max = float(
            os.getenv('AUTO_TUNE_THRESHOLD_MAX', 0.90))
        self.auto_tune_threshold_step = float(
            os.getenv('AUTO_TUNE_THRESHOLD_STEP', 0.02))
        self.auto_tune_min_trades = int(os.getenv('AUTO_TUNE_MIN_TRADES', 10))
        # Safety brake: maximum threshold adjustment per bot start
        self.auto_tune_max_delta = float(
            os.getenv('AUTO_TUNE_MAX_DELTA', 0.02))
        # Cooldown between auto-tune adjustments (in minutes)
        self.auto_tune_cooldown_minutes = int(
            os.getenv('AUTO_TUNE_COOLDOWN_MINUTES', 0))
        self.auto_tune_state_file = os.getenv(
            'AUTO_TUNE_STATE_FILE', 'auto_tune_state.json')
        # Performance-Tracking / Trade-Journal
        self.performance_log_enabled = _env_bool(
            'PERFORMANCE_LOG_ENABLED', True)
        self.performance_log_file = os.getenv(
            'PERFORMANCE_LOG_FILE', 'trade_journal.csv')
        self.performance_report_every = int(
            os.getenv('PERFORMANCE_REPORT_EVERY', 1))
        self.analytics_db_enabled = _env_bool_any(
            ['ANALYTICS_DB_ENABLED', 'POSTGRES_ENABLED'], False)
        self.analytics_db_url = _env_str_any(
            ['ANALYTICS_DB_URL', 'POSTGRES_URL'], '')
        self.analytics_db_host = _env_str_any(
            ['ANALYTICS_DB_HOST', 'POSTGRES_HOST'], '')
        self.analytics_db_port = int(_env_str_any(
            ['ANALYTICS_DB_PORT', 'POSTGRES_PORT'], '5432'))
        self.analytics_db_name = _env_str_any(
            ['ANALYTICS_DB_NAME', 'POSTGRES_DB'], '')
        self.analytics_db_user = _env_str_any(
            ['ANALYTICS_DB_USER', 'POSTGRES_USER'], '')
        self.analytics_db_password = _env_str_any(
            ['ANALYTICS_DB_PASSWORD', 'POSTGRES_PASSWORD'], '')
        self.analytics_db_sslmode = _env_str_any(
            ['ANALYTICS_DB_SSLMODE', 'POSTGRES_SSLMODE'], 'prefer')
        self.analytics_db_connect_timeout_seconds = int(
            _env_str_any(['ANALYTICS_DB_CONNECT_TIMEOUT_SECONDS', 'POSTGRES_CONNECT_TIMEOUT_SECONDS'], '5'))
        self.analytics_db_schema = _env_str_any(
            ['ANALYTICS_DB_SCHEMA', 'POSTGRES_SCHEMA'], 'public')
        self.analytics_db_snapshot_every = int(
            _env_str_any(['ANALYTICS_DB_SNAPSHOT_EVERY', 'POSTGRES_SNAPSHOT_EVERY'], '1'))
        # Hard risk guardrails (0 = disabled)
        self.max_daily_loss_pct = float(os.getenv('MAX_DAILY_LOSS_PCT', 0))
        self.max_buys_per_hour = int(os.getenv('MAX_BUYS_PER_HOUR', 0))
        self.loss_streak_pause_threshold = int(
            os.getenv('LOSS_STREAK_PAUSE_THRESHOLD', 0))
        self.loss_streak_pause_seconds = int(
            os.getenv('LOSS_STREAK_PAUSE_SECONDS', 0))

        # Optional external AI co-pilot (budget-limited, disabled by default)
        self.ai_copilot_enabled = _env_bool('AI_COPILOT_ENABLED', False)
        self.ai_copilot_api_url = _env_str(
            'AI_COPILOT_API_URL', 'https://api.mammouth.ai/v1/chat/completions')
        self.ai_copilot_api_key = _env_str('MAMMOUTH_API_KEY', '')
        self.ai_copilot_model = _env_str('AI_COPILOT_MODEL', 'gpt-5.4-nano')
        self.ai_copilot_interval_minutes = int(
            os.getenv('AI_COPILOT_INTERVAL_MINUTES', 62))
        self.ai_copilot_state_file = os.getenv(
            'AI_COPILOT_STATE_FILE', 'ai_copilot_state.json')
        self.ai_copilot_shadow_mode = _env_bool('AI_COPILOT_SHADOW_MODE', True)
        self.ai_copilot_max_calls_per_day = int(
            os.getenv('AI_COPILOT_MAX_CALLS_PER_DAY', 24))
        self.ai_copilot_max_calls_per_month = int(
            os.getenv('AI_COPILOT_MAX_CALLS_PER_MONTH', 720))
        self.ai_copilot_max_budget_usd_per_month = float(
            os.getenv('AI_COPILOT_MAX_BUDGET_USD_PER_MONTH', 5.0))
        self.ai_copilot_max_output_tokens = int(
            os.getenv('AI_COPILOT_MAX_OUTPUT_TOKENS', 300))
        self.ai_copilot_temperature = float(
            os.getenv('AI_COPILOT_TEMPERATURE', 0.1))
        self.ai_copilot_max_consecutive_errors = int(
            os.getenv('AI_COPILOT_MAX_CONSECUTIVE_ERRORS', 3))
        self.ai_copilot_cost_input_per_mtok = float(
            os.getenv('AI_COPILOT_COST_INPUT_PER_MTOK', 0.2))
        self.ai_copilot_cost_output_per_mtok = float(
            os.getenv('AI_COPILOT_COST_OUTPUT_PER_MTOK', 1.25))
        self.ai_copilot_hailo_gate_enabled = _env_bool(
            'AI_COPILOT_HAILO_GATE_ENABLED', False)
        self.ai_copilot_hailo_alerts_file = _env_str(
            'AI_COPILOT_HAILO_ALERTS_FILE', '/tmp/hailo_alerts.jsonl')
        self.ai_copilot_hailo_min_score = float(
            os.getenv('AI_COPILOT_HAILO_MIN_SCORE', os.getenv('HAILO8_ANOMALY_THRESHOLD', '85')))
        self.ai_copilot_hailo_min_confidence = float(
            os.getenv('AI_COPILOT_HAILO_MIN_CONFIDENCE', '0.6'))
        self.ai_copilot_hailo_max_alert_age_seconds = int(
            os.getenv('AI_COPILOT_HAILO_MAX_ALERT_AGE_SECONDS', '900'))
        self.ai_copilot_hailo_required_alerts = int(
            os.getenv('AI_COPILOT_HAILO_REQUIRED_ALERTS', '1'))
        self.ai_copilot_hailo_max_lines_scan = int(
            os.getenv('AI_COPILOT_HAILO_MAX_LINES_SCAN', '400'))
        self.ai_copilot_min_entry_score_min = int(
            os.getenv('AI_COPILOT_MIN_ENTRY_SCORE_MIN', 55))
        self.ai_copilot_min_entry_score_max = int(
            os.getenv('AI_COPILOT_MIN_ENTRY_SCORE_MAX', 70))
        self.ai_copilot_reentry_cooldown_min = int(
            os.getenv('AI_COPILOT_REENTRY_COOLDOWN_MIN', 300))
        self.ai_copilot_reentry_cooldown_max = int(
            os.getenv('AI_COPILOT_REENTRY_COOLDOWN_MAX', 1200))
        self.ai_copilot_tabular_buy_conf_min = float(
            os.getenv('AI_COPILOT_TABULAR_BUY_CONF_MIN', 0.50))
        self.ai_copilot_tabular_buy_conf_max = float(
            os.getenv('AI_COPILOT_TABULAR_BUY_CONF_MAX', 0.65))
        self.ai_copilot_benchmark_enabled = _env_bool(
            'AI_COPILOT_BENCHMARK_ENABLED', False)
        self.ai_copilot_benchmark_model = _env_str(
            'AI_COPILOT_BENCHMARK_MODEL', '')
        self.ai_copilot_benchmark_interval_minutes = int(
            os.getenv('AI_COPILOT_BENCHMARK_INTERVAL_MINUTES', str(self.ai_copilot_interval_minutes)))
        self.ai_copilot_benchmark_state_file = os.getenv(
            'AI_COPILOT_BENCHMARK_STATE_FILE', 'ai_copilot_benchmark_state.json')
        self.ai_copilot_benchmark_max_calls_per_day = int(
            os.getenv('AI_COPILOT_BENCHMARK_MAX_CALLS_PER_DAY', str(self.ai_copilot_max_calls_per_day)))
        self.ai_copilot_benchmark_max_calls_per_month = int(
            os.getenv('AI_COPILOT_BENCHMARK_MAX_CALLS_PER_MONTH', str(self.ai_copilot_max_calls_per_month)))
        self.ai_copilot_benchmark_max_budget_usd_per_month = float(
            os.getenv('AI_COPILOT_BENCHMARK_MAX_BUDGET_USD_PER_MONTH', 1.0))
        self.ai_copilot_benchmark_max_output_tokens = int(
            os.getenv('AI_COPILOT_BENCHMARK_MAX_OUTPUT_TOKENS', str(self.ai_copilot_max_output_tokens)))
        self.ai_copilot_benchmark_temperature = float(
            os.getenv('AI_COPILOT_BENCHMARK_TEMPERATURE', str(self.ai_copilot_temperature)))
        self.ai_copilot_benchmark_max_consecutive_errors = int(
            os.getenv('AI_COPILOT_BENCHMARK_MAX_CONSECUTIVE_ERRORS', str(self.ai_copilot_max_consecutive_errors)))
        self.ai_copilot_benchmark_cost_input_per_mtok = float(
            os.getenv('AI_COPILOT_BENCHMARK_COST_INPUT_PER_MTOK', str(self.ai_copilot_cost_input_per_mtok)))
        self.ai_copilot_benchmark_cost_output_per_mtok = float(
            os.getenv('AI_COPILOT_BENCHMARK_COST_OUTPUT_PER_MTOK', str(self.ai_copilot_cost_output_per_mtok)))

    def analytics_db_connect_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        if self.analytics_db_url:
            kwargs['conninfo'] = self.analytics_db_url
        else:
            if self.analytics_db_host:
                kwargs['host'] = self.analytics_db_host
            if self.analytics_db_port > 0:
                kwargs['port'] = self.analytics_db_port
            if self.analytics_db_name:
                kwargs['dbname'] = self.analytics_db_name
            if self.analytics_db_user:
                kwargs['user'] = self.analytics_db_user
            if self.analytics_db_password:
                kwargs['password'] = self.analytics_db_password
            if self.analytics_db_sslmode:
                kwargs['sslmode'] = self.analytics_db_sslmode

        timeout_seconds = max(
            1, int(self.analytics_db_connect_timeout_seconds))
        kwargs['connect_timeout'] = timeout_seconds
        return kwargs


class PostgresAnalyticsWriter:
    """Optional Postgres sink for trade and portfolio analytics."""

    def __init__(self, config: BotConfig):
        self.config = config
        self.enabled = config.analytics_db_enabled
        self._psycopg = None
        self._conn = None
        self._disabled_reason_logged = False
        self._schema = self._sanitize_identifier(
            config.analytics_db_schema, 'public')

    def _sanitize_identifier(self, value: str, fallback: str) -> str:
        candidate = (value or '').strip()
        if _SAFE_IDENTIFIER_RE.match(candidate):
            return candidate
        return fallback

    def _qualified_table(self, table_name: str) -> str:
        return f'{self._schema}.{table_name}'

    def _ensure_connection(self) -> bool:
        if not self.enabled:
            return False
        if self._conn is not None and not getattr(self._conn, 'closed', False):
            return True
        try:
            if self._psycopg is None:
                self._psycopg = importlib.import_module('psycopg')
        except Exception as exc:
            if not self._disabled_reason_logged:
                logger.warning(
                    'Analytics DB enabled but psycopg is unavailable: %s', exc)
                self._disabled_reason_logged = True
            return False

        kwargs = self.config.analytics_db_connect_kwargs()
        if 'conninfo' not in kwargs and not kwargs.get('host'):
            if not self._disabled_reason_logged:
                logger.warning(
                    'Analytics DB enabled but neither ANALYTICS_DB_URL nor ANALYTICS_DB_HOST is configured.')
                self._disabled_reason_logged = True
            return False

        try:
            self._conn = self._psycopg.connect(**kwargs)
            self._conn.autocommit = True
            self._ensure_schema()
            return True
        except Exception as exc:
            if not self._disabled_reason_logged:
                logger.warning(
                    'Failed to connect to analytics Postgres DB: %s', exc)
                self._disabled_reason_logged = True
            self.close()
            return False

    def _ensure_schema(self) -> None:
        if self._conn is None:
            return
        trades_table = self._qualified_table('trade_events')
        snapshots_table = self._qualified_table('portfolio_snapshots')
        with self._conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS {self._schema}')
            cur.execute(
                f'''
                CREATE TABLE IF NOT EXISTS {trades_table} (
                    id BIGSERIAL PRIMARY KEY,
                    event_ts TIMESTAMPTZ NOT NULL,
                    iteration INTEGER NOT NULL,
                    coin TEXT NOT NULL,
                    action TEXT NOT NULL,
                    price DOUBLE PRECISION NOT NULL,
                    amount_coin DOUBLE PRECISION NOT NULL,
                    amount_base DOUBLE PRECISION NOT NULL,
                    pnl_base DOUBLE PRECISION NOT NULL,
                    pnl_pct DOUBLE PRECISION NOT NULL,
                    hold_seconds DOUBLE PRECISION NOT NULL,
                    signal_source TEXT,
                    signal_confidence DOUBLE PRECISION,
                    recommendation TEXT,
                    reason TEXT,
                    dry_run BOOLEAN NOT NULL,
                    base_currency TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                '''
            )
            cur.execute(
                f'''
                CREATE INDEX IF NOT EXISTS trade_events_event_ts_idx
                ON {trades_table} (event_ts DESC)
                '''
            )
            cur.execute(
                f'''
                CREATE INDEX IF NOT EXISTS trade_events_coin_action_idx
                ON {trades_table} (coin, action)
                '''
            )
            cur.execute(
                f'''
                CREATE TABLE IF NOT EXISTS {snapshots_table} (
                    id BIGSERIAL PRIMARY KEY,
                    snapshot_ts TIMESTAMPTZ NOT NULL,
                    iteration INTEGER NOT NULL,
                    portfolio_value_base DOUBLE PRECISION NOT NULL,
                    cash_base DOUBLE PRECISION NOT NULL,
                    base_currency TEXT NOT NULL,
                    open_positions_count INTEGER NOT NULL,
                    analyzed_coins_count INTEGER NOT NULL,
                    buy_recommendations_count INTEGER NOT NULL,
                    dry_run BOOLEAN NOT NULL,
                    holdings_json JSONB NOT NULL,
                    open_trades_json JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                '''
            )
            cur.execute(
                f'''
                CREATE INDEX IF NOT EXISTS portfolio_snapshots_snapshot_ts_idx
                ON {snapshots_table} (snapshot_ts DESC)
                '''
            )

    def write_trade(self, row: Dict[str, Any], base_currency: str) -> None:
        if not self._ensure_connection():
            return
        trades_table = self._qualified_table('trade_events')
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f'''
                    INSERT INTO {trades_table} (
                        event_ts, iteration, coin, action, price, amount_coin,
                        amount_base, pnl_base, pnl_pct, hold_seconds,
                        signal_source, signal_confidence, recommendation, reason,
                        dry_run, base_currency
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s
                    )
                    ''',
                    (
                        row.get('timestamp', datetime.now(
                            timezone.utc).isoformat()),
                        int(row.get('iteration', 0)),
                        row.get('coin', ''),
                        row.get('action', ''),
                        float(row.get('price', 0.0)),
                        float(row.get('amount_coin', 0.0)),
                        float(row.get('amount_base', 0.0)),
                        float(row.get('pnl_base', 0.0)),
                        float(row.get('pnl_pct', 0.0)),
                        float(row.get('hold_seconds', 0.0)),
                        row.get('signal_source', ''),
                        None if row.get('signal_confidence', '') in (
                            '', None) else float(row.get('signal_confidence', 0.0)),
                        row.get('recommendation', ''),
                        row.get('reason', ''),
                        bool(row.get('dry_run', False)),
                        base_currency,
                    ),
                )
        except Exception as exc:
            logger.warning(
                'Failed to write trade event to analytics DB: %s', exc)
            self.close()

    def write_snapshot(
        self,
        *,
        iteration: int,
        portfolio_value: float,
        cash_value: float,
        base_currency: str,
        dry_run: bool,
        holdings: Dict[str, float],
        open_trades: Dict[str, Dict[str, Any]],
        analyzed_coins_count: int,
        buy_recommendations_count: int,
    ) -> None:
        if not self._ensure_connection():
            return
        snapshots_table = self._qualified_table('portfolio_snapshots')
        serialized_open_trades = json.dumps(
            open_trades,
            default=lambda value: value.isoformat() if isinstance(
                value, datetime) else str(value),
            ensure_ascii=True,
        )
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    f'''
                    INSERT INTO {snapshots_table} (
                        snapshot_ts, iteration, portfolio_value_base, cash_base,
                        base_currency, open_positions_count, analyzed_coins_count,
                        buy_recommendations_count, dry_run, holdings_json, open_trades_json
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s::jsonb, %s::jsonb
                    )
                    ''',
                    (
                        datetime.now(timezone.utc),
                        iteration,
                        float(portfolio_value),
                        float(cash_value),
                        base_currency,
                        len(open_trades),
                        analyzed_coins_count,
                        buy_recommendations_count,
                        bool(dry_run),
                        json.dumps(holdings, ensure_ascii=True),
                        serialized_open_trades,
                    ),
                )
        except Exception as exc:
            logger.warning(
                'Failed to write portfolio snapshot to analytics DB: %s', exc)
            self.close()

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass


class CryptoTradingBot:
    """Main class of the trading bot."""

    def __init__(self, config: BotConfig):
        self.config = config
        self.portfolio = Portfolio(config.base_currency)
        self.analytics_writer = PostgresAnalyticsWriter(config)
        self.iteration = 0
        self.performance = {
            'buys': 0,
            'sells': 0,
            'wins': 0,
            'losses': 0,
            'realized_pnl_base': 0.0,
            'closed_trades': 0,
            'by_source': defaultdict(lambda: {'buys': 0, 'sells': 0, 'pnl': 0.0}),
        }
        self.daily_anchor_date = None
        self.daily_anchor_value: float = 0.0
        self.buy_timestamps_utc: List[datetime] = []
        self.last_sell_timestamps_utc: Dict[str, datetime] = {}
        self.consecutive_losses: int = 0
        self.buy_pause_until_utc: Optional[datetime] = None
        self.exchange = self._initialize_exchange()
        self._simulation_cache_iteration: Optional[int] = None
        self._simulation_ohlcv_cache: Dict[Tuple[int,
                                                 str, str, int], pd.DataFrame] = {}
        # List of tradeable pairs (e.g. BTC/EUR)
        self.all_symbols: List[str] = []
        self.all_coins: List[str] = []   # List of base assets (e.g. BTC)
        if not self.config.simulate_data and self.exchange:
            self._load_market_info()

        # Sicherstellen, dass im Simulationsmodus immer Coins/Symbole gesetzt sind
        if self.config.simulate_data:
            if not self.all_coins:
                self.all_coins = ['BTC', 'ETH']
            if not self.all_symbols:
                self.all_symbols = [
                    f'{coin}/{config.base_currency}' for coin in self.all_coins]

        # ML model loading (optional, activated via USE_ML_MODEL=True)
        self.ml_predictor = None
        if self.config.use_ml_model:
            try:
                from src.predict import TradingModelPredictor
                self.ml_predictor = TradingModelPredictor(
                    self.config.model_path)
                logger.info(
                    f"🤖 ML model loaded from '{self.config.model_path}'.")
            except Exception as e:
                logger.warning(
                    f"ML model could not be loaded: {e}. Using rule-based analysis only.")

        self.tabular_predictor = None
        if self.config.use_tabular_model:
            try:
                from src.predict_catboost import CatBoostTradingPredictor
                self.tabular_predictor = CatBoostTradingPredictor(
                    self.config.tabular_model_path,
                    research_signal_path=self.config.tabular_research_signal_path,
                )
                logger.info(
                    f"📊 CatBoost model loaded from '{self.config.tabular_model_path}'.")
            except Exception as e:
                logger.warning(
                    f"CatBoost model could not be loaded: {e}.")

        if self.config.performance_log_enabled:
            self._init_trade_journal()

        if self.config.use_tabular_model:
            self._maybe_auto_tune_tabular_confidence()

    def _init_trade_journal(self):
        """Creates the journal file with header if it does not yet exist."""
        if os.path.exists(self.config.performance_log_file):
            return
        with open(self.config.performance_log_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'iteration', 'coin', 'action', 'price', 'amount_coin',
                'amount_base', 'pnl_base', 'pnl_pct', 'hold_seconds',
                'signal_source', 'signal_confidence', 'recommendation', 'reason', 'dry_run'
            ])

    def _append_trade_journal(self, row: Dict):
        if self.config.performance_log_enabled:
            with open(self.config.performance_log_file, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow([
                    row.get('timestamp', datetime.now().isoformat()),
                    row.get('iteration', self.iteration),
                    row.get('coin', ''),
                    row.get('action', ''),
                    row.get('price', 0.0),
                    row.get('amount_coin', 0.0),
                    row.get('amount_base', 0.0),
                    row.get('pnl_base', 0.0),
                    row.get('pnl_pct', 0.0),
                    row.get('hold_seconds', 0.0),
                    row.get('signal_source', ''),
                    row.get('signal_confidence', ''),
                    row.get('recommendation', ''),
                    row.get('reason', ''),
                    row.get('dry_run', self.config.dry_run),
                ])
        self.analytics_writer.write_trade(row, self.config.base_currency)

    def _write_analytics_snapshot(
        self,
        portfolio_value: float,
        market_analysis: Dict[str, Dict[str, Any]],
    ) -> None:
        snapshot_every = max(1, int(self.config.analytics_db_snapshot_every))
        if self.iteration % snapshot_every != 0:
            return
        buy_recommendations_count = sum(
            1 for data in market_analysis.values()
            if data.get('recommendation') == 'BUY'
        )
        self.analytics_writer.write_snapshot(
            iteration=self.iteration,
            portfolio_value=portfolio_value,
            cash_value=self.portfolio.cash,
            base_currency=self.config.base_currency,
            dry_run=self.config.dry_run,
            holdings=self.portfolio.holdings,
            open_trades=self.portfolio.open_trades,
            analyzed_coins_count=len(market_analysis),
            buy_recommendations_count=buy_recommendations_count,
        )

    def _recommend_tabular_threshold_from_journal(self) -> Optional[float]:
        """Recommends a confidence threshold based on historical sell trades."""
        path = self.config.performance_log_file
        if not os.path.exists(path):
            return None

        try:
            df = pd.read_csv(path)
        except Exception as e:
            logger.warning(
                f"Journal could not be read for auto-tuning: {e}")
            return None

        if df.empty or 'action' not in df.columns or 'pnl_base' not in df.columns or 'signal_confidence' not in df.columns:
            return None

        sells = df[df['action'] == 'sell'].copy()
        if sells.empty:
            return None

        # Prefer more recent trades
        sells = sells.tail(
            max(1, self.config.auto_tune_lookback_trades)).copy()
        sells['pnl_base'] = pd.to_numeric(sells['pnl_base'], errors='coerce')
        sells['signal_confidence'] = pd.to_numeric(
            sells['signal_confidence'], errors='coerce')
        sells = sells.dropna(subset=['pnl_base', 'signal_confidence'])
        if sells.empty:
            return None

        step = max(self.config.auto_tune_threshold_step, 0.001)
        thresholds = np.arange(
            self.config.auto_tune_threshold_min,
            self.config.auto_tune_threshold_max + (step * 0.5),
            step,
        )

        candidates = []
        for th in thresholds:
            part = sells[sells['signal_confidence'] >= th]
            n = len(part)
            if n < self.config.auto_tune_min_trades:
                continue
            pnl = float(part['pnl_base'].sum())
            avg = float(part['pnl_base'].mean())
            win_rate = float((part['pnl_base'] > 0).mean() * 100.0)
            candidates.append((th, n, pnl, avg, win_rate))

        if not candidates:
            return None

        # Primarily highest total PnL, secondarily higher AvgPnL, then more trades
        best = sorted(candidates, key=lambda x: (
            x[2], x[3], x[1]), reverse=True)[0]
        logger.info(
            f"🔧 Auto-Tune Threshold Recommendation: {best[0]:.2f} "
            f"(trades={best[1]}, pnl={best[2]:.6f} {self.config.base_currency}, "
            f"avg={best[3]:.6f}, win_rate={best[4]:.2f}%)")
        return float(best[0])

    def _dynamic_excluded_coins(self) -> Set[str]:
        """Builds a temporary exclusion set from recent sell-side underperformance."""
        if not self.config.dynamic_lossmaker_exclusion_enabled:
            return set()

        path = self.config.performance_log_file
        if not self.config.performance_log_enabled or not os.path.exists(path):
            return set()

        try:
            df = pd.read_csv(path)
        except Exception as e:
            logger.warning(
                f"Journal could not be read for dynamic exclusions: {e}")
            return set()

        required_columns = {'action', 'coin', 'pnl_base', 'reason'}
        if df.empty or not required_columns.issubset(df.columns):
            return set()

        sells = df.loc[
            df['action'].fillna(
                '').astype(str).str.lower() == 'sell'
        ].copy()
        recent_window = sells.tail(self.config.dynamic_lossmaker_window).copy()
        if recent_window.empty:
            return set()

        sells = recent_window.loc[
            recent_window['action'].fillna(
                '').astype(str).str.lower() == 'sell'
        ].copy()
        if sells.empty:
            return set()

        dynamic_exclusions: Set[str] = set()
        for coin, group in sells.groupby('coin', dropna=True):
            if len(group) < self.config.dynamic_lossmaker_min_sells:
                continue

            pnl = pd.to_numeric(group['pnl_base'], errors='coerce').fillna(0.0)
            pnl_sum = float(pnl.sum())
            if pnl_sum > -self.config.dynamic_lossmaker_min_pnl_loss:
                continue

            win_rate_pct = float((pnl > 0).mean() * 100.0)
            if win_rate_pct > self.config.dynamic_lossmaker_max_win_rate_pct:
                continue

            reasons = group['reason'].fillna('').astype(str)
            max_hold_exit_ratio = float(
                reasons.str.contains(
                    'MAX-HOLD-TIME', case=False, na=False).mean()
            )
            if max_hold_exit_ratio < self.config.dynamic_lossmaker_min_max_hold_exit_ratio:
                continue

            dynamic_exclusions.add(str(coin).upper())

        return dynamic_exclusions

    def _dynamic_excluded_entry_pairs(self) -> Set[Tuple[str, str]]:
        """Builds a temporary exclusion set for recent coin/source entry pairs."""
        if not self.config.dynamic_lossmaker_exclusion_enabled:
            return set()

        path = self.config.performance_log_file
        if not self.config.performance_log_enabled or not os.path.exists(path):
            return set()

        try:
            df = pd.read_csv(path)
        except Exception as e:
            logger.warning(
                f"Journal could not be read for dynamic entry pair exclusions: {e}")
            return set()

        blocked_pairs = _compute_dynamic_lossmaker_entry_pairs(
            df,
            window=self.config.dynamic_lossmaker_window,
            min_sells=self.config.dynamic_lossmaker_min_sells,
            max_win_rate_pct=self.config.dynamic_lossmaker_max_win_rate_pct,
            min_pnl_loss=self.config.dynamic_lossmaker_min_pnl_loss,
            min_max_hold_exit_ratio=self.config.dynamic_lossmaker_min_max_hold_exit_ratio,
            max_avg_hold_seconds=max(
                float(self.config.uptrend_rules_fast_exit_seconds),
                60.0,
            ),
        )
        return blocked_pairs

    def _degraded_entry_sources(self) -> Set[str]:
        """Builds a temporary blocklist for weak signal sources in fallback paths."""
        if not self.config.dynamic_lossmaker_exclusion_enabled:
            return set()

        path = self.config.performance_log_file
        if not self.config.performance_log_enabled or not os.path.exists(path):
            return set()

        try:
            df = pd.read_csv(path)
        except Exception as e:
            logger.warning(
                f"Journal could not be read for dynamic source exclusions: {e}")
            return set()

        return _compute_degraded_entry_sources(
            df,
            window=self.config.dynamic_lossmaker_window,
            min_sells=self.config.dynamic_lossmaker_min_sells,
            min_pnl_loss=self.config.dynamic_lossmaker_min_pnl_loss,
            max_profit_factor=1.0,
            max_avg_hold_seconds=max(
                float(self.config.uptrend_rules_fast_exit_seconds),
                120.0,
            ),
        )

    def _recent_pnl_guard_state(self) -> Dict[str, Any]:
        """Builds a recent realized-PnL guard state to tighten entries in weak phases."""
        if not self.config.recent_pnl_guard_enabled:
            return {
                "active": False,
                "recent_trades": 0,
                "recent_realized_pnl": 0.0,
                "recent_profit_factor": 999.0,
                "reason": "disabled",
            }

        path = self.config.performance_log_file
        if not self.config.performance_log_enabled or not os.path.exists(path):
            return {
                "active": False,
                "recent_trades": 0,
                "recent_realized_pnl": 0.0,
                "recent_profit_factor": 999.0,
                "reason": "journal_missing",
            }

        try:
            df = pd.read_csv(path)
        except Exception as e:
            logger.warning(
                f"Journal could not be read for recent PnL guard: {e}")
            return {
                "active": False,
                "recent_trades": 0,
                "recent_realized_pnl": 0.0,
                "recent_profit_factor": 999.0,
                "reason": "journal_read_error",
            }

        return _compute_recent_pnl_guard_state(
            df,
            window=self.config.recent_pnl_guard_window,
            min_trades=self.config.recent_pnl_guard_min_trades,
            min_realized_pnl=self.config.recent_pnl_guard_min_realized_pnl,
            max_profit_factor=self.config.recent_pnl_guard_max_profit_factor,
        )

    def _read_auto_tune_state(self) -> Dict:
        path = self.config.auto_tune_state_file
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _write_auto_tune_state(self, state: Dict):
        path = self.config.auto_tune_state_file
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=True, indent=2)
        except Exception as e:
            logger.warning(
                f"Auto-tune state could not be saved: {e}")

    def _cooldown_remaining_minutes(self, state: Dict) -> float:
        cooldown = max(0, self.config.auto_tune_cooldown_minutes)
        if cooldown <= 0:
            return 0.0
        last = state.get('last_applied_at')
        if not last:
            return 0.0
        try:
            last_dt = datetime.fromisoformat(last)
            # Backward-compatible: treat naive timestamps as UTC.
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except Exception:
            return 0.0
        elapsed_minutes = (datetime.now(timezone.utc) -
                           last_dt).total_seconds() / 60.0
        return max(0.0, cooldown - elapsed_minutes)

    def _maybe_auto_tune_tabular_confidence(self):
        """Applies the recommended tabular threshold if requested."""
        recommended = self._recommend_tabular_threshold_from_journal()
        if recommended is None:
            logger.info(
                "ℹ️ No auto-tune threshold available (insufficient valid journal data).")
            return

        if self.config.auto_tune_tabular_confidence:
            state = self._read_auto_tune_state()
            remaining = self._cooldown_remaining_minutes(state)
            if remaining > 0:
                logger.info(
                    f"⏱️ AUTO_TUNE cooldown active: next update in approx. {remaining:.1f} min."
                )
                return

            old = self.config.tabular_min_confidence
            max_delta = max(0.0, self.config.auto_tune_max_delta)
            if max_delta > 0:
                lower = old - max_delta
                upper = old + max_delta
                adjusted = min(max(recommended, lower), upper)
            else:
                adjusted = recommended

            # Protect: stay within configured search range
            adjusted = min(max(adjusted, self.config.auto_tune_threshold_min),
                           self.config.auto_tune_threshold_max)
            self.config.tabular_min_confidence = adjusted
            logger.info(
                f"✅ AUTO_TUNE active: TABULAR_MIN_CONFIDENCE {old:.2f} -> {adjusted:.2f} "
                f"(recommended {recommended:.2f}, max_delta {max_delta:.2f})")

            self._write_auto_tune_state({
                'last_applied_at': datetime.now(timezone.utc).isoformat(),
                'last_recommended': recommended,
                'last_applied': adjusted,
            })
        else:
            logger.info(
                f"ℹ️ AUTO_TUNE disabled. Recommended TABULAR_MIN_CONFIDENCE: {recommended:.2f}")

    def _read_ai_copilot_state(self, path: Optional[str] = None) -> Dict:
        path = path or self.config.ai_copilot_state_file
        if not path:
            return {}

        candidates = [path, f'{path}.bak']
        last_error = None
        for candidate in candidates:
            if not os.path.exists(candidate):
                continue
            try:
                with open(candidate, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                if candidate != path:
                    logger.warning(
                        'Recovered AI co-pilot state from backup file')
                return state
            except Exception as exc:
                last_error = exc

        if last_error is not None:
            logger.warning(
                f'AI co-pilot state could not be loaded: {last_error}')
        return {}

    def _write_ai_copilot_state(self, state: Dict, path: Optional[str] = None):
        path = path or self.config.ai_copilot_state_file
        if not path:
            return
        tmp_path = f'{path}.tmp'
        backup_path = f'{path}.bak'
        try:
            if os.path.exists(path):
                try:
                    shutil.copyfile(path, backup_path)
                except Exception as exc:
                    logger.warning(
                        f'AI co-pilot state backup could not be updated: {exc}')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=True, indent=2)
            os.replace(tmp_path, path)
        except Exception as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
            logger.warning(f"AI co-pilot state could not be saved: {e}")

    def _current_month_key(self) -> str:
        now = datetime.now(timezone.utc)
        return f"{now.year:04d}-{now.month:02d}"

    def _current_day_key(self) -> str:
        now = datetime.now(timezone.utc)
        return f"{now.year:04d}-{now.month:02d}-{now.day:02d}"

    def _normalize_ai_state(self, state: Dict, monthly_call_cap: int, budget_cap_usd: float) -> Dict:
        month_key = self._current_month_key()
        day_key = self._current_day_key()
        state['budget_cap_usd'] = float(budget_cap_usd)
        state['calls_cap_monthly'] = int(monthly_call_cap)
        if state.get('month_key') != month_key:
            state['month_key'] = month_key
            state['monthly_calls'] = 0
            state['monthly_spend_usd'] = 0.0
        if state.get('day_key') != day_key:
            state['day_key'] = day_key
            state['daily_calls'] = 0
            state['consecutive_errors'] = 0
            state.pop('last_suspended_at', None)
        state.setdefault('monthly_calls', 0)
        state.setdefault('daily_calls', 0)
        state.setdefault('monthly_spend_usd', 0.0)
        state.setdefault('consecutive_errors', 0)
        return state

    def _estimate_ai_cost_usd(self, prompt_tokens: int, completion_tokens: int, input_cost_per_mtok: float, output_cost_per_mtok: float) -> float:
        input_cost = (max(0, prompt_tokens) / 1_000_000.0) * \
            input_cost_per_mtok
        output_cost = (max(0, completion_tokens) / 1_000_000.0) * \
            output_cost_per_mtok
        return float(input_cost + output_cost)

    def _can_run_ai_copilot(
        self,
        state: Dict,
        *,
        enabled: bool,
        model: str,
        api_key: str,
        max_consecutive_errors: int,
        max_calls_per_day: int,
        max_calls_per_month: int,
        max_budget_usd_per_month: float,
        interval_minutes: int,
    ) -> Tuple[bool, str]:
        if not enabled:
            return False, 'disabled'
        if not model:
            return False, 'missing_model'
        if not api_key:
            return False, 'missing_api_key'
        if (
            max_consecutive_errors > 0
            and state.get('consecutive_errors', 0) >= max_consecutive_errors
            and state.get('last_suspended_at')
        ):
            return False, 'suspended_after_errors'
        if max_calls_per_day > 0 and state.get('daily_calls', 0) >= max_calls_per_day:
            return False, 'daily_call_limit'
        if max_calls_per_month > 0 and state.get('monthly_calls', 0) >= max_calls_per_month:
            return False, 'monthly_call_limit'
        if max_budget_usd_per_month > 0 and state.get('monthly_spend_usd', 0.0) >= max_budget_usd_per_month:
            return False, 'monthly_budget_limit'

        last_run_raw = state.get('last_attempt_at') or state.get('last_run_at')
        if last_run_raw and interval_minutes > 0:
            try:
                last_run = datetime.fromisoformat(last_run_raw)
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)
                elapsed_min = (datetime.now(timezone.utc) -
                               last_run).total_seconds() / 60.0
                if elapsed_min < interval_minutes:
                    return False, 'interval_not_reached'
            except Exception:
                pass
        return True, 'ok'

    @staticmethod
    def _parse_iso8601_utc(raw: Any) -> Optional[datetime]:
        text = str(raw or '').strip()
        if not text:
            return None
        normalized = text.replace('Z', '+00:00')
        try:
            parsed = datetime.fromisoformat(normalized)
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _read_recent_hailo_alerts(self) -> List[Dict[str, Any]]:
        path = self.config.ai_copilot_hailo_alerts_file
        if not path or not os.path.exists(path):
            return []

        max_lines = max(1, int(self.config.ai_copilot_hailo_max_lines_scan))
        buffered: List[str] = []
        try:
            with open(path, 'r', encoding='utf-8') as f:
                buffered = list(deque(f, maxlen=max_lines))
        except Exception as exc:
            logger.warning(
                'Could not read Hailo alerts file %s: %s', path, exc)
            return []

        now_utc = datetime.now(timezone.utc)
        max_age = max(
            0, int(self.config.ai_copilot_hailo_max_alert_age_seconds))
        min_score = float(self.config.ai_copilot_hailo_min_score)
        min_confidence = float(self.config.ai_copilot_hailo_min_confidence)

        recent: List[Dict[str, Any]] = []
        for raw_line in reversed(buffered):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue

            timestamp = self._parse_iso8601_utc(payload.get('timestamp'))
            if timestamp is None:
                continue
            age_seconds = (now_utc - timestamp).total_seconds()
            if age_seconds < 0:
                age_seconds = 0.0
            if max_age > 0 and age_seconds > max_age:
                break

            try:
                score = float(payload.get('hailo_score', 0.0))
            except Exception:
                score = 0.0
            try:
                confidence = float(payload.get('confidence', 0.0))
            except Exception:
                confidence = 0.0

            if score < min_score or confidence < min_confidence:
                continue

            recent.append({
                'timestamp': timestamp.isoformat(),
                'age_seconds': round(float(age_seconds), 2),
                'hailo_score': round(score, 4),
                'confidence': round(confidence, 4),
                'coin': str(payload.get('coin', '') or ''),
                'signal_type': str(payload.get('signal_type', '') or ''),
            })

        return recent

    def _evaluate_ai_copilot_hailo_gate(self) -> Tuple[bool, Dict[str, Any]]:
        if not self.config.ai_copilot_hailo_gate_enabled:
            return True, {
                'enabled': False,
                'allowed': True,
                'reason': 'disabled',
            }

        required_alerts = max(
            1, int(self.config.ai_copilot_hailo_required_alerts))
        recent_alerts = self._read_recent_hailo_alerts()
        allowed = len(recent_alerts) >= required_alerts
        reason = 'ok' if allowed else 'insufficient_recent_alerts'
        latest_age = recent_alerts[0]['age_seconds'] if recent_alerts else None
        max_score = max((a['hailo_score'] for a in recent_alerts), default=0.0)
        max_confidence = max((a['confidence']
                             for a in recent_alerts), default=0.0)

        details = {
            'enabled': True,
            'allowed': allowed,
            'reason': reason,
            'required_alerts': required_alerts,
            'recent_alert_count': len(recent_alerts),
            'max_alert_age_seconds': int(self.config.ai_copilot_hailo_max_alert_age_seconds),
            'min_score': float(self.config.ai_copilot_hailo_min_score),
            'min_confidence': float(self.config.ai_copilot_hailo_min_confidence),
            'latest_age_seconds': latest_age,
            'max_score': round(float(max_score), 4),
            'max_confidence': round(float(max_confidence), 4),
            'alerts_file': self.config.ai_copilot_hailo_alerts_file,
            'recent_alerts': recent_alerts[:5],
        }
        return allowed, details

    def _ai_copilot_snapshot(self) -> Dict[str, Any]:
        trades_recent = []
        recent_trade_summary: Dict[str, Any] = {
            'window_rows': 0,
            'recent_buy_count': 0,
            'recent_sell_count': 0,
            'recent_win_rate_pct': 0.0,
            'recent_avg_pnl_base': 0.0,
            'recent_avg_hold_seconds': 0.0,
            'recent_max_hold_exit_ratio': 0.0,
            'recent_exit_reasons': {},
            'coin_sell_summary': {},
            'top_winners': [],
            'top_losers': [],
        }
        if self.config.performance_log_enabled and os.path.exists(self.config.performance_log_file):
            try:
                df = pd.read_csv(self.config.performance_log_file)
                if not df.empty:
                    cols = [
                        'timestamp', 'coin', 'action', 'pnl_base', 'pnl_pct',
                        'signal_source', 'signal_confidence', 'reason', 'hold_seconds'
                    ]
                    cols = [c for c in cols if c in df.columns]
                    recent_window = df.tail(80).copy()
                    trades_recent = recent_window.tail(40)[cols].where(
                        pd.notna(recent_window.tail(40)[cols]), None
                    ).to_dict(orient='records')

                    if 'action' in recent_window.columns:
                        actions = recent_window['action'].fillna(
                            '').astype(str).str.lower()
                        recent_trade_summary['window_rows'] = int(
                            len(recent_window))
                        recent_trade_summary['recent_buy_count'] = int(
                            (actions == 'buy').sum())
                        sells = recent_window.loc[actions == 'sell'].copy()
                        recent_trade_summary['recent_sell_count'] = int(
                            len(sells))

                        if not sells.empty:
                            pnl = pd.to_numeric(
                                sells.get('pnl_base'), errors='coerce').fillna(0.0)
                            recent_trade_summary['recent_win_rate_pct'] = round(
                                float((pnl > 0).mean() * 100.0), 2
                            )
                            recent_trade_summary['recent_avg_pnl_base'] = round(
                                float(pnl.mean()), 6
                            )

                            hold_seconds = pd.to_numeric(
                                sells.get('hold_seconds'), errors='coerce'
                            ).fillna(0.0)
                            recent_trade_summary['recent_avg_hold_seconds'] = round(
                                float(hold_seconds.mean()), 2
                            )

                            reasons = sells.get('reason', pd.Series(
                                dtype=object)).fillna('').astype(str)
                            reason_counts = reasons[reasons !=
                                                    ''].value_counts().head(6)
                            recent_trade_summary['recent_exit_reasons'] = {
                                str(reason): int(count) for reason, count in reason_counts.items()
                            }
                            max_hold_exits = int(
                                reasons.str.contains(
                                    'MAX-HOLD-TIME', case=False, na=False).sum()
                            )
                            recent_trade_summary['recent_max_hold_exit_ratio'] = round(
                                max_hold_exits / max(len(sells), 1), 4
                            )

                            if 'coin' in sells.columns:
                                coin_summary: Dict[str, Any] = {}
                                for coin, group in sells.groupby('coin', dropna=True):
                                    coin_pnl = pd.to_numeric(
                                        group.get('pnl_base'), errors='coerce'
                                    ).fillna(0.0)
                                    coin_reasons = group.get('reason', pd.Series(
                                        dtype=object)).fillna('').astype(str)
                                    coin_summary[str(coin)] = {
                                        'sell_count': int(len(group)),
                                        'pnl_sum': round(float(coin_pnl.sum()), 6),
                                        'avg_pnl': round(float(coin_pnl.mean()), 6),
                                        'win_rate_pct': round(float((coin_pnl > 0).mean() * 100.0), 2),
                                        'max_hold_exit_ratio': round(
                                            float(
                                                coin_reasons.str.contains(
                                                    'MAX-HOLD-TIME', case=False, na=False
                                                ).mean()
                                            ),
                                            4,
                                        ),
                                    }
                                recent_trade_summary['coin_sell_summary'] = coin_summary

                                ordered_coins = sorted(
                                    coin_summary.items(),
                                    key=lambda item: item[1]['pnl_sum'],
                                )
                                recent_trade_summary['top_losers'] = [
                                    {'coin': coin, 'pnl_sum': data['pnl_sum']}
                                    for coin, data in ordered_coins[:3]
                                ]
                                recent_trade_summary['top_winners'] = [
                                    {'coin': coin, 'pnl_sum': data['pnl_sum']}
                                    for coin, data in reversed(ordered_coins[-3:])
                                ]
            except Exception:
                trades_recent = []
                recent_trade_summary = {
                    'window_rows': 0,
                    'recent_buy_count': 0,
                    'recent_sell_count': 0,
                    'recent_win_rate_pct': 0.0,
                    'recent_avg_pnl_base': 0.0,
                    'recent_avg_hold_seconds': 0.0,
                    'recent_max_hold_exit_ratio': 0.0,
                    'recent_exit_reasons': {},
                    'coin_sell_summary': {},
                    'top_winners': [],
                    'top_losers': [],
                }

        by_source = {}
        for source, data in self.performance['by_source'].items():
            by_source[source] = {
                'buys': int(data.get('buys', 0)),
                'sells': int(data.get('sells', 0)),
                'pnl': float(data.get('pnl', 0.0)),
            }

        return {
            'timestamp_utc': datetime.now(timezone.utc).isoformat(),
            'iteration': int(self.iteration),
            'config': {
                'min_entry_score': int(self.config.min_entry_score),
                'reentry_cooldown_seconds': int(self.config.reentry_cooldown_seconds),
                'reentry_cooldown_max_seconds': int(self.config.reentry_cooldown_max_seconds),
                'tabular_buy_min_confidence': float(self.config.tabular_buy_min_confidence),
                'tabular_min_confidence': float(self.config.tabular_min_confidence),
                'max_hold_seconds': int(self.config.max_hold_seconds),
                'fallback_min_score': int(self.config.fallback_min_score),
            },
            'performance': {
                'buys': int(self.performance['buys']),
                'sells': int(self.performance['sells']),
                'wins': int(self.performance['wins']),
                'losses': int(self.performance['losses']),
                'closed_trades': int(self.performance['closed_trades']),
                'realized_pnl_base': float(self.performance['realized_pnl_base']),
                'consecutive_losses': int(self.consecutive_losses),
                'by_source': by_source,
            },
            'portfolio': {
                'cash': float(self.portfolio.cash),
                'open_trades_count': int(len(self.portfolio.open_trades)),
                'open_trade_coins': sorted(list(self.portfolio.open_trades.keys())),
                'holding_coins': sorted(list(self.portfolio.holdings.keys())),
            },
            'recent_trade_summary': recent_trade_summary,
            'recent_trades': trades_recent,
            'guardrails': {
                'min_entry_score_min': self.config.ai_copilot_min_entry_score_min,
                'min_entry_score_max': self.config.ai_copilot_min_entry_score_max,
                'reentry_cooldown_min': self.config.ai_copilot_reentry_cooldown_min,
                'reentry_cooldown_max': self.config.ai_copilot_reentry_cooldown_max,
                'tabular_buy_conf_min': self.config.ai_copilot_tabular_buy_conf_min,
                'tabular_buy_conf_max': self.config.ai_copilot_tabular_buy_conf_max,
            },
            'hailo_gate': {
                'enabled': bool(self.config.ai_copilot_hailo_gate_enabled),
                'alerts_file': self.config.ai_copilot_hailo_alerts_file,
                'required_alerts': int(self.config.ai_copilot_hailo_required_alerts),
                'min_score': float(self.config.ai_copilot_hailo_min_score),
                'min_confidence': float(self.config.ai_copilot_hailo_min_confidence),
                'max_alert_age_seconds': int(self.config.ai_copilot_hailo_max_alert_age_seconds),
            },
        }

    def _extract_json_object(self, text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        start = text.find('{')
        end = text.rfind('}')
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
        return None

    def _call_ai_copilot(self, snapshot: Dict[str, Any], *, model: str, temperature: float, max_output_tokens: int) -> Tuple[Optional[Dict[str, Any]], int, int]:
        system_prompt = (
            "You are a conservative trading bot co-pilot. "
            "Return ONLY JSON with keys: proposed_changes (object), reason (string), confidence (0..1), risk_level (low|medium|high). "
            "Allowed proposed_changes keys: min_entry_score, reentry_cooldown_seconds, tabular_buy_min_confidence. "
            "Use the recent_trade_summary, exit-reason mix, churn indicators, and per-coin sell summary to reason from observed behavior instead of generic advice. "
            "Prefer no change when evidence is weak or sample size is small. Respect provided guardrails. Propose at most one parameter change at a time."
        )
        user_payload = json.dumps(snapshot, ensure_ascii=True)
        request_body = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_payload},
            ],
            'temperature': temperature,
            'max_tokens': max_output_tokens,
        }

        req = urllib.request.Request(
            self.config.ai_copilot_api_url,
            data=json.dumps(request_body).encode('utf-8'),
            headers={
                'Authorization': f"Bearer {self.config.ai_copilot_api_key}",
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'User-Agent': 'trading-bot/1.0 (+https://github.com/siegfried/trading_2)',
            },
            method='POST',
        )

        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
        parsed = json.loads(raw)
        usage = parsed.get('usage', {}) if isinstance(parsed, dict) else {}
        prompt_tokens = int(usage.get('prompt_tokens', 0) or 0)
        completion_tokens = int(usage.get('completion_tokens', 0) or 0)

        content = ''
        if isinstance(parsed, dict):
            choices = parsed.get('choices', [])
            if choices and isinstance(choices[0], dict):
                message = choices[0].get('message', {})
                if isinstance(message, dict):
                    content = str(message.get('content', '') or '')

        result = self._extract_json_object(content)
        return result, prompt_tokens, completion_tokens

    def _clamp_ai_changes(self, raw_changes: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(raw_changes, dict):
            return {}
        out: Dict[str, Any] = {}

        if 'min_entry_score' in raw_changes:
            try:
                v = int(round(float(raw_changes['min_entry_score'])))
                v = min(max(v, self.config.ai_copilot_min_entry_score_min),
                        self.config.ai_copilot_min_entry_score_max)
                out['min_entry_score'] = v
            except Exception:
                pass

        if 'reentry_cooldown_seconds' in raw_changes:
            try:
                v = int(round(float(raw_changes['reentry_cooldown_seconds'])))
                v = min(max(v, self.config.ai_copilot_reentry_cooldown_min),
                        self.config.ai_copilot_reentry_cooldown_max)
                out['reentry_cooldown_seconds'] = v
            except Exception:
                pass

        if 'tabular_buy_min_confidence' in raw_changes:
            try:
                v = float(raw_changes['tabular_buy_min_confidence'])
                v = min(max(v, self.config.ai_copilot_tabular_buy_conf_min),
                        self.config.ai_copilot_tabular_buy_conf_max)
                out['tabular_buy_min_confidence'] = round(v, 4)
            except Exception:
                pass

        return out

    def _apply_ai_changes(self, changes: Dict[str, Any]) -> Dict[str, Tuple[Any, Any]]:
        applied: Dict[str, Tuple[Any, Any]] = {}

        if 'min_entry_score' in changes:
            old = self.config.min_entry_score
            new = int(changes['min_entry_score'])
            if old != new:
                self.config.min_entry_score = new
                applied['min_entry_score'] = (old, new)

        if 'reentry_cooldown_seconds' in changes:
            old = self.config.reentry_cooldown_seconds
            new = int(changes['reentry_cooldown_seconds'])
            if old != new:
                self.config.reentry_cooldown_seconds = new
                applied['reentry_cooldown_seconds'] = (old, new)

        if 'tabular_buy_min_confidence' in changes:
            old = self.config.tabular_buy_min_confidence
            new = float(changes['tabular_buy_min_confidence'])
            if abs(old - new) > 1e-9:
                self.config.tabular_buy_min_confidence = new
                applied['tabular_buy_min_confidence'] = (old, new)

        return applied

    def _maybe_run_ai_copilot_variant(
        self,
        snapshot: Dict[str, Any],
        *,
        label: str,
        state_file: str,
        enabled: bool,
        model: str,
        shadow_mode: bool,
        max_calls_per_day: int,
        max_calls_per_month: int,
        max_budget_usd_per_month: float,
        max_output_tokens: int,
        temperature: float,
        max_consecutive_errors: int,
        cost_input_per_mtok: float,
        cost_output_per_mtok: float,
        interval_minutes: int,
        allow_apply: bool,
    ):
        state = self._normalize_ai_state(
            self._read_ai_copilot_state(state_file),
            max_calls_per_month,
            max_budget_usd_per_month,
        )
        state['model'] = model
        can_run, reason = self._can_run_ai_copilot(
            state,
            enabled=enabled,
            model=model,
            api_key=self.config.ai_copilot_api_key,
            max_consecutive_errors=max_consecutive_errors,
            max_calls_per_day=max_calls_per_day,
            max_calls_per_month=max_calls_per_month,
            max_budget_usd_per_month=max_budget_usd_per_month,
            interval_minutes=interval_minutes,
        )
        if not can_run:
            if reason not in {'interval_not_reached', 'disabled'}:
                logger.info(f"{label} skipped: {reason}")
            self._write_ai_copilot_state(state, state_file)
            return

        gate_allows_run, gate_details = self._evaluate_ai_copilot_hailo_gate()
        snapshot_with_gate = dict(snapshot)
        snapshot_with_gate['hailo_gate_runtime'] = gate_details
        state['last_hailo_gate'] = gate_details

        if not gate_allows_run:
            logger.info(
                '%s skipped: hailo_gate=%s (recent=%s required=%s min_score=%.1f min_conf=%.2f max_age=%ss)',
                label,
                gate_details.get('reason', 'blocked'),
                gate_details.get('recent_alert_count', 0),
                gate_details.get('required_alerts', 0),
                gate_details.get('min_score', 0.0),
                gate_details.get('min_confidence', 0.0),
                gate_details.get('max_alert_age_seconds', 0),
            )
            self._write_ai_copilot_state(state, state_file)
            return

        try:
            result, prompt_tokens, completion_tokens = self._call_ai_copilot(
                snapshot_with_gate,
                model=model,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
            estimated_cost = self._estimate_ai_cost_usd(
                prompt_tokens, completion_tokens,
                cost_input_per_mtok, cost_output_per_mtok)

            # Final budget check before recording the call.
            projected = state.get('monthly_spend_usd', 0.0) + estimated_cost
            if (
                max_budget_usd_per_month > 0
                and projected > max_budget_usd_per_month
            ):
                logger.warning(
                    f"{label} call ignored due to monthly budget cap: projected {projected:.4f} USD")
                state['last_attempt_at'] = datetime.now(
                    timezone.utc).isoformat()
                self._write_ai_copilot_state(state, state_file)
                return

            state['monthly_calls'] = int(state.get('monthly_calls', 0)) + 1
            state['daily_calls'] = int(state.get('daily_calls', 0)) + 1
            state['monthly_spend_usd'] = projected
            state['last_run_at'] = datetime.now(timezone.utc).isoformat()
            state['last_attempt_at'] = state['last_run_at']
            state['last_prompt_tokens'] = prompt_tokens
            state['last_completion_tokens'] = completion_tokens
            state['last_estimated_cost_usd'] = round(estimated_cost, 6)
            state['consecutive_errors'] = 0
            state.pop('last_suspended_at', None)

            if not result:
                logger.warning(f'{label} returned no valid JSON payload')
                self._write_ai_copilot_state(state, state_file)
                return

            changes = self._clamp_ai_changes(
                result.get('proposed_changes', {}))
            reason_text = str(result.get('reason', ''))
            risk_level = str(result.get('risk_level', 'unknown'))
            confidence = result.get('confidence', None)

            if not changes:
                state['last_suggestion'] = {
                    'changes': {},
                    'risk_level': risk_level,
                    'confidence': confidence,
                    'reason': reason_text,
                    'mode': 'no_change',
                    'model': model,
                }
                logger.info(
                    f"{label} suggestion: no change (model={model}, risk={risk_level}, confidence={confidence}, reason={reason_text})")
                self._write_ai_copilot_state(state, state_file)
                return

            # Keep adaptation conservative: apply at most one parameter per run.
            first_key = next(iter(changes.keys()))
            single_change = {first_key: changes[first_key]}
            state['last_suggestion'] = {
                'changes': single_change,
                'risk_level': risk_level,
                'confidence': confidence,
                'reason': reason_text,
                'mode': 'shadow' if shadow_mode or not allow_apply else 'applied',
                'model': model,
            }

            if shadow_mode or not allow_apply:
                logger.info(
                    f"{label} shadow suggestion: {single_change} "
                    f"(model={model}, risk={risk_level}, confidence={confidence}, reason={reason_text})")
            else:
                applied = self._apply_ai_changes(single_change)
                if applied:
                    logger.info(
                        f"{label} applied: {applied} "
                        f"(model={model}, risk={risk_level}, confidence={confidence}, reason={reason_text})")
                    state['last_applied_at'] = datetime.now(
                        timezone.utc).isoformat()
                    state['last_applied_changes'] = {
                        k: {'old': v[0], 'new': v[1]} for k, v in applied.items()
                    }
                else:
                    logger.info(
                        f"{label} suggested unchanged values: {single_change}")

            self._write_ai_copilot_state(state, state_file)

        except Exception as e:
            state['last_attempt_at'] = datetime.now(timezone.utc).isoformat()
            state['consecutive_errors'] = int(
                state.get('consecutive_errors', 0)) + 1
            state['last_error'] = str(e)
            state['last_error_at'] = state['last_attempt_at']
            logger.warning(f"{label} call failed: {e}")
            if (
                max_consecutive_errors > 0
                and state['consecutive_errors'] >= max_consecutive_errors
            ):
                logger.error(
                    f"{label} suspended after {state['consecutive_errors']} consecutive errors")
                state['last_suspended_at'] = datetime.now(
                    timezone.utc).isoformat()
            self._write_ai_copilot_state(state, state_file)

    def _maybe_run_ai_copilot(self, snapshot: Dict[str, Any]):
        self._maybe_run_ai_copilot_variant(
            snapshot,
            label='AI co-pilot',
            state_file=self.config.ai_copilot_state_file,
            enabled=self.config.ai_copilot_enabled,
            model=self.config.ai_copilot_model,
            shadow_mode=self.config.ai_copilot_shadow_mode,
            max_calls_per_day=self.config.ai_copilot_max_calls_per_day,
            max_calls_per_month=self.config.ai_copilot_max_calls_per_month,
            max_budget_usd_per_month=self.config.ai_copilot_max_budget_usd_per_month,
            max_output_tokens=self.config.ai_copilot_max_output_tokens,
            temperature=self.config.ai_copilot_temperature,
            max_consecutive_errors=self.config.ai_copilot_max_consecutive_errors,
            cost_input_per_mtok=self.config.ai_copilot_cost_input_per_mtok,
            cost_output_per_mtok=self.config.ai_copilot_cost_output_per_mtok,
            interval_minutes=self.config.ai_copilot_interval_minutes,
            allow_apply=not self.config.ai_copilot_shadow_mode,
        )

    def _maybe_run_ai_copilot_benchmark(self, snapshot: Dict[str, Any]):
        self._maybe_run_ai_copilot_variant(
            snapshot,
            label='AI co-pilot benchmark',
            state_file=self.config.ai_copilot_benchmark_state_file,
            enabled=self.config.ai_copilot_benchmark_enabled,
            model=self.config.ai_copilot_benchmark_model,
            shadow_mode=True,
            max_calls_per_day=self.config.ai_copilot_benchmark_max_calls_per_day,
            max_calls_per_month=self.config.ai_copilot_benchmark_max_calls_per_month,
            max_budget_usd_per_month=self.config.ai_copilot_benchmark_max_budget_usd_per_month,
            max_output_tokens=self.config.ai_copilot_benchmark_max_output_tokens,
            temperature=self.config.ai_copilot_benchmark_temperature,
            max_consecutive_errors=self.config.ai_copilot_benchmark_max_consecutive_errors,
            cost_input_per_mtok=self.config.ai_copilot_benchmark_cost_input_per_mtok,
            cost_output_per_mtok=self.config.ai_copilot_benchmark_cost_output_per_mtok,
            interval_minutes=self.config.ai_copilot_benchmark_interval_minutes,
            allow_apply=False,
        )

    def _record_close_performance(self, entry_trade: Dict, sell_price: float, sell_amount: float):
        buy_price = float(entry_trade.get('buy_price', 0.0))
        buy_amount_coin = float(entry_trade.get('amount_coin', 0.0))
        if buy_amount_coin <= 0:
            return 0.0, 0.0, 0.0
        pnl_base = (sell_price - buy_price) * sell_amount
        pnl_pct = ((sell_price - buy_price) / buy_price) * \
            100 if buy_price > 0 else 0.0
        hold_seconds = (
            datetime.now() - entry_trade.get('timestamp', datetime.now())).total_seconds()

        self.performance['sells'] += 1
        self.performance['closed_trades'] += 1
        self.performance['realized_pnl_base'] += pnl_base
        if pnl_base >= 0:
            self.performance['wins'] += 1
        else:
            self.performance['losses'] += 1

        source = entry_trade.get('signal_source', 'rules')
        self.performance['by_source'][source]['sells'] += 1
        self.performance['by_source'][source]['pnl'] += pnl_base

        if pnl_base < 0:
            self.consecutive_losses += 1
            if (
                self.config.loss_streak_pause_threshold > 0
                and self.config.loss_streak_pause_seconds > 0
                and self.consecutive_losses >= self.config.loss_streak_pause_threshold
            ):
                self.buy_pause_until_utc = datetime.now(
                    timezone.utc) + timedelta(seconds=self.config.loss_streak_pause_seconds)
                logger.warning(
                    f"🧯 Loss streak detected ({self.consecutive_losses} in a row). "
                    f"New BUYs paused until {self.buy_pause_until_utc.isoformat()}"
                )
        else:
            self.consecutive_losses = 0

        return pnl_base, pnl_pct, hold_seconds

    def _register_buy_timestamp(self):
        now_utc = datetime.now(timezone.utc)
        self.buy_timestamps_utc.append(now_utc)
        cutoff = now_utc - timedelta(hours=1)
        self.buy_timestamps_utc = [
            ts for ts in self.buy_timestamps_utc if ts >= cutoff]

    def _register_sell_timestamp(self, coin: str):
        self.last_sell_timestamps_utc[coin] = datetime.now(timezone.utc)

    def _resolve_reentry_cooldown_seconds(self, current_price: float, atr: float) -> Tuple[int, str]:
        """Returns an effective re-entry cooldown (seconds), optionally adjusted by volatility."""
        base = max(0, int(self.config.reentry_cooldown_seconds))
        minimum = max(0, int(self.config.reentry_cooldown_min_seconds))
        maximum = max(minimum, int(self.config.reentry_cooldown_max_seconds))

        if base <= 0:
            return 0, 'base_zero'

        if not self.config.reentry_dynamic_enabled:
            return min(max(base, minimum), maximum), 'static'

        if (
            current_price is None
            or current_price <= 0
            or atr is None
            or np.isnan(atr)
            or atr <= 0
        ):
            return min(max(base, minimum), maximum), 'fallback_no_atr'

        atr_pct = float(atr) / float(current_price)
        low = max(0.0, float(self.config.reentry_volatility_low_atr_pct))
        high = max(low, float(self.config.reentry_volatility_high_atr_pct))

        if atr_pct < low:
            mult = float(self.config.reentry_cooldown_mult_low_vol)
            regime = 'low_vol'
        elif atr_pct > high:
            mult = float(self.config.reentry_cooldown_mult_high_vol)
            regime = 'high_vol'
        else:
            mult = float(self.config.reentry_cooldown_mult_mid_vol)
            regime = 'mid_vol'

        effective = int(round(base * mult))
        effective = min(max(effective, minimum), maximum)
        reason = f'{regime},atr_pct={atr_pct:.4f},mult={mult:.2f}'
        return effective, reason

    def _is_coin_in_reentry_cooldown(self, coin: str, cooldown_seconds: Optional[int] = None) -> Tuple[bool, int]:
        cooldown = max(
            0,
            int(self.config.reentry_cooldown_seconds if cooldown_seconds is None else cooldown_seconds),
        )
        if cooldown <= 0:
            return False, 0

        last_sell = self.last_sell_timestamps_utc.get(coin)
        if last_sell is None:
            return False, 0

        elapsed = (datetime.now(timezone.utc) - last_sell).total_seconds()
        remaining = int(max(0, cooldown - elapsed))
        if remaining > 0:
            return True, remaining
        return False, 0

    def _refresh_daily_anchor(self, portfolio_value: float):
        today_utc = datetime.now(timezone.utc).date()
        if portfolio_value <= 0:
            return
        if self.daily_anchor_date != today_utc:
            self.daily_anchor_date = today_utc
            self.daily_anchor_value = portfolio_value
            self.consecutive_losses = 0
            self.buy_pause_until_utc = None
            logger.info(
                f"🗓️ Daily-risk-anchor set: {portfolio_value:.2f} {self.config.base_currency} ({today_utc})")

    def _can_open_new_positions(self, portfolio_value: float) -> Tuple[bool, str]:
        now_utc = datetime.now(timezone.utc)

        if self.buy_pause_until_utc and now_utc < self.buy_pause_until_utc:
            remaining = (self.buy_pause_until_utc - now_utc).total_seconds()
            return False, f"BUY pause active for another {int(max(0, remaining))}s"

        if self.config.max_daily_loss_pct > 0 and self.daily_anchor_value > 0:
            drawdown_pct = max(
                0.0,
                (self.daily_anchor_value - portfolio_value) /
                self.daily_anchor_value * 100.0,
            )
            if drawdown_pct >= self.config.max_daily_loss_pct:
                return False, (
                    f"Daily-Loss-Limit erreicht ({drawdown_pct:.2f}% >= "
                    f"{self.config.max_daily_loss_pct:.2f}%)"
                )

        if self.config.max_buys_per_hour > 0:
            cutoff = now_utc - timedelta(hours=1)
            self.buy_timestamps_utc = [
                ts for ts in self.buy_timestamps_utc if ts >= cutoff]
            if len(self.buy_timestamps_utc) >= self.config.max_buys_per_hour:
                return False, (
                    f"BUY-Limit pro Stunde erreicht "
                    f"({len(self.buy_timestamps_utc)}/{self.config.max_buys_per_hour})"
                )

        return True, ""

    def _log_performance_report(self):
        closed = self.performance['closed_trades']
        win_rate = (self.performance['wins'] /
                    closed * 100) if closed > 0 else 0.0
        avg_pnl = (
            self.performance['realized_pnl_base'] / closed) if closed > 0 else 0.0
        logger.info(
            f"📊 Performance: Buys={self.performance['buys']} | Sells={self.performance['sells']} | "
            f"Closed={closed} | WinRate={win_rate:.1f}% | "
            f"RealizedPnL={self.performance['realized_pnl_base']:.2f} {self.config.base_currency} | "
            f"AvgPnL/Trade={avg_pnl:.4f} {self.config.base_currency}")

        if self.performance['by_source']:
            for source, stats in self.performance['by_source'].items():
                logger.info(
                    f"   - Source {source}: buys={stats['buys']}, sells={stats['sells']}, "
                    f"pnl={stats['pnl']:.4f} {self.config.base_currency}")

    def _initialize_exchange(self):
        """Initialises the exchange with ccxt."""
        if self.config.simulate_data:
            logger.info(
                "Simulation mode active, no real exchange connection.")
            return None
        try:
            exchange_class = getattr(ccxt, self.config.exchange_name)
            exchange = exchange_class({
                'apiKey': self.config.api_key,
                'secret': self.config.api_secret,
                'enableRateLimit': True,  # ccxt Rate Limiting
                'timeout': self.config.exchange_timeout_ms,
                'options': {
                    'adjustForTimeDifference': True,
                    'verbose': False  # Set to True for verbose ccxt output
                }
            })
            logger.info(
                f"Exchange {self.config.exchange_name} successfully initialised.")
            return exchange
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.AuthenticationError) as e:
            logger.error(
                f"Exchange initialisation failed: {e}. Enabling simulation mode.")
            self.config.simulate_data = True
            return None
        except Exception as e:
            logger.error(
                f"Unexpected error during exchange initialisation: {e}. Enabling simulation mode.")
            self.config.simulate_data = True
            return None

    def _load_market_info(self):
        """Loads all tradeable markets from the exchange."""
        if self.exchange is None:
            return
        try:
            markets = self.exchange.load_markets()
            for symbol, market in markets.items():
                if market['quote'] == self.config.base_currency and market['active']:
                    self.all_symbols.append(symbol)
                    self.all_coins.append(market['base'])
            # Unique coin list
            self.all_coins = sorted(list(set(self.all_coins)))
            logger.info(
                f"Loaded {len(self.all_symbols)} trading pairs and {len(self.all_coins)} coins with {self.config.base_currency}.")
        except Exception as e:
            logger.error(f"Error loading market information: {e}")
            self.all_symbols = [
                f"{coin}/{self.config.base_currency}" for coin in self.config.quote_currencies]
            self.all_coins = self.config.quote_currencies
            logger.warning(
                "Using fallback coins for analysis due to an error loading market information.")
        if self.config.simulate_data:
            # Fallback for simulation mode if no markets were loaded
            if not self.all_coins:
                self.all_coins = ['BTC', 'ETH']
            if not self.all_symbols:
                self.all_symbols = [
                    f'{coin}/{self.config.base_currency}' for coin in self.all_coins]

    def _update_portfolio_balance(self):
        """Updates the portfolio with current balances from the exchange."""
        def _currency_aliases(currency: str) -> List[str]:
            base = currency.upper().strip()
            aliases = [base, f"Z{base}", f"X{base}"]
            if base == 'BTC':
                aliases.extend(['XBT', 'XXBT'])
            return list(dict.fromkeys(aliases))

        def _extract_cash_and_holdings(free_balances: Dict[str, float]) -> Tuple[float, Dict[str, float]]:
            cash = 0.0
            aliases = _currency_aliases(self.config.base_currency)
            for key in aliases:
                value = free_balances.get(key)
                if value is not None:
                    cash = float(value)
                    break

            holdings: Dict[str, float] = {}
            for currency, amount in free_balances.items():
                if amount <= 0:
                    continue
                if currency in aliases:
                    continue
                holdings[currency] = float(amount)
            return cash, holdings

        if self.config.simulate_data or self.exchange is None:
            # For simulate_data mode: initialize dry-run cash once, then respect persistence
            if self.portfolio.cash == 0.0 and not self.portfolio.holdings:
                # Try to load from persistent state first
                if not self.portfolio.load_state():
                    # No state file, initialize with default dry-run capital
                    self.portfolio.cash = 1000.0
                    self.portfolio.initial_portfolio_value = self.portfolio.cash
                    logger.info(
                        f"Simulated starting capital: {self.portfolio.cash:.2f} {self.config.base_currency}")
            self.portfolio.last_update = datetime.now()
            return

        # In dry-run mode:
        # 1. Load initial balance from exchange exactly once
        # 2. Then restore from persistent state file (portfolio changes from previous runs)
        # 3. Never overwrite simulated portfolio changes
        if self.config.dry_run:
            if self.portfolio.last_update is not None:
                return
            # First time: try to load persistent state
            if self.portfolio.load_state():
                # Restore from file and mark as initialized
                self.portfolio.last_update = datetime.now()
                return
            # No persistent state, load initial balance from exchange
            try:
                balance = self.exchange.fetch_balance()
                free_balances = balance.get('free', {})
                self.portfolio.cash, self.portfolio.holdings = _extract_cash_and_holdings(
                    free_balances)
                if not self.portfolio.holdings:
                    self.portfolio.initial_portfolio_value = self.portfolio.cash
                self.portfolio.last_update = datetime.now()
                logger.info(
                    f"Portfolio initialized from exchange (dry-run mode). Cash: {self.portfolio.cash:.2f} {self.config.base_currency}, Holdings: {self.portfolio.holdings}")
            except Exception as e:
                logger.warning(
                    f"Failed to load initial balance from exchange in dry-run mode: {e}")
            return

        # Live trading mode: always fetch current balance
        try:
            balance = self.exchange.fetch_balance()
            free_balances = balance.get('free', {})
            self.portfolio.cash, self.portfolio.holdings = _extract_cash_and_holdings(
                free_balances)
            self.portfolio.last_update = datetime.now()
            logger.info(
                f"Portfolio balance updated. Cash: {self.portfolio.cash:.2f} {self.config.base_currency}, Holdings: {self.portfolio.holdings}")
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(
                f"Error fetching/updating account balance: {e}")
        except Exception as e:
            logger.error(
                f"Unexpected error fetching/updating account balance: {e}")

    def _get_market_data(self) -> Dict[str, Dict]:
        """Fetches current ticker data (price, volume) for all relevant coins."""
        market_data: Dict[str, Dict] = {}
        if self.config.simulate_data:
            for coin in self.all_coins:
                symbol = f'{coin}/{self.config.base_currency}'
                sim_df = self._build_simulated_ohlcv_data(
                    symbol, timeframe='1h', limit=100)
                market_data[coin] = {
                    'price': float(sim_df['close'].iloc[-1]),
                    'volume': float(sim_df['volume'].tail(24).mean()),
                }
            return market_data

        if self.exchange is None:
            logger.error(
                "Exchange is not initialised, cannot fetch market data.")
            return {}

        symbols = list(self.all_symbols)
        if not symbols:
            return {}

        batch_size = max(1, self.config.ticker_batch_size)
        retries = max(0, self.config.ticker_fetch_retries)
        delay_seconds = max(0.0, self.config.ticker_retry_delay_seconds)

        for start in range(0, len(symbols), batch_size):
            batch = symbols[start:start + batch_size]
            tickers = None
            for attempt in range(retries + 1):
                try:
                    tickers = self.exchange.fetch_tickers(batch)
                    break
                except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                    if attempt >= retries:
                        logger.error(
                            f"Error fetching ticker data for batch {start//batch_size + 1}: {e}")
                    else:
                        wait = delay_seconds * (attempt + 1)
                        logger.warning(
                            f"Retrying ticker batch {start//batch_size + 1}/{(len(symbols)-1)//batch_size + 1} "
                            f"after error: {e} (wait {wait:.1f}s)")
                        if wait > 0:
                            time.sleep(wait)
                except Exception as e:
                    if attempt >= retries:
                        logger.error(
                            f"Unexpected ticker error for batch {start//batch_size + 1}: {e}")
                    else:
                        wait = delay_seconds * (attempt + 1)
                        if wait > 0:
                            time.sleep(wait)

            if not tickers:
                continue

            for symbol in batch:
                ticker = tickers.get(symbol)
                if ticker and ticker['last'] is not None and ticker['quoteVolume'] is not None:
                    coin = symbol.split('/')[0]
                    market_data[coin] = {
                        'price': ticker['last'],
                        # Volume in base currency
                        'volume': ticker['quoteVolume']
                    }

        return market_data

    def _simulation_series_seed(self, symbol: str, timeframe: str) -> int:
        token = f"{symbol}|{timeframe}|{self.config.simulation_seed}"
        checksum = sum((index + 1) * ord(char)
                       for index, char in enumerate(token))
        return checksum % (2 ** 32)

    def _simulation_time_delta(self, timeframe: str) -> timedelta:
        unit = timeframe[-1].lower()
        value = int(timeframe[:-1]) if timeframe[:-1].isdigit() else 1
        if unit == 'm':
            return timedelta(minutes=value)
        if unit == 'h':
            return timedelta(hours=value)
        if unit == 'd':
            return timedelta(days=value)
        return timedelta(hours=1)

    def _simulation_base_price(self, coin: str) -> float:
        defaults = {
            'BTC': 74000.0,
            'ETH': 2500.0,
            'SOL': 160.0,
            'XRP': 0.55,
            'ADA': 0.45,
            'DOGE': 0.16,
            'TRX': 0.12,
            'CHZ': 0.11,
            'VVV': 5.5,
        }
        if coin in defaults:
            return defaults[coin]
        checksum = sum(ord(char) for char in coin)
        return 10.0 + (checksum % 190)

    def _simulation_base_volume(self, coin: str) -> float:
        defaults = {
            'BTC': 2_000_000.0,
            'ETH': 1_400_000.0,
            'SOL': 900_000.0,
            'XRP': 700_000.0,
            'ADA': 550_000.0,
            'DOGE': 600_000.0,
            'TRX': 450_000.0,
            'CHZ': 280_000.0,
            'VVV': 180_000.0,
        }
        if coin in defaults:
            return defaults[coin]
        checksum = sum((index + 3) * ord(char)
                       for index, char in enumerate(coin))
        return float(120_000 + (checksum % 500_000))

    def _simulation_regime_for_coin(self, coin: str) -> str:
        regime = (self.config.simulation_regime or 'neutral').lower()
        if regime != 'mixed':
            return regime

        regimes = ['uptrend', 'sideways', 'downtrend', 'recovery']
        checksum = sum((index + 5) * ord(char)
                       for index, char in enumerate(coin))
        return regimes[checksum % len(regimes)]

    def _simulation_regime_shape(self, regime: str, bucket_index: int, phase: float) -> Tuple[float, float, float]:
        relative = bucket_index - self.iteration

        if regime == 'uptrend':
            price_bias = 0.16 * math.tanh((relative + 36.0) / 18.0)
            volume_multiplier = 1.00 + 0.12 * \
                math.sin(bucket_index / 4.0 + phase)
            wave_scale = 0.85
        elif regime == 'downtrend':
            price_bias = -0.16 * math.tanh((relative + 36.0) / 18.0)
            volume_multiplier = 1.05 + 0.16 * \
                abs(math.sin(bucket_index / 5.0 + phase))
            wave_scale = 0.95
        elif regime == 'sideways':
            price_bias = 0.02 * math.sin(relative / 6.0 + phase)
            volume_multiplier = 0.78 + 0.06 * \
                math.cos(bucket_index / 7.0 + phase)
            wave_scale = 0.35
        elif regime == 'crash':
            crash_step = 1.0 / (1.0 + math.exp(-(relative + 6.0) / 1.2))
            price_bias = -0.30 * crash_step + 0.01 * \
                math.sin(bucket_index / 4.0 + phase)
            volume_multiplier = 1.20 + 0.70 * crash_step
            wave_scale = 1.35
        elif regime == 'recovery':
            dip = -0.18 * math.exp(-((relative + 18.0) ** 2) / 90.0)
            bounce = 0.16 / (1.0 + math.exp(-(relative + 6.0) / 1.6))
            price_bias = dip + bounce + 0.02 * \
                math.sin(bucket_index / 6.0 + phase)
            volume_multiplier = 1.00 + 0.22 * \
                abs(math.sin(bucket_index / 4.0 + phase))
            wave_scale = 0.90
        else:
            price_bias = 0.03 * math.sin(relative / 8.0 + phase)
            volume_multiplier = 0.95 + 0.10 * \
                math.sin(bucket_index / 3.0 + phase * 0.5)
            wave_scale = 0.70

        return price_bias, max(0.25, volume_multiplier), wave_scale

    def _build_simulated_ohlcv_data(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> pd.DataFrame:
        if self._simulation_cache_iteration != self.iteration:
            self._simulation_ohlcv_cache = {}
            self._simulation_cache_iteration = self.iteration

        cache_key = (self.iteration, symbol, timeframe, limit)
        cached = self._simulation_ohlcv_cache.get(cache_key)
        if cached is not None:
            return cached.copy()

        coin = symbol.split('/')[0]
        regime = self._simulation_regime_for_coin(coin)
        rng = np.random.default_rng(
            self._simulation_series_seed(symbol, timeframe))
        interval = self._simulation_time_delta(timeframe)
        end_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        base_price = self._simulation_base_price(coin)
        base_volume = self._simulation_base_volume(coin)

        phase = rng.uniform(0.0, 2.0 * math.pi)
        volatility = rng.uniform(0.0025, 0.0120)
        trend_strength = rng.uniform(0.01, 0.04)

        def close_for_bucket(bucket_index: int) -> float:
            regime_bias, _, wave_scale = self._simulation_regime_shape(
                regime, bucket_index, phase)
            macro = 0.10 * math.sin(bucket_index / 18.0 + phase)
            micro = 0.03 * math.cos(bucket_index / 5.0 + phase * 0.7)
            local_trend = trend_strength * \
                math.sin(bucket_index / 9.0 + phase * 0.3)
            regime_cycle = 0.02 * \
                math.sin((self.iteration + bucket_index) / 7.0 + phase * 1.3)
            price_factor = max(0.25, 1.0 + regime_bias + wave_scale * (
                macro + micro + local_trend + regime_cycle))
            return base_price * price_factor

        rows = []
        for idx in range(limit):
            timestamp = end_time - interval * (limit - 1 - idx)
            bucket_index = self.iteration - (limit - 1 - idx)
            close_price = close_for_bucket(bucket_index)
            open_price = close_for_bucket(bucket_index - 1)
            _, volume_multiplier, _ = self._simulation_regime_shape(
                regime, bucket_index, phase)
            wick_up = abs(math.sin(bucket_index * 0.91 + phase)
                          ) * (0.002 + volatility * 0.5)
            wick_down = abs(math.cos(bucket_index * 1.07 + phase)
                            ) * (0.002 + volatility * 0.5)
            high_price = max(open_price, close_price) * (1.0 + wick_up)
            low_price = min(open_price, close_price) * \
                max(0.2, 1.0 - wick_down)
            price_move = abs(close_price - open_price) / max(open_price, 1e-9)
            volume = max(base_volume * 0.08,
                         base_volume * volume_multiplier * (1.0 + price_move * 8.0))

            rows.append([
                timestamp,
                float(open_price),
                float(high_price),
                float(low_price),
                float(close_price),
                float(volume),
            ])

        df = pd.DataFrame(
            rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        self._simulation_ohlcv_cache[cache_key] = df
        return df.copy()

    def _fetch_ohlcv_data(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> Optional[pd.DataFrame]:
        """Fetches OHLCV data for a symbol."""
        if self.config.simulate_data or self.exchange is None:
            return self._build_simulated_ohlcv_data(symbol, timeframe=timeframe, limit=limit)

        retries = max(0, self.config.ohlcv_fetch_retries)
        delay_seconds = max(0.0, self.config.ohlcv_retry_delay_seconds)

        for attempt in range(retries + 1):
            started_at = time.monotonic()
            try:
                ohlcv = self.exchange.fetch_ohlcv(
                    symbol, timeframe, limit=limit)
                elapsed = time.monotonic() - started_at
                if elapsed >= self.config.slow_analysis_warning_seconds:
                    logger.warning(
                        f"Slow OHLCV fetch for {symbol}: {elapsed:.1f}s "
                        f"(timeframe={timeframe}, limit={limit})")
                if not ohlcv:
                    logger.warning(f"No OHLCV data available for {symbol}.")
                    return None
                df = pd.DataFrame(
                    ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                return df
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                elapsed = time.monotonic() - started_at
                if attempt >= retries:
                    logger.error(
                        f"Error fetching OHLCV data for {symbol} after {elapsed:.1f}s: {e}")
                    return None
                wait = delay_seconds * (attempt + 1)
                logger.warning(
                    f"Retrying OHLCV fetch for {symbol} after {elapsed:.1f}s error: {e} "
                    f"(attempt {attempt + 2}/{retries + 1}, wait {wait:.1f}s)")
                if wait > 0:
                    time.sleep(wait)
            except Exception as e:
                elapsed = time.monotonic() - started_at
                if attempt >= retries:
                    logger.error(
                        f"Unexpected error fetching OHLCV data for {symbol} after {elapsed:.1f}s: {e}")
                    return None
                wait = delay_seconds * (attempt + 1)
                logger.warning(
                    f"Retrying unexpected OHLCV error for {symbol} after {elapsed:.1f}s: {e} "
                    f"(attempt {attempt + 2}/{retries + 1}, wait {wait:.1f}s)")
                if wait > 0:
                    time.sleep(wait)

        return None

    def _calculate_rsi(self, data: pd.Series, period: int) -> float:
        """Calculates the Relative Strength Index (RSI)."""
        if len(data) < period + 1:
            return np.nan  # Not enough data
        delta = data.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1]  # Last RSI value

    def _calculate_sma(self, data: pd.Series, period: int) -> float:
        """Calculates the Simple Moving Average (SMA)."""
        if len(data) < period:
            return np.nan
        return data.rolling(window=period).mean().iloc[-1]

    def _calculate_macd(self, data: pd.Series, fast: int = 12, slow: int = 26) -> float:
        """Calculates the MACD (EMA12 - EMA26)."""
        if len(data) < slow:
            return np.nan
        ema_fast = data.ewm(span=fast, adjust=False).mean()
        ema_slow = data.ewm(span=slow, adjust=False).mean()
        return (ema_fast - ema_slow).iloc[-1]

    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculates the Average True Range (ATR) for the given DataFrame."""
        if len(df) < period + 1:
            return np.nan
        high = df['high']
        low = df['low']
        close = df['close']
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()
        return atr.iloc[-1]

    def _effective_trade_amount(self, portfolio_value: float) -> float:
        effective_trade_amount = self.config.trade_amount
        if portfolio_value <= 0:
            return effective_trade_amount

        for threshold, multiplier in self.config.portfolio_trade_amount_multipliers:
            if portfolio_value >= threshold:
                effective_trade_amount = max(
                    effective_trade_amount,
                    self.config.trade_amount * multiplier,
                )

        return effective_trade_amount

    def _effective_max_open_trades(self, portfolio_value: float) -> int:
        effective_max_open_trades = self.config.max_open_trades
        if portfolio_value <= 0:
            return effective_max_open_trades

        for threshold, max_open_trades in self.config.portfolio_max_open_trades_tiers:
            if portfolio_value >= threshold:
                effective_max_open_trades = max(
                    effective_max_open_trades,
                    max_open_trades,
                )

        return effective_max_open_trades

    def _calculate_position_size(
        self,
        coin: str,
        price: float,
        atr: float,
        risk_pct: float = 0.01,
        portfolio_value: Optional[float] = None,
        trade_amount_cap: Optional[float] = None,
    ) -> float:
        """Calculates position size such that at most risk_pct of the portfolio is at risk (ATR-based)."""
        effective_trade_amount = self.config.trade_amount if trade_amount_cap is None else trade_amount_cap
        if atr is None or np.isnan(atr) or atr == 0:
            logger.warning(
                f"ATR for {coin} not available, using default position size.")
            return effective_trade_amount
        if portfolio_value is None:
            portfolio_value = self.portfolio.cash + sum([
                self.portfolio.holdings.get(c, 0) * price for c in self.portfolio.holdings
            ])
        risk_amount = portfolio_value * risk_pct
        position_size = risk_amount / atr  # How many ATRs can we afford?
        amount_in_base = min(position_size * atr, effective_trade_amount)
        return max(amount_in_base, 0)

    def _get_atr_for_coin(self, coin: str, period: int = 14) -> float:
        symbol = f"{coin}/{self.config.base_currency}"
        ohlcv_df = self._fetch_ohlcv_data(
            symbol, timeframe=self.config.ohlcv_timeframe, limit=period+20)
        if ohlcv_df is None or len(ohlcv_df) < period+1:
            return np.nan
        return self._calculate_atr(ohlcv_df, period=period)

    @staticmethod
    def _recommendation_bias(recommendation: str) -> int:
        normalized = (recommendation or '').strip().upper()
        if normalized in {'STRONG BUY', 'BUY', 'HOLD (UP-TREND)'}:
            return 1
        if normalized in {'SELL', 'WEAK SELL', 'HOLD (DOWN-TREND)'}:
            return -1
        return 0

    def _should_apply_tabular_signal(
        self,
        rule_recommendation: str,
        rule_score: int,
        tab_decision: str,
        tab_confidence: float,
    ) -> Tuple[bool, str]:
        if tab_decision == 'kaufen' and not self.config.tabular_allow_buy_entries:
            return False, 'buy_entries_disabled'

        required_confidence = self.config.tabular_buy_min_confidence
        if tab_decision == 'verkaufen':
            required_confidence = self.config.tabular_min_confidence

        if tab_confidence < required_confidence:
            return False, 'below_min_confidence'

        if not self.config.tabular_source_gate_enabled:
            return True, 'gate_disabled'

        tab_bias = 1 if tab_decision == 'kaufen' else - \
            1 if tab_decision == 'verkaufen' else 0
        rule_bias = self._recommendation_bias(rule_recommendation)

        if tab_bias == 0:
            return False, 'neutral_tabular_signal'

        if tab_bias == rule_bias:
            return True, 'rule_confirmed'

        rule_strength = abs(float(rule_score) - 50.0) / 50.0
        advantage = tab_confidence - rule_strength
        if (
            tab_confidence >= self.config.tabular_override_min_confidence
            and advantage >= self.config.tabular_override_margin
        ):
            return True, 'strong_override'

        return False, 'gated_by_rules'

    def _effective_stop_loss_level(self, trade_info: Dict, current_price: float, atr: float) -> float:
        """Builds a dynamic stop level combining base ATR stop, trailing stop and break-even protection."""
        buy_price = float(trade_info.get('buy_price', 0.0))
        if buy_price <= 0:
            return current_price

        peak_price = float(trade_info.get('peak_price', buy_price))
        peak_price = max(peak_price, current_price, buy_price)
        trade_info['peak_price'] = peak_price

        stop_loss_level = buy_price - self.config.atr_stop_mult * atr

        if self.config.trailing_stop_enabled:
            trailing_level = peak_price - self.config.trailing_stop_atr_mult * atr
            stop_loss_level = max(stop_loss_level, trailing_level)

        if self.config.break_even_enabled and buy_price > 0:
            runup_pct = (peak_price - buy_price) / buy_price * 100.0
            if runup_pct >= self.config.break_even_trigger_pct:
                break_even_level = buy_price * \
                    (1.0 + self.config.break_even_buffer_pct / 100.0)
                stop_loss_level = max(stop_loss_level, break_even_level)

        return stop_loss_level

    def _passes_entry_momentum_filter(self, coin_data: Dict) -> Tuple[bool, str]:
        """Checks if an entry candidate has acceptable short-term momentum."""
        if not self.config.entry_momentum_filter_enabled:
            return True, 'disabled'

        coin = str(coin_data.get('coin', '')).upper()
        recommendation = str(coin_data.get('recommendation', ''))
        if recommendation not in {'BUY', 'STRONG BUY', 'HOLD (Up-Trend)'}:
            return True, 'not_buy_signal'

        ret_3 = coin_data.get('ret_3')
        if ret_3 is not None and not np.isnan(ret_3):
            min_ret_3 = self.config.entry_min_ret_3_by_coin.get(
                coin, self.config.entry_min_ret_3)
            if float(ret_3) < min_ret_3:
                return False, f"ret_3_below_min ({float(ret_3):.4f} < {min_ret_3:.4f})"

        if self.config.entry_require_price_above_ema20:
            price = coin_data.get('price')
            ema20 = coin_data.get('ema_20')
            if (
                price is not None and ema20 is not None
                and not np.isnan(price) and not np.isnan(ema20)
                and float(price) < float(ema20)
            ):
                return False, f"price_below_ema20 ({float(price):.4f} < {float(ema20):.4f})"

        if self.config.entry_sharp_pump_filter_enabled:
            ret_1 = coin_data.get('ret_1')
            if ret_1 is not None and not np.isnan(ret_1):
                if float(ret_1) > self.config.entry_max_ret_1:
                    return False, f"sharp_pump_ret_1 ({float(ret_1):.4f} > {self.config.entry_max_ret_1:.4f})"

            if ret_3 is not None and not np.isnan(ret_3):
                if float(ret_3) > self.config.entry_max_ret_3:
                    return False, f"sharp_pump_ret_3 ({float(ret_3):.4f} > {self.config.entry_max_ret_3:.4f})"

        return True, 'ok'

    def _passes_uptrend_entry_filter(self, coin_data: Dict) -> Tuple[bool, str]:
        """Allows up-trend fallback entries only when trend and tabular quality are not too weak."""
        recommendation = str(coin_data.get('recommendation', ''))
        if recommendation != 'HOLD (Up-Trend)':
            return True, 'not_uptrend'

        if not self.config.uptrend_entry_gate_enabled:
            return True, 'uptrend_gate_disabled'

        coin = str(coin_data.get('coin', '')).upper()
        max_rsi = self.config.uptrend_entry_max_rsi_by_coin.get(
            coin, self.config.uptrend_entry_max_rsi)
        min_buy_proba = self.config.uptrend_entry_min_buy_proba_by_coin.get(
            coin, self.config.uptrend_entry_min_buy_proba)
        max_sell_proba = self.config.uptrend_entry_max_sell_proba_by_coin.get(
            coin, self.config.uptrend_entry_max_sell_proba)
        min_proba_edge = self.config.uptrend_entry_min_proba_edge_by_coin.get(
            coin, self.config.uptrend_entry_min_proba_edge)

        rsi = coin_data.get('rsi')
        if rsi is None or np.isnan(rsi):
            return False, 'missing_rsi'
        if float(rsi) > max_rsi:
            return False, f"rsi_above_uptrend_max ({float(rsi):.2f} > {max_rsi:.2f})"

        buy_proba = coin_data.get('tabular_buy_proba')
        sell_proba = coin_data.get('tabular_sell_proba')
        if buy_proba is None or np.isnan(buy_proba):
            return False, 'missing_buy_proba'
        if sell_proba is None or np.isnan(sell_proba):
            return False, 'missing_sell_proba'

        buy_proba = float(buy_proba)
        sell_proba = float(sell_proba)

        if buy_proba < min_buy_proba:
            return False, f"buy_proba_below_uptrend_min ({buy_proba:.3f} < {min_buy_proba:.3f})"
        if sell_proba > max_sell_proba:
            return False, f"sell_proba_above_uptrend_max ({sell_proba:.3f} > {max_sell_proba:.3f})"

        proba_edge = buy_proba - sell_proba
        if proba_edge < min_proba_edge:
            return False, f"proba_edge_below_uptrend_min ({proba_edge:.3f} < {min_proba_edge:.3f})"

        return True, 'ok'

    def _passes_fallback_entry_filter(self, coin: str, coin_data: Dict) -> Tuple[bool, str]:
        """Applies fallback-only entry checks after the directional filters passed."""
        rsi = coin_data.get('rsi')
        if rsi is None or np.isnan(rsi):
            return False, 'missing_rsi'

        fallback_max_rsi = self.config.fallback_max_rsi_by_coin.get(
            str(coin).upper(), self.config.fallback_max_rsi)
        if float(rsi) > fallback_max_rsi:
            return False, f"rsi_above_fallback_max ({float(rsi):.2f} > {fallback_max_rsi:.2f})"

        return True, 'ok'

    def _entry_market_mode(self, market_analysis: Dict[str, Dict]) -> str:
        """Classifies the market to suppress aggressive entry filling in weak environments."""
        if not market_analysis:
            return 'normal'

        severity_rank = {'normal': 0, 'cautious': 1, 'defensive': 2}

        regime_mode = 'normal'
        if self.config.simulate_data:
            regime = (self.config.simulation_regime or 'neutral').lower()
            if regime in {'crash', 'downtrend'}:
                regime_mode = 'defensive'
            elif regime in {'recovery', 'sideways', 'mixed'}:
                regime_mode = 'cautious'

        bearish_signals = {'SELL', 'WEAK SELL', 'HOLD (Down-Trend)'}
        bullish_signals = {'BUY', 'STRONG BUY', 'HOLD (Up-Trend)'}
        bearish = sum(
            1 for data in market_analysis.values()
            if data.get('recommendation') in bearish_signals
        )
        bullish = sum(
            1 for data in market_analysis.values()
            if data.get('recommendation') in bullish_signals
        )
        total = max(1, len(market_analysis))
        bearish_share = bearish / total

        signal_mode = 'normal'
        if bearish_share >= 0.60 or bearish >= bullish + 2:
            signal_mode = 'defensive'
        elif bearish_share >= 0.40 or bearish > bullish:
            signal_mode = 'cautious'

        return max((regime_mode, signal_mode), key=severity_rank.get)

    @staticmethod
    def _filter_reason_code(reason: str) -> str:
        """Normalizes verbose filter reasons into stable summary keys."""
        return str(reason).split(' ', 1)[0]

    def _log_blocked_uptrend_candidates(
        self,
        market_analysis: Dict[str, Dict],
        uptrend_filter_results: Dict[str, Tuple[bool, str]],
        limit: int = 5,
    ) -> None:
        """Logs concrete rejection details for blocked rules-based up-trend entries."""
        blocked_candidates = []
        for coin, data in market_analysis.items():
            if data.get('recommendation') != 'HOLD (Up-Trend)':
                continue
            passes_filter, filter_reason = uptrend_filter_results.get(
                coin, (True, 'not_evaluated'))
            if passes_filter:
                continue
            buy_proba = data.get('tabular_buy_proba')
            sell_proba = data.get('tabular_sell_proba')
            proba_edge = None
            if buy_proba is not None and sell_proba is not None:
                try:
                    if not np.isnan(buy_proba) and not np.isnan(sell_proba):
                        proba_edge = float(buy_proba) - float(sell_proba)
                except TypeError:
                    proba_edge = None
            blocked_candidates.append({
                'coin': coin,
                'reason': filter_reason,
                'reason_code': self._filter_reason_code(filter_reason),
                'rsi': data.get('rsi'),
                'buy_proba': buy_proba,
                'sell_proba': sell_proba,
                'proba_edge': proba_edge,
                'signal_source': data.get('signal_source', 'n/a'),
                'score': data.get('score', 0),
                'rule_score': data.get('rule_score', 0),
            })

        if not blocked_candidates:
            return

        reason_mix = dict(Counter(
            candidate['reason_code'] for candidate in blocked_candidates
        ))
        logger.info(
            '  🚧 Uptrend entry gate blocked %d candidate(s): %s',
            len(blocked_candidates),
            reason_mix,
        )
        for candidate in blocked_candidates[:limit]:
            rsi = candidate['rsi']
            buy_proba = candidate['buy_proba']
            sell_proba = candidate['sell_proba']
            proba_edge = candidate['proba_edge']
            logger.info(
                '    - %s: reason=%s, RSI=%s, buy_proba=%s, sell_proba=%s, edge=%s, source=%s, score=%s, rule_score=%s',
                candidate['coin'],
                candidate['reason'],
                f'{float(rsi):.2f}' if rsi is not None and not np.isnan(
                    rsi) else 'n/a',
                f'{float(buy_proba):.3f}' if buy_proba is not None and not np.isnan(
                    buy_proba) else 'n/a',
                f'{float(sell_proba):.3f}' if sell_proba is not None and not np.isnan(
                    sell_proba) else 'n/a',
                f'{float(proba_edge):.3f}' if proba_edge is not None else 'n/a',
                candidate['signal_source'],
                candidate['score'],
                candidate['rule_score'],
            )

    def _log_blocked_downtrend_reversal_candidates(
        self,
        market_analysis: Dict[str, Dict],
        downtrend_filter_results: Dict[str, Tuple[bool, str]],
        limit: int = 5,
    ) -> None:
        """Logs concrete rejection details for blocked down-trend reversal entries."""
        blocked_candidates = []
        for coin, data in market_analysis.items():
            if data.get('recommendation') != 'HOLD (Down-Trend)':
                continue
            passes_filter, filter_reason = downtrend_filter_results.get(
                coin, (True, 'not_evaluated'))
            if passes_filter:
                continue
            buy_proba = data.get('tabular_buy_proba')
            sell_proba = data.get('tabular_sell_proba')
            proba_edge = None
            if buy_proba is not None and sell_proba is not None:
                try:
                    if not np.isnan(buy_proba) and not np.isnan(sell_proba):
                        proba_edge = float(buy_proba) - float(sell_proba)
                except TypeError:
                    proba_edge = None
            blocked_candidates.append({
                'coin': coin,
                'reason': filter_reason,
                'reason_code': self._filter_reason_code(filter_reason),
                'rsi': data.get('rsi'),
                'buy_proba': buy_proba,
                'sell_proba': sell_proba,
                'proba_edge': proba_edge,
                'signal_source': data.get('signal_source', 'n/a'),
                'score': data.get('score', 0),
                'rule_score': data.get('rule_score', 0),
            })

        if not blocked_candidates:
            return

        reason_mix = dict(Counter(
            candidate['reason_code'] for candidate in blocked_candidates
        ))
        logger.info(
            '  🚧 Downtrend reversal gate blocked %d candidate(s): %s',
            len(blocked_candidates),
            reason_mix,
        )
        for candidate in blocked_candidates[:limit]:
            rsi = candidate['rsi']
            buy_proba = candidate['buy_proba']
            sell_proba = candidate['sell_proba']
            proba_edge = candidate['proba_edge']
            logger.info(
                '    - %s: reason=%s, RSI=%s, buy_proba=%s, sell_proba=%s, edge=%s, source=%s, score=%s, rule_score=%s',
                candidate['coin'],
                candidate['reason'],
                f'{float(rsi):.2f}' if rsi is not None and not np.isnan(
                    rsi) else 'n/a',
                f'{float(buy_proba):.3f}' if buy_proba is not None and not np.isnan(
                    buy_proba) else 'n/a',
                f'{float(sell_proba):.3f}' if sell_proba is not None and not np.isnan(
                    sell_proba) else 'n/a',
                f'{float(proba_edge):.3f}' if proba_edge is not None else 'n/a',
                candidate['signal_source'],
                candidate['score'],
                candidate['rule_score'],
            )

    def _log_fallback_entry_diagnostics(
        self,
        market_analysis: Dict[str, Dict],
        fallback_base_candidates: List[str],
        fallback_filter_results: Dict[str, Tuple[bool, str]],
        fallback_allowed: bool,
        fallback_suppression_reason: str,
        entry_market_mode: str,
        limit: int = 5,
    ) -> None:
        """Logs why fallback entries were blocked or fully suppressed."""
        blocked_candidates = []
        for coin in fallback_base_candidates:
            passes_filter, filter_reason = fallback_filter_results.get(
                coin, (True, 'not_evaluated'))
            if passes_filter:
                continue
            data = market_analysis.get(coin, {})
            blocked_candidates.append({
                'coin': coin,
                'reason': filter_reason,
                'reason_code': self._filter_reason_code(filter_reason),
                'rsi': data.get('rsi'),
                'signal_source': data.get('signal_source', 'n/a'),
                'score': data.get('score', 0),
                'rule_score': data.get('rule_score', 0),
            })

        if blocked_candidates:
            reason_mix = dict(Counter(
                candidate['reason_code'] for candidate in blocked_candidates
            ))
            logger.info(
                '  🚧 Fallback RSI gate blocked %d candidate(s): %s',
                len(blocked_candidates),
                reason_mix,
            )
            for candidate in blocked_candidates[:limit]:
                rsi = candidate['rsi']
                logger.info(
                    '    - %s: reason=%s, RSI=%s, source=%s, score=%s, rule_score=%s',
                    candidate['coin'],
                    candidate['reason'],
                    f'{float(rsi):.2f}' if rsi is not None and not np.isnan(
                        rsi) else 'n/a',
                    candidate['signal_source'],
                    candidate['score'],
                    candidate['rule_score'],
                )

        if fallback_allowed or not fallback_base_candidates:
            return

        logger.info(
            '  🛑 Fallback entry suppressed (%s): %d candidate(s) met all non-RSI fallback conditions in entry_mode=%s.',
            fallback_suppression_reason,
            len(fallback_base_candidates),
            entry_market_mode,
        )
        for coin in fallback_base_candidates[:limit]:
            data = market_analysis.get(coin, {})
            signal_confidence = data.get('signal_confidence')
            confidence_text = ''
            if signal_confidence is not None:
                confidence_text = f", confidence={float(signal_confidence) * 100:.0f}%"
            logger.info(
                '    - %s: rec=%s, score=%s, rule_score=%s, RSI=%s, source=%s%s',
                coin,
                data.get('recommendation', 'n/a'),
                data.get('score', 0),
                data.get('rule_score', 0),
                f"{float(data.get('rsi', np.nan)):.2f}" if not np.isnan(
                    data.get('rsi', np.nan)) else 'n/a',
                data.get('signal_source', 'n/a'),
                confidence_text,
            )

    def _log_blocked_buy_attempt_candidates(
        self,
        blocked_attempts: List[Dict[str, Any]],
        limit: int = 5,
    ) -> None:
        """Logs why shortlisted buy candidates still failed during the execution path."""
        if not blocked_attempts:
            return

        reason_mix = dict(Counter(
            self._filter_reason_code(attempt.get('reason', 'unknown'))
            for attempt in blocked_attempts
        ))
        logger.info(
            '  🚫 Buy attempt blocked %d candidate(s): %s',
            len(blocked_attempts),
            reason_mix,
        )
        for attempt in blocked_attempts[:limit]:
            logger.info(
                '    - %s: reason=%s, source=%s, score=%s, recommendation=%s, position_size=%s, cash=%s, cooldown_remaining=%s',
                attempt.get('coin', 'n/a'),
                attempt.get('reason', 'unknown'),
                attempt.get('signal_source', 'n/a'),
                attempt.get('score', 'n/a'),
                attempt.get('recommendation', 'n/a'),
                attempt.get('position_size_text', 'n/a'),
                attempt.get('cash_text', 'n/a'),
                attempt.get('cooldown_text', 'n/a'),
            )

    @staticmethod
    def _is_rules_uptrend_trade(trade_info: Dict) -> bool:
        recommendation = str(trade_info.get('recommendation', ''))
        signal_source = str(trade_info.get('signal_source', ''))
        return recommendation == 'HOLD (Up-Trend)' and signal_source == 'rules'

    def _passes_downtrend_reversal_filter(self, coin_data: Dict) -> Tuple[bool, str]:
        """Allows down-trend fallback entries only for oversold coins with improving CatBoost odds."""
        recommendation = str(coin_data.get('recommendation', ''))
        if recommendation != 'HOLD (Down-Trend)':
            return True, 'not_downtrend'

        coin = str(coin_data.get('coin', '')).upper()
        max_rsi = self.config.downtrend_reversal_max_rsi_by_coin.get(
            coin, self.config.downtrend_reversal_max_rsi)
        min_buy_proba = self.config.downtrend_reversal_min_buy_proba_by_coin.get(
            coin, self.config.downtrend_reversal_min_buy_proba)
        max_sell_proba = self.config.downtrend_reversal_max_sell_proba_by_coin.get(
            coin, self.config.downtrend_reversal_max_sell_proba)
        min_proba_edge = self.config.downtrend_reversal_min_proba_edge_by_coin.get(
            coin, self.config.downtrend_reversal_min_proba_edge)

        if coin and self.config.downtrend_reversal_allowed_coins and coin not in self.config.downtrend_reversal_allowed_coins:
            return False, f"coin_not_allowed_for_reversal ({coin or 'unknown'})"

        if not self.config.downtrend_reversal_entry_enabled:
            return False, 'downtrend_reversal_disabled'

        rsi = coin_data.get('rsi')
        if rsi is None or np.isnan(rsi):
            return False, 'missing_rsi'
        if float(rsi) > max_rsi:
            return False, f"rsi_above_reversal_max ({float(rsi):.2f} > {max_rsi:.2f})"

        buy_proba = coin_data.get('tabular_buy_proba')
        sell_proba = coin_data.get('tabular_sell_proba')
        if buy_proba is None or sell_proba is None:
            return False, 'missing_tabular_proba'

        buy_proba = float(buy_proba)
        sell_proba = float(sell_proba)
        if buy_proba < min_buy_proba:
            return False, f"buy_proba_below_min ({buy_proba:.3f} < {min_buy_proba:.3f})"
        if sell_proba > max_sell_proba:
            return False, f"sell_proba_above_max ({sell_proba:.3f} > {max_sell_proba:.3f})"
        if (buy_proba - sell_proba) < min_proba_edge:
            return False, (
                f"buy_sell_edge_too_small ({buy_proba - sell_proba:.3f} < "
                f"{min_proba_edge:.3f})"
            )

        ret_1 = coin_data.get('ret_1')
        if ret_1 is None or np.isnan(ret_1):
            return False, 'missing_ret_1'
        if float(ret_1) < self.config.downtrend_reversal_min_ret_1:
            return False, (
                f"ret_1_below_reversal_min ({float(ret_1):.4f} < "
                f"{self.config.downtrend_reversal_min_ret_1:.4f})"
            )

        ret_3 = coin_data.get('ret_3')
        if ret_3 is None or np.isnan(ret_3):
            return False, 'missing_ret_3'
        if float(ret_3) < self.config.downtrend_reversal_min_ret_3:
            return False, (
                f"ret_3_below_reversal_min ({float(ret_3):.4f} < "
                f"{self.config.downtrend_reversal_min_ret_3:.4f})"
            )

        macd_hist = coin_data.get('macd_hist')
        if macd_hist is None or np.isnan(macd_hist):
            return False, 'missing_macd_hist'
        if float(macd_hist) < self.config.downtrend_reversal_min_macd_hist:
            return False, (
                f"macd_hist_below_reversal_min ({float(macd_hist):.4f} < "
                f"{self.config.downtrend_reversal_min_macd_hist:.4f})"
            )

        return True, 'downtrend_reversal_ok'

    @staticmethod
    def _allows_entry_signal(
        recommendation: str,
        downtrend_reversal_allowed: bool,
        excluded_signals: Set[str],
    ) -> bool:
        """Keeps explicit downtrend-reversal approvals eligible even when generic exits block downtrend entries."""
        if recommendation != 'HOLD (Down-Trend)':
            return recommendation not in excluded_signals
        return downtrend_reversal_allowed

    @staticmethod
    def _is_downtrend_reversal_trade(trade_info: Dict) -> bool:
        """Returns True for trades opened via the guarded downtrend reversal path."""
        return str(trade_info.get('recommendation', '')) == 'HOLD (Down-Trend)'

    def _analyze_coin(self, coin: str, current_price: float) -> Optional[Dict]:
        """Performs a detailed analysis for a single coin."""
        symbol = f"{coin}/{self.config.base_currency}"
        ohlcv_df = self._fetch_ohlcv_data(
            symbol, timeframe=self.config.ohlcv_timeframe, limit=self.config.ohlcv_limit)

        # Less tolerance for missing data
        if ohlcv_df is None or len(ohlcv_df) < self.config.ohlcv_limit - 5:
            logger.warning(
                f"Insufficient historical data for {coin} for analysis. Skipping.")
            return None

        # Calculate indicators
        rsi = self._calculate_rsi(ohlcv_df['close'], self.config.rsi_period)
        sma_short = self._calculate_sma(
            ohlcv_df['close'], self.config.sma_period_short)
        sma_long = self._calculate_sma(
            ohlcv_df['close'], self.config.sma_period_long)
        close = ohlcv_df['close']
        ema_20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ret_1 = float(close.pct_change(
            1).iloc[-1]) if len(ohlcv_df) >= 2 else 0.0
        ret_3 = float(close.pct_change(
            3).iloc[-1]) if len(ohlcv_df) >= 4 else 0.0

        # Simple strategy based on indicators
        recommendation = "HOLD"
        score = 50  # Neutral score
        signal_source = 'rules'
        signal_confidence = None
        tab_buy_proba = None
        tab_hold_proba = None
        tab_sell_proba = None
        macd_hist = None

        if not np.isnan(rsi) and not np.isnan(sma_short) and not np.isnan(sma_long):
            if rsi < 30 and sma_short > sma_long and current_price > sma_short:
                recommendation = "STRONG BUY"
                score = 90
            elif rsi < 40 and sma_short > sma_long:
                recommendation = "BUY"
                score = 75
            elif rsi > 70 and sma_short < sma_long and current_price < sma_short:
                recommendation = "SELL"
                score = 10
            elif rsi > 60 and sma_short < sma_long:
                recommendation = "WEAK SELL"
                score = 25
            elif sma_short > sma_long:
                recommendation = "HOLD (Up-Trend)"
                score = 60
            elif sma_short < sma_long:
                recommendation = "HOLD (Down-Trend)"
                score = 40

        rule_recommendation = recommendation
        rule_score = score

        # ML model analysis (optional, only if loaded and data is valid)
        if self.ml_predictor is not None:
            try:
                macd = self._calculate_macd(ohlcv_df['close'])
                row_data = pd.DataFrame([{
                    'coin': coin,
                    'timestamp': int(ohlcv_df['timestamp'].iloc[-1].timestamp() * 1000),
                    'open': float(ohlcv_df['open'].iloc[-1]),
                    'high': float(ohlcv_df['high'].iloc[-1]),
                    'low': float(ohlcv_df['low'].iloc[-1]),
                    'close': float(current_price),
                    'volume': float(ohlcv_df['volume'].iloc[-1]),
                    'rsi': float(rsi) if not np.isnan(rsi) else 50.0,
                    'macd': float(macd) if not np.isnan(macd) else 0.0,
                }])
                ml_result = self.ml_predictor.predict(row_data)
                ml_decision = ml_result['decision']
                ml_confidence = ml_result['confidence']
                logger.info(
                    f"  🤖 ML for {coin}: {ml_decision.upper()} "
                    f"(Confidence: {ml_confidence*100:.0f}%, "
                    f"Votes: {ml_result.get('llm_votes', {})}, "
                    f"Rule: {ml_result.get('rule', '-')})")

                # Ensemble: weave ML signal into score/recommendation
                if ml_confidence >= self.config.ml_min_confidence:
                    signal_source = 'llm'
                    signal_confidence = ml_confidence
                    if ml_decision == 'kaufen':
                        if recommendation in ['HOLD', 'HOLD (Up-Trend)', 'HOLD (Down-Trend)']:
                            recommendation = 'BUY'
                            score = max(score, 70)
                        elif recommendation in ['BUY', 'STRONG BUY']:
                            score = min(score + int(ml_confidence * 15), 95)
                    elif ml_decision == 'verkaufen':
                        if recommendation in ['HOLD', 'HOLD (Up-Trend)', 'BUY', 'STRONG BUY']:
                            recommendation = 'WEAK SELL'
                            score = min(score, 30)
                        elif recommendation in ['SELL', 'WEAK SELL']:
                            score = max(score - int(ml_confidence * 10), 5)
            except Exception as e:
                logger.warning(f"ML prediction for {coin} failed: {e}")

        # Fast tabular model analysis (optional)
        if self.tabular_predictor is not None:
            try:
                n = len(ohlcv_df)
                latest = ohlcv_df.iloc[-1]

                # MACD + signal + histogram
                ema12 = close.ewm(span=12, adjust=False).mean()
                ema26 = close.ewm(span=26, adjust=False).mean()
                macd_line = ema12 - ema26
                macd_signal_line = macd_line.ewm(span=9, adjust=False).mean()
                _macd = float(macd_line.iloc[-1])
                _macd_sig = float(macd_signal_line.iloc[-1])
                _macd_hist = _macd - _macd_sig
                macd_hist = _macd_hist

                # EMAs
                _ema20 = float(
                    close.ewm(span=20, adjust=False).mean().iloc[-1])
                _ema50 = float(
                    close.ewm(span=50, adjust=False).mean().iloc[-1])
                _ema200 = float(
                    close.ewm(span=200, adjust=False).mean().iloc[-1])

                # Stochastic %K/%D
                _stoch_k, _stoch_d = 50.0, 50.0
                if n >= 17:
                    low14 = ohlcv_df['low'].rolling(14).min()
                    high14 = ohlcv_df['high'].rolling(14).max()
                    _raw_k = 100.0 * (close - low14) / (high14 - low14 + 1e-12)
                    _stoch_k = float(_raw_k.rolling(3).mean().iloc[-1])
                    _stoch_d = float(_raw_k.rolling(
                        3).mean().rolling(3).mean().iloc[-1])

                # CCI 20
                _cci = 0.0
                if n >= 20:
                    tp = (ohlcv_df['high'] + ohlcv_df['low'] + close) / 3.0
                    tp_sma = tp.rolling(20).mean()
                    tp_mad = tp.rolling(20).apply(
                        lambda x: np.mean(np.abs(x - x.mean())), raw=True)
                    _cci = float(
                        ((tp - tp_sma) / (0.015 * tp_mad + 1e-12)).iloc[-1])

                # OBV
                if n >= 2:
                    _direction = np.sign(close.diff().fillna(0))
                    _obv = float(
                        (_direction * ohlcv_df['volume']).cumsum().iloc[-1])
                else:
                    _obv = 0.0

                # Bollinger Bands (20, 2)
                _bb_mid = float(close.rolling(20).mean(
                ).iloc[-1]) if n >= 20 else float(current_price)
                _bb_std = float(close.rolling(
                    20).std().iloc[-1]) if n >= 20 else 0.0
                _bb_upper = _bb_mid + 2.0 * _bb_std
                _bb_lower = _bb_mid - 2.0 * _bb_std

                # Return features
                _ret1 = float(close.pct_change(1).iloc[-1]) if n >= 2 else 0.0
                _ret3 = float(close.pct_change(3).iloc[-1]) if n >= 4 else 0.0
                _ret6 = float(close.pct_change(6).iloc[-1]) if n >= 7 else 0.0
                _vol6 = float(close.pct_change().rolling(
                    6).std().iloc[-1]) if n >= 7 else 0.0

                row_data = pd.DataFrame([{
                    'rsi': float(rsi) if not np.isnan(rsi) else 50.0,
                    'macd': _macd,
                    'macd_signal': _macd_sig,
                    'macd_hist': _macd_hist,
                    'sma_50': float(sma_short) if not np.isnan(sma_short) else float(current_price),
                    'sma_200': float(sma_long) if not np.isnan(sma_long) else float(current_price),
                    'ema_20': _ema20,
                    'ema_50': _ema50,
                    'ema_200': _ema200,
                    'atr_14': float(self._calculate_atr(ohlcv_df, 14)) if n >= 15 else 0.0,
                    'stoch_k': _stoch_k,
                    'stoch_d': _stoch_d,
                    'cci_20': _cci,
                    'obv': _obv,
                    'bb_upper': _bb_upper,
                    'bb_middle': _bb_mid,
                    'bb_lower': _bb_lower,
                    'volume': float(latest['volume']) if 'volume' in latest else 0.0,
                    'ret_1': _ret1,
                    'ret_3': _ret3,
                    'ret_6': _ret6,
                    'vol_6': _vol6,
                }])

                tab_result = self.tabular_predictor.predict(
                    row_data, confidence_threshold=self.config.tabular_min_confidence)
                tab_decision = tab_result.get('decision', 'halten')
                tab_confidence = float(tab_result.get('confidence', 0.0))
                tab_proba = tab_result.get('proba', {})
                tab_sell_proba = float(tab_proba.get('verkaufen', 0.0))
                tab_hold_proba = float(tab_proba.get('halten', 0.0))
                tab_buy_proba = float(tab_proba.get('kaufen', 0.0))
                logger.info(
                    f"  📊 CatBoost for {coin}: {tab_decision.upper()} "
                    f"(Confidence: {tab_confidence*100:.0f}%, Proba: {tab_proba})")

                # Safety filter: block new entries even on rules-BUYs
                # if CatBoost sees a high-probability sell signal.
                if self.config.tabular_block_on_sell and tab_sell_proba >= self.config.tabular_sell_block_min_proba:
                    if recommendation in ['HOLD', 'HOLD (Up-Trend)', 'HOLD (Down-Trend)', 'BUY', 'STRONG BUY']:
                        recommendation = 'WEAK SELL'
                        score = min(score, 25)
                        signal_source = 'catboost'
                        signal_confidence = tab_sell_proba
                        logger.info(
                            f"  🛡️ CatBoost sell-filter active for {coin}: "
                            f"P(sell)={tab_sell_proba*100:.0f}% >= "
                            f"{self.config.tabular_sell_block_min_proba*100:.0f}%")

                should_apply, gate_reason = self._should_apply_tabular_signal(
                    rule_recommendation=rule_recommendation,
                    rule_score=rule_score,
                    tab_decision=tab_decision,
                    tab_confidence=tab_confidence,
                )
                required_confidence = self.config.tabular_buy_min_confidence if tab_decision == 'kaufen' else self.config.tabular_min_confidence
                if tab_confidence >= required_confidence and not should_apply:
                    logger.info(
                        f"  🚧 CatBoost gated for {coin}: {gate_reason} "
                        f"(rule={rule_recommendation}, rule_score={rule_score}, "
                        f"tab_decision={tab_decision}, tab_confidence={tab_confidence*100:.0f}%)"
                    )

                if should_apply:
                    signal_source = 'catboost'
                    signal_confidence = tab_confidence
                    if tab_decision == 'kaufen':
                        if recommendation in ['HOLD', 'HOLD (Up-Trend)', 'HOLD (Down-Trend)']:
                            recommendation = 'BUY'
                            score = max(score, 72)
                        elif recommendation in ['BUY', 'STRONG BUY']:
                            score = min(score + int(tab_confidence * 12), 97)
                    elif tab_decision == 'verkaufen':
                        if recommendation in ['HOLD', 'HOLD (Up-Trend)', 'BUY', 'STRONG BUY']:
                            recommendation = 'WEAK SELL'
                            score = min(score, 28)
                        elif recommendation in ['SELL', 'WEAK SELL']:
                            score = max(score - int(tab_confidence * 8), 3)
            except Exception as e:
                logger.warning(
                    f"CatBoost prediction for {coin} failed: {e}")

        return {
            'rsi': rsi,
            'sma_short': sma_short,
            'sma_long': sma_long,
            'ema_20': ema_20,
            'ret_1': ret_1,
            'ret_3': ret_3,
            'recommendation': recommendation,
            'score': score,
            'rule_recommendation': rule_recommendation,
            'rule_score': rule_score,
            'signal_source': signal_source,
            'signal_confidence': signal_confidence,
            'tabular_buy_proba': tab_buy_proba,
            'tabular_hold_proba': tab_hold_proba,
            'tabular_sell_proba': tab_sell_proba,
            'macd_hist': macd_hist,
        }

    def _analyze_markets(self, market_data: Dict[str, Dict], extra_coins: Optional[List[str]] = None) -> Dict[str, Dict]:
        """Performs a comprehensive market analysis and returns recommendations."""
        dynamic_excluded_coins = self._dynamic_excluded_coins()
        if dynamic_excluded_coins:
            logger.info(
                "Dynamically excluded lossmaker coins: %s",
                ', '.join(sorted(dynamic_excluded_coins)),
            )
        filtered_coins_by_volume = []
        for coin, data in market_data.items():
            if coin in self.config.excluded_coins or coin in dynamic_excluded_coins:
                continue
            if data['volume'] >= self.config.min_volume_base:
                filtered_coins_by_volume.append(coin)

        logger.info(
            f"Coins filtered by volume ({self.config.min_volume_base:,.0f} {self.config.base_currency} minimum): {len(filtered_coins_by_volume)}")

        # Select only the Top-N by volume for detailed analysis to limit API calls
        # or analyse all if TOP_N_FOR_ANALYSIS is very high
        if len(filtered_coins_by_volume) > self.config.top_n_for_analysis:
            # Sort by volume descending and select the Top-N
            sorted_by_volume = sorted(
                filtered_coins_by_volume, key=lambda c: market_data[c]['volume'], reverse=True)
            coins_for_detailed_analysis = sorted_by_volume[:self.config.top_n_for_analysis]
            logger.info(
                f"Running detailed analysis for top {self.config.top_n_for_analysis} coins (by volume).")
        else:
            coins_for_detailed_analysis = filtered_coins_by_volume
            logger.info(
                f"Running detailed analysis for all {len(coins_for_detailed_analysis)} high-volume coins.")

        # Always analyse open positions, even if they are not in the volume top-N.
        if extra_coins:
            for coin in extra_coins:
                if coin in market_data and coin not in coins_for_detailed_analysis:
                    coins_for_detailed_analysis.append(coin)

        analysis: Dict[str, Dict] = {}
        total_analysis_coins = len(coins_for_detailed_analysis)
        for idx, coin in enumerate(coins_for_detailed_analysis, start=1):
            logger.info(
                f"🔎 Analysis progress: {idx}/{total_analysis_coins} - {coin}")
            started_at = time.monotonic()
            current_price = market_data[coin]['price']
            coin_analysis = self._analyze_coin(coin, current_price)
            elapsed = time.monotonic() - started_at
            if elapsed >= self.config.slow_analysis_warning_seconds:
                logger.warning(
                    f"Slow coin analysis for {coin}: {elapsed:.1f}s")
            if coin_analysis:
                analysis[coin] = {
                    'coin': coin,
                    'price': current_price,
                    'volume': market_data[coin]['volume'],
                    **coin_analysis
                }

        # Sort by score/recommendation
        sorted_analysis = dict(
            sorted(analysis.items(), key=lambda x: x[1]['score'], reverse=True))
        return sorted_analysis

    def _execute_trade(
        self,
        coin: str,
        action: str,
        price: float,
        amount_in_base_currency: float,
        atr: float = None,
        signal_source: str = 'rules',
        signal_confidence: Optional[float] = None,
        recommendation: str = 'HOLD',
        reason: str = '',
    ):
        """
        Executes a (simulated) trade.
        Uses market orders for simplicity; limit orders would be preferable in a real bot.
        """
        if self.config.dry_run:
            amount_coin = amount_in_base_currency / price
            logger.info(
                f"💰 DRY-RUN: Would {action.upper()} {amount_coin:.4f} {coin} at {price:.4f} {self.config.base_currency} (total: {amount_in_base_currency:.2f} {self.config.base_currency})")
            if action == "buy":
                if self.portfolio.cash < amount_in_base_currency:
                    logger.warning(
                        f"Insufficient cash for DRY-RUN buy of {coin}: {self.portfolio.cash:.2f} < {amount_in_base_currency:.2f} {self.config.base_currency}")
                    return False
                self.portfolio.cash -= amount_in_base_currency
                self.portfolio.holdings[coin] = self.portfolio.holdings.get(
                    coin, 0.0) + amount_coin
                self.portfolio.add_trade(
                    coin,
                    price,
                    amount_coin,
                    amount_in_base_currency,
                    signal_source=signal_source,
                    signal_confidence=signal_confidence,
                    recommendation=recommendation,
                )
                self.performance['buys'] += 1
                self.performance['by_source'][signal_source]['buys'] += 1
                self._register_buy_timestamp()
                self._append_trade_journal({
                    'coin': coin,
                    'action': 'buy',
                    'price': price,
                    'amount_coin': amount_coin,
                    'amount_base': amount_in_base_currency,
                    'signal_source': signal_source,
                    'signal_confidence': signal_confidence,
                    'recommendation': recommendation,
                    'reason': reason,
                    'dry_run': True,
                })
                # Persist portfolio state after successful dry-run buy
                if self.config.dry_run:
                    self.portfolio.save_state()
            elif action == "sell":
                held = self.portfolio.holdings.get(coin, 0.0)
                sell_amount = min(amount_coin, held)
                if sell_amount <= 0:
                    logger.warning(
                        f"DRY-RUN sell skipped, no holdings for {coin}.")
                    return False
                self.portfolio.cash += sell_amount * price
                remaining = held - sell_amount
                if remaining > 1e-12:
                    self.portfolio.holdings[coin] = remaining
                elif coin in self.portfolio.holdings:
                    del self.portfolio.holdings[coin]

                entry_trade = self.portfolio.open_trades.get(coin, {})
                original_amount_coin = float(
                    entry_trade.get('amount_coin', sell_amount))
                remaining_open_amount = max(
                    0.0, original_amount_coin - sell_amount)
                remaining_ratio = (
                    remaining_open_amount / original_amount_coin
                    if original_amount_coin > 0 else 0.0
                )
                pnl_base, pnl_pct, hold_seconds = self._record_close_performance(
                    entry_trade, price, sell_amount)
                self._append_trade_journal({
                    'coin': coin,
                    'action': 'sell',
                    'price': price,
                    'amount_coin': sell_amount,
                    'amount_base': sell_amount * price,
                    'pnl_base': pnl_base,
                    'pnl_pct': pnl_pct,
                    'hold_seconds': hold_seconds,
                    'signal_source': entry_trade.get('signal_source', 'rules'),
                    'signal_confidence': entry_trade.get('signal_confidence', ''),
                    'recommendation': entry_trade.get('recommendation', ''),
                    'reason': reason,
                    'dry_run': True,
                })
                self._register_sell_timestamp(coin)
                if remaining_open_amount > 1e-12 and coin in self.portfolio.open_trades:
                    self.portfolio.open_trades[coin]['amount_coin'] = remaining_open_amount
                    self.portfolio.open_trades[coin]['amount_base'] = float(
                        entry_trade.get('amount_base', 0.0)) * remaining_ratio
                else:
                    self.portfolio.remove_trade(coin)
                # Persist portfolio state after successful dry-run sell
                if self.config.dry_run:
                    self.portfolio.save_state()
            return True

        if self.exchange is None:
            logger.error(
                "Real trade cannot be executed in simulation mode or without an exchange.")
            return False

        symbol = f"{coin}/{self.config.base_currency}"
        try:
            market = self.exchange.market(symbol)
            min_amount_base = market['limits']['amount']['min'] * \
                price if market['limits']['amount']['min'] else 0
            if amount_in_base_currency < min_amount_base and min_amount_base > 0:
                logger.warning(
                    f"Trade amount {amount_in_base_currency:.2f} {self.config.base_currency} for {coin} is below the minimum of {min_amount_base:.2f} {self.config.base_currency}. Trade not executed.")
                return False

            if action == "buy":
                order = self.exchange.create_market_buy_order(
                    symbol, amount_in_base_currency / price)
                logger.info(
                    f"✅ REAL TRADE (BUY): {order['amount']:.4f} {coin} @ {order['price']:.4f} {self.config.base_currency} (cost: {order['cost']:.2f} {self.config.base_currency}, ID: {order['id']})")
                self.portfolio.add_trade(
                    coin,
                    order['price'],
                    order['amount'],
                    order['cost'],
                    signal_source=signal_source,
                    signal_confidence=signal_confidence,
                    recommendation=recommendation,
                )
                self.performance['buys'] += 1
                self.performance['by_source'][signal_source]['buys'] += 1
                self._register_buy_timestamp()
                self._append_trade_journal({
                    'coin': coin,
                    'action': 'buy',
                    'price': order.get('price', price),
                    'amount_coin': order.get('amount', amount_in_base_currency / price),
                    'amount_base': order.get('cost', amount_in_base_currency),
                    'signal_source': signal_source,
                    'signal_confidence': signal_confidence,
                    'recommendation': recommendation,
                    'reason': reason,
                    'dry_run': False,
                })
            elif action == "sell":
                if coin not in self.portfolio.holdings or self.portfolio.holdings[coin] < amount_in_base_currency / price:
                    logger.warning(
                        f"Insufficient {coin} ({self.portfolio.holdings.get(coin, 0):.4f}) in portfolio to sell {amount_in_base_currency / price:.4f} {coin}.")
                    return False
                order = self.exchange.create_market_sell_order(
                    symbol, amount_in_base_currency / price)
                logger.info(
                    f"✅ REAL TRADE (SELL): {order['amount']:.4f} {coin} @ {order['price']:.4f} {self.config.base_currency} (proceeds: {order['cost']:.2f} {self.config.base_currency}, ID: {order['id']})")

                entry_trade = self.portfolio.open_trades.get(coin, {})
                original_amount_coin = float(
                    entry_trade.get('amount_coin', amount_in_base_currency / price))
                sold_amount_coin = float(
                    order.get('amount', amount_in_base_currency / price))
                remaining_open_amount = max(
                    0.0, original_amount_coin - sold_amount_coin)
                remaining_ratio = (
                    remaining_open_amount / original_amount_coin
                    if original_amount_coin > 0 else 0.0
                )
                pnl_base, pnl_pct, hold_seconds = self._record_close_performance(
                    entry_trade, float(order.get('price', price)), sold_amount_coin)
                self._append_trade_journal({
                    'coin': coin,
                    'action': 'sell',
                    'price': order.get('price', price),
                    'amount_coin': sold_amount_coin,
                    'amount_base': order.get('cost', amount_in_base_currency),
                    'pnl_base': pnl_base,
                    'pnl_pct': pnl_pct,
                    'hold_seconds': hold_seconds,
                    'signal_source': entry_trade.get('signal_source', 'rules'),
                    'signal_confidence': entry_trade.get('signal_confidence', ''),
                    'recommendation': entry_trade.get('recommendation', ''),
                    'reason': reason,
                    'dry_run': False,
                })
                self._register_sell_timestamp(coin)
                if remaining_open_amount > 1e-12 and coin in self.portfolio.open_trades:
                    self.portfolio.open_trades[coin]['amount_coin'] = remaining_open_amount
                    self.portfolio.open_trades[coin]['amount_base'] = float(
                        entry_trade.get('amount_base', 0.0)) * remaining_ratio
                else:
                    self.portfolio.remove_trade(coin)
            self._update_portfolio_balance()
            return True
        except ccxt.InsufficientFunds as e:
            logger.error(f"Insufficient funds for {action} {coin}: {e}")
            return False
        except ccxt.InvalidOrder as e:
            logger.error(
                f"Invalid order parameter for {action} {coin}: {e}")
            return False
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            logger.error(
                f"Error executing trade ({action} {coin}): {e}")
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error executing trade ({action} {coin}): {e}")
            return False

    def _manage_open_trades(self, current_market_data: Dict[str, Dict], market_analysis: Optional[Dict] = None):
        """Monitors open trades for stop-loss, take-profit, time and signal exits."""
        trades_to_remove = []
        for coin, trade_info in list(self.portfolio.open_trades.items()):
            buy_price = trade_info['buy_price']
            amount_coin = trade_info['amount_coin']
            partial_tp_taken = bool(trade_info.get('partial_tp_taken', False))
            partial_tp_timestamp = trade_info.get('partial_tp_timestamp')
            current_price_data = current_market_data.get(coin)
            if current_price_data is None:
                logger.warning(
                    f"Current market data for {coin} not available, cannot monitor trade.")
                continue
            current_price = current_price_data['price']
            pnl_pct = (current_price - buy_price) / buy_price * 100
            hold_seconds = (datetime.now() -
                            trade_info['timestamp']).total_seconds()
            max_hold_seconds = self.config.coin_max_hold_seconds.get(
                coin, self.config.max_hold_seconds)
            # Fetch ATR for dynamic stops
            atr = self._get_atr_for_coin(coin, period=14)
            if np.isnan(atr) or atr == 0:
                atr = buy_price * 0.02  # Fallback: 2% of price
            stop_loss_level = self._effective_stop_loss_level(
                trade_info, current_price, atr)
            take_profit_level = buy_price + self.config.atr_tp_mult * atr
            partial_take_profit_level = buy_price + \
                self.config.partial_take_profit_atr_mult * atr
            peak_price = float(trade_info.get('peak_price', buy_price))
            logger.info(
                f"  📊 {coin}: Buy {buy_price:.4f} → Current {current_price:.4f} "
                f"({pnl_pct:+.2f}%) | SL {stop_loss_level:.4f} | TP {take_profit_level:.4f} "
                f"| Peak {peak_price:.4f} | Hold time {int(hold_seconds)}s")
            exit_reason = None
            partial_fraction = min(
                max(self.config.partial_take_profit_fraction, 0.0), 1.0)
            if (
                self.config.partial_take_profit_enabled
                and not partial_tp_taken
                and 0.0 < partial_fraction < 1.0
                and current_price >= partial_take_profit_level
            ):
                partial_amount_coin = amount_coin * partial_fraction
                if partial_amount_coin > 1e-12:
                    partial_reason = (
                        f"💸 PARTIAL-TAKE-PROFIT ({partial_fraction*100:.0f}% @ "
                        f"{partial_take_profit_level:.4f})"
                    )
                    logger.info(
                        f"{partial_reason} → Teilverkauf {coin} @ {current_price:.4f}")
                    if self._execute_trade(
                        coin,
                        "sell",
                        current_price,
                        partial_amount_coin * current_price,
                        atr=atr,
                        reason=partial_reason,
                    ):
                        if coin in self.portfolio.open_trades:
                            self.portfolio.open_trades[coin]['partial_tp_taken'] = True
                            self.portfolio.open_trades[coin]['partial_tp_timestamp'] = datetime.now(
                            )
                            if self.config.dry_run:
                                self.portfolio.save_state()
                    continue
            if partial_tp_taken and partial_tp_timestamp:
                partial_tp_elapsed_seconds = (
                    datetime.now() - partial_tp_timestamp).total_seconds()
                if (
                    self.config.partial_take_profit_remainder_max_hold_seconds > 0
                    and partial_tp_elapsed_seconds >= self.config.partial_take_profit_remainder_max_hold_seconds
                ):
                    exit_reason = (
                        "⏱️ PARTIAL-TP-REMAINDER-TIMEOUT "
                        f"({int(partial_tp_elapsed_seconds)}s >= "
                        f"{self.config.partial_take_profit_remainder_max_hold_seconds}s)"
                    )
                elif (
                    self.config.partial_take_profit_exit_on_weak_signal
                    and market_analysis is not None
                ):
                    signal = market_analysis.get(
                        coin, {}).get('recommendation', '')
                    if signal in {'SELL', 'WEAK SELL', 'HOLD (Down-Trend)'}:
                        exit_reason = (
                            '📉 PARTIAL-TP-REMAINDER-WEAK-SIGNAL '
                            f'(recommendation: {signal})'
                        )
            is_downtrend_reversal_trade = self._is_downtrend_reversal_trade(
                trade_info)
            if (
                exit_reason is None
                and is_downtrend_reversal_trade
                and self.config.downtrend_reversal_fast_exit_enabled
                and market_analysis is not None
            ):
                current_signal = market_analysis.get(
                    coin, {}).get('recommendation', '')
                if (
                    self.config.downtrend_reversal_max_hold_seconds > 0
                    and hold_seconds >= self.config.downtrend_reversal_max_hold_seconds
                ):
                    exit_reason = (
                        '⏱️ DOWNTREND-REVERSAL-MAX-HOLD '
                        f'({int(hold_seconds)}s >= {self.config.downtrend_reversal_max_hold_seconds}s)'
                    )
                elif (
                    hold_seconds >= self.config.downtrend_reversal_fast_exit_seconds
                    and pnl_pct <= self.config.downtrend_reversal_flat_max_profit_pct
                    and current_signal in {'HOLD (Down-Trend)', 'WEAK SELL', 'SELL'}
                ):
                    exit_reason = (
                        '📉 DOWNTREND-REVERSAL-FAST-EXIT '
                        f'({pnl_pct:+.2f}% <= {self.config.downtrend_reversal_flat_max_profit_pct:.2f}% '
                        f'after {int(hold_seconds)}s, signal: {current_signal})'
                    )
                elif (
                    hold_seconds >= self.config.downtrend_reversal_weak_signal_exit_seconds
                    and pnl_pct <= self.config.downtrend_reversal_weak_signal_max_profit_pct
                    and current_signal in {'HOLD (Down-Trend)', 'WEAK SELL', 'SELL'}
                ):
                    exit_reason = (
                        '📉 DOWNTREND-REVERSAL-WEAK-SIGNAL-EXIT '
                        f'({pnl_pct:+.2f}% <= {self.config.downtrend_reversal_weak_signal_max_profit_pct:.2f}% '
                        f'after {int(hold_seconds)}s, signal: {current_signal})'
                    )
            is_rules_uptrend_trade = self._is_rules_uptrend_trade(trade_info)
            if (
                exit_reason is None
                and is_rules_uptrend_trade
                and self.config.uptrend_rules_fast_exit_enabled
                and market_analysis is not None
            ):
                current_signal = market_analysis.get(
                    coin, {}).get('recommendation', '')
                fast_exit_seconds = self.config.uptrend_rules_fast_exit_seconds_by_coin.get(
                    coin, self.config.uptrend_rules_fast_exit_seconds)
                flat_max_profit_pct = self.config.uptrend_rules_flat_max_profit_pct_by_coin.get(
                    coin, self.config.uptrend_rules_flat_max_profit_pct)
                uptrend_rules_max_hold_seconds = self.config.uptrend_rules_max_hold_seconds_by_coin.get(
                    coin, self.config.uptrend_rules_max_hold_seconds)
                if (
                    uptrend_rules_max_hold_seconds > 0
                    and hold_seconds >= uptrend_rules_max_hold_seconds
                ):
                    exit_reason = (
                        '⏱️ UPTREND-RULES-MAX-HOLD '
                        f'({int(hold_seconds)}s >= {uptrend_rules_max_hold_seconds}s)'
                    )
                elif (
                    fast_exit_seconds > 0
                    and hold_seconds >= fast_exit_seconds
                    and pnl_pct <= flat_max_profit_pct
                    and current_signal not in {'BUY', 'STRONG BUY'}
                ):
                    exit_reason = (
                        '📉 UPTREND-RULES-FAST-EXIT '
                        f'({pnl_pct:+.2f}% <= {flat_max_profit_pct:.2f}% '
                        f'after {int(hold_seconds)}s, signal: {current_signal})'
                    )
            if current_price <= stop_loss_level:
                exit_reason = f"🚨 ATR-STOP-LOSS (Stop: {stop_loss_level:.4f})"
            elif (not self.config.partial_take_profit_enabled) and current_price >= take_profit_level:
                exit_reason = f"🎉 ATR-TAKE-PROFIT (TP: {take_profit_level:.4f})"
            elif max_hold_seconds > 0 and hold_seconds >= max_hold_seconds:
                exit_reason = f"⏰ MAX-HOLD-TIME reached ({int(hold_seconds)}s >= {max_hold_seconds}s)"
            elif self.config.exit_on_downtrend and market_analysis is not None:
                signal = market_analysis.get(
                    coin, {}).get('recommendation', '')
                if signal == 'HOLD (Down-Trend)':
                    exit_reason = f"📉 Down-trend signal detected (recommendation: {signal})"
            elif pnl_pct < 0 and market_analysis is not None:
                signal = market_analysis.get(
                    coin, {}).get('recommendation', '')
                if signal not in {'BUY', 'STRONG BUY', 'HOLD (Up-Trend)'}:
                    exit_reason = f"🔻 PnL-NEG-WEAK-SIGNAL ({pnl_pct:+.2f}% | signal: {signal})"
            if exit_reason:
                logger.info(
                    f"{exit_reason} → Verkauf {coin} @ {current_price:.4f}")
                self._execute_trade(coin, "sell", current_price,
                                    amount_coin * current_price, atr=atr, reason=exit_reason)
                trades_to_remove.append(coin)
        for coin in trades_to_remove:
            self.portfolio.remove_trade(coin)

    def run(self):
        """Main loop of the bot."""
        logger.info(
            "🚀 Crypto trading bot started - analysing markets for trading opportunities.")
        safe_config = vars(self.config).copy()
        safe_config['api_key'] = _mask_secret(safe_config.get('api_key', ''))
        safe_config['api_secret'] = _mask_secret(
            safe_config.get('api_secret', ''))
        safe_config['ai_copilot_api_key'] = _mask_secret(
            safe_config.get('ai_copilot_api_key', ''))
        logger.info(f"Configuration: {safe_config}")
        if self.config.dry_run:
            logger.warning(
                "DRY-RUN MODE IS ACTIVE! No real trades will be executed.")
        if self.config.simulate_data:
            logger.warning(
                "SIMULATION MODE IS ACTIVE! No real exchange data will be fetched or trades executed.")

        try:
            while True:
                self.iteration += 1
                logger.info(
                    f"\n🕒 Iteration {self.iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

                # Step 1: Update balance and portfolio
                self._update_portfolio_balance()

                # Step 2: Fetch market data (prices, volumes)
                current_market_data = self._get_market_data()

                if not current_market_data:
                    logger.error(
                        "No market data available. Skipping this iteration.")
                    time.sleep(60)  # Brief pause before retrying
                    continue

                portfolio_value_live = self.portfolio.get_value(
                    {coin: data['price'] for coin, data in current_market_data.items()})
                self._refresh_daily_anchor(portfolio_value_live)

                # Step 3: Run market analysis and identify top recommendations
                market_analysis = self._analyze_markets(
                    current_market_data,
                    extra_coins=list(self.portfolio.open_trades.keys())
                )

                occupied_positions = set(self.portfolio.open_trades.keys()) | set(
                    self.portfolio.holdings.keys())
                available_trade_slots = max(
                    0, self.config.max_open_trades - len(self.portfolio.open_trades))
                dynamic_excluded_entry_pairs = self._dynamic_excluded_entry_pairs()
                if dynamic_excluded_entry_pairs:
                    logger.info(
                        "Dynamically excluded lossmaker entry pairs: %s",
                        ', '.join(sorted(
                            f"{coin}/{source}" for coin, source in dynamic_excluded_entry_pairs)),
                    )
                degraded_entry_sources = self._degraded_entry_sources()
                if degraded_entry_sources:
                    logger.info(
                        "Dynamically degraded entry sources (fallback/force-fill blocked): %s",
                        ', '.join(sorted(degraded_entry_sources)),
                    )
                recent_pnl_guard = self._recent_pnl_guard_state()
                recent_pnl_guard_active = bool(
                    recent_pnl_guard.get('active', False))
                if recent_pnl_guard_active:
                    logger.warning(
                        "Recent PnL guard active: trades=%d, pnl=%.6f %s, pf=%.4f, reason=%s",
                        int(recent_pnl_guard.get('recent_trades', 0)),
                        float(recent_pnl_guard.get(
                            'recent_realized_pnl', 0.0)),
                        self.config.base_currency,
                        float(recent_pnl_guard.get(
                            'recent_profit_factor', 999.0)),
                        str(recent_pnl_guard.get('reason', 'n/a')),
                    )

                def _entry_pair_allowed(coin: str, data: Dict[str, Any]) -> bool:
                    source = str(data.get('signal_source',
                                 'rules')).strip().lower()
                    return (str(coin).upper(), source) not in dynamic_excluded_entry_pairs

                downtrend_filter_results = {
                    coin: self._passes_downtrend_reversal_filter(data)
                    for coin, data in market_analysis.items()
                }
                uptrend_filter_results = {
                    coin: self._passes_uptrend_entry_filter(data)
                    for coin, data in market_analysis.items()
                }
                entry_market_mode = self._entry_market_mode(market_analysis)

                # Only suggest coins not yet held.
                strict_buy_candidates = [
                    coin for coin, data in market_analysis.items()
                    if coin not in occupied_positions
                    and data.get('recommendation') in ['STRONG BUY', 'BUY']
                    and data.get('score', 0) >= self.config.min_entry_score
                    and (
                        not recent_pnl_guard_active
                        or str(data.get('signal_source', 'rules')).strip().lower() == 'catboost'
                    )
                    and _entry_pair_allowed(coin, data)
                ]
                top_buy_recommendations = strict_buy_candidates[:min(
                    self.config.top_n_for_analysis, available_trade_slots)]

                # Fallback: fill remaining slots with solid non-SELL candidates.
                # If exit_on_downtrend is active, exclude down-trend coins from entries.
                _excluded_signals_fallback = ['SELL', 'WEAK SELL']
                if self.config.exit_on_downtrend:
                    _excluded_signals_fallback.append('HOLD (Down-Trend)')
                fallback_base_candidates = [
                    coin for coin, data in market_analysis.items()
                    if coin not in occupied_positions
                    and coin not in top_buy_recommendations
                    if data.get('score', 0) >= self.config.fallback_min_score
                    and str(data.get('signal_source', 'rules')).strip().lower() not in degraded_entry_sources
                    and self._allows_entry_signal(
                        str(data.get('recommendation', '')),
                        downtrend_filter_results.get(
                            coin, (False, 'not_evaluated'))[0],
                        set(_excluded_signals_fallback),
                    )
                    and downtrend_filter_results.get(coin, (False, 'not_evaluated'))[0]
                    and uptrend_filter_results.get(coin, (False, 'not_evaluated'))[0]
                    and _entry_pair_allowed(coin, data)
                ]
                fallback_filter_results = {
                    coin: self._passes_fallback_entry_filter(
                        coin, market_analysis.get(coin, {}))
                    for coin in fallback_base_candidates
                }
                fallback_candidates = []
                fallback_allowed = (
                    self.config.enable_fallback_entry
                    and not recent_pnl_guard_active
                    and available_trade_slots > len(top_buy_recommendations)
                    and (
                        not self.config.defensive_entry_mode_enabled
                        or entry_market_mode != 'defensive'
                    )
                )
                fallback_suppression_reason = 'allowed'
                if not self.config.enable_fallback_entry:
                    fallback_suppression_reason = 'fallback_disabled'
                elif available_trade_slots <= len(top_buy_recommendations):
                    fallback_suppression_reason = 'no_free_slots'
                elif self.config.defensive_entry_mode_enabled and entry_market_mode == 'defensive':
                    fallback_suppression_reason = 'entry_mode_defensive'
                if fallback_allowed:
                    fallback_candidates = [
                        coin for coin in fallback_base_candidates
                        if fallback_filter_results.get(coin, (False, 'not_evaluated'))[0]
                    ]
                    if fallback_candidates:
                        missing_slots = available_trade_slots - \
                            len(top_buy_recommendations)
                        top_buy_recommendations.extend(
                            fallback_candidates[:missing_slots])
                        best_coin = top_buy_recommendations[0]
                        best_data = market_analysis.get(best_coin, {})
                        logger.info(
                            f"ℹ️ Fallback entry activated (slots filled): lead candidate {best_coin} ({best_data.get('recommendation', 'n/a')}, score {best_data.get('score', 0)}).")

                # Optional more aggressive slot filling: use neutral candidates even without RSI filter,
                # down-trend coins are also excluded when exit_on_downtrend is active.
                force_fill_candidates = []
                force_fill_allowed = (
                    self.config.force_fill_slots
                    and not recent_pnl_guard_active
                    and available_trade_slots > len(top_buy_recommendations)
                    and (
                        not self.config.defensive_entry_mode_enabled
                        or entry_market_mode == 'normal'
                    )
                )
                if force_fill_allowed:
                    force_fill_candidates = [
                        coin for coin, data in market_analysis.items()
                        if coin not in occupied_positions
                        and coin not in top_buy_recommendations
                        and data.get('score', 0) >= self.config.force_fill_min_score
                        and str(data.get('signal_source', 'rules')).strip().lower() not in degraded_entry_sources
                        and self._allows_entry_signal(
                            str(data.get('recommendation', '')),
                            downtrend_filter_results.get(
                                coin, (False, 'not_evaluated'))[0],
                            set(_excluded_signals_fallback),
                        )
                        and downtrend_filter_results.get(coin, (False, 'not_evaluated'))[0]
                        and uptrend_filter_results.get(coin, (False, 'not_evaluated'))[0]
                        and _entry_pair_allowed(coin, data)
                    ]
                    if force_fill_candidates:
                        missing_slots = available_trade_slots - \
                            len(top_buy_recommendations)
                        top_buy_recommendations.extend(
                            force_fill_candidates[:missing_slots])
                        logger.info(
                            f"⚙️ Force-fill active: {min(len(force_fill_candidates), missing_slots)} additional candidates added (min score {self.config.force_fill_min_score}).")

                logger.info(
                    f"🔍 Market analysis complete. Top {len(top_buy_recommendations)} buy recommendations:")

                if not top_buy_recommendations and market_analysis:
                    recommendation_mix = dict(Counter(
                        data.get('recommendation', 'UNKNOWN')
                        for data in market_analysis.values()
                    ))
                    downtrend_reversal_eligible = sum(
                        1
                        for coin, data in market_analysis.items()
                        if data.get('recommendation') == 'HOLD (Down-Trend)'
                        and downtrend_filter_results.get(coin, (False, 'not_evaluated'))[0]
                    )
                    logger.info(
                        "ℹ️ No entry candidates this round: strict=%d, fallback=%d, force_fill=%d, downtrend_reversal_eligible=%d, entry_mode=%s, recommendation_mix=%s",
                        len(strict_buy_candidates),
                        len(fallback_candidates),
                        len(force_fill_candidates),
                        downtrend_reversal_eligible,
                        entry_market_mode,
                        recommendation_mix,
                    )
                    self._log_blocked_uptrend_candidates(
                        market_analysis,
                        uptrend_filter_results,
                    )
                    self._log_blocked_downtrend_reversal_candidates(
                        market_analysis,
                        downtrend_filter_results,
                    )
                    self._log_fallback_entry_diagnostics(
                        market_analysis,
                        fallback_base_candidates,
                        fallback_filter_results,
                        fallback_allowed,
                        fallback_suppression_reason,
                        entry_market_mode,
                    )
                    for coin, data in list(market_analysis.items())[:5]:
                        signal_confidence = data.get('signal_confidence')
                        confidence_text = ''
                        if signal_confidence is not None:
                            confidence_text = f", confidence={float(signal_confidence) * 100:.0f}%"
                        logger.info(
                            "  - %s: rec=%s, rule=%s, score=%s, rule_score=%s, RSI=%.2f, source=%s%s",
                            coin,
                            data.get('recommendation', 'n/a'),
                            data.get('rule_recommendation', 'n/a'),
                            data.get('score', 0),
                            data.get('rule_score', 0),
                            float(data.get('rsi', np.nan)),
                            data.get('signal_source', 'n/a'),
                            confidence_text,
                        )

                for i, coin in enumerate(top_buy_recommendations):
                    data = market_analysis.get(coin, {})
                    logger.info(f"  {i+1}. {coin}: {data.get('recommendation', 'HOLD')} "
                                f"(Price: {data.get('price', 0):.4f} {self.config.base_currency}, "
                                f"Score: {data.get('score', 0)}%, "
                                f"Volumen: {data.get('volume', 0):,.0f} {self.config.base_currency}, "
                                f"RSI: {data.get('rsi', np.nan):.2f})")

                # Step 4: Exit logic - close positions on SELL signals
                if self.config.enable_signal_exits and self.portfolio.open_trades:
                    sell_signal_coins = []
                    for coin in list(self.portfolio.open_trades.keys()):
                        signal = market_analysis.get(
                            coin, {}).get('recommendation')
                        if signal in ['SELL', 'WEAK SELL']:
                            sell_signal_coins.append(coin)

                    if sell_signal_coins:
                        logger.info(
                            f"📉 Exit signals detected for {len(sell_signal_coins)} coins: {sell_signal_coins}")
                        for coin in sell_signal_coins:
                            price_data = current_market_data.get(coin)
                            trade_info = self.portfolio.open_trades.get(coin)
                            if not price_data or not trade_info:
                                continue
                            current_price = price_data['price']
                            amount_coin = trade_info['amount_coin']
                            self._execute_trade(
                                coin,
                                "sell",
                                current_price,
                                amount_coin * current_price,
                                reason='📉 SIGNAL-EXIT'
                            )

                # Step 5: Trade logic - open new positions
                current_open_trades_count = len(self.portfolio.open_trades)
                available_funds_for_trade = self.portfolio.cash
                effective_trade_amount = self._effective_trade_amount(
                    portfolio_value_live)
                effective_max_open_trades = self._effective_max_open_trades(
                    portfolio_value_live)
                min_required_cash = self.config.min_trade_amount if self.config.allow_partial_trades else effective_trade_amount
                can_open_positions, block_reason = self._can_open_new_positions(
                    portfolio_value_live)

                if not can_open_positions:
                    logger.warning(
                        f"⛔ Risk guardrail blocking new positions: {block_reason}")
                elif available_funds_for_trade >= min_required_cash and current_open_trades_count < effective_max_open_trades:
                    logger.info(
                        f"Attempting to open new positions. Available: {available_funds_for_trade:.2f} {self.config.base_currency}, Open trades: {current_open_trades_count}/{effective_max_open_trades}, Min amount: {min_required_cash:.2f} {self.config.base_currency}, Effective trade amount: {effective_trade_amount:.2f} {self.config.base_currency}.")
                    blocked_buy_attempts: List[Dict[str, Any]] = []
                    successful_buy_count = 0
                    for coin in top_buy_recommendations:
                        # Only trade if no position is already open
                        if coin not in self.portfolio.open_trades and coin not in self.portfolio.holdings:
                            coin_data = market_analysis.get(coin, {})
                            attempt_meta = {
                                'coin': coin,
                                'signal_source': coin_data.get('signal_source', 'rules'),
                                'score': coin_data.get('score', 0),
                                'recommendation': coin_data.get('recommendation', 'HOLD'),
                                'position_size_text': 'n/a',
                                'cash_text': f"{self.portfolio.cash:.2f}",
                                'cooldown_text': 'n/a',
                            }
                            price_data = current_market_data.get(coin)
                            if price_data:
                                current_price = price_data['price']
                                atr = self._get_atr_for_coin(coin, period=14)
                                target_position_size = self._calculate_position_size(
                                    coin,
                                    current_price,
                                    atr,
                                    portfolio_value=portfolio_value_live,
                                    trade_amount_cap=effective_trade_amount,
                                )
                                reserved_cash = self.portfolio.cash * self.config.cash_reserve_pct
                                max_affordable = max(
                                    self.portfolio.cash - reserved_cash, 0.0)
                                position_size = min(
                                    target_position_size, max_affordable)
                                attempt_meta['position_size_text'] = f"{position_size:.2f}"
                                attempt_meta['cash_text'] = f"{self.portfolio.cash:.2f}"

                                # Very small ATR risk amounts should not completely block entries
                                # as long as the minimum trade is still affordable with the cash reserve.
                                if position_size < self.config.min_trade_amount and max_affordable >= self.config.min_trade_amount:
                                    position_size = self.config.min_trade_amount
                                    attempt_meta['position_size_text'] = f"{position_size:.2f}"

                                if position_size < self.config.min_trade_amount:
                                    logger.info(
                                        f"Skipping {coin}: effective trade amount {position_size:.2f} {self.config.base_currency} is below MIN_TRADE_AMOUNT {self.config.min_trade_amount:.2f}.")
                                    blocked_buy_attempts.append({
                                        **attempt_meta,
                                        'reason': (
                                            'below_min_trade_amount '
                                            f'({position_size:.2f} < {self.config.min_trade_amount:.2f})'
                                        ),
                                    })
                                    continue

                                if self.portfolio.cash >= position_size:
                                    logger.info(
                                        f"ℹ️ Attempting to buy {coin}...")
                                    signal_source = coin_data.get(
                                        'signal_source', 'rules')
                                    signal_confidence = coin_data.get(
                                        'signal_confidence')
                                    passes_filter, filter_reason = self._passes_entry_momentum_filter(
                                        coin_data)
                                    if not passes_filter:
                                        logger.info(
                                            f"⛔ Momentum filter blocked entry for {coin}: {filter_reason}")
                                        blocked_buy_attempts.append({
                                            **attempt_meta,
                                            'reason': filter_reason,
                                        })
                                        continue
                                    effective_cooldown, cooldown_mode = self._resolve_reentry_cooldown_seconds(
                                        current_price, atr)
                                    in_cooldown, cooldown_remaining = self._is_coin_in_reentry_cooldown(
                                        coin, cooldown_seconds=effective_cooldown)
                                    if in_cooldown:
                                        cooldown_text = (
                                            f"{cooldown_remaining}s remaining "
                                            f"(effective={effective_cooldown}s, {cooldown_mode})"
                                        )
                                        logger.info(
                                            f"⛔ Re-entry cooldown active for {coin}: {cooldown_remaining}s remaining "
                                            f"(effective={effective_cooldown}s, base={self.config.reentry_cooldown_seconds}s, {cooldown_mode})")
                                        blocked_buy_attempts.append({
                                            **attempt_meta,
                                            'reason': 'reentry_cooldown_active',
                                            'cooldown_text': cooldown_text,
                                        })
                                        continue
                                    if self._execute_trade(
                                        coin,
                                        "buy",
                                        current_price,
                                        position_size,
                                        atr=atr,
                                        signal_source=signal_source,
                                        signal_confidence=signal_confidence,
                                        recommendation=coin_data.get(
                                            'recommendation', 'HOLD'),
                                        reason='ENTRY',
                                    ):
                                        successful_buy_count += 1
                                        # Update available capital
                                        available_funds_for_trade = self.portfolio.cash
                                        current_open_trades_count = len(
                                            self.portfolio.open_trades)
                                        if current_open_trades_count >= effective_max_open_trades:
                                            logger.info(
                                                "Maximum number of open trades reached, stopping further buys.")
                                            break
                                    else:
                                        blocked_buy_attempts.append({
                                            **attempt_meta,
                                            'reason': 'execute_trade_failed',
                                        })
                                else:
                                    logger.warning(
                                        f"Insufficient available funds ({self.portfolio.cash:.2f} {self.config.base_currency}) for a trade of {position_size:.2f} {self.config.base_currency} for {coin}.")
                                    blocked_buy_attempts.append({
                                        **attempt_meta,
                                        'reason': (
                                            'insufficient_available_funds '
                                            f'({self.portfolio.cash:.2f} < {position_size:.2f})'
                                        ),
                                    })
                            else:
                                logger.warning(
                                    f"Price data for {coin} not found, cannot buy.")
                                blocked_buy_attempts.append({
                                    **attempt_meta,
                                    'reason': 'missing_price_data',
                                })
                        else:
                            logger.debug(
                                f"{coin} already in portfolio or open position.")
                    if top_buy_recommendations and successful_buy_count == 0:
                        self._log_blocked_buy_attempt_candidates(
                            blocked_buy_attempts)
                elif current_open_trades_count >= effective_max_open_trades:
                    logger.info(
                        f"Maximum number of open trades ({effective_max_open_trades}) reached. No new buys.")
                else:
                    logger.info(
                        f"Insufficient available funds ({available_funds_for_trade:.2f} {self.config.base_currency}) for minimum trade ({min_required_cash:.2f} {self.config.base_currency}).")

                # Step 6: Monitor open trades (stop-loss, take-profit, time, signals)
                if self.portfolio.open_trades:
                    logger.info(
                        f"💼 Monitoring {len(self.portfolio.open_trades)} open trades...")
                    self._manage_open_trades(
                        current_market_data, market_analysis)

                # Step 7: Display portfolio status
                portfolio_value = self.portfolio.get_value(
                    {coin: data['price'] for coin, data in current_market_data.items()})
                if self.portfolio.initial_portfolio_value is None:
                    self.portfolio.initial_portfolio_value = portfolio_value
                    if self.config.dry_run:
                        self.portfolio.save_state()
                logger.info(
                    f"📈 Portfolio value: {portfolio_value:.2f} {self.config.base_currency}")
                logger.info(
                    f"  - Cash: {self.portfolio.cash:.2f} {self.config.base_currency}")
                logger.info(f"  - Holdings: {self.portfolio.holdings}")
                logger.info(
                    f"  - Open trades details: {self.portfolio.open_trades}")
                self._write_analytics_snapshot(
                    portfolio_value, market_analysis)

                if self.iteration % max(1, self.config.performance_report_every) == 0:
                    self._log_performance_report()

                # Step 8: Optional external AI co-pilot(s) (rate- and budget-limited)
                if self.config.ai_copilot_enabled or self.config.ai_copilot_benchmark_enabled:
                    ai_snapshot = self._ai_copilot_snapshot()
                    self._maybe_run_ai_copilot(ai_snapshot)
                    self._maybe_run_ai_copilot_benchmark(ai_snapshot)

                # Step 9: Wait
                logger.info(
                    f"⏳ Next analysis in {self.config.check_interval} seconds...")
                time.sleep(self.config.check_interval)

        except KeyboardInterrupt:
            logger.info("\n🛑 Bot stopping...")
        except Exception as e:
            logger.critical(
                f"Critical, unexpected error in main loop: {e}", exc_info=True)
        finally:
            self.analytics_writer.close()
            logger.info("👋 Bot stopped")


class Backtester:
    """Backtesting module for the trading strategy on historical data."""

    def __init__(self, data_file="training_data.csv", initial_cash=10000, base_currency="EUR"):
        self.data_file = data_file
        self.initial_cash = initial_cash
        self.base_currency = base_currency
        self.trades = []
        self.equity_curve = []

    def run(self, strategy_func, **kwargs):
        df = pd.read_csv(self.data_file)
        df = df.sort_values(["coin", "timestamp"]).reset_index(drop=True)
        cash = self.initial_cash
        holdings = {}
        self.equity_curve = []
        self.trades = []

        for _, row in df.iterrows():
            coin = row["coin"]
            price = row["close"]
            signal = strategy_func(row, **kwargs)

            if signal == "kaufen" and cash > 0:
                amount = cash / price
                holdings[coin] = holdings.get(coin, 0) + amount
                self.trades.append(
                    (row["timestamp"], coin, "buy", price, amount))
                cash = 0
            elif signal == "verkaufen" and holdings.get(coin, 0) > 0:
                amount = holdings[coin]
                cash += amount * price
                self.trades.append(
                    (row["timestamp"], coin, "sell", price, amount))
                holdings[coin] = 0

            equity = cash + sum(holdings.get(c, 0) *
                                row["close"] for c in holdings)
            self.equity_curve.append(equity)

        self._report()

    def _report(self):
        if not self.equity_curve:
            print("No backtest run.")
            return
        total_return = (
            self.equity_curve[-1] - self.initial_cash) / self.initial_cash * 100
        max_drawdown = self._max_drawdown(self.equity_curve)
        print("\nBacktest report:")
        print(
            f"  Starting capital: {self.initial_cash:.2f} {self.base_currency}")
        print(
            f"  End capital:   {self.equity_curve[-1]:.2f} {self.base_currency}")
        print(f"  Total return: {total_return:.2f}%")
        print(f"  Max drawdown:  {max_drawdown:.2f}%")
        print(f"  Trades:        {len(self.trades)}")

    def _max_drawdown(self, curve):
        curve = np.array(curve)
        highwater = np.maximum.accumulate(curve)
        drawdowns = (curve - highwater) / highwater
        return drawdowns.min() * 100


def simple_strategy(row, rsi_buy=30, rsi_sell=70):
    # Example strategy: RSI-based buy/sell signals
    if "rsi" in row:
        if row["rsi"] < rsi_buy:
            return "kaufen"
        if row["rsi"] > rsi_sell:
            return "verkaufen"
    return "halten"


if __name__ == "__main__":
    import argparse

    configure_logging(os.getenv("BOT_LOG_FILE", "trading_bot.log"))

    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest", action="store_true",
                        help="Start backtest on training data")
    args, _ = parser.parse_known_args()

    if args.backtest:
        print("\nStarting backtest on training data...",)
        backtester = Backtester(
            data_file="training_data.csv", initial_cash=10000)
        backtester.run(simple_strategy)

    try:
        config = BotConfig()
        if not config.simulate_data and (not config.api_key or not config.api_secret):
            logger.critical(
                "❌ API keys not found in .env file and simulation mode is DISABLED!")
            logger.info("Please create a .env file with:")
            logger.info("KRAKEN_API_KEY=your_api_key")
            logger.info("KRAKEN_API_SECRET=your_api_secret")
            logger.info(
                "Or set SIMULATE_DATA=True in the .env file.")
            sys.exit(1)

        # Test whether pandas and numpy are correctly installed (required for indicator calculation)
        try:
            import pandas as pd  # noqa: F401
            import numpy as np  # noqa: F401
        except ImportError:
            logger.critical(
                "❌ Pandas or Numpy are not installed. These are required for indicator calculation. Please install them with 'pip install pandas numpy'.")
            sys.exit(1)

        logger.info("🛠️ Bot configuration:")
        logger.info(f"   Exchange: {config.exchange_name}")
        logger.info(f"   Base currency: {config.base_currency}")
        logger.info(
            f"   Trade amount per buy: {config.trade_amount:.2f} {config.base_currency}")
        logger.info(
            f"   Check interval: {config.check_interval} seconds")
        logger.info(
            f"   Dry-run mode: {'Enabled' if config.dry_run else 'Disabled'}")
        logger.info(
            f"   Simulation mode: {'Enabled' if config.simulate_data else 'Disabled'}")
        logger.info(f"   Stop-loss percent: {config.stop_loss_pct * 100:.1f}%")
        logger.info(
            f"   Take-profit percent: {config.take_profit_pct * 100:.1f}%")
        logger.info(f"   Max. open trades: {config.max_open_trades}")
        logger.info(
            f"   Min. 24h volume for analysis: {config.min_volume_base:,.0f} {config.base_currency}")
        logger.info(
            f"   Top N coins for detailed analysis: {config.top_n_for_analysis}")
        logger.info(f"   RSI period: {config.rsi_period}")
        logger.info(
            f"   SMA short/long periods: {config.sma_period_short}/{config.sma_period_long}")
        logger.info(
            f"   OHLCV Timeframe: {config.ohlcv_timeframe}")
        logger.info(
            f"   OHLCV limit (for indicators): {config.ohlcv_limit} candles")

        bot = CryptoTradingBot(config)
        bot.run()
    except Exception as e:
        logger.critical(f"Initialisation error: {e}", exc_info=True)
        sys.exit(1)
