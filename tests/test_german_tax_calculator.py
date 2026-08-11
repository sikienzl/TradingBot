"""
Unit tests for German Tax Calculator.

This module contains comprehensive tests for the German tax calculator
implementation to ensure proper calculation of cryptocurrency taxes according
to German tax law.
"""

import pytest
from src.utils.german_tax_calculator import GermanTaxCalculator, Transaction, TaxableEvent
from datetime import datetime


def test_tax_calculator_initialization():
    """Test that GermanTaxCalculator initializes correctly."""
    # Create a simple config-like object
    class MockConfig:
        def __init__(self):
            self.german_tax_enabled = True
            self.tax_free_allowance = 600.0
            self.withholding_tax_rate = 0.30
            self.min_taxable_gain = 1.0
    
    config = MockConfig()
    calculator = GermanTaxCalculator(config)
    
    assert calculator is not None
    assert calculator.tax_free_allowance == 600.0
    assert calculator.withholding_tax_rate == 0.30
    assert calculator.min_taxable_gain == 1.0


def test_add_transaction_buy():
    """Test adding a buy transaction."""
    class MockConfig:
        def __init__(self):
            self.german_tax_enabled = True
            self.tax_free_allowance = 600.0
            self.withholding_tax_rate = 0.30
            self.min_taxable_gain = 1.0
    
    config = MockConfig()
    calculator = GermanTaxCalculator(config)
    
    # Add a buy transaction
    transaction = Transaction(
        tx_id="test_buy_1",
        type="buy",
        coin="BTC",
        amount=1.0,
        price=50000.0,
        timestamp=datetime(2022, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    calculator.add_transaction(transaction)
    
    # Verify transaction was added
    assert len(calculator.transactions) == 1
    assert calculator.transactions[0].tx_id == "test_buy_1"
    assert calculator.transactions[0].type == "buy"


def test_add_transaction_sell():
    """Test adding a sell transaction."""
    class MockConfig:
        def __init__(self):
            self.german_tax_enabled = True
            self.tax_free_allowance = 600.0
            self.withholding_tax_rate = 0.30
            self.min_taxable_gain = 1.0
    
    config = MockConfig()
    calculator = GermanTaxCalculator(config)
    
    # Add a sell transaction
    transaction = Transaction(
        tx_id="test_sell_1",
        type="sell",
        coin="BTC",
        amount=0.5,
        price=60000.0,
        timestamp=datetime(2022, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    calculator.add_transaction(transaction)
    
    # Verify transaction was added
    assert len(calculator.transactions) == 1
    assert calculator.transactions[0].tx_id == "test_sell_1"
    assert calculator.transactions[0].type == "sell"


def test_calculate_acquisition_cost():
    """Test acquisition cost calculation using FIFO method."""
    class MockConfig:
        def __init__(self):
            self.german_tax_enabled = True
            self.tax_free_allowance = 600.0
            self.withholding_tax_rate = 0.30
            self.min_taxable_gain = 1.0
    
    config = MockConfig()
    calculator = GermanTaxCalculator(config)
    
    # Add some buy transactions first
    buy1 = Transaction(
        tx_id="buy_1",
        type="buy",
        coin="BTC",
        amount=1.0,
        price=50000.0,
        timestamp=datetime(2022, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    buy2 = Transaction(
        tx_id="buy_2",
        type="buy",
        coin="BTC",
        amount=0.5,
        price=55000.0,
        timestamp=datetime(2022, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    calculator.add_transaction(buy1)
    calculator.add_transaction(buy2)
    
    # Calculate acquisition cost for 0.75 BTC (should use FIFO)
    cost = calculator._calculate_acquisition_cost("BTC", 0.75)
    
    # Should be 0.5 BTC at 50000 + 0.25 BTC at 55000 = 25000 + 13750 = 38750
    expected_cost = 0.5 * 50000 + 0.25 * 55000
    assert cost == expected_cost


def test_capital_gains_calculation():
    """Test capital gains calculation with tax-free allowance."""
    class MockConfig:
        def __init__(self):
            self.german_tax_enabled = True
            self.tax_free_allowance = 600.0
            self.withholding_tax_rate = 0.30
            self.min_taxable_gain = 1.0
    
    config = MockConfig()
    calculator = GermanTaxCalculator(config)
    
    # Add a buy transaction
    buy_transaction = Transaction(
        tx_id="buy_1",
        type="buy",
        coin="BTC",
        amount=1.0,
        price=50000.0,
        timestamp=datetime(2022, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    # Add a sell transaction
    sell_transaction = Transaction(
        tx_id="sell_1",
        type="sell",
        coin="BTC",
        amount=1.0,
        price=60000.0,
        timestamp=datetime(2022, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    calculator.add_transaction(buy_transaction)
    calculator.add_transaction(sell_transaction)
    
    # Calculate capital gains
    events = calculator.calculate_capital_gains()
    
    assert len(events) == 1
    assert events[0].tx_id == "sell_1"
    assert events[0].capital_gain_loss == 10000.0  # 60000 - 50000


def test_tax_summary_with_allowance():
    """Test tax summary calculation with tax-free allowance applied."""
    class MockConfig:
        def __init__(self):
            self.german_tax_enabled = True
            self.tax_free_allowance = 600.0
            self.withholding_tax_rate = 0.30
            self.min_taxable_gain = 1.0
    
    config = MockConfig()
    calculator = GermanTaxCalculator(config)
    
    # Add a buy transaction
    buy_transaction = Transaction(
        tx_id="buy_1",
        type="buy",
        coin="BTC",
        amount=1.0,
        price=50000.0,
        timestamp=datetime(2022, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    # Add a sell transaction with profit
    sell_transaction = Transaction(
        tx_id="sell_1",
        type="sell",
        coin="BTC",
        amount=1.0,
        price=60000.0,
        timestamp=datetime(2022, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    calculator.add_transaction(buy_transaction)
    calculator.add_transaction(sell_transaction)
    
    # Calculate tax summary
    summary = calculator.calculate_tax_summary()
    
    assert summary['total_gains'] == 10000.0
    assert summary['taxable_income'] == 9400.0  # 10000 - 600 allowance
    assert summary['withholding_tax'] == 2820.0  # 9400 * 0.30


def test_no_tax_due_below_allowance():
    """Test that no tax is due when gains are below the allowance."""
    class MockConfig:
        def __init__(self):
            self.german_tax_enabled = True
            self.tax_free_allowance = 600.0
            self.withholding_tax_rate = 0.30
            self.min_taxable_gain = 1.0
    
    config = MockConfig()
    calculator = GermanTaxCalculator(config)
    
    # Add a buy transaction
    buy_transaction = Transaction(
        tx_id="buy_1",
        type="buy",
        coin="BTC",
        amount=1.0,
        price=50000.0,
        timestamp=datetime(2022, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    # Add a sell transaction with profit below allowance
    sell_transaction = Transaction(
        tx_id="sell_1",
        type="sell",
        coin="BTC",
        amount=1.0,
        price=50500.0,  # Only 500 profit - below allowance
        timestamp=datetime(2022, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    calculator.add_transaction(buy_transaction)
    calculator.add_transaction(sell_transaction)
    
    # Calculate tax summary
    summary = calculator.calculate_tax_summary()
    
    assert summary['total_gains'] == 500.0
    assert summary['taxable_income'] == 0.0  # 500 - 600 = -100, but minimum is 0
    assert summary['withholding_tax'] == 0.0


def test_multiple_transactions():
    """Test with multiple transactions across different years."""
    class MockConfig:
        def __init__(self):
            self.german_tax_enabled = True
            self.tax_free_allowance = 600.0
            self.withholding_tax_rate = 0.30
            self.min_taxable_gain = 1.0
    
    config = MockConfig()
    calculator = GermanTaxCalculator(config)
    
    # Add multiple transactions
    buy1 = Transaction(
        tx_id="buy_1",
        type="buy",
        coin="BTC",
        amount=1.0,
        price=50000.0,
        timestamp=datetime(2022, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    sell1 = Transaction(
        tx_id="sell_1",
        type="sell",
        coin="BTC",
        amount=0.5,
        price=60000.0,
        timestamp=datetime(2022, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    buy2 = Transaction(
        tx_id="buy_2",
        type="buy",
        coin="BTC",
        amount=1.0,
        price=65000.0,
        timestamp=datetime(2023, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    sell2 = Transaction(
        tx_id="sell_2",
        type="sell",
        coin="BTC",
        amount=0.5,
        price=70000.0,
        timestamp=datetime(2023, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    calculator.add_transaction(buy1)
    calculator.add_transaction(sell1)
    calculator.add_transaction(buy2)
    calculator.add_transaction(sell2)
    
    # Calculate tax summary
    summary = calculator.calculate_tax_summary()
    
    assert 'yearly_details' in summary
    assert 2022 in summary['yearly_details']
    assert 2023 in summary['yearly_details']


def test_empty_calculator():
    """Test that empty calculator works correctly."""
    class MockConfig:
        def __init__(self):
            self.german_tax_enabled = True
            self.tax_free_allowance = 600.0
            self.withholding_tax_rate = 0.30
            self.min_taxable_gain = 1.0
    
    config = MockConfig()
    calculator = GermanTaxCalculator(config)
    
    # Should not crash on empty calculator
    summary = calculator.calculate_tax_summary()
    
    assert summary['total_gains'] == 0.0
    assert summary['withholding_tax'] == 0.0


def test_transaction_details():
    """Test that transaction details are properly tracked."""
    class MockConfig:
        def __init__(self):
            self.german_tax_enabled = True
            self.tax_free_allowance = 600.0
            self.withholding_tax_rate = 0.30
            self.min_taxable_gain = 1.0
    
    config = MockConfig()
    calculator = GermanTaxCalculator(config)
    
    # Add a transaction
    transaction = Transaction(
        tx_id="test_1",
        type="buy",
        coin="BTC",
        amount=1.0,
        price=50000.0,
        timestamp=datetime(2022, 1, 1),
        exchange="Kraken",
        fee=0.0
    )
    
    calculator.add_transaction(transaction)
    
    # Verify transaction details
    retrieved = calculator.get_transaction_by_id("test_1")
    assert retrieved is not None
    assert retrieved.tx_id == "test_1"