"""Chronological, rule-only replay of OHLCV candles."""

import argparse
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from src.strategy_rules import rule_recommendation


@dataclass(frozen=True)
class ReplaySettings:
    initial_cash: float = 19.5
    fee_rate: float = 0.002
    slippage: float = 0.001
    rsi_period: int = 14
    sma_short: int = 20
    sma_long: int = 50
    min_entry_score: int = 60
    risk_pct: float = 0.01
    trade_amount: float = 20.0
    min_trade_amount: float = 5.0
    max_open_trades: int = 3
    cash_reserve_pct: float = 0.02
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.10
    max_hold_seconds: int = 120
    min_volume_base: float = 100_000.0
    top_n: int = 10
    recent_pnl_guard_window: int = 120
    recent_pnl_guard_min_trades: int = 60
    recent_pnl_guard_max_profit_factor: float = 1.0
    recent_pnl_guard_min_realized_pnl: float = 0.0
    timeframe_seconds: int = 3600
    excluded_coins: frozenset[str] = frozenset({
        "USDC", "USDT", "EURT", "DAI", "TUSD", "USDP", "FDUSD", "USDE",
        "ZEC", "HYPE", "TON", "BTC", "XRP",
    })

    def validate(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be greater than zero")
        if not 0 <= self.fee_rate < 1 or not 0 <= self.slippage < 1:
            raise ValueError("fee_rate and slippage must be in the range [0, 1)")
        if min(self.rsi_period, self.sma_short, self.sma_long, self.top_n) < 1:
            raise ValueError("indicator periods and top_n must be positive")
        if self.sma_short >= self.sma_long:
            raise ValueError("sma_short must be less than sma_long")
        if self.min_entry_score < 0 or self.trade_amount <= 0 or self.min_trade_amount <= 0:
            raise ValueError("entry score and trade amounts must be valid")
        if not 0 <= self.risk_pct <= 1:
            raise ValueError("risk_pct must be in the range [0, 1]")
        if self.max_open_trades < 1:
            raise ValueError("max_open_trades must be positive")
        if not 0 <= self.cash_reserve_pct < 1:
            raise ValueError("cash_reserve_pct must be in the range [0, 1)")
        if not 0 <= self.stop_loss_pct < 1 or self.take_profit_pct < 0:
            raise ValueError("stop_loss_pct and take_profit_pct must be valid")
        if self.max_hold_seconds < 0 or self.timeframe_seconds < 1:
            raise ValueError("hold time and timeframe must be non-negative/positive")
        if self.min_volume_base < 0:
            raise ValueError("min_volume_base must be non-negative")
        if min(self.recent_pnl_guard_window, self.recent_pnl_guard_min_trades) < 1:
            raise ValueError("recent PnL guard window and minimum trades must be positive")


def load_ohlcv(path: str | Path, settings: ReplaySettings) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"timestamp", "coin", "open", "high", "low", "close", "volume"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(
            f"OHLCV data is missing required columns: {', '.join(sorted(missing))}"
        )

    df = df[list(required)].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    if df["timestamp"].isna().any():
        raise ValueError("OHLCV data contains invalid timestamps")
    if df["coin"].isna().any():
        raise ValueError("OHLCV coin symbols must not be empty")
    df["coin"] = df["coin"].astype(str).str.strip().str.upper()
    if df["coin"].eq("").any():
        raise ValueError("OHLCV coin symbols must not be empty")
    for column in ("open", "high", "low", "close", "volume"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    if not np.isfinite(df[["open", "high", "low", "close", "volume"]].to_numpy()).all():
        raise ValueError("OHLCV data contains invalid numeric values")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("OHLCV prices must be greater than zero")
    if (df["volume"] < 0).any():
        raise ValueError("OHLCV volume must be non-negative")
    if (df["high"] < df[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError("OHLCV high is lower than another price in its candle")
    if (df["low"] > df[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("OHLCV low is higher than another price in its candle")
    if df.duplicated(["timestamp", "coin"]).any():
        raise ValueError("OHLCV data contains duplicate coin/timestamp rows")

    df = df.sort_values(["timestamp", "coin"], kind="stable").reset_index(drop=True)
    # Avoid using an unfinished current candle as an observation or execution bar.
    now = pd.Timestamp.now(tz="UTC")
    current_period_start = pd.Timestamp(
        (now.value // (settings.timeframe_seconds * 1_000_000_000))
        * settings.timeframe_seconds,
        unit="s",
        tz="UTC",
    )
    df = df[df["timestamp"] < current_period_start].copy()
    if df.empty:
        raise ValueError("No completed OHLCV candles remain after validation")

    return _add_rule_signals(df, settings)


def _add_rule_signals(df: pd.DataFrame, settings: ReplaySettings) -> pd.DataFrame:
    parts = []
    for _, coin_df in df.groupby("coin", sort=False):
        coin_df = coin_df.sort_values("timestamp", kind="stable").copy()
        close = coin_df["close"]
        delta = close.diff()
        gains = delta.where(delta > 0, 0.0).rolling(settings.rsi_period).mean()
        losses = (-delta.where(delta < 0, 0.0)).rolling(settings.rsi_period).mean()
        with np.errstate(divide="ignore", invalid="ignore"):
            rsi = 100 - (100 / (1 + gains / losses))
        short = close.rolling(settings.sma_short).mean()
        long = close.rolling(settings.sma_long).mean()
        signals = [
            rule_recommendation(float(r), float(s), float(l), float(price))
            if pd.notna(r) and pd.notna(s) and pd.notna(l)
            else ("HOLD", 50)
            for r, s, l, price in zip(rsi, short, long, close)
        ]
        coin_df["recommendation"] = [signal[0] for signal in signals]
        coin_df["score"] = [signal[1] for signal in signals]
        coin_df["quote_volume_24h"] = (
            (coin_df["close"] * coin_df["volume"])
            .rolling(max(1, round(86_400 / settings.timeframe_seconds)), min_periods=1)
            .sum()
        )
        parts.append(coin_df)
    return pd.concat(parts, ignore_index=True).sort_values(
        ["timestamp", "coin"], kind="stable"
    ).reset_index(drop=True)


class HistoricalRuleBacktester:
    def __init__(self, data: pd.DataFrame, settings: ReplaySettings):
        settings.validate()
        self.data = data
        self.settings = settings
        self.rows_by_time: dict[pd.Timestamp, dict[str, dict]] = {}
        for row in data.to_dict("records"):
            timestamp = row["timestamp"]
            coin = row["coin"]
            self.rows_by_time.setdefault(timestamp, {})[coin] = row

    def run(
        self,
        start_time: pd.Timestamp | None = None,
        end_time: pd.Timestamp | None = None,
        prior_closed_pnls: list[float] | None = None,
    ) -> dict:
        settings = self.settings
        cash = settings.initial_cash
        positions: dict[str, dict] = {}
        trades: list[dict] = []
        equity_curve = [settings.initial_cash]
        closed_pnls = list(prior_closed_pnls or [])
        latest_closes: dict[str, float] = {}
        times = sorted(self.rows_by_time)
        replay_times = [
            timestamp for timestamp in times
            if end_time is None or timestamp <= end_time
        ]
        eligible_times = [
            timestamp for timestamp in replay_times
            if start_time is None or timestamp >= start_time
        ]
        if not eligible_times:
            raise ValueError("No candles fall within the requested replay window")

        previous_signals: dict[str, tuple[str, int]] = {}
        previous_quote_volumes: dict[str, float] = {}
        for timestamp in replay_times:
            market = self.rows_by_time[timestamp]
            current_prices = dict(latest_closes)
            current_prices.update({
                coin: float(row["open"]) for coin, row in market.items()
            })
            active = start_time is None or timestamp >= start_time
            prior_signals = previous_signals

            if active:
                for coin in list(positions):
                    row = market.get(coin)
                    if row is None:
                        continue
                    position = positions[coin]
                    reason = self._open_exit_reason(
                        position, row, prior_signals.get(coin, ("HOLD", 50))[0], timestamp
                    )
                    if reason:
                        exit_price = self._exit_price(position, row, reason)
                        cash += self._close_position(
                            positions, trades, closed_pnls, coin, timestamp, exit_price, reason
                        )

                ranked_market = sorted(
                    (
                        (coin, row)
                        for coin, row in market.items()
                        if coin not in settings.excluded_coins
                        and previous_quote_volumes.get(coin, 0.0) >= settings.min_volume_base
                    ),
                    key=lambda item: previous_quote_volumes.get(item[0], 0.0),
                    reverse=True,
                )[:settings.top_n]
                candidates = [
                    (coin, row, prior_signals[coin][1])
                    for coin, row in ranked_market
                    if coin not in positions
                    and coin in prior_signals
                    and prior_signals[coin][0] in {"BUY", "STRONG BUY"}
                    and prior_signals[coin][1] >= settings.min_entry_score
                ]
                candidates.sort(key=lambda item: item[2], reverse=True)

                guard_active = self._recent_pnl_guard_active(closed_pnls)
                if not guard_active:
                    for coin, row, score in candidates:
                        if len(positions) >= settings.max_open_trades:
                            break
                        available = max(0.0, cash * (1 - settings.cash_reserve_pct))
                        if available < settings.min_trade_amount:
                            break
                        portfolio_value = cash + sum(
                            position["amount"] * current_prices.get(
                                held_coin, position["entry_price"])
                            for held_coin, position in positions.items()
                        )
                        spend = min(
                            portfolio_value * settings.risk_pct,
                            settings.trade_amount,
                            available,
                        )
                        if spend < settings.min_trade_amount and available >= settings.min_trade_amount:
                            spend = settings.min_trade_amount
                        if spend < settings.min_trade_amount:
                            continue
                        fill_price = float(row["open"]) * (1 + settings.slippage)
                        amount = spend / (fill_price * (1 + settings.fee_rate))
                        cash -= spend
                        positions[coin] = {
                            "amount": amount,
                            "entry_price": fill_price,
                            "entry_cost": spend,
                            "entry_time": timestamp,
                            "score": score,
                        }
                        trades.append({
                            "timestamp": timestamp,
                            "coin": coin,
                            "action": "buy",
                            "price": fill_price,
                            "amount_coin": amount,
                            "pnl_base": 0.0,
                            "reason": "strict_buy",
                        })

                for coin in list(positions):
                    row = market.get(coin)
                    if row is None:
                        continue
                    reason = self._intrabar_exit_reason(positions[coin], row)
                    if reason:
                        exit_price = self._exit_price(positions[coin], row, reason)
                        cash += self._close_position(
                            positions, trades, closed_pnls, coin, timestamp, exit_price, reason
                        )

                latest_closes.update({
                    coin: float(row["close"]) for coin, row in market.items()
                })
                marked_value = cash + sum(
                    position["amount"] * latest_closes[coin]
                    * (1 - settings.slippage) * (1 - settings.fee_rate)
                    for coin, position in positions.items()
                    if coin in latest_closes
                )
                equity_curve.append(marked_value)

            for coin, row in market.items():
                previous_signals[coin] = (row["recommendation"], int(row["score"]))
                previous_quote_volumes[coin] = float(row["quote_volume_24h"])

        final_time = eligible_times[-1]
        for coin in list(positions):
            if coin not in latest_closes:
                continue
            final_price = latest_closes[coin] * (1 - settings.slippage)
            cash += self._close_position(
                positions, trades, closed_pnls, coin, final_time, final_price,
                "end_of_data",
            )
        equity_curve[-1] = cash
        return self._summary(trades, equity_curve, cash)

    def _recent_pnl_guard_active(self, closed_pnls: list[float]) -> bool:
        settings = self.settings
        if len(closed_pnls) < settings.recent_pnl_guard_min_trades:
            return False
        recent = closed_pnls[-settings.recent_pnl_guard_window:]
        gross_profit = sum(pnl for pnl in recent if pnl > 0)
        gross_loss = -sum(pnl for pnl in recent if pnl < 0)
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else math.inf
        return (
            sum(recent) < settings.recent_pnl_guard_min_realized_pnl
            and profit_factor <= settings.recent_pnl_guard_max_profit_factor
        )

    def _open_exit_reason(
        self,
        position: dict,
        row: dict,
        recommendation: str,
        timestamp: pd.Timestamp,
    ) -> str | None:
        settings = self.settings
        if recommendation in {"SELL", "WEAK SELL"}:
            return "sell_signal"
        held_seconds = (timestamp - position["entry_time"]).total_seconds()
        if settings.max_hold_seconds and held_seconds >= settings.max_hold_seconds:
            return "max_hold"
        stop = position["entry_price"] * (1 - settings.stop_loss_pct)
        take = position["entry_price"] * (1 + settings.take_profit_pct)
        if settings.stop_loss_pct > 0 and float(row["open"]) <= stop:
            return "stop_loss"
        if settings.take_profit_pct > 0 and float(row["open"]) >= take:
            return "take_profit"
        return None

    def _intrabar_exit_reason(self, position: dict, row: dict) -> str | None:
        settings = self.settings
        stop = position["entry_price"] * (1 - settings.stop_loss_pct)
        take = position["entry_price"] * (1 + settings.take_profit_pct)
        if settings.stop_loss_pct > 0 and float(row["low"]) <= stop:
            return "stop_loss"
        if settings.take_profit_pct > 0 and float(row["high"]) >= take:
            return "take_profit"
        return None

    def _exit_price(self, position: dict, row: dict, reason: str) -> float:
        if reason == "stop_loss":
            threshold = position["entry_price"] * (1 - self.settings.stop_loss_pct)
            raw_price = min(float(row["open"]), threshold)
        elif reason == "take_profit":
            threshold = position["entry_price"] * (1 + self.settings.take_profit_pct)
            raw_price = max(float(row["open"]), threshold)
        else:
            raw_price = float(row["open"])
        return raw_price * (1 - self.settings.slippage)

    def _close_position(
        self,
        positions: dict,
        trades: list[dict],
        closed_pnls: list[float],
        coin: str,
        timestamp: pd.Timestamp,
        exit_price: float,
        reason: str,
    ) -> float:
        position = positions.pop(coin)
        proceeds = (
            position["amount"] * exit_price * (1 - self.settings.fee_rate)
        )
        pnl = proceeds - position["entry_cost"]
        closed_pnls.append(pnl)
        trades.append({
            "timestamp": timestamp,
            "coin": coin,
            "action": "sell",
            "price": exit_price,
            "amount_coin": position["amount"],
            "pnl_base": pnl,
            "reason": reason,
        })
        return proceeds

    def _summary(self, trades: list[dict], equity: list[float], ending_cash: float) -> dict:
        sells = [trade for trade in trades if trade["action"] == "sell"]
        wins = [trade["pnl_base"] for trade in sells if trade["pnl_base"] > 0]
        losses = [-trade["pnl_base"] for trade in sells if trade["pnl_base"] < 0]
        gross_loss = sum(losses)
        peak = equity[0]
        max_drawdown = 0.0
        for value in equity:
            peak = max(peak, value)
            if peak:
                max_drawdown = min(max_drawdown, (value - peak) / peak)
        return {
            "start_cash": self.settings.initial_cash,
            "end_cash": ending_cash,
            "pnl": ending_cash - self.settings.initial_cash,
            "return_pct": (ending_cash / self.settings.initial_cash - 1) * 100,
            "closed_trades": len(sells),
            "win_rate_pct": len(wins) / len(sells) * 100 if sells else 0.0,
            "profit_factor": sum(wins) / gross_loss if gross_loss else math.inf,
            "max_drawdown_pct": max_drawdown * 100,
            "trades": trades,
            "equity_curve": equity,
        }


def _print_result(label: str, result: dict, base_currency: str) -> None:
    profit_factor = result["profit_factor"]
    factor_text = "inf" if math.isinf(profit_factor) else f"{profit_factor:.3f}"
    print(
        f"{label}: closed={result['closed_trades']} "
        f"win_rate={result['win_rate_pct']:.1f}% "
        f"PnL={result['pnl']:+.4f} {base_currency} "
        f"return={result['return_pct']:+.2f}% "
        f"profit_factor={factor_text} "
        f"max_drawdown={result['max_drawdown_pct']:.2f}%"
    )
    if result["closed_trades"] < 20:
        print("  Small sample (<20 closed trades); descriptive only.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay the bot's shared rule signals on historical OHLCV data."
    )
    parser.add_argument("--file", default="full_crypto_data.csv")
    parser.add_argument("--base-currency", default="EUR")
    parser.add_argument("--initial-cash", type=float, default=19.5)
    parser.add_argument("--fee-rate", type=float, default=0.002)
    parser.add_argument("--slippage", type=float, default=0.001)
    parser.add_argument("--rsi-period", type=int, default=14)
    parser.add_argument("--sma-short", type=int, default=20)
    parser.add_argument("--sma-long", type=int, default=50)
    parser.add_argument("--min-entry-score", type=int, default=60)
    parser.add_argument("--risk-pct", type=float, default=0.01)
    parser.add_argument("--trade-amount", type=float, default=20.0)
    parser.add_argument("--min-trade-amount", type=float, default=5.0)
    parser.add_argument("--max-open-trades", type=int, default=3)
    parser.add_argument("--cash-reserve-pct", type=float, default=0.02)
    parser.add_argument("--stop-loss-pct", type=float, default=0.05)
    parser.add_argument("--take-profit-pct", type=float, default=0.10)
    parser.add_argument("--max-hold-seconds", type=int, default=120)
    parser.add_argument("--min-volume-base", type=float, default=100_000.0)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--recent-pnl-guard-window", type=int, default=120)
    parser.add_argument("--recent-pnl-guard-min-trades", type=int, default=60)
    parser.add_argument("--recent-pnl-guard-min-realized-pnl", type=float, default=0.0)
    parser.add_argument("--recent-pnl-guard-max-profit-factor", type=float, default=1.0)
    parser.add_argument("--timeframe-seconds", type=int, default=3600)
    parser.add_argument("--split", type=float, default=0.7)
    parser.add_argument("--excluded-coins", default="USDC,USDT,EURT,DAI,TUSD,USDP,FDUSD,USDE,ZEC,HYPE,TON,BTC,XRP")
    args = parser.parse_args()

    try:
        settings = ReplaySettings(
            initial_cash=args.initial_cash,
            fee_rate=args.fee_rate,
            slippage=args.slippage,
            rsi_period=args.rsi_period,
            sma_short=args.sma_short,
            sma_long=args.sma_long,
            min_entry_score=args.min_entry_score,
            risk_pct=args.risk_pct,
            trade_amount=args.trade_amount,
            min_trade_amount=args.min_trade_amount,
            max_open_trades=args.max_open_trades,
            cash_reserve_pct=args.cash_reserve_pct,
            stop_loss_pct=args.stop_loss_pct,
            take_profit_pct=args.take_profit_pct,
            max_hold_seconds=args.max_hold_seconds,
            min_volume_base=args.min_volume_base,
            top_n=args.top_n,
            recent_pnl_guard_window=args.recent_pnl_guard_window,
            recent_pnl_guard_min_trades=args.recent_pnl_guard_min_trades,
            recent_pnl_guard_min_realized_pnl=args.recent_pnl_guard_min_realized_pnl,
            recent_pnl_guard_max_profit_factor=args.recent_pnl_guard_max_profit_factor,
            timeframe_seconds=args.timeframe_seconds,
            excluded_coins=frozenset(
                coin.strip().upper() for coin in args.excluded_coins.split(",")
                if coin.strip()
            ),
        )
        settings.validate()
        if not 0 < args.split < 1:
            raise ValueError("split must be between 0 and 1")
        data = load_ohlcv(args.file, settings)
        timestamps = sorted(data["timestamp"].unique())
        if len(timestamps) < 2:
            raise ValueError("At least two distinct candle timestamps are required")
        split_index = min(max(1, int(len(timestamps) * args.split)), len(timestamps) - 1)
        cutoff = pd.Timestamp(timestamps[split_index])
        first = pd.Timestamp(timestamps[0])
        last = pd.Timestamp(timestamps[-1])
        latest_age_hours = (pd.Timestamp.now(tz="UTC") - last).total_seconds() / 3600

        print(
            f"OHLCV: {len(data)} rows, {data['coin'].nunique()} coins, "
            f"{first.isoformat()} to {last.isoformat()}"
        )
        print(f"Latest completed candle age: {latest_age_hours:.1f} hours")
        if latest_age_hours > 24:
            print("WARNING: Data is older than 24 hours; do not treat it as current market data.")
        print(
            "Replay settings: rule-only, strict BUY entries, "
            f"fee/side={settings.fee_rate:.3%}, slippage/side={settings.slippage:.3%}, "
            f"split={args.split:.0%}"
        )
        print(
            f"Indicators: RSI={settings.rsi_period}, SMA={settings.sma_short}/"
            f"{settings.sma_long}; entry score>={settings.min_entry_score}; "
            f"risk={settings.risk_pct:.2%}, trade cap={settings.trade_amount:.2f} "
            f"{args.base_currency}, minimum={settings.min_trade_amount:.2f}; "
            f"24h volume>={settings.min_volume_base:.0f}, top_n={settings.top_n}; "
            f"guard={settings.recent_pnl_guard_min_trades}/"
            f"{settings.recent_pnl_guard_window} trades, "
            f"pnl<{settings.recent_pnl_guard_min_realized_pnl:.4f} and "
            f"PF<={settings.recent_pnl_guard_max_profit_factor:.3f}"
        )
        print(
            f"Training range: {first.isoformat()} to "
            f"{(cutoff - pd.Timedelta(nanoseconds=1)).isoformat()}; "
            f"holdout starts: {cutoff.isoformat()}"
        )
        print(
            "Approximation limits: journal-driven dynamic exclusions, fallback/"
            "force-fill entries, optional ML models, other runtime guards, "
            "portfolio tiers, ATR/trailing/partial exits, and exchange-specific "
            "fills are not modeled."
        )
        if settings.max_hold_seconds and settings.max_hold_seconds < settings.timeframe_seconds:
            print(
                "WARNING: max-hold time is shorter than one candle; time exits "
                "are quantized to the next candle and are only approximate."
            )

        backtester = HistoricalRuleBacktester(data, settings)
        training = backtester.run(end_time=cutoff - pd.Timedelta(nanoseconds=1))
        prior_guard_pnls = [
            trade["pnl_base"]
            for trade in training["trades"]
            if trade["action"] == "sell" and trade["reason"] != "end_of_data"
        ]
        holdout = backtester.run(
            start_time=cutoff,
            prior_closed_pnls=prior_guard_pnls,
        )
        print(f"Holdout PnL-guard history: {len(prior_guard_pnls)} training closes")
        _print_result("Training", training, args.base_currency)
        _print_result("Chronological holdout", holdout, args.base_currency)
        print("Offline results are not a profitability forecast or live-trading recommendation.")
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
