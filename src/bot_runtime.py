"""Long-running bot loop and its process-lifetime execution lock."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any


def run_bot(bot: Any, logger: logging.Logger) -> None:
    try:
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