"""Initialize a new isolated paper portfolio from Kraken's free EUR balance."""

import argparse
import os
from pathlib import Path

from dotenv import dotenv_values

from src.trading_bot import BotConfig, CryptoTradingBot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", required=True)
    args = parser.parse_args()

    state_path = Path(".portfolio_state.json")
    if os.path.lexists(state_path):
        raise SystemExit("Portfolio state already exists; refusing to overwrite it")

    credentials = dotenv_values(args.credentials_file)
    key = credentials.get("KRAKEN_API_KEY")
    secret = credentials.get("KRAKEN_API_SECRET")
    if not key or not secret:
        raise SystemExit("Credentials file must contain KRAKEN_API_KEY and KRAKEN_API_SECRET")

    os.environ.update({
        "KRAKEN_API_KEY": key,
        "KRAKEN_API_SECRET": secret,
        "DRY_RUN": "true",
        "SIMULATE_DATA": "true",
        "SIMULATION_INITIAL_BALANCE_FROM_KRAKEN": "true",
        "EXCHANGE_NAME": "kraken",
        "BASE_CURRENCY": "EUR",
        "USE_ML_MODEL": "false",
        "USE_TABULAR_MODEL": "false",
        "ANALYTICS_DB_ENABLED": "false",
        "PERFORMANCE_LOG_ENABLED": "false",
    })
    bot = CryptoTradingBot(BotConfig())
    try:
        bot._update_portfolio_balance()
    finally:
        bot.analytics_writer.close()


if __name__ == "__main__":
    main()
