"""Backtesting harness for the trading strategy on historical CSV data.

Extracted from ``src.trading_bot`` so the backtester can be tested and reused
without importing the full trading bot.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class Backtester:
    """Backtesting module for the trading strategy on historical data."""

    def __init__(
        self,
        data_file="training_data.csv",
        initial_cash=10000,
        base_currency="EUR",
        fee_per_trade=0.002,
        slippage=0.001,
    ):
        if initial_cash <= 0:
            raise ValueError("initial_cash must be greater than zero")
        if not 0 <= fee_per_trade < 1:
            raise ValueError("fee_per_trade must be in the range [0, 1)")
        if not 0 <= slippage < 1:
            raise ValueError("slippage must be in the range [0, 1)")
        self.data_file = data_file
        self.initial_cash = initial_cash
        self.base_currency = base_currency
        self.fee_per_trade = fee_per_trade
        self.slippage = slippage
        self.trades = []
        self.equity_curve = []

    def run(self, strategy_func, **kwargs):
        df = pd.read_csv(self.data_file)
        required = {"coin", "timestamp", "close"}
        missing = required.difference(df.columns)
        if missing:
            raise ValueError(
                f"Backtest data is missing required columns: {', '.join(sorted(missing))}"
            )
        df["timestamp"] = pd.to_datetime(
            df["timestamp"], utc=True, errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        if df["timestamp"].isna().any() or df["close"].isna().any():
            raise ValueError("Backtest data contains invalid timestamps or close prices")
        if (df["close"] <= 0).any():
            raise ValueError("Backtest close prices must be greater than zero")
        if df["coin"].isna().any() or df["coin"].astype(str).str.strip().eq("").any():
            raise ValueError("Backtest coin symbols must not be empty")
        df["coin"] = df["coin"].astype(str)
        if df.duplicated(["timestamp", "coin"]).any():
            raise ValueError("Backtest data contains duplicate coin/timestamp rows")
        df = df.sort_values(
            ["timestamp", "coin"], kind="stable").reset_index(drop=True)

        cash = self.initial_cash
        holdings: dict[str, float] = {}
        latest_prices: dict[str, float] = {}
        previous_rows: dict[str, pd.Series] = {}
        self.equity_curve = [self.initial_cash]
        self.trades = []

        for timestamp, timestamp_rows in df.groupby("timestamp", sort=True):
            rows = list(timestamp_rows.iterrows())
            for _, row in rows:
                latest_prices[row["coin"]] = float(row["close"])

            for _, row in rows:
                coin = row["coin"]
                previous = previous_rows.get(coin)
                if previous is not None:
                    signal = strategy_func(previous, **kwargs)
                    close = float(row["close"])
                    if signal == "kaufen" and cash > 0:
                        fill_price = close * (1 + self.slippage)
                        amount = cash / \
                            (fill_price * (1 + self.fee_per_trade))
                        holdings[coin] = holdings.get(coin, 0.0) + amount
                        self.trades.append(
                            (timestamp, coin, "buy", fill_price, amount))
                        cash = 0.0
                    elif signal == "verkaufen" and holdings.get(coin, 0) > 0:
                        amount = holdings[coin]
                        fill_price = close * (1 - self.slippage)
                        cash += amount * fill_price * \
                            (1 - self.fee_per_trade)
                        self.trades.append(
                            (timestamp, coin, "sell", fill_price, amount))
                        holdings[coin] = 0.0
                previous_rows[coin] = row

            liquidation_factor = (1 - self.slippage) * \
                (1 - self.fee_per_trade)
            equity = cash + sum(
                amount * latest_prices[coin] * liquidation_factor
                for coin, amount in holdings.items()
            )
            self.equity_curve.append(equity)

        self._report()

    def _report(self):
        if not self.equity_curve:
            print("No backtest run.")
            return
        total_return = (
            self.equity_curve[-1] - self.initial_cash) / self.initial_cash * 100
        max_drawdown = self._max_drawdown(self.equity_curve)
        print("\nBacktest report:")
        print(
            f"  Starting capital: {self.initial_cash:.2f} {self.base_currency}")
        print(
            f"  End capital:   {self.equity_curve[-1]:.2f} {self.base_currency}")
        print(f"  Total return: {total_return:.2f}%")
        print(f"  Max drawdown:  {max_drawdown:.2f}%")
        print(
            f"  Trade events:  {len(self.trades)} "
            f"(estimated transaction cost per side: "
            f"{(self.fee_per_trade + self.slippage) * 100:.3f}%)"
        )

    def _max_drawdown(self, curve):
        curve = np.array(curve)
        highwater = np.maximum.accumulate(curve)
        drawdowns = (curve - highwater) / highwater
        return drawdowns.min() * 100


def simple_strategy(row, rsi_buy=30, rsi_sell=70):
    # Example strategy: RSI-based buy/sell signals
    if "rsi" in row:
        if row["rsi"] < rsi_buy:
            return "kaufen"
        if row["rsi"] > rsi_sell:
            return "verkaufen"
    return "halten"
