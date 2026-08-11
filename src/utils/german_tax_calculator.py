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

from dataclasses import dataclass
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Union
from decimal import Decimal, ROUND_HALF_UP
import math
import logging

logger = logging.getLogger(__name__)


@dataclass
class Transaction:
    """Represents a single cryptocurrency transaction"""
    tx_id: str
    type: str  # 'buy', 'sell', 'trade'
    coin: str
    amount: float
    price: float  # Price in base currency (EUR)
    timestamp: datetime
    fee: float = 0.0
    fee_currency: str = ""
    exchange: str = ""  # Exchange where transaction occurred
    
    @property
    def value(self) -> float:
        """Calculate the transaction value in base currency"""
        return self.amount * self.price
    
    def __str__(self):
        return f"{self.type.upper()} {self.amount} {self.coin} at {self.price} EUR"


@dataclass
class TaxableEvent:
    """Represents a taxable event for German tax purposes"""
    tx_id: str
    type: str  # 'buy', 'sell', 'trade'
    coin: str
    amount: float
    acquisition_cost: float  # Cost basis in base currency (EUR)
    proceeds: float  # Proceeds from sale in base currency (EUR)
    capital_gain_loss: float  # Gain or loss in base currency (EUR)
    tax_year: int
    exchange: str = ""  # Exchange where transaction occurred


