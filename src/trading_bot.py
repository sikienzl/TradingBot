import hashlib
import json
import logging
import math
import os
import signal
import sys
import uuid
from collections import defaultdict
from collections.abc import Container
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import ccxt
import numpy as np
import pandas as pd
from dotenv import load_dotenv  # noqa: F401  (re-exported: tests patch src.trading_bot.load_dotenv)

from src.ai_copilot_service import (
    ai_copilot_snapshot as _copilot_ai_copilot_snapshot,
)
from src.ai_copilot_service import (
    apply_ai_changes as _copilot_apply_ai_changes,
)
from src.ai_copilot_service import (
    call_ai_copilot as _copilot_call_ai_copilot,
)
from src.ai_copilot_service import (
    can_run_ai_copilot as _copilot_can_run_ai_copilot,
)
from src.ai_copilot_service import (
    clamp_ai_changes as _copilot_clamp_ai_changes,
)
from src.ai_copilot_service import (
    estimate_ai_cost_usd as _copilot_estimate_ai_cost_usd,
)
from src.ai_copilot_service import (
    extract_json_object as _copilot_extract_json_object,
)
from src.ai_copilot_service import (
    maybe_run_ai_copilot_variant as _copilot_maybe_run_ai_copilot_variant,
)
from src.analytics import PostgresAnalyticsWriter
from src.backtesting import Backtester, simple_strategy
from src.bot_bootstrap import (
    load_ml_predictor as _bootstrap_load_ml_predictor,
)
from src.bot_bootstrap import (
    load_tabular_predictor as _bootstrap_load_tabular_predictor,
)
from src.bot_bootstrap import (
    log_startup_details as _bootstrap_log_startup_details,
)
from src.bot_bootstrap import (
    validate_startup_config as _bootstrap_validate_startup_config,
)
from src.bot_config import BotConfig
from src.bot_runtime import run_bot as _run_bot
from src.coin_analysis import (
    analyze_coin as _coin_analysis_analyze_coin,
)
from src.coin_analysis import (
    apply_ml_signal as _coin_analysis_apply_ml_signal,
)
from src.coin_analysis import (
    build_tabular_features as _coin_analysis_build_tabular_features,
)
from src.coin_analysis import (
    recommendation_bias as _coin_analysis_recommendation_bias,
)
from src.coin_analysis import (
    should_apply_tabular_signal as _coin_analysis_should_apply_tabular_signal,
)
from src.dry_run_execution import (
    DryRunHooks as _DryRunHooks,
)
from src.dry_run_execution import (
    apply_dry_run_sell_to_holdings as _dre_apply_sell_to_holdings,
)
from src.dry_run_execution import (
    dry_run_buy as _dre_buy,
)
from src.dry_run_execution import (
    dry_run_sell as _dre_sell,
)
from src.dry_run_execution import (
    dry_run_sell_gross_net_pnl as _dre_sell_gross_net_pnl,
)
from src.dry_run_execution import (
    dry_run_sell_proceeds as _dre_sell_proceeds,
)
from src.dry_run_execution import (
    execute_dry_run_trade as _dre_execute,
)
from src.dry_run_execution import (
    plan_dry_run_fill as _dre_plan_fill,
)
from src.dry_run_execution import (
    record_dry_run_sell_close as _dre_record_sell_close,
)
from src.dry_run_execution import (
    resolve_dry_run_sell_entry as _dre_resolve_sell_entry,
)
from src.dry_run_execution import (
    update_open_trades_after_dry_run_sell as _dre_update_open_trades,
)
from src.entry_filters import (
    entry_volatility_guard as _entry_filters_entry_volatility_guard,
)
from src.entry_filters import (
    passes_downtrend_reversal_filter as _entry_filters_passes_downtrend_reversal_filter,
)
from src.entry_filters import (
    passes_entry_momentum_filter as _entry_filters_passes_entry_momentum_filter,
)
from src.entry_filters import (
    passes_fallback_entry_filter as _entry_filters_passes_fallback_entry_filter,
)
from src.entry_filters import (
    passes_uptrend_entry_filter as _entry_filters_passes_uptrend_entry_filter,
)
from src.entry_selection import (
    EntryCandidateContext as _EntryCandidateContext,
)
from src.entry_selection import (
    EntryCandidateHooks as _EntryCandidateHooks,
)
from src.entry_selection import allows_entry_signal as _entry_allows_signal
from src.entry_selection import classify_entry_market_mode as _entry_classify_mode
from src.entry_selection import (
    entry_guards_allow as _entry_selection_entry_guards_allow,
)
from src.entry_selection import filter_reason_code as _entry_filter_reason_code
from src.entry_selection import (
    log_blocked_buy_attempt_candidates as _entry_log_blocked_attempts,
)
from src.entry_selection import log_blocked_gate_candidates as _entry_log_blocked_gate
from src.entry_selection import (
    log_fallback_entry_diagnostics as _entry_log_fallback_diagnostics,
)
from src.entry_selection import (
    select_entry_candidate_shortlist as _entry_selection_select_shortlist,
)
from src.execution_owner import ExecutionOwnerLock, PostgresAdvisoryExecutionLease
from src.execution_service import (
    EntryHooks as _EntryHooks,
)
from src.execution_service import (
    RealTradeHooks as _RealTradeHooks,
)
from src.execution_service import (
    confirm_order_fill as _service_confirm_order_fill,
)
from src.execution_service import (
    confirm_order_fill_record as _service_confirm_order_fill_record,
)
from src.execution_service import (
    execute_real_trade as _service_execute_real_trade,
)
from src.execution_service import (
    open_new_positions as _service_open_new_positions,
)
from src.execution_service import (
    # Re-exported: tests import src.trading_bot._order_fill_details.
    order_fill_details as _order_fill_details,  # noqa: F401
)
from src.exit_policy import (
    effective_stop_loss_level as _exit_effective_stop_loss_level,
)
from src.exit_policy import (
    is_downtrend_reversal_trade as _exit_is_downtrend_reversal_trade,
)
from src.exit_policy import (
    is_rules_uptrend_trade as _exit_is_rules_uptrend_trade,
)
from src.indicator_service import calculate_atr
from src.journal_guards import (
    _compute_degraded_entry_sources,
    _compute_dynamic_lossmaker_entry_pairs,
    _compute_recent_pnl_guard_state,
)
from src.market_analysis import (
    analyze_markets as _market_analysis_analyze_markets,
)
from src.market_analysis import (
    dynamic_excluded_coins as _market_analysis_dynamic_excluded_coins,
)
from src.market_data import fetch_market_data, fetch_ohlcv
from src.news_veto import evaluate_news_veto
from src.open_trade_manager import (
    OpenTradeContext as _OpenTradeContext,
)
from src.open_trade_manager import (
    OpenTradeHooks as _OpenTradeHooks,
)
from src.open_trade_manager import (
    manage_open_trades as _open_trade_manage_open_trades,
)
from src.portfolio import Portfolio
from src.portfolio_bootstrap import (
    PortfolioBootstrapContext,
    PortfolioBootstrapHooks,
)
from src.portfolio_bootstrap import (
    update_portfolio_balance as _portfolio_bootstrap_update_balance,
)
from src.simulation import SimulationEngine
from src.state_store import (
    cooldown_remaining_minutes as _state_cooldown_remaining_minutes,
)
from src.state_store import current_day_key as _state_current_day_key
from src.state_store import current_month_key as _state_current_month_key
from src.state_store import normalize_ai_state as _state_normalize_ai_state
from src.state_store import read_ai_copilot_state as _state_read_ai_copilot_state
from src.state_store import read_auto_tune_state as _state_read_auto_tune_state
from src.state_store import write_ai_copilot_state as _state_write_ai_copilot_state
from src.state_store import write_auto_tune_state as _state_write_auto_tune_state
from src.strategy_rules import rule_recommendation as get_rule_recommendation
from src.trade_journal import (
    TRADE_JOURNAL_FIELDS as _TRADE_JOURNAL_FIELDS,
)
from src.trade_journal import (
    JournalContext,
    PaperTradeHooks,
)
from src.trade_journal import (
    append_trade_journal as _trade_journal_append_trade_journal,
)
from src.trade_journal import (
    commit_paper_trade as _trade_journal_commit_paper_trade,
)
from src.trade_journal import (
    init_journal as _trade_journal_init_journal,
)
from src.trade_journal import (
    journal_row as _trade_journal_journal_row,
)
from src.trade_journal import (
    recommend_tabular_threshold as _trade_journal_recommend_tabular_threshold,
)
from src.trade_journal import (
    recover_paper_trade as _trade_journal_recover_paper_trade,
)
from src.trade_journal import (
    sync_directory as _trade_journal_sync_directory,
)

