"""
Bot Configuration

This module handles the configuration of the trading bot.
"""

import os
from typing import Optional, Dict, Any
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class BotConfig:
    """Configuration class for the trading bot."""
    
    # Exchange settings
    exchange_name: str = "kraken"
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    sandbox_mode: bool = False
    
    # Trading settings
    max_position_size: float = 1.0
    risk_per_trade: float = 0.01
    stop_loss_percentage: float = 0.05
    take_profit_percentage: float = 0.10
    
    # Data settings
    data_frequency: str = "1h"
    lookback_period: int = 100
    
    # Logging settings
    log_level: str = "INFO"
    log_file: Optional[str] = None
    
    # Model settings
    use_ml_model: bool = True
    model_path: Optional[str] = None
    
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
            data_frequency=os.getenv("DATA_FREQUENCY", "1h"),
            lookback_period=int(os.getenv("LOOKBACK_PERIOD", "100")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            model_path=os.getenv("MODEL_PATH")
        )

# Global configuration instance
config = BotConfig()