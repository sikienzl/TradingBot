"""Startup building blocks for the trading bot.

Extracted from ``src.trading_bot`` so that startup validation, startup logging
and optional model loading can be tested and reused without importing the full
trading bot. All logic is copied verbatim from ``CryptoTradingBot.__init__``;
the only change is that the logger is passed in explicitly.
"""

from __future__ import annotations

import logging
from typing import Any


def validate_startup_config(config: Any) -> None:
    """Rejects startup configurations that contradict each other.

    Raises ``ValueError`` with the original messages from the bot
    constructor.
    """
    if config.simulation_initial_balance_from_kraken and (
        not config.dry_run or config.exchange_name != 'kraken'
        or config.base_currency != 'EUR'
    ):
        raise ValueError(
            "Kraken simulation initialization requires DRY_RUN, Kraken and EUR"
        )
    if (config.simulation_initial_balance_from_kraken and not config.simulate_data
            and (config.api_key or config.api_secret)):
        raise ValueError(
            "Public-market paper trading requires a previously initialized portfolio "
            "and no Kraken API credentials"
        )


def log_startup_details(
    logger: logging.Logger,
    config: Any,
    run_id: str,
    config_hash: str,
) -> None:
    """Emits the startup banner lines exactly as the constructor did."""
    logger.info(
        "Trading run started: run_id=%s config_hash=%s dry_run=%s simulated_data=%s",
        run_id,
        config_hash,
        config.dry_run,
        config.simulate_data,
    )
    if config.dry_run:
        logger.info(
            "DRY-RUN execution cost mode=%s fee_rate=%.6f slippage_rate=%.6f",
            'net_costs' if config.dry_run_costs_enabled else 'gross_legacy',
            config.dry_run_fee_rate if config.dry_run_costs_enabled else 0.0,
            config.dry_run_slippage_rate if config.dry_run_costs_enabled else 0.0,
        )


def load_ml_predictor(logger: logging.Logger, config: Any) -> Any:
    """Loads the optional rule/ML predictor (USE_ML_MODEL=True)."""
    ml_predictor = None
    if config.use_ml_model:
        try:
            from src.predict import TradingModelPredictor
            ml_predictor = TradingModelPredictor(
                config.model_path)
            logger.info(
                f"🤖 ML model loaded from '{config.model_path}'.")
        except (
            ImportError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            KeyError,
        ) as e:
            logger.warning(
                f"ML model could not be loaded: {e}. Using rule-based analysis only.")
    return ml_predictor


def load_tabular_predictor(logger: logging.Logger, config: Any) -> Any:
    """Loads the optional tabular model (Hailo remote or CatBoost)."""
    tabular_predictor: Any = None
    if config.use_tabular_model:
        if config.tabular_model_backend == 'hailo':
            from src.inference_fallback import TabularInferenceFallback

            try:
                from src.predict_hailo_remote import HailoRemotePredictor
                hailo_predictor = HailoRemotePredictor(
                    service_url=config.hailo_service_url,
                    api_token=config.hailo_api_token,
                    timeout_seconds=config.hailo_timeout_seconds,
                )
                logger.info(
                    "Tabular inference selected backend=hailo at %s",
                    config.hailo_service_url,
                )
            except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
                hailo_predictor = None
                logger.warning("Hailo backend unavailable; falling back to CPU: %s", exc)

            def load_cpu_predictor() -> Any:
                from src.predict_catboost import CatBoostTradingPredictor
                return CatBoostTradingPredictor(
                    config.tabular_model_path,
                    research_signal_path=config.tabular_research_signal_path,
                )

            tabular_predictor = TabularInferenceFallback(
                hailo_predictor, load_cpu_predictor, logger)
        else:
            try:
                from src.predict_catboost import CatBoostTradingPredictor
                tabular_predictor = CatBoostTradingPredictor(
                    config.tabular_model_path,
                    research_signal_path=config.tabular_research_signal_path,
                )
                logger.info(
                    f"📊 CatBoost model loaded from '{config.tabular_model_path}'.")
            except (
                ImportError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                KeyError,
            ) as e:
                logger.warning(
                    f"CatBoost model could not be loaded: {e}.")
    return tabular_predictor
