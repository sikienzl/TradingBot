"""
German Tax Calculator for Cryptocurrency Trading

This module implements comprehensive German tax rules for cryptocurrency trading, including:
- Capital gains/losses calculation using FIFO method
- 30% withholding tax on profits (as per German tax law)
- Different transaction types (buy, sell, trade)
- Tax-free allowances and exemptions
- Support for both simulated and real trading scenarios
- Integration with the existing trading bot framework
"""

# Import the enhanced implementation from utils
from src.utils.german_tax_calculator import GermanTaxCalculator, Transaction, TaxableEvent, create_tax_calculator

# This file serves as a compatibility layer that re-exports the enhanced functionality
# The actual implementation is in src/utils/german_tax_calculator.py