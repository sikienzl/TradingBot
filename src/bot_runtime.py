"""Long-running bot loop and its process-lifetime execution lock.

The loop is entered only after the live-mode scorecard gate has been checked
again. ``src.bot_bootstrap.validate_startup_config`` already refuses a live
configuration at construction time; repeating the check here covers a bot
object that is built elsewhere (tests, tooling, a future cluster supervisor)
and then started, and it records the accepted verdict in the log so an operator
can see which scorecard run authorised real orders.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from .bot_bootstrap import LIVE_MODE_ACTION, evaluate_live_scorecard_gate
from .scorecard_gate import scorecard_gate_refusal, scorecard_gate_settings


def assert_live_scorecard_gate(bot: Any, logger: logging.Logger) -> None:
    """Refuse to start the loop in live mode without a fresh ``GO`` verdict."""
    config = getattr(bot, "config", None)
    if config is None or getattr(config, "dry_run", True):
        # Simulated trading never reaches the exchange, so no health evidence
        # is required and the scorecard markers are not read at all.
        return
    settings = scorecard_gate_settings()
    status = evaluate_live_scorecard_gate(config, settings)
    if status is None:
        # Only the explicit operator override can get here now that live mode
        # is established - say so loudly, it removes the last pre-trade gate.
        logger.warning(
            "Live trading started with the scorecard gate disabled by %s=false",
            "SCORECARD_GATE_ENABLED",
        )
        return
    if status.allowed:
        logger.info(
            "Live trading scorecard gate accepted: verdict=%s age=%.0fs",
            status.verdict,
            status.age_seconds or 0.0,
        )
        return
    raise ValueError(scorecard_gate_refusal(LIVE_MODE_ACTION, status, settings))


def run_bot(bot: Any, logger: logging.Logger) -> None:
    try:
        assert_live_scorecard_gate(bot, logger)
        with bot._execution_owner_lock:
            _run_loop(bot, logger)
    finally:
        bot.analytics_writer.close()
        logger.info("👋 Bot stopped")


def _run_loop(bot: Any, logger: logging.Logger) -> None:
    bot._log_startup_banner()
    try:
        while True:
            bot.iteration += 1
            iteration_started = time.monotonic()
            logger.info(
                f"\n🕒 Iteration {bot.iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            bot._update_portfolio_balance()
            if bot.config.simulation_initial_balance_from_kraken:
                bot.portfolio.simulation_iteration = bot.iteration

            current_market_data = bot._get_market_data()
            if not current_market_data:
                logger.error("No market data available. Skipping this iteration.")
                time.sleep(60)
                continue

            portfolio_value_live = bot.portfolio.get_value(
                {coin: data['price'] for coin, data in current_market_data.items()})
            bot._refresh_daily_anchor(portfolio_value_live)
            market_analysis = bot._analyze_markets(
                current_market_data,
                extra_coins=list(bot.portfolio.open_trades.keys()),
            )
            top_buy_recommendations = bot._select_entry_candidates(market_analysis)
            bot._execute_signal_exits(current_market_data, market_analysis)
            bot._open_new_positions(
                current_market_data,
                market_analysis,
                top_buy_recommendations,
                portfolio_value_live,
            )

            if bot.portfolio.open_trades:
                logger.info(
                    "💼 Monitoring %d open trades after %.1fs of iteration work...",
                    len(bot.portfolio.open_trades),
                    time.monotonic() - iteration_started,
                )
                bot._manage_open_trades(current_market_data, market_analysis)

            bot._log_portfolio_status(current_market_data, market_analysis)
            if bot.iteration % max(1, bot.config.performance_report_every) == 0:
                bot._log_performance_report()

            if bot.config.ai_copilot_enabled or bot.config.ai_copilot_benchmark_enabled:
                ai_snapshot = bot._ai_copilot_snapshot()
                bot._maybe_run_ai_copilot(ai_snapshot)
                bot._maybe_run_ai_copilot_benchmark(ai_snapshot)

            logger.info("⏳ Next analysis in %s seconds...", bot.config.check_interval)
            time.sleep(bot.config.check_interval)
    except KeyboardInterrupt:
        logger.info("\n🛑 Bot stopping...")