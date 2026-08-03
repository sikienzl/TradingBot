# Cryptocurrency Trading Bot

A modular cryptocurrency trading bot built with Python that supports multiple exchanges, strategies, and risk management.

## Project Structure

```
trading_bot/
├── src/
│   ├── __init__.py
│   ├── main.py                 # Entry point
│   ├── trading_bot/
│   │   ├── __init__.py
│   │   ├── bot.py              # Main trading bot class
│   │   └── config/
│   │       ├── __init__.py
│   │       ├── schema.py       # Configuration schema
│   │       └── loader.py       # Configuration loader
│   ├── exchange/
│   │   ├── __init__.py
│   │   └── exchange_manager.py # Exchange manager
│   ├── portfolio/
│   │   ├── __init__.py
│   │   └── portfolio_manager.py # Portfolio manager
│   ├── strategy/
│   │   ├── __init__.py
│   │   └── strategy_engine.py  # Strategy engine
│   ├── risk_management/
│   │   ├── __init__.py
│   │   └── risk_manager.py     # Risk management
│   └── data/
│       ├── __init__.py
│       └── fetcher.py          # Data fetcher
├── config.yaml                 # Default configuration
├── requirements.txt            # Dependencies
└── README.md                   # This file
```

## Features

- Modular architecture for easy extension
- Support for multiple exchanges
- Configurable trading strategies
- Risk management capabilities
- Portfolio tracking and management
- Data fetching from various sources

## Installation

1. Clone the repository
2. Install dependencies: `pip install -r requirements.txt`
3. Configure your settings in `config.yaml`
4. Run the bot: `python src/main.py`

## Configuration

The bot uses a centralized configuration system that supports:
- Environment variables
- Configuration files (YAML/JSON)
- Default values

See `config.yaml` for available options.

## Usage

```bash
# Start the trading bot with default config
python src/main.py

# Start the trading bot with custom config file
python src/main.py path/to/config.yaml
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a pull request