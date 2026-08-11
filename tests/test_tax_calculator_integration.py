"""
Integration tests for German Tax Calculator with Trading Bot.

This module contains integration tests that verify the German tax calculator
works correctly within the trading bot context.
"""

import pytest
from src.trading_bot import BotConfig
from src.utils.german_tax_calculator import create_tax_calculator


def test_create_tax_calculator_disabled():
    """Test creating tax calculator when disabled in config."""
    # Create a config with tax calculation disabled
    class MockConfigDisabled:
        def __init__(self):
            self.german_tax_enabled = False
    
    config = MockConfigDisabled()
    calculator = create_tax_calculator(config)
    
    # Should return None when disabled
    assert calculator is None


def test_create_tax_calculator_enabled():
    """Test creating tax calculator when enabled in config."""
    # Create a config with tax calculation enabled
    class MockConfigEnabled:
        def __init__(self):
            self.german_tax_enabled = True
    
    config = MockConfigEnabled()
    calculator = create_tax_calculator(config)
    
    # Should return a calculator instance when enabled
    assert calculator is not None
    assert hasattr(calculator, 'calculate_tax_summary')


def test_tax_calculator_integration_with_bot_config():
    """Test that tax calculator integrates with BotConfig properly."""
    # Create a config similar to what the bot would use
    config = BotConfig()
    
    # Verify we can create a tax calculator from the bot's config
    calculator = create_tax_calculator(config)
    
    # If German tax is disabled by default, should return None
    assert calculator is None or hasattr(calculator, 'calculate_tax_summary')