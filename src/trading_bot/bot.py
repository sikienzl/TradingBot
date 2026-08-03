"""
Main Trading Bot Class

This module contains the core TradingBot class that orchestrates all components
of the cryptocurrency trading system.
"""

import logging
import time
from typing import Optional, Dict, Any, List
from .config import BotConfig
from ..exchange import ExchangeManager
from ..portfolio import PortfolioManager
from ..strategy import StrategyEngine, ModularStrategyEngine
from ..risk_management import RiskManager, AdvancedRiskManager
from ..data.fetcher import AdvancedDataFetcher
from src.config.loader import load_config

class TradingBot:
    """Main trading bot class that coordinates all components."""
    
    def __init__(self, config: Optional[BotConfig] = None) -> None:
        """
        Initialize the trading bot.
        
        Args:
            config: Bot configuration. If None, default config will be used.
        """
        # Load configuration using centralized system
        self.config = config or load_config()
        self.logger = logging.getLogger(__name__)
        
        # Initialize components
        self.exchange_manager = ExchangeManager(self.config)
        self.portfolio_manager = PortfolioManager(self.config)
        self.strategy_engine = ModularStrategyEngine(self.config)
        self.risk_manager = AdvancedRiskManager(self.config)
        self.data_fetcher = AdvancedDataFetcher()
        
        self.logger.info("Trading bot initialized successfully")
    
    def start(self) -> None:
        """Start the trading bot."""
        self.logger.info("Starting trading bot...")
        
        # Validate configuration
        errors = self.config.validate()
        if errors:
            self.logger.error(f"Configuration validation failed: {errors}")
            raise ValueError(f"Invalid configuration: {errors}")
        
        # Main trading loop
        try:
            while True:
                self.run_cycle()
                time.sleep(self.config.trading.check_interval_seconds)
        except KeyboardInterrupt:
            self.logger.info("Trading bot stopped by user")
            self.stop()
        except Exception as e:
            self.logger.error(f"Unexpected error in trading bot: {e}")
            raise
    
    def stop(self) -> None:
        """Stop the trading bot."""
        self.logger.info("Stopping trading bot...")
        # Clean up resources if needed
        pass
    
    def run_cycle(self) -> None:
        """
        Run a complete trading cycle.
        
        This method orchestrates all components for one trading iteration.
        """
        try:
            self.logger.info("Starting trading cycle...")
            
            # Fetch market data
            market_data = self.data_fetcher.fetch_all_data()
            
            # Update portfolio
            self.portfolio_manager.update_positions(market_data)
            
            # Generate trading signals
            signals = self.strategy_engine.generate_signals(market_data)
            
            # Execute trades based on signals
            self.execute_trades(signals)
            
            self.logger.info("Trading cycle completed successfully")
            
        except Exception as e:
            self.logger.error(f"Error in trading cycle: {e}")
            raise
    
    def execute_trades(self, signals) -> None:
        """
        Execute trades based on generated signals.
        
        Args:
            signals: Trading signals from strategy engine
        """
        # Implementation would go here
        pass
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get current status of the trading bot.
        
        Returns:
            Dictionary with current status information
        """
        return {
            "is_running": True,
            "config": self.config.to_dict(),
            "portfolio_value": self.portfolio_manager.get_portfolio_value(),
            "open_positions": len(self.portfolio_manager.get_open_positions()),
            "last_update": time.time()
        }

# Example usage
if __name__ == "__main__":
    bot = TradingBot()
    bot.start()