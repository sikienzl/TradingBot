"""Isolated rules-only paper comparison; never connects to a private account."""

import argparse
import fcntl
import logging
import os
from pathlib import Path

from src.trading_bot import BotConfig, CryptoTradingBot, Portfolio, configure_logging

logger = logging.getLogger(__name__)


class RulesComparisonBot(CryptoTradingBot):
    """Keep the original RSI and risk gates but omit absent ML probabilities."""

    def _passes_uptrend_entry_filter(self, coin_data: dict) -> tuple[bool, str]:
        passed, reason = super()._passes_uptrend_entry_filter(coin_data)
        if (
            reason == "missing_buy_proba"
            and self.config.dry_run
            and not self.config.use_tabular_model
            and str(coin_data.get("signal_source", "")).lower() == "rules"
            and coin_data.get("tabular_buy_proba") is None
            and coin_data.get("tabular_sell_proba") is None
        ):
            return True, "rules_comparison_no_model"
        return passed, reason

    def _update_portfolio_balance(self):
        if not Path(self.portfolio.state_file).is_file():
            raise RuntimeError("Comparison portfolio missing; refusing private balance lookup")
        return super()._update_portfolio_balance()

    def _execute_trade(self, coin, action, price, amount_in_base_currency, *args, **kwargs):
        if not self.config.dry_run:
            raise RuntimeError("Comparison bot refuses live trades")
        if action.lower() == "buy" and amount_in_base_currency > 5:
            raise RuntimeError("Comparison buy exceeds the 5 EUR limit")
        return super()._execute_trade(coin, action, price, amount_in_base_currency, *args, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--initial-cash", type=float)
    args = parser.parse_args()
    directory = args.data_dir.resolve(strict=True)
    if not directory.is_dir():
        raise ValueError("Comparison data directory does not exist")
    state = directory / "portfolio.json"

    with (directory / "comparison.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Comparison instance already owns this directory") from exc
        if args.initialize:
            if state.exists():
                raise FileExistsError("Comparison portfolio already exists")
            if args.initial_cash is None or not 0 < args.initial_cash < float("inf"):
                raise ValueError("A finite positive initial cash amount is required")
            portfolio = Portfolio("EUR")
            portfolio.cash = args.initial_cash
            portfolio.initial_portfolio_value = args.initial_cash
            if not portfolio.save_state(str(state)):
                raise RuntimeError("Could not initialize comparison portfolio")
            logger.warning("Initialized isolated virtual comparison portfolio")
            return
        if args.initial_cash is not None or not state.is_file():
            raise RuntimeError("Existing comparison portfolio required; initialize explicitly")

        os.environ.update({
            "DRY_RUN": "true", "SIMULATE_DATA": "false", "BASE_CURRENCY": "EUR",
            "TRADE_AMOUNT": "5", "PORTFOLIO_STATE_PATH": str(state),
            "PERFORMANCE_LOG_FILE": str(directory / "trades.csv"),
            "BOT_LOG_FILE": str(directory / "bot.log"), "PERFORMANCE_LOG_ENABLED": "true",
            "ANALYTICS_DB_ENABLED": "false", "USE_ML_MODEL": "false",
            "USE_TABULAR_MODEL": "false", "AI_COPILOT_ENABLED": "false",
            "AI_COPILOT_BENCHMARK_ENABLED": "false", "AUTORESEARCH_ACTIVE": "false",
            "HAILO_REMOTE_ENABLED": "false", "KRAKEN_API_KEY": "", "KRAKEN_API_SECRET": "",
            "DEFENSIVE_ENTRY_MODE_ENABLED": "true", "UPTREND_ENTRY_GATE_ENABLED": "true",
            "ENABLE_FALLBACK_ENTRY": "true", "FORCE_FILL_SLOTS": "false",
        })
        configure_logging(str(directory / "bot.log"))
        config = BotConfig()
        if (
            not config.dry_run or config.simulate_data or config.api_key or config.api_secret
            or config.use_ml_model or config.use_tabular_model or config.analytics_db_enabled
            or not config.defensive_entry_mode_enabled or not config.uptrend_entry_gate_enabled
            or not config.enable_fallback_entry or config.force_fill_slots
            or os.getenv("PORTFOLIO_STATE_PATH") != str(state)
            or config.performance_log_file != str(directory / "trades.csv")
            or not config.performance_log_enabled
            or config.trade_amount != 5 or config.base_currency != "EUR"
            or os.getenv("HAILO_REMOTE_ENABLED", "").lower() != "false"
            or os.getenv("AUTORESEARCH_ACTIVE", "").lower() != "false"
            or config.ai_copilot_enabled or config.ai_copilot_benchmark_enabled
        ):
            raise RuntimeError("Unsafe comparison configuration; refusing startup")
        bot = RulesComparisonBot(config)
        if bot.exchange is None or config.simulate_data:
            raise RuntimeError("Public market data unavailable; refusing startup")
        logger.warning(
            "Isolated rules comparison: synthetic capital, no private keys, "
            "no ML probabilities, defensive and RSI gates active"
        )
        bot.run()


if __name__ == "__main__":
    main()
