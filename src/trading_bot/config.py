"""
Bot Configuration

This module handles the configuration of the trading bot using a centralized system.
"""

import os
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from pathlib import Path

# Import our new centralized configuration
from src.config.schema import BotConfig as CentralizedBotConfig


def get_config() -> CentralizedBotConfig:
    """
    Get the centralized configuration for the trading bot.
    
    Returns:
        Configuration object with all settings
    """
    return CentralizedBotConfig()


# For backward compatibility, we'll keep the old class but make it use the new system
class BotConfig(CentralizedBotConfig):
    """Configuration class for the trading bot (using centralized system)."""
    
    def __init__(self):
        """Initialize configuration using centralized system."""
        # Call parent constructor which will load from environment
        super().__init__()
    
    def __post_init__(self):
        """Initialize configuration after dataclass fields are set."""
        # Load from environment variables if not explicitly set
        if self.api_key is None:
            self.api_key = os.getenv("TRADING_API_KEY")
            
        if self.api_secret is None:
            self.api_secret = os.getenv("TRADING_API_SECRET")
            
        if self.model_path is None:
            self.model_path = os.getenv("MODEL_PATH", "model/")
    
    @classmethod
    def from_env(cls):
        """Create configuration from environment variables."""
        # Load the centralized config to get default values
        config = cls()
        
        return cls(
            exchange_name=os.getenv("EXCHANGE_NAME", config.exchange.name),
            api_key=os.getenv("TRADING_API_KEY"),
            api_secret=os.getenv("TRADING_API_SECRET"),
            sandbox_mode=os.getenv("SANDBOX_MODE", str(config.exchange.sandbox_mode)).lower() == "true",
            max_position_size=float(os.getenv("MAX_POSITION_SIZE", str(config.trading.max_open_trades))),
            risk_per_trade=float(os.getenv("RISK_PER_TRADE", str(config.trading.risk_per_trade))),
            stop_loss_percentage=float(os.getenv("STOP_LOSS_PERCENTAGE", str(config.trading.stop_loss_percentage))),
            take_profit_percentage=float(os.getenv("TAKE_PROFIT_PERCENTAGE", str(config.trading.take_profit_percentage))),
            max_portfolio_risk=float(os.getenv("MAX_PORTFOLIO_RISK", str(config.trading.max_portfolio_risk))),
            data_frequency=os.getenv("DATA_FREQUENCY", config.data.data_frequency),
            lookback_period=int(os.getenv("LOOKBACK_PERIOD", str(config.data.lookback_period))),
            enable_sentiment_data=os.getenv("ENABLE_SENTIMENT_DATA", str(config.data.enable_sentiment_data)).lower() == "true",
            log_level=os.getenv("LOG_LEVEL", config.logging.log_level),
            model_path=os.getenv("MODEL_PATH"),
            enable_online_learning=os.getenv("ENABLE_ONLINE_LEARNING", str(config.model.enable_online_learning)).lower() == "true",
            enable_strategy_optimization=os.getenv("ENABLE_STRATEGY_OPTIMIZATION", str(config.model.enable_strategy_optimization)).lower() == "true",
            cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", str(config.data.cache_ttl_seconds))),
            max_concurrent_requests=int(os.getenv("MAX_CONCURRENT_REQUESTS", str(config.exchange.max_concurrent_requests))),
            request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", str(config.exchange.request_timeout_seconds)))
        )
    
    def validate(self):
        """
        Validate configuration values.
        
        Raises:
            ValueError: If any configuration value is invalid
        """
        # Validate trading settings
        if self.trading.max_open_trades <= 0:
            raise ValueError("max_position_size must be positive")
            
        if self.trading.risk_per_trade < 0 or self.trading.risk_per_trade > 1:
            raise ValueError("risk_per_trade must be between 0 and 1")
            
        if self.trading.stop_loss_percentage < 0:
            raise ValueError("stop_loss_percentage cannot be negative")
            
        if self.trading.take_profit_percentage < 0:
            raise ValueError("take_profit_percentage cannot be negative")
            
        if self.trading.max_portfolio_risk < 0 or self.trading.max_portfolio_risk > 1:
            raise ValueError("max_portfolio_risk must be between 0 and 1")
            
        if self.data.lookback_period <= 0:
            raise ValueError("lookback_period must be positive")
            
        if self.data.cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds cannot be negative")
            
        if self.exchange.max_concurrent_requests <= 0:
            raise ValueError("max_concurrent_requests must be positive")
            
        if self.exchange.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")

# Global configuration instance - removed to prevent circular imports
# config = BotConfig()