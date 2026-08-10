# Centralized Configuration System

This document explains how to use the centralized configuration system in the trading bot.

## Overview

The trading bot uses a unified configuration system that supports multiple sources and provides a consistent interface for all components. This approach ensures that all modules can access configuration settings in a standardized way, making the system more maintainable and flexible.

## Configuration Sources

Configuration values are loaded in the following order of precedence (highest to lowest):
1. **Environment Variables** - Highest priority
2. **Configuration Files** - YAML or JSON format
3. **Default Values** - Built-in fallbacks

## File Structure

The configuration is organized into logical sections:

```yaml
# Core settings
environment: "development"
simulate_data: false

# Exchange configuration
exchange:
  name: "kraken"
  api_key: ""
  api_secret: ""
  sandbox_mode: false
  base_currency: "EUR"
  excluded_coins:
    - "USDC"
    - "USDT"
    - "EURT"
  max_concurrent_requests: 10
  request_timeout_seconds: 30
  retry_attempts: 3
  retry_delay_seconds: 1.0

# Trading configuration
trading:
  trade_amount: 10.0
  min_trade_amount: 3.0
  max_open_trades: 4
  allow_partial_trades: true
  cash_reserve_pct: 0.02
  risk_per_trade: 0.01
  max_portfolio_risk: 0.10

# Data configuration
data:
  data_frequency: "1h"
  lookback_period: 100
  enable_sentiment_data: true
  min_volume_base: 100000.0
  top_n_for_analysis: 12
  ticker_batch_size: 80

# Model configuration
model:
  use_ml_model: true
  model_path: null
  model_type: "catboost"
  enable_online_learning: false

# Logging configuration
logging:
  log_level: "INFO"
  log_file: null
  enable_console_logging: true
  enable_file_logging: false

# Advanced configuration
advanced:
  max_concurrent_tasks: 10
  task_timeout_seconds: 300
  enable_hailo_integration: true
```

## Usage Examples

### Basic Usage
```python
from src.config.loader import load_config

# Load configuration with defaults
config = load_config()

# Load configuration from a specific file
config = load_config("config.yaml")
```

### Accessing Configuration Values
```python
# Access exchange settings
exchange_name = config.exchange.name
api_key = config.exchange.api_key

# Access trading parameters
trade_amount = config.trading.trade_amount
max_open_trades = config.trading.max_open_trades

# Access data settings
data_frequency = config.data.data_frequency
enable_sentiment = config.data.enable_sentiment_data
```

## Environment Variables

Environment variables can be used to override configuration values. They follow the pattern:

```
TRADING_BOT_EXCHANGE_API_KEY=your_api_key_here
TRADING_BOT_TRADING_TRADE_AMOUNT=25.0
TRADING_BOT_DATA_ENABLE_SENTIMENT_DATA=true
TRADING_BOT_GERMAN_TAX_ENABLED=true
```

### Available Configuration Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GERMAN_TAX_ENABLED` | Enable German tax calculation rules (international compatibility) | `false` |

## Best Practices

1. **Always use `load_config()`** - Instead of directly instantiating configuration classes
2. **Validate configuration values** - Use validation functions to ensure values are within expected ranges
3. **Document configuration parameters** - Add comments to explain what each parameter does
4. **Use environment-specific files** - Create separate config files for different environments (dev, test, prod)
5. **Keep sensitive data out of version control** - Use environment variables or encrypted config files

## Example Configuration Files

### Development Environment
```yaml
environment: "development"
simulate_data: true
exchange:
  name: "kraken"
  sandbox_mode: true
  base_currency: "EUR"
trading:
  trade_amount: 5.0
  max_open_trades: 2
  enable_dry_run: true
```

### Production Environment
```yaml
environment: "production"
simulate_data: false
exchange:
  name: "kraken"
  sandbox_mode: false
  base_currency: "USD"
trading:
  trade_amount: 10.0
  max_open_trades: 4
  enable_dry_run: false
```