logger = logging.getLogger(__name__)


def _handle_sigterm(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def _install_sigterm_handler() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)



def _config_fingerprint(config: Any) -> str:
    sensitive_name_parts = (
        "key", "secret", "password", "token", "url", "user",
    )
    safe_config = {
        name: value
        for name, value in vars(config).items()
        if not any(part in name.lower() for part in sensitive_name_parts)
    }
    serialized = json.dumps(
        safe_config,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: sorted(value) if isinstance(value, set) else str(value),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def configure_logging(log_file: str = 'trading_bot.log', level: int = logging.INFO) -> None:
    """Configures runtime logging lazily so importing the module has no file-system side effects."""
    root_logger = logging.getLogger()
    if getattr(root_logger, '_trading_bot_logging_configured', False):
        return

    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s')
    handlers: list[logging.Handler] = []

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
    # Flag is informational only; read back via getattr(..., False) above.
    root_logger._trading_bot_logging_configured = True  # type: ignore[attr-defined]


def _mask_secret(value: str) -> str:
    """Masks sensitive strings for logging output."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"




class CryptoTradingBot:
    """Main class of the trading bot."""

    def __init__(self, config: BotConfig):
        self.config = config
        _bootstrap_validate_startup_config(config)
        remote_lease = (
            PostgresAdvisoryExecutionLease(
                config.cluster_execution_lease_database_url,
                config.execution_account_id,
            )
            if getattr(config, 'cluster_execution_lease_enabled', False)
            else None
        )
        self._execution_owner_lock = ExecutionOwnerLock(
            getattr(config, 'execution_lock_file', '.trading_execution.lock'),
            remote_lease,
        )
        self.run_id = os.getenv('TRADING_RUN_ID', '').strip() or uuid.uuid4().hex
        self.config_hash = _config_fingerprint(config)
        self._journal_fields = list(_TRADE_JOURNAL_FIELDS)
        self._paper_trade_failed = False
        self.portfolio = Portfolio(config.base_currency)
        self.portfolio.strict_persistence = (
            config.simulation_initial_balance_from_kraken or not config.dry_run)
        if not config.dry_run:
            self.portfolio.state_file = config.live_portfolio_state_file
        self.analytics_writer = PostgresAnalyticsWriter(config)
        self.iteration = 0
        self.performance: dict[str, Any] = {
            'buys': 0,
            'sells': 0,
            'wins': 0,
            'losses': 0,
            'break_even': 0,
            'realized_pnl_base': 0.0,
            'closed_trades': 0,
            'by_source': defaultdict(lambda: {'buys': 0, 'sells': 0, 'pnl': 0.0}),
        }
        self.daily_anchor_date: date | None = None
        self.daily_anchor_value: float = 0.0
        self.buy_timestamps_utc: list[datetime] = []
        self.last_sell_timestamps_utc: dict[str, datetime] = {}
        self.consecutive_losses: int = 0
        self.buy_pause_until_utc: datetime | None = None
        _bootstrap_log_startup_details(logger, config, self.run_id, self.config_hash)
        self.exchange = self._initialize_exchange()
        self._simulation_engine = SimulationEngine(
            config, iteration_provider=lambda: self.iteration)
        # List of tradeable pairs (e.g. BTC/EUR)
        self.all_symbols: list[str] = []
        self.all_coins: list[str] = []   # List of base assets (e.g. BTC)
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
        self.ml_predictor = _bootstrap_load_ml_predictor(logger, self.config)

        self.tabular_predictor: Any = _bootstrap_load_tabular_predictor(
            logger, self.config)

        if self._public_paper_mode():
            if not self.config.performance_log_enabled:
                raise RuntimeError("Public paper trading requires an enabled trade journal")
            self._recover_paper_trade()
        if self.config.performance_log_enabled:
            self._init_trade_journal()

        if (
            self.config.use_tabular_model
            and self.config.tabular_model_backend == 'catboost'
        ):
            self._maybe_auto_tune_tabular_confidence()

    def _journal_context(self) -> JournalContext:
        """Journal bookkeeping handed to :mod:`src.trade_journal`."""
        return JournalContext(
            config=self.config,
            analytics_writer=self.analytics_writer,
            run_id=self.run_id,
            config_hash=self.config_hash,
            journal_fields=self._journal_fields,
            journal_row=self._journal_row,
        )

    def _paper_trade_hooks(self) -> PaperTradeHooks:
        """Transaction helpers for :func:`src.trade_journal.commit_paper_trade`."""
        return PaperTradeHooks(
            sync_directory=self._sync_directory,
            recover_paper_trade=self._recover_paper_trade,
            set_paper_trade_failed=lambda failed: setattr(
                self, '_paper_trade_failed', failed),
        )

    def _init_trade_journal(self):
        """Delegates to :func:`src.trade_journal.init_journal`."""
        self._journal_fields = _trade_journal_init_journal(self.config)

    def _public_paper_mode(self) -> bool:
        return (self.config.dry_run
                and getattr(self.config, 'simulation_initial_balance_from_kraken', False)
                and not getattr(self.config, 'simulate_data', True))

    @staticmethod
    def _sync_directory(path: str) -> None:
        """Delegates to :func:`src.trade_journal.sync_directory`."""
        _trade_journal_sync_directory(path)

    def _recover_paper_trade(self) -> None:
        """Delegates to :func:`src.trade_journal.recover_paper_trade`."""
        _trade_journal_recover_paper_trade(
            self._journal_context(), self.portfolio,
            sync_directory=self._sync_directory,
            logger=logger)

    def _commit_paper_trade(self, row: dict) -> None:
        """Delegates to :func:`src.trade_journal.commit_paper_trade`."""
        _trade_journal_commit_paper_trade(
            self._journal_context(), self.portfolio, row,
            hooks=self._paper_trade_hooks(),
            logger=logger)

    def _journal_row(self, row: dict) -> dict:
        """Delegates to :func:`src.trade_journal.journal_row`."""
        return _trade_journal_journal_row(
            self.config, self.run_id, self.config_hash, self.iteration, row)

    def _append_trade_journal(self, row: dict):
        """Delegates to :func:`src.trade_journal.append_trade_journal`."""
        _trade_journal_append_trade_journal(self._journal_context(), row)

    def _write_analytics_snapshot(
        self,
        portfolio_value: float,
        market_analysis: dict[str, dict[str, Any]],
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

    def _recommend_tabular_threshold_from_journal(self) -> float | None:
        """Delegates to :func:`src.trade_journal.recommend_tabular_threshold`."""
        return _trade_journal_recommend_tabular_threshold(self.config, logger=logger)

    def _dynamic_excluded_coins(self) -> set[str]:
        """Delegates to :func:`src.market_analysis.dynamic_excluded_coins`."""
        return _market_analysis_dynamic_excluded_coins(self.config, logger=logger)

    def _dynamic_excluded_entry_pairs(self) -> set[tuple[str, str]]:
        """Builds a temporary exclusion set for recent coin/source entry pairs."""
        if not self.config.dynamic_lossmaker_exclusion_enabled:
            return set()

        path = self.config.performance_log_file
        if not self.config.performance_log_enabled or not os.path.exists(path):
            return set()

        try:
            df = pd.read_csv(path)
        except (
            OSError,
            UnicodeError,
            pd.errors.ParserError,
            pd.errors.EmptyDataError,
        ) as e:
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

    def _degraded_entry_sources(self) -> set[str]:
        """Builds a temporary blocklist for weak signal sources in fallback paths."""
        if not self.config.dynamic_lossmaker_exclusion_enabled:
            return set()

        path = self.config.performance_log_file
        if not self.config.performance_log_enabled or not os.path.exists(path):
            return set()

        try:
            df = pd.read_csv(path)
        except (
            OSError,
            UnicodeError,
            pd.errors.ParserError,
            pd.errors.EmptyDataError,
        ) as e:
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

    def _recent_pnl_guard_state(self) -> dict[str, Any]:
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
        except (
            OSError,
            UnicodeError,
            pd.errors.ParserError,
            pd.errors.EmptyDataError,
        ) as e:
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
            stale_after_hours=self.config.recent_pnl_guard_stale_hours,
        )

    def _log_recent_pnl_guard_status(self, state: dict[str, Any]) -> None:
        active = bool(state.get('active', False))
        journal_age = state.get('journal_age_hours')
        freshness = (
            'unknown' if journal_age is None
            else 'stale' if state.get('journal_stale', False)
            else 'fresh'
        )
        log = logger.warning if active else logger.info
        log(
            "Recent PnL guard %s: trades=%d, pnl=%.6f %s, pf=%.4f, "
            "reason=%s, journal_age_hours=%s, journal_stale=%s, journal_freshness=%s, "
            "stale_after_hours=%.1f, latest_closed_trade=%s",
            'active' if active else 'inactive',
            int(state.get('recent_trades', 0)),
            float(state.get('recent_realized_pnl', 0.0)),
            self.config.base_currency,
            float(state.get('recent_profit_factor', 999.0)),
            str(state.get('reason', 'n/a')),
            f"{float(journal_age):.1f}" if journal_age is not None else "unknown",
            bool(state.get('journal_stale', False)),
            freshness,
            self.config.recent_pnl_guard_stale_hours,
            state.get('latest_closed_trade_at') or "unknown",
        )

    def _read_auto_tune_state(self) -> dict:
        return _state_read_auto_tune_state(self.config.auto_tune_state_file)

    def _write_auto_tune_state(self, state: dict):
        _state_write_auto_tune_state(state, self.config.auto_tune_state_file)

    def _cooldown_remaining_minutes(self, state: dict) -> float:
        return _state_cooldown_remaining_minutes(
            state, self.config.auto_tune_cooldown_minutes)

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
                'last_applied_at': datetime.now(UTC).isoformat(),
                'last_recommended': recommended,
                'last_applied': adjusted,
            })
        else:
            logger.info(
                f"ℹ️ AUTO_TUNE disabled. Recommended TABULAR_MIN_CONFIDENCE: {recommended:.2f}")

    def _read_ai_copilot_state(self, path: str | None = None) -> dict:
        return _state_read_ai_copilot_state(
            path or self.config.ai_copilot_state_file)

    def _write_ai_copilot_state(self, state: dict, path: str | None = None):
        _state_write_ai_copilot_state(
            state, path or self.config.ai_copilot_state_file)

    def _current_month_key(self) -> str:
        return _state_current_month_key()

    def _current_day_key(self) -> str:
        return _state_current_day_key()

    def _normalize_ai_state(self, state: dict, monthly_call_cap: int, budget_cap_usd: float) -> dict:
        return _state_normalize_ai_state(state, monthly_call_cap, budget_cap_usd)

    def _estimate_ai_cost_usd(self, prompt_tokens: int, completion_tokens: int, input_cost_per_mtok: float, output_cost_per_mtok: float) -> float:
        return _copilot_estimate_ai_cost_usd(prompt_tokens, completion_tokens, input_cost_per_mtok, output_cost_per_mtok)

    def _can_run_ai_copilot(
        self,
        state: dict[str, Any],
        *,
        enabled: bool,
        model: str,
        api_key: str,
        max_consecutive_errors: int,
        max_calls_per_day: int,
        max_calls_per_month: int,
        max_budget_usd_per_month: float,
        interval_minutes: int,
    ) -> tuple[bool, str]:
        return _copilot_can_run_ai_copilot(
            state,
            enabled=enabled,
            model=model,
            api_key=api_key,
            max_consecutive_errors=max_consecutive_errors,
            max_calls_per_day=max_calls_per_day,
            max_calls_per_month=max_calls_per_month,
            max_budget_usd_per_month=max_budget_usd_per_month,
            interval_minutes=interval_minutes,
            logger=logger,
        )

    def _ai_copilot_snapshot(self) -> dict[str, Any]:
        return _copilot_ai_copilot_snapshot(
            self.config,
            logger,
            performance=self.performance,
            consecutive_losses=self.consecutive_losses,
            iteration=self.iteration,
            portfolio=self.portfolio,
        )

    def _extract_json_object(self, text: str) -> dict[str, Any] | None:
        return _copilot_extract_json_object(text)

    def _call_ai_copilot(self, snapshot: dict[str, Any], *, model: str, temperature: float, max_output_tokens: int) -> tuple[dict[str, Any] | None, int, int]:
        return _copilot_call_ai_copilot(
            self.config,
            snapshot,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )

    def _clamp_ai_changes(self, raw_changes: dict[str, Any]) -> dict[str, Any]:
        return _copilot_clamp_ai_changes(raw_changes, self.config)

    def _apply_ai_changes(self, changes: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
        return _copilot_apply_ai_changes(changes, self.config)

    def _maybe_run_ai_copilot_variant(
        self,
        snapshot: dict[str, Any],
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
    ) -> None:
        _copilot_maybe_run_ai_copilot_variant(
            self.config,
            logger,
            snapshot,
            label=label,
            # Empty path -> configured file, as the original state helpers did.
            state_file=state_file or self.config.ai_copilot_state_file,
            enabled=enabled,
            model=model,
            shadow_mode=shadow_mode,
            max_calls_per_day=max_calls_per_day,
            max_calls_per_month=max_calls_per_month,
            max_budget_usd_per_month=max_budget_usd_per_month,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            max_consecutive_errors=max_consecutive_errors,
            cost_input_per_mtok=cost_input_per_mtok,
            cost_output_per_mtok=cost_output_per_mtok,
            interval_minutes=interval_minutes,
            allow_apply=allow_apply,
        )

    def _maybe_run_ai_copilot(self, snapshot: dict[str, Any]):
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

    def _maybe_run_ai_copilot_benchmark(self, snapshot: dict[str, Any]):
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

    @staticmethod
    def _trade_excursion_metrics(entry_trade: dict) -> tuple[float, float]:
        buy_price = float(entry_trade.get('buy_price', 0.0))
        if buy_price <= 0:
            return 0.0, 0.0
        peak_price = max(
            buy_price, float(entry_trade.get('peak_price', buy_price)))
        trough_price = min(
            buy_price, float(entry_trade.get('trough_price', buy_price)))
        mfe_pct = (peak_price - buy_price) / buy_price * 100.0
        mae_pct = (trough_price - buy_price) / buy_price * 100.0
        return mfe_pct, mae_pct

    @staticmethod
    def _entry_cost_basis(entry_trade: dict, buy_price: float, buy_amount_coin: float) -> float:
        raw_basis = entry_trade.get('amount_base')
        if raw_basis is None and entry_trade.get('execution_cost_mode') != 'net_costs':
            logger.warning("Legacy DRY-RUN position has no cost basis; reconstructing from entry price")
            raw_basis = buy_price * buy_amount_coin
        try:
            basis = float(cast(Any, raw_basis))
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid entry cost basis") from exc
        if not math.isfinite(basis) or basis <= 0:
            raise ValueError("Invalid entry cost basis")
        return basis

    def _record_close_performance(self, entry_trade: dict, sell_price: float, sell_amount: float,
                                  *, net_proceeds: float | None = None,
                                  entry_basis: float | None = None):
        buy_price = float(entry_trade.get('buy_price', 0.0))
        buy_amount_coin = float(entry_trade.get('amount_coin', 0.0))
        if buy_amount_coin <= 0:
            return 0.0, 0.0, 0.0
        pnl_base = (sell_price - buy_price) * sell_amount
        pnl_pct = ((sell_price - buy_price) / buy_price) * \
            100 if buy_price > 0 else 0.0
        if net_proceeds is not None:
            total_basis = entry_basis if entry_basis is not None else self._entry_cost_basis(
                entry_trade, buy_price, buy_amount_coin)
            basis = total_basis * min(sell_amount / buy_amount_coin, 1.0)
            pnl_base = net_proceeds - basis
            pnl_pct = pnl_base / basis * 100 if basis > 0 else 0.0
        hold_seconds = (
            datetime.now() - entry_trade.get('timestamp', datetime.now())).total_seconds()

        self.performance['sells'] += 1
        self.performance['closed_trades'] += 1
        self.performance['realized_pnl_base'] += pnl_base
        if pnl_base > 0:
            self.performance['wins'] += 1
        elif pnl_base < 0:
            self.performance['losses'] += 1
        else:
            self.performance['break_even'] += 1

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
                    UTC) + timedelta(seconds=self.config.loss_streak_pause_seconds)
                logger.warning(
                    f"🧯 Loss streak detected ({self.consecutive_losses} in a row). "
                    f"New BUYs paused until {self.buy_pause_until_utc.isoformat()}"
                )
        else:
            self.consecutive_losses = 0

        return pnl_base, pnl_pct, hold_seconds

    def _register_buy_timestamp(self):
        now_utc = datetime.now(UTC)
        self.buy_timestamps_utc.append(now_utc)
        cutoff = now_utc - timedelta(hours=1)
        self.buy_timestamps_utc = [
            ts for ts in self.buy_timestamps_utc if ts >= cutoff]

    def _register_sell_timestamp(self, coin: str):
        self.last_sell_timestamps_utc[coin] = datetime.now(UTC)

    def _resolve_reentry_cooldown_seconds(self, current_price: float, atr: float) -> tuple[int, str]:
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

        effective = round(base * mult)
        effective = min(max(effective, minimum), maximum)
        reason = f'{regime},atr_pct={atr_pct:.4f},mult={mult:.2f}'
        return effective, reason

    def _is_coin_in_reentry_cooldown(self, coin: str, cooldown_seconds: int | None = None) -> tuple[bool, int]:
        cooldown = max(
            0,
            int(self.config.reentry_cooldown_seconds if cooldown_seconds is None else cooldown_seconds),
        )
        if cooldown <= 0:
            return False, 0

        last_sell = self.last_sell_timestamps_utc.get(coin)
        if last_sell is None:
            return False, 0

        elapsed = (datetime.now(UTC) - last_sell).total_seconds()
        remaining = int(max(0, cooldown - elapsed))
        if remaining > 0:
            return True, remaining
        return False, 0

    def _refresh_daily_anchor(self, portfolio_value: float):
        today_utc = datetime.now(UTC).date()
        if portfolio_value <= 0:
            return
        if self.daily_anchor_date != today_utc:
            self.daily_anchor_date = today_utc
            self.daily_anchor_value = portfolio_value
            self.consecutive_losses = 0
            self.buy_pause_until_utc = None
            logger.info(
                f"🗓️ Daily-risk-anchor set: {portfolio_value:.2f} {self.config.base_currency} ({today_utc})")

    def _can_open_new_positions(self, portfolio_value: float) -> tuple[bool, str]:
        now_utc = datetime.now(UTC)

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
            f"Closed={closed} | Wins={self.performance['wins']} | "
            f"Losses={self.performance['losses']} | "
            f"BreakEven={self.performance['break_even']} | WinRate={win_rate:.1f}% | "
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
            if self.config.simulation_initial_balance_from_kraken:
                raise RuntimeError("Cannot initialize public Kraken exchange") from e
            logger.error(
                f"Exchange initialisation failed: {e}. Enabling simulation mode.")
            self.config.simulate_data = True
            return None
        except (ccxt.BaseError, AttributeError, TypeError, ValueError) as e:
            if self.config.simulation_initial_balance_from_kraken:
                raise RuntimeError("Cannot initialize public Kraken exchange") from e
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
            self.all_coins = sorted(set(self.all_coins))
            logger.info(
                f"Loaded {len(self.all_symbols)} trading pairs and {len(self.all_coins)} coins with {self.config.base_currency}.")
        except ccxt.BaseError as e:
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
        """Delegates to :func:`src.portfolio_bootstrap.update_portfolio_balance`."""
        _portfolio_bootstrap_update_balance(
            self._portfolio_bootstrap_context(),
            self._portfolio_bootstrap_hooks(),
        )

    def _portfolio_bootstrap_context(self) -> PortfolioBootstrapContext:
        """Portfolio startup state handed to :mod:`src.portfolio_bootstrap`."""
        return PortfolioBootstrapContext(
            config=self.config,
            portfolio=self.portfolio,
            exchange=self.exchange,
        )

    def _portfolio_bootstrap_hooks(self) -> PortfolioBootstrapHooks:
        """Callbacks for :mod:`src.portfolio_bootstrap`."""
        return PortfolioBootstrapHooks(
            public_paper_mode=self._public_paper_mode,
            recover_paper_trade=self._recover_paper_trade,
            set_iteration=lambda value: setattr(self, 'iteration', value),
        )


    def _get_market_data(self) -> dict[str, dict]:
        """Fetches current ticker data (price, volume) for all relevant coins."""
        if self.config.simulate_data:
            market_data: dict[str, dict] = {}
            for coin in self.all_coins:
                symbol = f'{coin}/{self.config.base_currency}'
                sim_df = self._build_simulated_ohlcv_data(
                    symbol, timeframe='1h', limit=100)
                market_data[coin] = {
                    'price': float(sim_df['close'].iloc[-1]),
                    'volume': float(sim_df['volume'].tail(24).mean()),
                    'bid': float(sim_df['close'].iloc[-1]),
                    'ask': float(sim_df['close'].iloc[-1]),
                    'spread_bps': 0.0,
                }
            return market_data

        return fetch_market_data(
            self.exchange,
            list(self.all_symbols),
            batch_size=self.config.ticker_batch_size,
            retries=self.config.ticker_fetch_retries,
            delay_seconds=self.config.ticker_retry_delay_seconds,
        )

    def _build_simulated_ohlcv_data(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> pd.DataFrame:
        """Builds deterministic simulated OHLCV data via the simulation engine."""
        return self._simulation_engine.build_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def _fetch_ohlcv_data(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> pd.DataFrame | None:
        """Fetches OHLCV data for a symbol."""
        if self.config.simulate_data or self.exchange is None:
            return self._build_simulated_ohlcv_data(symbol, timeframe=timeframe, limit=limit)

        return fetch_ohlcv(
            self.exchange,
            symbol,
            timeframe,
            limit,
            retries=self.config.ohlcv_fetch_retries,
            delay_seconds=self.config.ohlcv_retry_delay_seconds,
            slow_warning_seconds=self.config.slow_analysis_warning_seconds,
        )

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
        portfolio_value: float | None = None,
        trade_amount_cap: float | None = None,
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
        return calculate_atr(ohlcv_df, period=period)

    @staticmethod
    def _recommendation_bias(recommendation: str) -> int:
        """Delegates to :func:`src.coin_analysis.recommendation_bias`."""
        return _coin_analysis_recommendation_bias(recommendation)

    def _should_apply_tabular_signal(
        self,
        rule_recommendation: str,
        rule_score: int,
        tab_decision: str,
        tab_confidence: float,
    ) -> tuple[bool, str]:
        """Delegates to :func:`src.coin_analysis.should_apply_tabular_signal`."""
        return _coin_analysis_should_apply_tabular_signal(
            self.config,
            rule_recommendation,
            rule_score,
            tab_decision,
            tab_confidence,
        )


    def _effective_stop_loss_level(self, trade_info: dict, current_price: float, atr: float) -> float:
        """Delegates to :func:`src.exit_policy.effective_stop_loss_level`."""
        return _exit_effective_stop_loss_level(
            self.config, trade_info, current_price, atr)

    def _passes_entry_momentum_filter(self, coin_data: dict) -> tuple[bool, str]:
        """Delegates to :func:`src.entry_filters.passes_entry_momentum_filter`."""
        return _entry_filters_passes_entry_momentum_filter(self.config, coin_data)

    def _passes_uptrend_entry_filter(self, coin_data: dict) -> tuple[bool, str]:
        """Delegates to :func:`src.entry_filters.passes_uptrend_entry_filter`."""
        return _entry_filters_passes_uptrend_entry_filter(self.config, coin_data)

    def _passes_fallback_entry_filter(self, coin: str, coin_data: dict) -> tuple[bool, str]:
        """Delegates to :func:`src.entry_filters.passes_fallback_entry_filter`."""
        return _entry_filters_passes_fallback_entry_filter(self.config, coin, coin_data)

    def _entry_volatility_guard(self, coin_data: dict) -> tuple[bool, str]:
        """Delegates to :func:`src.entry_filters.entry_volatility_guard`."""
        return _entry_filters_entry_volatility_guard(self.config, coin_data)

    def _entry_market_mode(self, market_analysis: dict[str, dict]) -> str:
        """Classifies the market to suppress aggressive entry filling in weak environments."""
        return _entry_classify_mode(
            market_analysis,
            simulate_data=self.config.simulate_data,
            simulation_regime=self.config.simulation_regime,
        )

    @staticmethod
    def _filter_reason_code(reason: str) -> str:
        """Normalizes verbose filter reasons into stable summary keys."""
        return _entry_filter_reason_code(reason)

    def _log_blocked_uptrend_candidates(
        self,
        market_analysis: dict[str, dict],
        uptrend_filter_results: dict[str, tuple[bool, str]],
        limit: int = 5,
    ) -> None:
        """Logs concrete rejection details for blocked rules-based up-trend entries."""
        _entry_log_blocked_gate(
            market_analysis,
            uptrend_filter_results,
            recommendation='HOLD (Up-Trend)',
            gate_label='Uptrend entry gate',
            limit=limit,
            logger=logger,
        )

    def _log_blocked_downtrend_reversal_candidates(
        self,
        market_analysis: dict[str, dict],
        downtrend_filter_results: dict[str, tuple[bool, str]],
        limit: int = 5,
    ) -> None:
        """Logs concrete rejection details for blocked down-trend reversal entries."""
        _entry_log_blocked_gate(
            market_analysis,
            downtrend_filter_results,
            recommendation='HOLD (Down-Trend)',
            gate_label='Downtrend reversal gate',
            limit=limit,
            logger=logger,
        )

    def _log_fallback_entry_diagnostics(
        self,
        market_analysis: dict[str, dict],
        fallback_base_candidates: list[str],
        fallback_filter_results: dict[str, tuple[bool, str]],
        fallback_allowed: bool,
        fallback_suppression_reason: str,
        entry_market_mode: str,
        limit: int = 5,
    ) -> None:
        """Logs why fallback entries were blocked or fully suppressed."""
        _entry_log_fallback_diagnostics(
            market_analysis,
            fallback_base_candidates,
            fallback_filter_results,
            fallback_allowed,
            fallback_suppression_reason,
            entry_market_mode,
            limit=limit,
            logger=logger,
        )

    def _log_blocked_buy_attempt_candidates(
        self,
        blocked_attempts: list[dict[str, Any]],
        limit: int = 5,
    ) -> None:
        """Logs why shortlisted buy candidates still failed during the execution path."""
        _entry_log_blocked_attempts(blocked_attempts, limit=limit, logger=logger)
    @staticmethod
    def _is_rules_uptrend_trade(trade_info: dict) -> bool:
        """Delegates to :func:`src.exit_policy.is_rules_uptrend_trade`."""
        return _exit_is_rules_uptrend_trade(trade_info)

    def _passes_downtrend_reversal_filter(self, coin_data: dict) -> tuple[bool, str]:
        """Delegates to :func:`src.entry_filters.passes_downtrend_reversal_filter`."""
        return _entry_filters_passes_downtrend_reversal_filter(self.config, coin_data)

    @staticmethod
    def _allows_entry_signal(
        recommendation: str,
        downtrend_reversal_allowed: bool,
        excluded_signals: set[str],
    ) -> bool:
        """Keeps explicit downtrend-reversal approvals eligible even when generic exits block downtrend entries."""
        return _entry_allows_signal(
            recommendation,
            downtrend_reversal_allowed=downtrend_reversal_allowed,
            excluded_signals=excluded_signals,
        )
    @staticmethod
    def _is_downtrend_reversal_trade(trade_info: dict) -> bool:
        """Delegates to :func:`src.exit_policy.is_downtrend_reversal_trade`."""
        return _exit_is_downtrend_reversal_trade(trade_info)

    def _build_tabular_features(
        self,
        ohlcv_df,
        close,
        current_price: float,
        rsi: float,
        sma_short: float,
        sma_long: float,
        atr_14: float,
    ) -> tuple[pd.DataFrame, float]:
        """Delegates to :func:`src.coin_analysis.build_tabular_features`."""
        return _coin_analysis_build_tabular_features(
            ohlcv_df,
            close,
            current_price,
            rsi,
            sma_short,
            sma_long,
            atr_14,
        )


    def _apply_ml_signal(
        self,
        coin: str,
        ohlcv_df,
        current_price: float,
        rsi: float,
        recommendation: str,
        score: int,
        signal_source: str,
        signal_confidence,
    ) -> tuple[str, int, str, Any]:
        """Delegates to :func:`src.coin_analysis.apply_ml_signal`."""
        return _coin_analysis_apply_ml_signal(
            self.config,
            self.ml_predictor,
            coin,
            ohlcv_df,
            current_price,
            rsi,
            recommendation,
            score,
            signal_source,
            signal_confidence,
        )


    def _analyze_coin(self, coin: str, current_price: float) -> dict | None:
        """Delegates to :func:`src.coin_analysis.analyze_coin`."""
        return _coin_analysis_analyze_coin(
            self.config,
            self._fetch_ohlcv_data,
            self.ml_predictor,
            self.tabular_predictor,
            coin,
            current_price,
            rule_recommender=get_rule_recommendation,
        )


    def _analyze_markets(self, market_data: dict[str, dict], extra_coins: list[str] | None = None) -> dict[str, dict]:
        """Delegates to :func:`src.market_analysis.analyze_markets`."""
        return _market_analysis_analyze_markets(
            self.config,
            self._dynamic_excluded_coins,
            self._analyze_coin,
            market_data,
            extra_coins,
            logger=logger,
        )

    def _confirm_order_fill(
        self,
        order: dict[str, Any],
        symbol: str,
        requested_amount: float,
        fallback_price: float,
    ) -> tuple[float, float, float]:
        """Delegates to :func:`src.execution_service.confirm_order_fill`."""
        return _service_confirm_order_fill(
            self.exchange,
            order,
            symbol,
            requested_amount,
            fallback_price,
            logger=logger,
        )

    def _confirm_order_fill_record(
        self,
        order: dict[str, Any],
        symbol: str,
        requested_amount: float,
        fallback_price: float,
    ) -> tuple[float, float, float, dict[str, Any]]:
        return _service_confirm_order_fill_record(
            self.exchange, order, symbol, requested_amount, fallback_price,
            logger=logger,
        )

    def _execute_trade(
        self,
        coin: str,
        action: str,
        price: float,
        amount_in_base_currency: float,
        atr: float | None = None,
        signal_source: str = 'rules',
        signal_confidence: float | None = None,
        recommendation: str = 'HOLD',
        reason: str = '',
        partial_take_profit: bool = False,
    ):
        """
        Executes a (simulated) trade.
        Uses market orders for simplicity; limit orders would be preferable in a real bot.
        """
        if self._public_paper_mode() and getattr(self, '_paper_trade_failed', False):
            raise RuntimeError("Paper trade transaction failed; restart to recover")
        if self.config.dry_run:
            return self._execute_dry_run_trade(
                coin, action, price, amount_in_base_currency,
                signal_source=signal_source,
                signal_confidence=signal_confidence,
                recommendation=recommendation,
                reason=reason,
                partial_take_profit=partial_take_profit,
            )
        return self._execute_real_trade(
            coin, action, price, amount_in_base_currency,
            signal_source=signal_source,
            signal_confidence=signal_confidence,
            recommendation=recommendation,
            reason=reason,
        )

    def _dry_run_hooks(self) -> _DryRunHooks:
        """Bot-owned collaborators injected into :mod:`src.dry_run_execution`.

        Built per call so the service never reaches into the bot and
        monkeypatching a bot method in a test still takes effect.
        """
        return _DryRunHooks(
            record_close_performance=self._record_close_performance,
            trade_excursion_metrics=self._trade_excursion_metrics,
            entry_cost_basis=self._entry_cost_basis,
            append_trade_journal=self._append_trade_journal,
            commit_paper_trade=self._commit_paper_trade,
            public_paper_mode=self._public_paper_mode,
            register_buy_timestamp=self._register_buy_timestamp,
            register_sell_timestamp=self._register_sell_timestamp,
            now=datetime.now,
        )

    def _execute_dry_run_trade(
        self,
        coin: str,
        action: str,
        price: float,
        amount_in_base_currency: float,
        *,
        signal_source: str = 'rules',
        signal_confidence: float | None = None,
        recommendation: str = 'HOLD',
        reason: str = '',
        partial_take_profit: bool = False,
    ) -> bool:
        """Delegates to :func:`src.dry_run_execution.execute_dry_run_trade`."""
        return _dre_execute(
            self.config,
            self.portfolio,
            self.performance,
            self._dry_run_hooks(),
            coin,
            action,
            price,
            amount_in_base_currency,
            signal_source=signal_source,
            signal_confidence=signal_confidence,
            recommendation=recommendation,
            reason=reason,
            partial_take_profit=partial_take_profit,
            logger=logger,
        )

    def _plan_dry_run_fill(
        self,
        coin: str,
        action: str,
        price: float,
        amount_in_base_currency: float,
        costs_enabled: bool,
        net_tracked: bool,
    ) -> tuple[float, float] | None:
        """Delegates to :func:`src.dry_run_execution.plan_dry_run_fill`."""
        return _dre_plan_fill(
            self.config,
            coin,
            action,
            price,
            amount_in_base_currency,
            costs_enabled,
            net_tracked,
            logger=logger,
        )

    def _dry_run_buy(
        self,
        coin: str,
        price: float,
        amount_in_base_currency: float,
        amount_coin: float,
        fill_price: float,
        costs_enabled: bool,
        signal_source: str,
        signal_confidence: float | None,
        recommendation: str,
        reason: str,
    ) -> bool:
        """Delegates to :func:`src.dry_run_execution.dry_run_buy`."""
        return _dre_buy(
            self.config,
            self.portfolio,
            self.performance,
            self._dry_run_hooks(),
            coin,
            price,
            amount_in_base_currency,
            amount_coin,
            fill_price,
            costs_enabled,
            signal_source,
            signal_confidence,
            recommendation,
            reason,
            logger=logger,
        )

    def _dry_run_sell(
        self,
        coin: str,
        price: float,
        amount_coin: float,
        fill_price: float,
        costs_enabled: bool,
        net_tracked: bool,
        tracked_entry: dict,
        reason: str,
        partial_take_profit: bool,
    ) -> bool:
        """Delegates to :func:`src.dry_run_execution.dry_run_sell`."""
        return _dre_sell(
            self.config,
            self.portfolio,
            self._dry_run_hooks(),
            coin,
            price,
            amount_coin,
            fill_price,
            costs_enabled,
            net_tracked,
            tracked_entry,
            reason,
            partial_take_profit,
            logger=logger,
        )

    def _resolve_dry_run_sell_entry(
        self, coin: str, entry_trade: dict, held: float,
        sell_amount: float, costs_enabled: bool, net_tracked: bool,
    ) -> tuple[str, float | None, float] | None:
        """Delegates to :func:`src.dry_run_execution.resolve_dry_run_sell_entry`."""
        return _dre_resolve_sell_entry(
            coin, entry_trade, held, sell_amount, costs_enabled, net_tracked,
            self._entry_cost_basis, logger=logger)

    def _dry_run_sell_proceeds(
        self, coin: str, sell_amount: float, fill_price: float,
        costs_enabled: bool, net_tracked: bool,
    ) -> tuple[float, float] | None:
        """Delegates to :func:`src.dry_run_execution.dry_run_sell_proceeds`."""
        return _dre_sell_proceeds(
            self.config, coin, sell_amount, fill_price, costs_enabled,
            net_tracked, logger=logger)

    def _apply_dry_run_sell_to_holdings(
        self, coin: str, held: float, sell_amount: float
    ) -> None:
        """Delegates to :func:`src.dry_run_execution.apply_dry_run_sell_to_holdings`."""
        _dre_apply_sell_to_holdings(self.portfolio, coin, held, sell_amount)

    def _record_dry_run_sell_close(
        self, entry_trade: dict, fill_price: float, sell_amount: float,
        proceeds: float, entry_basis: float | None, costs_enabled: bool,
        entry_cost_mode: str,
    ) -> tuple[float, float, float]:
        """Delegates to :func:`src.dry_run_execution.record_dry_run_sell_close`."""
        return _dre_record_sell_close(
            self._record_close_performance, entry_trade, fill_price,
            sell_amount, proceeds, entry_basis, costs_enabled,
            entry_cost_mode)

    def _dry_run_sell_gross_net_pnl(
        self, entry_trade: dict, price: float, sell_amount: float,
        pnl_base: float, entry_cost_mode: str,
    ) -> tuple[str | float, str | float]:
        """Delegates to :func:`src.dry_run_execution.dry_run_sell_gross_net_pnl`."""
        return _dre_sell_gross_net_pnl(
            entry_trade, price, sell_amount, pnl_base, entry_cost_mode)

    def _update_open_trades_after_dry_run_sell(
        self, coin: str, entry_trade: dict,
        remaining_open_amount: float, remaining_ratio: float,
        entry_basis: float | None, costs_enabled: bool,
        entry_cost_mode: str, partial_take_profit: bool,
    ) -> None:
        """Delegates to :func:`src.dry_run_execution.update_open_trades_after_dry_run_sell`."""
        _dre_update_open_trades(
            self.portfolio, self._public_paper_mode, datetime.now,
            coin, entry_trade, remaining_open_amount, remaining_ratio,
            entry_basis, costs_enabled, entry_cost_mode, partial_take_profit)

    def _execute_real_trade(
        self,
        coin: str,
        action: str,
        price: float,
        amount_in_base_currency: float,
        *,
        signal_source: str = 'rules',
        signal_confidence: float | None = None,
        recommendation: str = 'HOLD',
        reason: str = '',
    ):
        """Delegates to :func:`src.execution_service.execute_real_trade`."""
        return _service_execute_real_trade(
            self.exchange,
            self.config,
            self.portfolio,
            self.performance,
            _RealTradeHooks(
                confirm_fill=self._confirm_order_fill,
                confirm_fill_with_order=self._confirm_order_fill_record,
                assert_execution_owner=self._execution_owner_lock.assert_owned,
                record_close_performance=self._record_close_performance,
                trade_excursion_metrics=self._trade_excursion_metrics,
                append_trade_journal=self._append_trade_journal,
                register_buy_timestamp=self._register_buy_timestamp,
                register_sell_timestamp=self._register_sell_timestamp,
                update_portfolio_balance=self._update_portfolio_balance,
            ),
            coin,
            action,
            price,
            amount_in_base_currency,
            signal_source=signal_source,
            signal_confidence=signal_confidence,
            recommendation=recommendation,
            reason=reason,
            logger=logger,
        )

    def _manage_open_trades(
        self,
        current_market_data: dict[str, dict],
        market_analysis: dict | None = None,
    ) -> None:
        """Delegates to :func:`src.open_trade_manager.manage_open_trades`."""
        _open_trade_manage_open_trades(
            _OpenTradeContext(
                config=self.config,
                portfolio=self.portfolio,
            ),
            _OpenTradeHooks(
                get_atr=self._get_atr_for_coin,
                execute_trade=self._execute_trade,
                public_paper_mode=self._public_paper_mode,
            ),
            current_market_data,
            market_analysis,
        )

    def _entry_pair_allowed(
        self,
        coin: str,
        data: dict[str, Any],
        excluded_pairs: Container[tuple[str, str]],
    ) -> bool:
        """True unless this (coin, signal_source) pair is dynamically excluded."""
        source = str(data.get('signal_source', 'rules')).strip().lower()
        return (str(coin).upper(), source) not in excluded_pairs

    def _entry_guards_allow(
        self,
        coin: str,
        data: dict[str, Any],
        cache: dict[str, bool],
    ) -> bool:
        """Cached per-iteration entry guards: tabular veto + volatility proxy."""
        return _entry_selection_entry_guards_allow(
            self.config,
            coin,
            data,
            cache,
            volatility_guard=self._entry_volatility_guard,
        )

    def _log_portfolio_status(
        self,
        current_market_data: dict[str, dict[str, Any]],
        market_analysis: dict[str, Any],
    ) -> None:
        """Compute portfolio value, log status, and persist the analytics snapshot."""
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
        if self.config.simulation_initial_balance_from_kraken:
            self.portfolio.save_state()


    def _log_startup_banner(self) -> None:
        """Log the masked configuration and active-mode warnings at startup."""
        logger.info(
            "🚀 Crypto trading bot started - analysing markets for trading opportunities.")
        safe_config = vars(self.config).copy()
        safe_config['api_key'] = '[REDACTED]' if safe_config.get('api_key') else ''
        safe_config['api_secret'] = '[REDACTED]' if safe_config.get('api_secret') else ''
        safe_config['ai_copilot_api_key'] = _mask_secret(
            safe_config.get('ai_copilot_api_key', ''))
        safe_config['hailo_api_token'] = (
            '[REDACTED]' if safe_config.get('hailo_api_token') else ''
        )
        logger.info(f"Configuration: {safe_config}")
        if self.config.dry_run:
            logger.warning(
                "DRY-RUN MODE IS ACTIVE! No real trades will be executed.")
        if self.config.simulate_data:
            if self.config.simulation_initial_balance_from_kraken:
                logger.warning(
                    "SIMULATION MODE IS ACTIVE! Market data and trades are simulated; "
                    "Kraken may be queried once for the initial free EUR balance."
                )
            else:
                logger.warning(
                    "SIMULATION MODE IS ACTIVE! No real exchange data will be fetched or trades executed.")
        elif self.config.simulation_initial_balance_from_kraken:
            logger.warning(
                "PUBLIC-MARKET PAPER MODE: Kraken market data, simulated trades and "
                "an isolated previously initialized portfolio; no Kraken credentials."
            )


    def _select_entry_candidates(
        self, market_analysis: dict[str, Any]
    ) -> list[str]:
        """Rank entry candidates and fill trade slots (strict, fallback, force-fill)."""
        return _entry_selection_select_shortlist(
            _EntryCandidateContext(
                config=self.config, portfolio=self.portfolio
            ),
            _EntryCandidateHooks(
                dynamic_excluded_entry_pairs=self._dynamic_excluded_entry_pairs,
                degraded_entry_sources=self._degraded_entry_sources,
                recent_pnl_guard_state=self._recent_pnl_guard_state,
                log_recent_pnl_guard_status=self._log_recent_pnl_guard_status,
                entry_market_mode=self._entry_market_mode,
                entry_pair_allowed=self._entry_pair_allowed,
                entry_guards_allow=self._entry_guards_allow,
                passes_fallback_entry_filter=self._passes_fallback_entry_filter,
                passes_downtrend_reversal_filter=(
                    self._passes_downtrend_reversal_filter
                ),
                passes_uptrend_entry_filter=self._passes_uptrend_entry_filter,
                log_blocked_uptrend_candidates=(
                    self._log_blocked_uptrend_candidates
                ),
                log_blocked_downtrend_reversal_candidates=(
                    self._log_blocked_downtrend_reversal_candidates
                ),
                log_fallback_entry_diagnostics=self._log_fallback_entry_diagnostics,
            ),
            market_analysis,
        )

    def _execute_signal_exits(
        self,
        current_market_data: dict[str, Any],
        market_analysis: dict[str, Any],
    ) -> None:
        """Close open positions that received a SELL or WEAK SELL signal."""
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


    def _compute_entry_position_size(
        self,
        coin: str,
        current_price: float,
        atr: float,
        portfolio_value: float,
        trade_amount_cap: float,
        attempt_meta: dict[str, Any],
    ) -> float:
        """Size a new entry from ATR risk and the cash reserve, updating attempt_meta."""
        target_position_size = self._calculate_position_size(
            coin,
            current_price,
            atr,
            portfolio_value=portfolio_value,
            trade_amount_cap=trade_amount_cap,
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
        return position_size


    def _open_new_positions(
        self,
        current_market_data: dict[str, Any],
        market_analysis: dict[str, Any],
        top_buy_recommendations: list[str],
        portfolio_value_live: float,
    ) -> None:
        """Delegates to :func:`src.execution_service.open_new_positions`."""
        effective_trade_amount = self._effective_trade_amount(
            portfolio_value_live)
        effective_max_open_trades = self._effective_max_open_trades(
            portfolio_value_live)
        can_open_positions, block_reason = self._can_open_new_positions(
            portfolio_value_live)
        _service_open_new_positions(
            self.config,
            self.portfolio,
            _EntryHooks(
                get_atr=self._get_atr_for_coin,
                compute_position_size=self._compute_entry_position_size,
                passes_momentum_filter=self._passes_entry_momentum_filter,
                resolve_cooldown_seconds=self._resolve_reentry_cooldown_seconds,
                in_reentry_cooldown=self._is_coin_in_reentry_cooldown,
                execute_trade=self._execute_trade,
                log_blocked_attempts=self._log_blocked_buy_attempt_candidates,
                news_veto=lambda coin, candidate: evaluate_news_veto(
                    self.config, coin, candidate),
            ),
            current_market_data,
            market_analysis,
            top_buy_recommendations,
            portfolio_value_live,
            effective_trade_amount=effective_trade_amount,
            effective_max_open_trades=effective_max_open_trades,
            can_open_positions=can_open_positions,
            block_reason=block_reason,
            logger=logger,
        )


    def run(self):
        """Main loop of the bot."""
        _run_bot(self, logger)


if __name__ == "__main__":
    import argparse

    configure_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest", action="store_true",
                        help="Start backtest on training data")
    parser.add_argument(
        "--backtest-fee-rate",
        type=float,
        default=0.002,
        help="Estimated exchange fee per side as a decimal (default: 0.002)",
    )
    parser.add_argument(
        "--backtest-slippage",
        type=float,
        default=0.001,
        help="Estimated slippage per side as a decimal (default: 0.001)",
    )
    args, _ = parser.parse_known_args()

    if args.backtest:
        print("\nStarting backtest on training data...",)
        backtester = Backtester(
            data_file="training_data.csv",
            initial_cash=10000,
            fee_per_trade=args.backtest_fee_rate,
            slippage=args.backtest_slippage,
        )
        backtester.run(simple_strategy)

    try:
        config = BotConfig()
        if (not config.simulate_data and not config.simulation_initial_balance_from_kraken
                and (not config.api_key or not config.api_secret)):
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
            import numpy as np
            import pandas as pd
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
        _install_sigterm_handler()
        bot.run()
    except (ImportError, OSError, TypeError, ValueError, KeyError) as e:
        logger.critical(f"Initialisation error: {e}", exc_info=True)
        sys.exit(1)
