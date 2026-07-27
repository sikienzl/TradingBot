#!/usr/bin/env python3
"""
Simple test for the centralized configuration system.
"""

import sys
import os

# Add current directory to path to avoid importing project modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    # Import just the schema module directly
    from src.config.schema import BotConfig
    
    # Create an instance
    config = BotConfig()
    
    print("Configuration loaded successfully!")
    print(f"Environment: {config.environment}")
    print(f"Simulate data: {config.simulate_data}")
    print(f"Exchange name: {config.exchange.name}")
    print(f"Trading amount: {config.trading.trade_amount}")
    
except Exception as e:
    print(f"Error loading configuration: {e}")
    import traceback
    traceback.print_exc()