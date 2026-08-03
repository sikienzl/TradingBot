"""
Main Application Entry Point

This module is the entry point for the trading bot application.
"""

import logging
import sys
import os
from typing import Optional

# Configure logging before anything else
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('trading_bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

from trading_bot.bot import TradingBot
from src.config.loader import load_config

def main(config_path: Optional[str] = None) -> None:
    """
    Main entry point for the trading bot.
    
    Args:
        config_path: Path to configuration file (optional)
    """
    logger = logging.getLogger(__name__)
    logger.info("Starting trading bot application")
    
    try:
        # Load configuration
        config = load_config(config_path)
        
        # Initialize and start the bot
        bot = TradingBot(config)
        bot.start()
        
        logger.info("Trading bot started successfully")
        
    except KeyboardInterrupt:
        logger.info("Trading bot stopped by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Error starting trading bot: {str(e)}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    # Check if config file path is provided as command line argument
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    
    # If no config path provided, check for default config files
    if not config_path:
        default_configs = ['config.yaml', 'config.json', 'config.yml']
        for cfg in default_configs:
            if os.path.exists(cfg):
                config_path = cfg
                break
    
    main(config_path)