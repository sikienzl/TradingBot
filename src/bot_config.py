"""Environment-driven configuration for the trading bot.

Extracted from ``src.trading_bot`` so configuration parsing and validation can
be tested and reused without importing the full trading bot.
"""

from __future__ import annotations

import math
import os
from typing import Any

from dotenv import load_dotenv

from src.env_parsing import env_bool as _env_bool
from src.env_parsing import env_optional_float as _env_optional_float
from src.env_parsing import env_str as _env_str
from src.env_parsing import symbol_float_map as _env_symbol_float_map
from src.env_parsing import symbol_int_map as _env_symbol_int_map
from src.env_parsing import threshold_float_pairs as _env_threshold_float_pairs
from src.env_parsing import threshold_int_pairs as _env_threshold_int_pairs


class BotConfig:
    """Configuration class for the trading bot."""

    def __init__(self):
        load_dotenv()

        self._load_exchange_settings()
        self._load_risk_management_settings()
        self._load_trading_parameters()
        self._load_entry_strategy_settings()
        self._load_entry_filter_and_reentry_settings()
        self._load_exit_management_settings()
        self._load_ml_settings()
        self._load_analytics_and_guardrail_settings()
        self._load_ai_copilot_settings()
    def _load_exchange_settings(self) -> None:
        self.exchange_name = os.getenv('EXCHANGE_NAME', 'kraken').lower()
        self.api_key = os.getenv(
            'KRAKEN_API_KEY', '')
        self.api_secret = os.getenv(
            'KRAKEN_API_SECRET', '')
        self.base_currency = os.getenv('BASE_CURRENCY', 'EUR').upper()
        self.quote_currencies = [c.strip().upper() for c in os.getenv(
            'QUOTE_CURRENCIES', 'BTC,ETH').split(',') if c.strip()]
        configured_excluded_coins = {c.strip().upper() for c in os.getenv(
            'EXCLUDED_COINS', 'USDC,USDT,EURT,EURC,DAI,TUSD,USDP,FDUSD,USDE').split(',') if c.strip()}
        lossmaker_excluded_coins = {c.strip().upper() for c in os.getenv(
            'LOSSMAKER_EXCLUDED_COINS', 'ZEC,HYPE,TON,BTC,XRP').split(',') if c.strip()}
        self.excluded_coins = configured_excluded_coins | lossmaker_excluded_coins
        self.dynamic_lossmaker_exclusion_enabled = _env_bool(
            'DYNAMIC_LOSSMAKER_EXCLUSION_ENABLED', True)
        self.dynamic_lossmaker_window = int(
            os.getenv('DYNAMIC_LOSSMAKER_WINDOW', str(80)))
        self.dynamic_lossmaker_min_sells = int(
            os.getenv('DYNAMIC_LOSSMAKER_MIN_SELLS', str(3)))
        self.dynamic_lossmaker_max_win_rate_pct = float(
            os.getenv('DYNAMIC_LOSSMAKER_MAX_WIN_RATE_PCT', str(45.0)))
        self.dynamic_lossmaker_min_pnl_loss = float(
            os.getenv('DYNAMIC_LOSSMAKER_MIN_PNL_LOSS', str(0.003)))
        self.dynamic_lossmaker_min_max_hold_exit_ratio = float(
            os.getenv('DYNAMIC_LOSSMAKER_MIN_MAX_HOLD_EXIT_RATIO', str(0.5)))
        self.recent_pnl_guard_enabled = _env_bool(
            'RECENT_PNL_GUARD_ENABLED', True)
        self.recent_pnl_guard_window = int(
            os.getenv('RECENT_PNL_GUARD_WINDOW', str(120)))
        self.recent_pnl_guard_min_trades = int(
            os.getenv('RECENT_PNL_GUARD_MIN_TRADES', str(60)))
        self.recent_pnl_guard_min_realized_pnl = float(
            os.getenv('RECENT_PNL_GUARD_MIN_REALIZED_PNL', str(0.0)))
        self.recent_pnl_guard_max_profit_factor = float(
            os.getenv('RECENT_PNL_GUARD_MAX_PROFIT_FACTOR', str(1.0)))
        self.recent_pnl_guard_stale_hours = float(
            os.getenv('RECENT_PNL_GUARD_STALE_HOURS', str(24.0)))
        # Amount in base currency per trade

    def _load_risk_management_settings(self) -> None:
        self.kill_switch_file = _env_str(
            'TRADING_KILL_SWITCH_FILE', '.trading_kill_switch')
        self.execution_lock_file = _env_str(
            'TRADING_EXECUTION_LOCK_FILE', '.trading_execution.lock')
        self.cluster_execution_lease_enabled = _env_bool(
            'TRADING_CLUSTER_EXECUTION_LEASE_ENABLED', False)
        self.cluster_execution_lease_database_url = _env_str(
            'TRADING_CLUSTER_EXECUTION_LEASE_DATABASE_URL', '')
        self.execution_account_id = _env_str('TRADING_EXECUTION_ACCOUNT_ID', '')
        if self.cluster_execution_lease_enabled and (
            not self.cluster_execution_lease_database_url or not self.execution_account_id
        ):
            raise ValueError(
                "Cluster execution lease requires a database URL and account ID"
            )
        self.live_portfolio_state_file = _env_str(
            'LIVE_PORTFOLIO_STATE_FILE', '.live_portfolio_state.json')
        # Durable daily-loss anchor and loss-streak state (audit point 4).
        self.risk_state_file = _env_str(
            'TRADING_RISK_STATE_FILE', '.trading_risk_state.json')
        self.trade_amount = float(os.getenv('TRADE_AMOUNT', str(20)))
        self.portfolio_trade_amount_multipliers = _env_threshold_float_pairs(
            'PORTFOLIO_TRADE_AMOUNT_MULTIPLIERS',
            '50:1.10,100:1.25,200:1.50,300:1.75,500:2.20,1000:3.00',
        )
        # Minimum amount per trade in base currency
        self.min_trade_amount = float(os.getenv('MIN_TRADE_AMOUNT', str(5)))
        # Seconds between iterations
        self.check_interval = int(os.getenv('CHECK_INTERVAL', str(100)))
        self.dry_run = _env_bool('DRY_RUN', True)
        self.dry_run_costs_enabled = _env_bool('DRY_RUN_COSTS_ENABLED', False)
        self.dry_run_fee_rate = 0.002
        self.dry_run_slippage_rate = 0.001
        if self.dry_run and self.dry_run_costs_enabled:
            for name, attr, default in (
                ('DRY_RUN_FEE_RATE', 'dry_run_fee_rate', '0.002'),
                ('DRY_RUN_SLIPPAGE_RATE', 'dry_run_slippage_rate', '0.001'),
            ):
                try:
                    value = float(os.getenv(name, default))
                except ValueError as exc:
                    raise ValueError(f"{name} must be finite and in [0, 1)") from exc
                if not math.isfinite(value) or not 0 <= value < 1:
                    raise ValueError(f"{name} must be finite and in [0, 1)")
                setattr(self, attr, value)
        self.simulate_data = _env_bool('SIMULATE_DATA', False)
        self.simulation_initial_balance_from_kraken = _env_bool(
            'SIMULATION_INITIAL_BALANCE_FROM_KRAKEN', False)
        self.simulation_seed = int(os.getenv('SIMULATION_SEED', str(42)))
        self.simulation_regime = _env_str(
            'SIMULATION_REGIME', 'neutral').lower()

    def _load_trading_parameters(self) -> None:
        self.stop_loss_pct = float(
            os.getenv('STOP_LOSS_PCT', str(0.05)))  # 5% loss
        self.take_profit_pct = float(
            os.getenv('TAKE_PROFIT_PCT', str(0.10)))  # 10% gain
        self.max_open_trades = int(os.getenv('MAX_OPEN_TRADES', str(3)))
        self.portfolio_max_open_trades_tiers = _env_threshold_int_pairs(
            'PORTFOLIO_MAX_OPEN_TRADES_TIERS',
            '100:3,200:4,500:5,1000:6',
        )
        # Minimum 24h volume in base currency
        self.min_volume_base = float(os.getenv('MIN_VOLUME_BASE', str(100000)))
        self.max_entry_spread_bps = float(
            os.getenv('MAX_ENTRY_SPREAD_BPS', '100'))
        if not math.isfinite(self.max_entry_spread_bps) or self.max_entry_spread_bps < 0:
            raise ValueError("MAX_ENTRY_SPREAD_BPS must be finite and non-negative")
        # Pre-trade execution-cost budget for live orders, in basis points of the
        # order value (fee + slippage + spread). 0 disables the cost ceiling; the
        # spread ceiling above still applies.
        self.max_entry_cost_bps = float(os.getenv('MAX_ENTRY_COST_BPS', '120'))
        if not math.isfinite(self.max_entry_cost_bps) or self.max_entry_cost_bps < 0:
            raise ValueError("MAX_ENTRY_COST_BPS must be finite and non-negative")
        # Fee/slippage assumptions for the pre-trade estimate. Unset means "use the
        # exchange-reported taker fee / the documented default".
        self.live_taker_fee_rate = _env_optional_float('LIVE_TAKER_FEE_RATE')
        if (self.live_taker_fee_rate is not None
                and not 0.0 <= self.live_taker_fee_rate < 1.0):
            raise ValueError("LIVE_TAKER_FEE_RATE must be in [0, 1)")
        self.live_slippage_rate = _env_optional_float('LIVE_SLIPPAGE_RATE')
        if (self.live_slippage_rate is not None
                and not 0.0 <= self.live_slippage_rate < 1.0):
            raise ValueError("LIVE_SLIPPAGE_RATE must be in [0, 1)")
        # Number of top coins for analysis
        self.top_n_for_analysis = int(os.getenv('TOP_N_FOR_ANALYSIS', str(10)))
        self.exchange_timeout_ms = int(os.getenv('EXCHANGE_TIMEOUT_MS', str(15000)))
        # Split ticker requests into manageable chunks to avoid oversized exchange calls.
        self.ticker_batch_size = int(os.getenv('TICKER_BATCH_SIZE', str(80)))
        self.ticker_fetch_retries = int(os.getenv('TICKER_FETCH_RETRIES', str(2)))
        self.ticker_retry_delay_seconds = float(
            os.getenv('TICKER_RETRY_DELAY_SECONDS', str(1.0)))
        self.ohlcv_fetch_retries = int(os.getenv('OHLCV_FETCH_RETRIES', str(1)))
        self.ohlcv_retry_delay_seconds = float(
            os.getenv('OHLCV_RETRY_DELAY_SECONDS', str(1.0)))
        self.slow_analysis_warning_seconds = float(
            os.getenv('SLOW_ANALYSIS_WARNING_SECONDS', str(8.0)))
        self.rsi_period = int(os.getenv('RSI_PERIOD', str(14)))
        self.sma_period_short = int(os.getenv('SMA_PERIOD_SHORT', str(20)))
        self.sma_period_long = int(os.getenv('SMA_PERIOD_LONG', str(50)))
        # Minimum score for entries (HOLD (Up-Trend) also possible)
        self.min_entry_score = int(os.getenv('MIN_ENTRY_SCORE', str(60)))
        # ATR/price is a volatility proxy for entry screening, not an expected return.
        self.min_entry_atr_pct = float(os.getenv('MIN_ENTRY_ATR_PCT', str(0.012)))
        # If no classic BUY recommendation is available, still trade the strongest candidate

    def _load_entry_strategy_settings(self) -> None:
        self.enable_fallback_entry = _env_bool('ENABLE_FALLBACK_ENTRY', True)
        # Minimum score for fallback entries only (can be lower than MIN_ENTRY_SCORE)
        self.fallback_min_score = int(os.getenv('FALLBACK_MIN_SCORE', str(45)))
        # RSI limit for fallback entries (avoid overbought assets)
        self.fallback_max_rsi = float(os.getenv('FALLBACK_MAX_RSI', str(68)))
        self.fallback_max_rsi_by_coin = _env_symbol_float_map(
            'FALLBACK_MAX_RSI_BY_COIN', 'TRX:72')
        # Optionally forces filling of free slots with neutral non-SELL candidates
        self.force_fill_slots = _env_bool('FORCE_FILL_SLOTS', False)
        # Minimum score for forced slot filling (without RSI limit)
        self.force_fill_min_score = int(os.getenv('FORCE_FILL_MIN_SCORE', str(35)))
        self.defensive_entry_mode_enabled = _env_bool(
            'DEFENSIVE_ENTRY_MODE_ENABLED', True)
        # Guard aggressive fallback entries in down-trends behind a stricter reversal filter.
        self.downtrend_reversal_entry_enabled = _env_bool(
            'DOWNTREND_REVERSAL_ENTRY_ENABLED', True)
        self.downtrend_reversal_max_rsi = float(
            os.getenv('DOWNTREND_REVERSAL_MAX_RSI', str(30.0)))
        self.downtrend_reversal_max_rsi_by_coin = _env_symbol_float_map(
            'DOWNTREND_REVERSAL_MAX_RSI_BY_COIN', '')
        self.downtrend_reversal_min_buy_proba = float(
            os.getenv('DOWNTREND_REVERSAL_MIN_BUY_PROBA', str(0.20)))
        self.downtrend_reversal_min_buy_proba_by_coin = _env_symbol_float_map(
            'DOWNTREND_REVERSAL_MIN_BUY_PROBA_BY_COIN', '')
        self.downtrend_reversal_max_sell_proba = float(
            os.getenv('DOWNTREND_REVERSAL_MAX_SELL_PROBA', str(0.35)))
        self.downtrend_reversal_max_sell_proba_by_coin = _env_symbol_float_map(
            'DOWNTREND_REVERSAL_MAX_SELL_PROBA_BY_COIN', '')
        self.downtrend_reversal_min_proba_edge = float(
            os.getenv('DOWNTREND_REVERSAL_MIN_PROBA_EDGE', str(0.02)))
        self.downtrend_reversal_min_proba_edge_by_coin = _env_symbol_float_map(
            'DOWNTREND_REVERSAL_MIN_PROBA_EDGE_BY_COIN', '')
        self.downtrend_reversal_min_ret_1 = float(
            os.getenv('DOWNTREND_REVERSAL_MIN_RET_1', str(0.0)))
        self.downtrend_reversal_min_ret_3 = float(
            os.getenv('DOWNTREND_REVERSAL_MIN_RET_3', str(-0.01)))
        self.downtrend_reversal_min_macd_hist = float(
            os.getenv('DOWNTREND_REVERSAL_MIN_MACD_HIST', str(0.0)))
        self.downtrend_reversal_allowed_coins = {
            c.strip().upper() for c in os.getenv(
                'DOWNTREND_REVERSAL_ALLOWED_COINS', 'ETH').split(',') if c.strip()
        }
        self.downtrend_reversal_fast_exit_enabled = _env_bool(
            'DOWNTREND_REVERSAL_FAST_EXIT_ENABLED', True)
        self.downtrend_reversal_fast_exit_seconds = int(
            os.getenv('DOWNTREND_REVERSAL_FAST_EXIT_SECONDS', str(120)))
        self.downtrend_reversal_flat_max_profit_pct = float(
            os.getenv('DOWNTREND_REVERSAL_FLAT_MAX_PROFIT_PCT', str(0.10)))
        self.downtrend_reversal_weak_signal_exit_seconds = int(
            os.getenv('DOWNTREND_REVERSAL_WEAK_SIGNAL_EXIT_SECONDS', str(45)))
        self.downtrend_reversal_weak_signal_max_profit_pct = float(
            os.getenv('DOWNTREND_REVERSAL_WEAK_SIGNAL_MAX_PROFIT_PCT', str(0.02)))
        self.downtrend_reversal_max_hold_seconds = int(
            os.getenv('DOWNTREND_REVERSAL_MAX_HOLD_SECONDS', str(240)))
        self.uptrend_entry_gate_enabled = _env_bool(
            'UPTREND_ENTRY_GATE_ENABLED', True)
        self.uptrend_entry_max_rsi = float(
            os.getenv('UPTREND_ENTRY_MAX_RSI', str(72.0)))
        self.uptrend_entry_max_rsi_by_coin = _env_symbol_float_map(
            'UPTREND_ENTRY_MAX_RSI_BY_COIN', 'XDC:78,TRX:74')
        self.uptrend_entry_min_buy_proba = float(
            os.getenv('UPTREND_ENTRY_MIN_BUY_PROBA', str(0.24)))
        self.uptrend_entry_min_buy_proba_by_coin = _env_symbol_float_map(
            'UPTREND_ENTRY_MIN_BUY_PROBA_BY_COIN', 'TRX:0.150')
        self.uptrend_entry_max_sell_proba = float(
            os.getenv('UPTREND_ENTRY_MAX_SELL_PROBA', str(0.34)))
        self.uptrend_entry_max_sell_proba_by_coin = _env_symbol_float_map(
            'UPTREND_ENTRY_MAX_SELL_PROBA_BY_COIN', 'ONDO:0.445')
        self.uptrend_entry_min_proba_edge = float(
            os.getenv('UPTREND_ENTRY_MIN_PROBA_EDGE', str(-0.05)))
        self.uptrend_entry_min_proba_edge_by_coin = _env_symbol_float_map(
            'UPTREND_ENTRY_MIN_PROBA_EDGE_BY_COIN', 'TRX:-0.13,ONDO:-0.055')
        self.uptrend_rules_fast_exit_enabled = _env_bool(
            'UPTREND_RULES_FAST_EXIT_ENABLED', True)
        self.uptrend_rules_fast_exit_seconds = int(
            os.getenv('UPTREND_RULES_FAST_EXIT_SECONDS', str(120)))
        self.uptrend_rules_fast_exit_seconds_by_coin = _env_symbol_int_map(
            'UPTREND_RULES_FAST_EXIT_SECONDS_BY_COIN')
        self.uptrend_rules_flat_max_profit_pct = float(
            os.getenv('UPTREND_RULES_FLAT_MAX_PROFIT_PCT', str(0.08)))
        self.uptrend_rules_flat_max_profit_pct_by_coin = _env_symbol_float_map(
            'UPTREND_RULES_FLAT_MAX_PROFIT_PCT_BY_COIN')
        self.uptrend_rules_max_hold_seconds = int(
            os.getenv('UPTREND_RULES_MAX_HOLD_SECONDS', str(300)))
        self.uptrend_rules_max_hold_seconds_by_coin = _env_symbol_int_map(
            'UPTREND_RULES_MAX_HOLD_SECONDS_BY_COIN')
        # Optional momentum quality filter for new BUY entries.

    def _load_entry_filter_and_reentry_settings(self) -> None:
        self.entry_momentum_filter_enabled = _env_bool(
            'ENTRY_MOMENTUM_FILTER_ENABLED', True)
        self.entry_min_ret_3 = float(os.getenv('ENTRY_MIN_RET_3', str(-0.01)))
        self.entry_min_ret_3_by_coin = _env_symbol_float_map(
            'ENTRY_MIN_RET_3_BY_COIN')
        self.entry_require_price_above_ema20 = _env_bool(
            'ENTRY_REQUIRE_PRICE_ABOVE_EMA20', False)
        self.entry_sharp_pump_filter_enabled = _env_bool(
            'ENTRY_SHARP_PUMP_FILTER_ENABLED', True)
        self.entry_max_ret_1 = float(os.getenv('ENTRY_MAX_RET_1', str(0.04)))
        self.entry_max_ret_3 = float(os.getenv('ENTRY_MAX_RET_3', str(0.08)))
        self.reentry_cooldown_seconds = int(
            os.getenv('REENTRY_COOLDOWN_SECONDS', str(900)))
        self.reentry_dynamic_enabled = _env_bool(
            'REENTRY_DYNAMIC_ENABLED', True)
        self.reentry_cooldown_min_seconds = int(
            os.getenv('REENTRY_COOLDOWN_MIN_SECONDS', str(300)))
        self.reentry_cooldown_max_seconds = int(
            os.getenv(
                'REENTRY_COOLDOWN_MAX_SECONDS',
                str(max(1200, self.reentry_cooldown_seconds)),
            ))
        self.reentry_cooldown_max_seconds = max(self.reentry_cooldown_max_seconds, self.reentry_cooldown_min_seconds)
        self.reentry_volatility_low_atr_pct = float(
            os.getenv('REENTRY_VOLATILITY_LOW_ATR_PCT', str(0.008)))
        self.reentry_volatility_high_atr_pct = float(
            os.getenv('REENTRY_VOLATILITY_HIGH_ATR_PCT', str(0.018)))
        self.reentry_cooldown_mult_low_vol = float(
            os.getenv('REENTRY_COOLDOWN_MULT_LOW_VOL', str(0.70)))
        self.reentry_cooldown_mult_mid_vol = float(
            os.getenv('REENTRY_COOLDOWN_MULT_MID_VOL', str(1.00)))
        self.reentry_cooldown_mult_high_vol = float(
            os.getenv('REENTRY_COOLDOWN_MULT_HIGH_VOL', str(1.30)))
        # Enables additional exit signals from market analysis

    def _load_exit_management_settings(self) -> None:
        self.enable_signal_exits = _env_bool('ENABLE_SIGNAL_EXITS', True)
        # Close positions also on HOLD (Down-Trend) signal
        self.exit_on_downtrend = _env_bool('EXIT_ON_DOWNTREND', True)
        # ATR multipliers for stop-loss and take-profit
        self.atr_stop_mult = float(os.getenv('ATR_STOP_MULT', str(1.0)))
        self.atr_tp_mult = float(os.getenv('ATR_TP_MULT', str(2.0)))
        self.partial_take_profit_enabled = _env_bool(
            'PARTIAL_TAKE_PROFIT_ENABLED', True)
        self.partial_take_profit_atr_mult = float(
            os.getenv('PARTIAL_TAKE_PROFIT_ATR_MULT', str(1.0)))
        self.partial_take_profit_fraction = float(
            os.getenv('PARTIAL_TAKE_PROFIT_FRACTION', str(0.5)))
        self.partial_take_profit_remainder_max_hold_seconds = int(
            os.getenv('PARTIAL_TAKE_PROFIT_REMAINDER_MAX_HOLD_SECONDS', str(300)))
        self.partial_take_profit_exit_on_weak_signal = _env_bool(
            'PARTIAL_TAKE_PROFIT_EXIT_ON_WEAK_SIGNAL', True)
        self.trailing_stop_enabled = _env_bool('TRAILING_STOP_ENABLED', True)
        self.trailing_stop_atr_mult = float(
            os.getenv('TRAILING_STOP_ATR_MULT', str(1.0)))
        self.break_even_enabled = _env_bool('BREAK_EVEN_ENABLED', True)
        self.break_even_trigger_pct = float(
            os.getenv('BREAK_EVEN_TRIGGER_PCT', str(1.0)))
        self.break_even_buffer_pct = float(
            os.getenv('BREAK_EVEN_BUFFER_PCT', str(0.1)))
        # Maximum hold time in seconds (0 = disabled)
        self.max_hold_seconds = int(os.getenv('MAX_HOLD_SECONDS', str(120)))
        self.coin_max_hold_seconds = _env_symbol_int_map(
            'COIN_MAX_HOLD_SECONDS', 'LINK:420,SUI:420,ONDO:420')
        # Allow smaller trades when cash is below TRADE_AMOUNT
        self.allow_partial_trades = _env_bool('ALLOW_PARTIAL_TRADES', True)
        # Fraction of cash kept in reserve (safety buffer)
        self.cash_reserve_pct = float(os.getenv('CASH_RESERVE_PCT', str(0.02)))
        # Additional data for calculations
        self.ohlcv_limit = max(self.rsi_period, self.sma_period_long) + 5
        # OHLCV timeframe (e.g. 1h = 60m)
        self.ohlcv_timeframe = os.getenv('OHLCV_TIMEFRAME', '1h')
        # ML-Modell Integration

    def _load_ml_settings(self) -> None:
        self.use_ml_model = _env_bool('USE_ML_MODEL', False)
        self.model_path = os.getenv(
            'MODEL_PATH', './model/fine_tuned_trading_model')
        # Minimum confidence for ML signals (0.0 - 1.0)
        self.ml_min_confidence = float(os.getenv('ML_MIN_CONFIDENCE', str(0.5)))
        # Optional local CatBoost or remote Hailo inference for tabular signals
        self.use_tabular_model = _env_bool('USE_TABULAR_MODEL', False)
        self.tabular_model_backend = os.getenv(
            'TABULAR_MODEL_BACKEND', 'catboost').strip().lower()
        if self.tabular_model_backend not in {'catboost', 'hailo'}:
            raise ValueError(
                "TABULAR_MODEL_BACKEND must be 'catboost' or 'hailo'")
        self.hailo_service_url = os.getenv(
            'HAILO_SERVICE_URL', 'http://127.0.0.1:8001').strip()
        self.hailo_api_token = os.getenv('HAILO_API_TOKEN', '').strip()
        self.hailo_timeout_seconds = float(
            os.getenv('HAILO_TIMEOUT_SECONDS', str(3.0)))
        self.tabular_model_path = os.getenv(
            'TABULAR_MODEL_PATH', './model/catboost_trading_model')
        self.tabular_research_signal_path = os.getenv(
            'TABULAR_RESEARCH_SIGNAL_PATH', './data/research_signal_latest.json')
        self.tabular_min_confidence = float(
            os.getenv('TABULAR_MIN_CONFIDENCE', str(0.45)))
        self.tabular_buy_min_confidence = float(
            os.getenv('TABULAR_BUY_MIN_CONFIDENCE', str(str(self.tabular_min_confidence))))
        self.tabular_allow_buy_entries = _env_bool(
            'TABULAR_ALLOW_BUY_ENTRIES', False)
        self.tabular_source_gate_enabled = _env_bool(
            'TABULAR_SOURCE_GATE_ENABLED', False)
        self.tabular_override_min_confidence = float(
            os.getenv('TABULAR_OVERRIDE_MIN_CONFIDENCE', str(0.60)))
        self.tabular_override_margin = float(
            os.getenv('TABULAR_OVERRIDE_MARGIN', str(0.15)))
        # Optional safety filter: block new entries when the tabular model signals a strong sell
        self.tabular_block_on_sell = _env_bool('TABULAR_BLOCK_ON_SELL', False)
        self.tabular_sell_block_min_proba = float(
            os.getenv('TABULAR_SELL_BLOCK_MIN_PROBA', str(0.50)))
        self.news_veto_enabled = _env_bool('NEWS_VETO_ENABLED', False)
        self.news_veto_api_url = _env_str('NEWS_VETO_API_URL', '')
        self.news_veto_api_key = _env_str('NEWS_VETO_API_KEY', '')
        self.news_veto_model = _env_str('NEWS_VETO_MODEL', '')
        self.news_veto_max_calls_per_day = int(
            os.getenv('NEWS_VETO_MAX_CALLS_PER_DAY', '10'))
        self.news_veto_state_file = _env_str(
            'NEWS_VETO_STATE_FILE', 'news_veto_state.json')
        # Optional: automatic recommendation/application of confidence threshold from journal data

    def _load_analytics_and_guardrail_settings(self) -> None:
        self.auto_tune_tabular_confidence = _env_bool(
            'AUTO_TUNE_TABULAR_CONFIDENCE', False)
        self.auto_tune_lookback_trades = int(
            os.getenv('AUTO_TUNE_LOOKBACK_TRADES', str(200)))
        self.auto_tune_threshold_min = float(
            os.getenv('AUTO_TUNE_THRESHOLD_MIN', str(0.45)))
        self.auto_tune_threshold_max = float(
            os.getenv('AUTO_TUNE_THRESHOLD_MAX', str(0.90)))
        self.auto_tune_threshold_step = float(
            os.getenv('AUTO_TUNE_THRESHOLD_STEP', str(0.02)))
        self.auto_tune_min_trades = int(os.getenv('AUTO_TUNE_MIN_TRADES', str(10)))
        # Safety brake: maximum threshold adjustment per bot start
        self.auto_tune_max_delta = float(
            os.getenv('AUTO_TUNE_MAX_DELTA', str(0.02)))
        # Cooldown between auto-tune adjustments (in minutes)
        self.auto_tune_cooldown_minutes = int(
            os.getenv('AUTO_TUNE_COOLDOWN_MINUTES', str(0)))
        self.auto_tune_state_file = os.getenv(
            'AUTO_TUNE_STATE_FILE', 'auto_tune_state.json')
        # Performance-Tracking / Trade-Journal
        self.performance_log_enabled = _env_bool(
            'PERFORMANCE_LOG_ENABLED', True)
        self.performance_log_file = os.getenv(
            'PERFORMANCE_LOG_FILE', 'trade_journal.csv')
        self.performance_report_every = int(
            os.getenv('PERFORMANCE_REPORT_EVERY', str(1)))
        self.analytics_db_enabled = _env_bool('ANALYTICS_DB_ENABLED', False)
        self.analytics_db_url = _env_str('ANALYTICS_DB_URL', '')
        self.analytics_db_host = _env_str('ANALYTICS_DB_HOST', '')
        self.analytics_db_port = int(os.getenv('ANALYTICS_DB_PORT', str(5432)))
        self.analytics_db_name = _env_str('ANALYTICS_DB_NAME', '')
        self.analytics_db_user = _env_str('ANALYTICS_DB_USER', '')
        self.analytics_db_password = _env_str('ANALYTICS_DB_PASSWORD', '')
        self.analytics_db_sslmode = _env_str('ANALYTICS_DB_SSLMODE', 'prefer')
        self.analytics_db_connect_timeout_seconds = int(
            os.getenv('ANALYTICS_DB_CONNECT_TIMEOUT_SECONDS', str(5)))
        self.analytics_db_schema = _env_str('ANALYTICS_DB_SCHEMA', 'public')
        self.analytics_db_snapshot_every = int(
            os.getenv('ANALYTICS_DB_SNAPSHOT_EVERY', str(1)))
        # Hard risk guardrails (0 = disabled)
        self.max_daily_loss_pct = float(os.getenv('MAX_DAILY_LOSS_PCT', str(0)))
        self.max_buys_per_hour = int(os.getenv('MAX_BUYS_PER_HOUR', str(0)))
        self.loss_streak_pause_threshold = int(
            os.getenv('LOSS_STREAK_PAUSE_THRESHOLD', str(0)))
        self.loss_streak_pause_seconds = int(
            os.getenv('LOSS_STREAK_PAUSE_SECONDS', str(0)))

        # Optional external AI co-pilot (budget-limited, disabled by default)

    def _load_ai_copilot_settings(self) -> None:
        self.ai_copilot_enabled = _env_bool('AI_COPILOT_ENABLED', False)
        self.ai_copilot_api_url = _env_str(
            'AI_COPILOT_API_URL', 'https://api.mammouth.ai/v1/chat/completions')
        self.ai_copilot_api_key = _env_str('MAMMOUTH_API_KEY', '')
        self.ai_copilot_model = _env_str('AI_COPILOT_MODEL', 'gpt-5.4-nano')
        self.ai_copilot_interval_minutes = int(
            os.getenv('AI_COPILOT_INTERVAL_MINUTES', str(62)))
        self.ai_copilot_state_file = os.getenv(
            'AI_COPILOT_STATE_FILE', 'ai_copilot_state.json')
        self.ai_copilot_shadow_mode = _env_bool('AI_COPILOT_SHADOW_MODE', True)
        self.ai_copilot_max_calls_per_day = int(
            os.getenv('AI_COPILOT_MAX_CALLS_PER_DAY', str(24)))
        self.ai_copilot_max_calls_per_month = int(
            os.getenv('AI_COPILOT_MAX_CALLS_PER_MONTH', str(720)))
        self.ai_copilot_max_budget_usd_per_month = float(
            os.getenv('AI_COPILOT_MAX_BUDGET_USD_PER_MONTH', str(5.0)))
        self.ai_copilot_max_output_tokens = int(
            os.getenv('AI_COPILOT_MAX_OUTPUT_TOKENS', str(300)))
        self.ai_copilot_temperature = float(
            os.getenv('AI_COPILOT_TEMPERATURE', str(0.1)))
        self.ai_copilot_max_consecutive_errors = int(
            os.getenv('AI_COPILOT_MAX_CONSECUTIVE_ERRORS', str(3)))
        self.ai_copilot_cost_input_per_mtok = float(
            os.getenv('AI_COPILOT_COST_INPUT_PER_MTOK', str(0.2)))
        self.ai_copilot_cost_output_per_mtok = float(
            os.getenv('AI_COPILOT_COST_OUTPUT_PER_MTOK', str(1.25)))
        self.ai_copilot_min_entry_score_min = int(
            os.getenv('AI_COPILOT_MIN_ENTRY_SCORE_MIN', str(55)))
        self.ai_copilot_min_entry_score_max = int(
            os.getenv('AI_COPILOT_MIN_ENTRY_SCORE_MAX', str(70)))
        self.ai_copilot_reentry_cooldown_min = int(
            os.getenv('AI_COPILOT_REENTRY_COOLDOWN_MIN', str(300)))
        self.ai_copilot_reentry_cooldown_max = int(
            os.getenv('AI_COPILOT_REENTRY_COOLDOWN_MAX', str(1200)))
        self.ai_copilot_tabular_buy_conf_min = float(
            os.getenv('AI_COPILOT_TABULAR_BUY_CONF_MIN', str(0.50)))
        self.ai_copilot_tabular_buy_conf_max = float(
            os.getenv('AI_COPILOT_TABULAR_BUY_CONF_MAX', str(0.65)))
        self.ai_copilot_benchmark_enabled = _env_bool(
            'AI_COPILOT_BENCHMARK_ENABLED', False)
        self.ai_copilot_benchmark_model = _env_str(
            'AI_COPILOT_BENCHMARK_MODEL', '')
        self.ai_copilot_benchmark_interval_minutes = int(
            os.getenv('AI_COPILOT_BENCHMARK_INTERVAL_MINUTES', str(str(self.ai_copilot_interval_minutes))))
        self.ai_copilot_benchmark_state_file = os.getenv(
            'AI_COPILOT_BENCHMARK_STATE_FILE', 'ai_copilot_benchmark_state.json')
        self.ai_copilot_benchmark_max_calls_per_day = int(
            os.getenv('AI_COPILOT_BENCHMARK_MAX_CALLS_PER_DAY', str(str(self.ai_copilot_max_calls_per_day))))
        self.ai_copilot_benchmark_max_calls_per_month = int(
            os.getenv('AI_COPILOT_BENCHMARK_MAX_CALLS_PER_MONTH', str(str(self.ai_copilot_max_calls_per_month))))
        self.ai_copilot_benchmark_max_budget_usd_per_month = float(
            os.getenv('AI_COPILOT_BENCHMARK_MAX_BUDGET_USD_PER_MONTH', str(1.0)))
        self.ai_copilot_benchmark_max_output_tokens = int(
            os.getenv('AI_COPILOT_BENCHMARK_MAX_OUTPUT_TOKENS', str(str(self.ai_copilot_max_output_tokens))))
        self.ai_copilot_benchmark_temperature = float(
            os.getenv('AI_COPILOT_BENCHMARK_TEMPERATURE', str(str(self.ai_copilot_temperature))))
        self.ai_copilot_benchmark_max_consecutive_errors = int(
            os.getenv('AI_COPILOT_BENCHMARK_MAX_CONSECUTIVE_ERRORS', str(str(self.ai_copilot_max_consecutive_errors))))
        self.ai_copilot_benchmark_cost_input_per_mtok = float(
            os.getenv('AI_COPILOT_BENCHMARK_COST_INPUT_PER_MTOK', str(str(self.ai_copilot_cost_input_per_mtok))))
        self.ai_copilot_benchmark_cost_output_per_mtok = float(
            os.getenv('AI_COPILOT_BENCHMARK_COST_OUTPUT_PER_MTOK', str(str(self.ai_copilot_cost_output_per_mtok))))


    def analytics_db_connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
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
