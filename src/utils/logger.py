"""
Logger Configuration

This module handles logging configuration for the trading bot.
"""

import logging
import os
from typing import Optional

def setup_logger(name: str, log_level: Optional[int] = None) -> logging.Logger:
    """
    Setup and configure logger.
    
    Args:
        name: Logger name
        log_level: Logging level (optional)
        
    Returns:
        Configured logger instance
    """
    if log_level is None:
        log_level = logging.INFO
    
    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(log_level)
    
    # Prevent adding multiple handlers if function is called multiple times
    if not logger.handlers:
        # Create console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
        
        # Create file handler (optional)
        log_file = os.getenv('TRADING_LOG_FILE', 'trading_bot.log')
        if log_file:
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(log_level)
            
            # Create formatter
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            console_handler.setFormatter(formatter)
            file_handler.setFormatter(formatter)
            
            logger.addHandler(file_handler)
        
        # Add console handler
        logger.addHandler(console_handler)
    
    return logger

# Example usage
if __name__ == "__main__":
    logger = setup_logger("test_logger")
    logger.info("Logger configured successfully")