"""
Trading Bot Package Initialization

This package contains the core components of the cryptocurrency trading bot.
"""

# Import key components for easy access
from .bot import TradingBot
from .config import BotConfig
from .exchange import ExchangeManager
from .portfolio import PortfolioManager
from .strategy import StrategyEngine
from .risk_management import RiskManager

__all__ = [
    'TradingBot',
    'BotConfig', 
    'ExchangeManager',
    'PortfolioManager',
    'StrategyEngine',
    'RiskManager'
]