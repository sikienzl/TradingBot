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
        return cls(
            exchange_name=os.getenv("EXCHANGE_NAME", "kraken"),
            api_key=os.getenv("TRADING_API_KEY"),
            api_secret=os.getenv("TRADING_API_SECRET"),
            sandbox_mode=os.getenv("SANDBOX_MODE", "false").lower() == "true",
            max_position_size=float(os.getenv("MAX_POSITION_SIZE", "1.0")),
            risk_per_trade=float(os.getenv("RISK_PER_TRADE", "0.01")),
            stop_loss_percentage=float(os.getenv("STOP_LOSS_PERCENTAGE", "0.05")),
            take_profit_percentage=float(os.getenv("TAKE_PROFIT_PERCENTAGE", "0.10")),
            max_portfolio_risk=float(os.getenv("MAX_PORTFOLIO_RISK", "0.10")),
            data_frequency=os.getenv("DATA_FREQUENCY", "1h"),
            lookback_period=int(os.getenv("LOOKBACK_PERIOD", "100")),
            enable_sentiment_data=os.getenv("ENABLE_SENTIMENT_DATA", "true").lower() == "true",
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            model_path=os.getenv("MODEL_PATH"),
            enable_online_learning=os.getenv("ENABLE_ONLINE_LEARNING", "false").lower() == "true",
            enable_strategy_optimization=os.getenv("ENABLE_STRATEGY_OPTIMIZATION", "true").lower() == "true",
            cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", "300")),
            max_concurrent_requests=int(os.getenv("MAX_CONCURRENT_REQUESTS", "10")),
            request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
        )
    
    def validate(self):
        """
        Validate configuration values.
        
        Raises:
            ValueError: If any configuration value is invalid
        """
        if self.max_position_size <= 0:
            raise ValueError("max_position_size must be positive")
            
        if self.risk_per_trade < 0 or self.risk_per_trade > 1:
            raise ValueError("risk_per_trade must be between 0 and 1")
            
        if self.stop_loss_percentage < 0:
            raise ValueError("stop_loss_percentage cannot be negative")
            
        if self.take_profit_percentage < 0:
            raise ValueError("take_profit_percentage cannot be negative")
            
        if self.max_portfolio_risk < 0 or self.max_portfolio_risk > 1:
            raise ValueError("max_portfolio_risk must be between 0 and 1")
            
        if self.lookback_period <= 0:
            raise ValueError("lookback_period must be positive")
            
        if self.cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds cannot be negative")
            
        if self.max_concurrent_requests <= 0:
            raise ValueError("max_concurrent_requests must be positive")
            
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")

# Global configuration instance - removed to prevent circular imports
# config = BotConfig()