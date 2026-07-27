"""
Centralized Configuration Schema

This module defines a comprehensive configuration schema with validation
for the trading bot system.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import os
from pathlib import Path


@dataclass
class ExchangeConfig:
    """Exchange configuration settings."""
    
    # Core exchange settings
    name: str = "kraken"
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    sandbox_mode: bool = False
    base_currency: str = "EUR"
    excluded_coins: List[str] = field(default_factory=lambda: ["USDC", "USDT", "EURT", "DAI", "TUSD", "USDP", "FDUSD", "USDE"])
    
    # Connection settings
    max_concurrent_requests: int = 10
    request_timeout_seconds: int = 30
    retry_attempts: int = 3
    retry_delay_seconds: float = 1.0


@dataclass
class TradingConfig:
    """Trading configuration settings."""
    
    # Position sizing
    trade_amount: float = 10.0
    min_trade_amount: float = 3.0
    max_open_trades: int = 4
    allow_partial_trades: bool = True
    cash_reserve_pct: float = 0.02
    
    # Risk management
    risk_per_trade: float = 0.01
    max_portfolio_risk: float = 0.10
    stop_loss_percentage: float = 0.05
    take_profit_percentage: float = 0.10
    
    # Execution cadence
    check_interval_seconds: int = 8
    enable_dry_run: bool = True
    
    # Entry logic
    min_entry_score: int = 50
    enable_fallback_entry: bool = True
    enable_signal_exits: bool = True
    exit_on_downtrend: bool = True
    entry_require_price_above_ema20: bool = False
    fallback_min_score: int = 40
    fallback_max_rsi: int = 80
    force_fill_slots: bool = True
    force_fill_min_score: int = 30
    
    # Downtrend reversal settings
    downtrend_reversal_entry_enabled: bool = True
    downtrend_reversal_allowed_coins: List[str] = field(default_factory=lambda: ["ETH"])
    downtrend_reversal_max_rsi: int = 30
    downtrend_reversal_min_buy_proba: float = 0.20
    downtrend_reversal_max_sell_proba: float = 0.35


@dataclass
class DataConfig:
    """Data configuration settings."""
    
    # Data source settings
    data_frequency: str = "1h"
    lookback_period: int = 100
    enable_sentiment_data: bool = True
    
    # Market filtering
    min_volume_base: float = 100000.0
    top_n_for_analysis: int = 12
    ticker_batch_size: int = 80
    ticker_fetch_retries: int = 2
    ticker_retry_delay_seconds: float = 1.0
    
    # Data cache settings
    cache_ttl_seconds: int = 300
    enable_data_caching: bool = True


@dataclass
class ModelConfig:
    """Model configuration settings."""
    
    # Model selection and usage
    use_ml_model: bool = True
    model_path: Optional[str] = None
    model_type: str = "catboost"
    
    # Model performance settings
    enable_online_learning: bool = False
    enable_strategy_optimization: bool = True
    
    # Confidence thresholds
    recommended_confidence_threshold: float = 0.45
    margin_threshold: float = 0.03


@dataclass
class LoggingConfig:
    """Logging configuration settings."""
    
    log_level: str = "INFO"
    log_file: Optional[str] = None
    enable_console_logging: bool = True
    enable_file_logging: bool = False
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


@dataclass
class AdvancedConfig:
    """Advanced configuration settings."""
    
    # Performance settings
    max_concurrent_tasks: int = 10
    task_timeout_seconds: int = 300
    
    # Feature flags
    enable_hailo_integration: bool = True
    enable_edge_computing: bool = False
    enable_hybrid_decision_making: bool = True
    
    # Debug settings
    debug_mode: bool = False
    verbose_logging: bool = False


@dataclass
class BotConfig:
    """Centralized configuration class for the trading bot."""
    
    # Core configuration sections
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    data: DataConfig = field(default_factory=DataConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    advanced: AdvancedConfig = field(default_factory=AdvancedConfig)
    
    # Environment-specific settings
    environment: str = "development"
    simulate_data: bool = False
    
    def __post_init__(self):
        """Initialize configuration with environment variables."""
        self._load_from_env()
    
    def _load_from_env(self):
        """Load configuration from environment variables."""
        # Exchange settings
        self.exchange.name = os.getenv("EXCHANGE_NAME", self.exchange.name)
        self.exchange.api_key = os.getenv("KRAKEN_API_KEY", self.exchange.api_key)
        self.exchange.api_secret = os.getenv("KRAKEN_API_SECRET", self.exchange.api_secret)
        self.exchange.sandbox_mode = os.getenv("SANDBOX_MODE", str(self.exchange.sandbox_mode)).lower() == "true"
        self.exchange.base_currency = os.getenv("BASE_CURRENCY", self.exchange.base_currency)
        
        # Trading settings
        self.trading.enable_dry_run = os.getenv("DRY_RUN", str(self.trading.enable_dry_run)).lower() == "true"
        self.trading.simulate_data = os.getenv("SIMULATE_DATA", str(self.trading.simulate_data)).lower() == "true"
        self.trading.check_interval_seconds = int(os.getenv("CHECK_INTERVAL", self.trading.check_interval_seconds))
        self.trading.max_open_trades = int(os.getenv("MAX_OPEN_TRADES", self.trading.max_open_trades))
        self.trading.trade_amount = float(os.getenv("TRADE_AMOUNT", self.trading.trade_amount))
        
        # Data settings
        self.data.data_frequency = os.getenv("DATA_FREQUENCY", self.data.data_frequency)
        self.data.lookback_period = int(os.getenv("LOOKBACK_PERIOD", self.data.lookback_period))
        
        # Model settings
        self.model.use_ml_model = os.getenv("USE_ML_MODEL", str(self.model.use_ml_model)).lower() == "true"
        self.model.model_path = os.getenv("MODEL_PATH", self.model.model_path)
        
        # Logging settings
        self.logging.log_level = os.getenv("LOG_LEVEL", self.logging.log_level)
        self.logging.log_file = os.getenv("LOG_FILE", self.logging.log_file)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary for serialization."""
        return {
            "exchange": {
                "name": self.exchange.name,
                "api_key": self.exchange.api_key,
                "api_secret": self.exchange.api_secret,
                "sandbox_mode": self.exchange.sandbox_mode,
                "base_currency": self.exchange.base_currency,
                "excluded_coins": self.exchange.excluded_coins,
                "max_concurrent_requests": self.exchange.max_concurrent_requests,
                "request_timeout_seconds": self.exchange.request_timeout_seconds,
                "retry_attempts": self.exchange.retry_attempts,
                "retry_delay_seconds": self.exchange.retry_delay_seconds
            },
            "trading": {
                "trade_amount": self.trading.trade_amount,
                "min_trade_amount": self.trading.min_trade_amount,
                "max_open_trades": self.trading.max_open_trades,
                "allow_partial_trades": self.trading.allow_partial_trades,
                "cash_reserve_pct": self.trading.cash_reserve_pct,
                "risk_per_trade": self.trading.risk_per_trade,
                "max_portfolio_risk": self.trading.max_portfolio_risk,
                "stop_loss_percentage": self.trading.stop_loss_percentage,
                "take_profit_percentage": self.trading.take_profit_percentage,
                "check_interval_seconds": self.trading.check_interval_seconds,
                "enable_dry_run": self.trading.enable_dry_run,
                "min_entry_score": self.trading.min_entry_score,
                "enable_fallback_entry": self.trading.enable_fallback_entry,
                "enable_signal_exits": self.trading.enable_signal_exits,
                "exit_on_downtrend": self.trading.exit_on_downtrend,
                "entry_require_price_above_ema20": self.trading.entry_require_price_above_ema20,
                "fallback_min_score": self.trading.fallback_min_score,
                "fallback_max_rsi": self.trading.fallback_max_rsi,
                "force_fill_slots": self.trading.force_fill_slots,
                "force_fill_min_score": self.trading.force_fill_min_score,
                "downtrend_reversal_entry_enabled": self.trading.downtrend_reversal_entry_enabled,
                "downtrend_reversal_allowed_coins": self.trading.downtrend_reversal_allowed_coins,
                "downtrend_reversal_max_rsi": self.trading.downtrend_reversal_max_rsi,
                "downtrend_reversal_min_buy_proba": self.trading.downtrend_reversal_min_buy_proba,
                "downtrend_reversal_max_sell_proba": self.trading.downtrend_reversal_max_sell_proba
            },
            "data": {
                "data_frequency": self.data.data_frequency,
                "lookback_period": self.data.lookback_period,
                "enable_sentiment_data": self.data.enable_sentiment_data,
                "min_volume_base": self.data.min_volume_base,
                "top_n_for_analysis": self.data.top_n_for_analysis,
                "ticker_batch_size": self.data.ticker_batch_size,
                "ticker_fetch_retries": self.data.ticker_fetch_retries,
                "ticker_retry_delay_seconds": self.data.ticker_retry_delay_seconds,
                "cache_ttl_seconds": self.data.cache_ttl_seconds,
                "enable_data_caching": self.data.enable_data_caching
            },
            "model": {
                "use_ml_model": self.model.use_ml_model,
                "model_path": self.model.model_path,
                "model_type": self.model.model_type,
                "enable_online_learning": self.model.enable_online_learning,
                "enable_strategy_optimization": self.model.enable_strategy_optimization,
                "recommended_confidence_threshold": self.model.recommended_confidence_threshold,
                "margin_threshold": self.model.margin_threshold
            },
            "logging": {
                "log_level": self.logging.log_level,
                "log_file": self.logging.log_file,
                "enable_console_logging": self.logging.enable_console_logging,
                "enable_file_logging": self.logging.enable_file_logging,
                "log_format": self.logging.log_format
            },
            "advanced": {
                "max_concurrent_tasks": self.advanced.max_concurrent_tasks,
                "task_timeout_seconds": self.advanced.task_timeout_seconds,
                "enable_hailo_integration": self.advanced.enable_hailo_integration,
                "enable_edge_computing": self.advanced.enable_edge_computing,
                "enable_hybrid_decision_making": self.advanced.enable_hybrid_decision_making,
                "debug_mode": self.advanced.debug_mode,
                "verbose_logging": self.advanced.verbose_logging
            },
            "environment": self.environment,
            "simulate_data": self.simulate_data
        }
    
    def validate(self) -> List[str]:
        """Validate configuration settings."""
        errors = []
        
        # Validate trading settings
        if self.trading.trade_amount <= 0:
            errors.append("Trade amount must be positive")
        if self.trading.risk_per_trade <= 0 or self.trading.risk_per_trade > 1:
            errors.append("Risk per trade must be between 0 and 1")
        
        # Validate data settings
        if self.data.lookback_period <= 0:
            errors.append("Lookback period must be positive")
        
        # Validate model settings
        if self.model.recommended_confidence_threshold < 0 or self.model.recommended_confidence_threshold > 1:
            errors.append("Recommended confidence threshold must be between 0 and 1")
        
        return errors