"""
Trading Bot Package Initialization
==================================

This package contains all modules for the trading bot system.

Note: intentionally does NOT eagerly import submodules (e.g. `.trading_bot`,
`.models`, `.data`, `.utils`) here. The production bot (`src/trading_bot.py`)
is executed directly as a script and many submodules have heavy or
side-effecting imports (pandas, ccxt, torch, dotenv-based env loading, etc.).
Eager wildcard imports in this file previously caused import-time crashes
for any code doing `from src.<module> import ...` (including the test
suite) whenever any single submodule failed to import.
"""

__version__ = "0.1.0"
__author__ = "Trading Bot Team"