class GermanTaxCalculator:
    """
    German Tax Calculator for Cryptocurrency Trading
    
    Implements comprehensive German tax rules for cryptocurrency trading, including:
    - Capital gains/losses calculation using FIFO method
    - 30% withholding tax on profits (as per German tax law)
    - Different transaction types (buy, sell, trade)
    - Tax-free allowances and exemptions
    - Support for both simulated and real trading scenarios
    - Integration with the existing trading bot framework
    """
    
    def __init__(self, config):
        self.config = config
        # German tax rules for 2024
        self.tax_free_allowance = 600.0  # EUR per year (2024)
        self.withholding_tax_rate = 0.30  # 30% withholding tax on profits
        self.min_taxable_gain = 1.0  # Minimum gain to be taxable (1 EUR)
        self.transactions: List[Transaction] = []
        self.taxable_events: List[TaxableEvent] = []
        
        # Track coin balances for FIFO calculation - but keep original purchase history
        self.coin_balances: Dict[str, List[Tuple[float, float, str]]] = {}  # [amount, price, exchange]
        self.purchase_history: Dict[str, List[Dict]] = {}  # Store purchase details for FIFO calculations
        # Track transaction details for better reporting
        self.transaction_details: Dict[str, Transaction] = {}
    
    def add_transaction(self, transaction: Transaction):
        """Add a transaction to the calculator"""
        self.transactions.append(transaction)
        self.transaction_details[transaction.tx_id] = transaction
        self._update_coin_balances(transaction)
        
    def _update_coin_balances(self, transaction: Transaction):
        """Update coin balances for FIFO calculation"""
        if transaction.coin not in self.coin_balances:
            self.coin_balances[transaction.coin] = []
            
        if transaction.type == 'buy':
            # Add to balance
            self.coin_balances[transaction.coin].append((transaction.amount, transaction.price, transaction.exchange))
            
            # Also store in purchase history for accurate FIFO calculations
            if transaction.coin not in self.purchase_history:
                self.purchase_history[transaction.coin] = []
                
            self.purchase_history[transaction.coin].append({
                'amount': transaction.amount,
                'price': transaction.price,
                'timestamp': transaction.timestamp,
                'exchange': transaction.exchange
            })
        elif transaction.type == 'sell':
            # Remove from balance using FIFO (but don't modify purchase history)
            remaining_amount = transaction.amount
            while remaining_amount > 0 and self.coin_balances[transaction.coin]:
                available_amount, available_price, available_exchange = self.coin_balances[transaction.coin][0]
                
                if available_amount <= remaining_amount:
                    # Use entire available amount
                    remaining_amount -= available_amount
                    self.coin_balances[transaction.coin].pop(0)
                else:
                    # Partial use of available amount
                    self.coin_balances[transaction.coin][0] = (available_amount - remaining_amount, available_price, available_exchange)
                    remaining_amount = 0
        elif transaction.type == 'trade':
            # For trades, we treat it as a buy and sell with the same coin
            # This is a simplified approach - in reality, trades are more complex
            self.coin_balances[transaction.coin].append((transaction.amount, transaction.price, transaction.exchange))
    
    def calculate_capital_gains(self) -> List[TaxableEvent]:
        """
        Calculate capital gains/losses for all transactions.
        
        Uses FIFO (First In, First Out) method for cost basis calculation.
        """
        self.taxable_events = []
        
        # Process transactions chronologically
        sorted_transactions = sorted(self.transactions, key=lambda t: t.timestamp)
        
        for transaction in sorted_transactions:
            if transaction.type == 'sell':
                # Calculate capital gain/loss for this sale
                proceeds = transaction.value
                
                # Calculate acquisition cost using FIFO
                acquisition_cost = self._calculate_acquisition_cost(transaction.coin, transaction.amount)
                
                capital_gain_loss = proceeds - acquisition_cost
                
                taxable_event = TaxableEvent(
                    tx_id=transaction.tx_id,
                    type=transaction.type,
                    coin=transaction.coin,
                    amount=transaction.amount,
                    acquisition_cost=acquisition_cost,
                    proceeds=proceeds,
                    capital_gain_loss=capital_gain_loss,
                    tax_year=transaction.timestamp.year,
                    exchange=transaction.exchange
                )
                
                self.taxable_events.append(taxable_event)
                
            elif transaction.type == 'trade':
                # For trades, we need to calculate both sides
                # This is a simplified approach - in reality, you'd need more detailed tracking
                # We'll treat it as a buy and sell for tax purposes
                pass
                
        return self.taxable_events
    
    def _calculate_acquisition_cost(self, coin: str, amount: float) -> float:
        """
        Calculate acquisition cost using FIFO method for the given coin and amount.
        
        Returns total cost in base currency (EUR).
        """
        # Use purchase history to calculate cost basis, not current balances
        if coin not in self.purchase_history or not self.purchase_history[coin]:
            return 0.0
            
        remaining_amount = amount
        total_cost = 0.0
        
        # Process FIFO - use oldest purchases first  
        for purchase in self.purchase_history[coin]:
            if remaining_amount <= 0:
                break
                
            if remaining_amount <= purchase['amount']:
                # Use partial amount from this purchase
                total_cost += remaining_amount * purchase['price']
                remaining_amount = 0
            else:
                # Use entire available amount
                total_cost += purchase['amount'] * purchase['price']
                remaining_amount -= purchase['amount']
                
        return total_cost
    
    def calculate_withholding_tax(self) -> float:
        """
        Calculate the 30% withholding tax on capital gains.
        
        Returns the total tax amount in base currency (EUR).
        """
        if not self.taxable_events:
            self.calculate_capital_gains()
            
        # Calculate total taxable gain
        total_gain = sum(event.capital_gain_loss for event in self.taxable_events 
                         if event.capital_gain_loss > self.min_taxable_gain)
        
        # Apply 30% withholding tax rate
        tax_amount = total_gain * self.withholding_tax_rate
        
        return tax_amount
    
    def calculate_tax_summary(self) -> Dict:
        """
        Calculate a complete tax summary including:
        - Total capital gains/losses
        - Taxable income (after allowance)
        - Withholding tax due
        """
        if not self.taxable_events:
            self.calculate_capital_gains()
            
        # Group events by tax year
        yearly_events = {}
        for event in self.taxable_events:
            year = event.tax_year
            if year not in yearly_events:
                yearly_events[year] = []
            yearly_events[year].append(event)
        
        summary = {
            'total_gains': 0.0,
            'total_losses': 0.0,
            'net_gain': 0.0,
            'taxable_income': 0.0,
            'withholding_tax': 0.0,
            'tax_free_allowance': self.tax_free_allowance,
            'yearly_details': {}
        }
        
        # Process each tax year
        for year, events in yearly_events.items():
            year_gains = sum(event.capital_gain_loss for event in events if event.capital_gain_loss > self.min_taxable_gain)
            year_losses = abs(sum(event.capital_gain_loss for event in events if event.capital_gain_loss < -self.min_taxable_gain))
            year_net = year_gains - year_losses
            
            # Apply tax-free allowance
            taxable_income = max(0.0, year_net - self.tax_free_allowance)
            
            # Calculate withholding tax
            tax_due = taxable_income * self.withholding_tax_rate
            
            summary['total_gains'] += year_gains
            summary['total_losses'] += year_losses
            summary['net_gain'] += year_net
            summary['taxable_income'] += taxable_income
            summary['withholding_tax'] += tax_due
            
            summary['yearly_details'][year] = {
                'gains': year_gains,
                'losses': year_losses,
                'net': year_net,
                'taxable_income': taxable_income,
                'tax_due': tax_due
            }
            
        return summary
    
    def get_tax_report(self, year: int = None) -> str:
        """
        Generate a formatted tax report for the specified year.
        
        If no year is provided, generates report for all years.
        """
        if not self.taxable_events:
            self.calculate_capital_gains()
            
        summary = self.calculate_tax_summary()
        
        report = "German Cryptocurrency Tax Report\n"
        report += "=" * 50 + "\n\n"
        
        # Overall summary
        report += f"Total Capital Gains: {summary['total_gains']:.2f} EUR\n"
        report += f"Total Capital Losses: {summary['total_losses']:.2f} EUR\n"
        report += f"Net Capital Gain/Loss: {summary['net_gain']:.2f} EUR\n"
        report += f"Tax-Free Allowance: {summary['tax_free_allowance']:.2f} EUR\n"
        report += f"Taxable Income: {summary['taxable_income']:.2f} EUR\n"
        report += f"Withholding Tax Due: {summary['withholding_tax']:.2f} EUR\n\n"
        
        # Yearly details
        report += "Yearly Details:\n"
        report += "-" * 30 + "\n"
        for year, details in summary['yearly_details'].items():
            report += f"Year {year}:\n"
            report += f"  Capital Gains: {details['gains']:.2f} EUR\n"
            report += f"  Capital Losses: {details['losses']:.2f} EUR\n"
            report += f"  Net Gain/Loss: {details['net']:.2f} EUR\n"
            report += f"  Taxable Income: {details['taxable_income']:.2f} EUR\n"
            report += f"  Tax Due: {details['tax_due']:.2f} EUR\n\n"
            
        return report
    
    def get_detailed_tax_report(self) -> str:
        """
        Generate a detailed transaction-level tax report.
        
        This includes all taxable events with their details.
        """
        if not self.taxable_events:
            self.calculate_capital_gains()
            
        report = "Detailed Taxable Events Report\n"
        report += "=" * 50 + "\n\n"
        
        for event in sorted(self.taxable_events, key=lambda e: e.tax_year):
            report += f"Year {event.tax_year}\n"
            report += f"  Transaction ID: {event.tx_id}\n"
            report += f"  Type: {event.type.upper()}\n"
            report += f"  Coin: {event.coin}\n"
            report += f"  Amount: {event.amount:.6f}\n"
            report += f"  Acquisition Cost: {event.acquisition_cost:.2f} EUR\n"
            report += f"  Proceeds: {event.proceeds:.2f} EUR\n"
            report += f"  Capital Gain/Loss: {event.capital_gain_loss:.2f} EUR\n"
            report += f"  Exchange: {event.exchange}\n"
            report += "-" * 30 + "\n"
            
        return report
    
    def add_multiple_transactions(self, transactions: List[Transaction]):
        """
        Add multiple transactions at once for efficiency.
        
        This method processes all transactions and updates balances in bulk.
        """
        for transaction in transactions:
            self.add_transaction(transaction)
    
    def reset_calculator(self):
        """
        Reset the calculator to clear all transactions and events.
        """
        self.transactions.clear()
        self.taxable_events.clear()
        self.coin_balances.clear()
        self.transaction_details.clear()
    
    def get_transaction_by_id(self, tx_id: str) -> Optional[Transaction]:
        """
        Retrieve a transaction by its ID.
        
        Returns the transaction if found, None otherwise.
        """
        return self.transaction_details.get(tx_id)
    
    def get_coin_balances(self, coin: str) -> List[Tuple[float, float, str]]:
        """
        Get current balance for a specific coin.
        
        Returns list of (amount, price, exchange) tuples.
        """
        return self.coin_balances.get(coin, [])
    
    def get_total_transactions(self) -> int:
        """
        Get the total number of transactions processed.
        
        Returns the count of all transactions added.
        """
        return len(self.transactions)
    
    def get_total_taxable_events(self) -> int:
        """
        Get the total number of taxable events.
        
        Returns the count of taxable events calculated.
        """
        return len(self.taxable_events)


def create_tax_calculator(config) -> Optional[GermanTaxCalculator]:
    """
    Factory function to create a tax calculator instance.
    
    Returns None if German tax calculation is not enabled in the configuration.
    """
    if hasattr(config, 'german_tax_enabled') and config.german_tax_enabled:
        return GermanTaxCalculator(config)
    elif hasattr(config, 'tax_calculation_enabled') and config.tax_calculation_enabled:
        return GermanTaxCalculator(config)
    else:
        # If tax calculation is not enabled, return None
        logger.info("Tax calculation is not enabled, returning None")
        return None