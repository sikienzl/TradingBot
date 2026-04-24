import argparse
from dataclasses import dataclass
from typing import List

import numpy as np
import pandas as pd


@dataclass
class ScorecardResult:
    verdict: str
    reasons: List[str]


def _safe_float(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0.0)


def _max_drawdown_base(equity_curve: pd.Series) -> float:
    if equity_curve.empty:
        return 0.0
    running_max = equity_curve.cummax()
    drawdown = equity_curve - running_max
    return float(drawdown.min()) if not drawdown.empty else 0.0


def _evaluate_verdict(
    closed_trades: int,
    min_closed_trades: int,
    realized_pnl: float,
    win_rate: float,
    min_win_rate: float,
    profit_factor: float,
    min_profit_factor: float,
    avg_pnl: float,
    min_avg_pnl: float,
    max_drawdown_pct: float,
    max_allowed_drawdown_pct: float,
) -> ScorecardResult:
    reasons: List[str] = []

    # Hard no-go conditions
    if closed_trades < max(1, int(min_closed_trades * 0.5)):
        reasons.append(
            f"Too few closed trades: {closed_trades} < {max(1, int(min_closed_trades * 0.5))} (Hard-Fail)"
        )
    if realized_pnl <= 0:
        reasons.append(
            f"Realized PnL not positive: {realized_pnl:.6f} (Hard-Fail)")
    if profit_factor < 1.0:
        reasons.append(
            f"Profit factor below 1.0: {profit_factor:.4f} (Hard-Fail)")
    if max_drawdown_pct > max_allowed_drawdown_pct * 1.5:
        reasons.append(
            f"Max drawdown significantly too high: {max_drawdown_pct:.2f}% > {max_allowed_drawdown_pct * 1.5:.2f}% (Hard-Fail)"
        )

    if reasons:
        return ScorecardResult(verdict="NO-GO", reasons=reasons)

    soft_fails: List[str] = []
    if closed_trades < min_closed_trades:
        soft_fails.append(
            f"Trade count still too low: {closed_trades} < {min_closed_trades}")
    if win_rate < min_win_rate:
        soft_fails.append(
            f"Win rate too low: {win_rate:.2f}% < {min_win_rate:.2f}%")
    if profit_factor < min_profit_factor:
        soft_fails.append(
            f"Profit factor too low: {profit_factor:.4f} < {min_profit_factor:.4f}")
    if avg_pnl < min_avg_pnl:
        soft_fails.append(
            f"Avg PnL/trade too low: {avg_pnl:.6f} < {min_avg_pnl:.6f}")
    if max_drawdown_pct > max_allowed_drawdown_pct:
        soft_fails.append(
            f"Max drawdown too high: {max_drawdown_pct:.2f}% > {max_allowed_drawdown_pct:.2f}%")

    if soft_fails:
        return ScorecardResult(verdict="HOLD", reasons=soft_fails)

    return ScorecardResult(verdict="GO", reasons=["All defined scorecard criteria met."])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Go/No-Go scorecard based on the trade journal")
    parser.add_argument("--file", default="trade_journal.csv",
                        help="Path to the journal file")
    parser.add_argument("--base-currency", default="EUR",
                        help="Display base currency")
    parser.add_argument("--lookback-days", type=int, default=0,
                        help="Evaluate only trades from the last N days (0 = all)")
    parser.add_argument("--starting-capital", type=float, default=20.0,
                        help="Starting capital in base currency for drawdown percentage")

    # Scorecard thresholds
    parser.add_argument("--min-closed-trades", type=int, default=200,
                        help="Minimum number of closed trades for GO")
    parser.add_argument("--min-win-rate", type=float, default=45.0,
                        help="Minimum win rate in percent")
    parser.add_argument("--min-profit-factor", type=float, default=1.2,
                        help="Minimum profit factor")
    parser.add_argument("--min-avg-pnl", type=float, default=0.0,
                        help="Minimum avg PnL per sell")
    parser.add_argument("--max-drawdown-pct", type=float, default=10.0,
                        help="Maximum drawdown as percentage of starting capital")

    args = parser.parse_args()

    try:
        df = pd.read_csv(args.file)
    except FileNotFoundError:
        print(f"File not found: {args.file}")
        raise SystemExit(1)

    if df.empty or "action" not in df.columns:
        print("Invalid or empty journal (column 'action' missing).")
        raise SystemExit(1)

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        if args.lookback_days > 0:
            cutoff = pd.Timestamp.now(
                tz=None) - pd.Timedelta(days=args.lookback_days)
            df = df[df["timestamp"] >= cutoff].copy()

    if df.empty:
        print(f"ERROR: No data in the selected lookback window.")
        if args.lookback_days > 0:
            print(f"       Lookback window: last {args.lookback_days} day(s)")
            print(f"       Hint: Use --lookback-days 0 to evaluate all data")
        raise SystemExit(1)

    df["pnl_base"] = _safe_float(df.get("pnl_base", pd.Series(dtype=float)))

    sells = df[df["action"] == "sell"].copy()
    closed = len(sells)

    realized_pnl = float(sells["pnl_base"].sum()) if closed > 0 else 0.0
    avg_pnl = float(sells["pnl_base"].mean()) if closed > 0 else 0.0
    win_rate = float((sells["pnl_base"] > 0).mean()
                     * 100.0) if closed > 0 else 0.0

    gross_profit = float(sells.loc[sells["pnl_base"] > 0, "pnl_base"].sum())
    gross_loss = float(-sells.loc[sells["pnl_base"] < 0, "pnl_base"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else np.inf

    equity = sells["pnl_base"].cumsum(
    ) if closed > 0 else pd.Series(dtype=float)
    max_dd_base = _max_drawdown_base(equity)
    max_dd_pct = abs(max_dd_base) / max(args.starting_capital, 1e-9) * 100.0

    result = _evaluate_verdict(
        closed_trades=closed,
        min_closed_trades=args.min_closed_trades,
        realized_pnl=realized_pnl,
        win_rate=win_rate,
        min_win_rate=args.min_win_rate,
        profit_factor=profit_factor,
        min_profit_factor=args.min_profit_factor,
        avg_pnl=avg_pnl,
        min_avg_pnl=args.min_avg_pnl,
        max_drawdown_pct=max_dd_pct,
        max_allowed_drawdown_pct=args.max_drawdown_pct,
    )

    print("=== Go/No-Go Scorecard ===")
    print(f"File:                 {args.file}")
    print(f"Closed trades:        {closed}")
    print(f"Win rate:             {win_rate:.2f}%")
    print(f"Realized PnL:         {realized_pnl:.6f} {args.base_currency}")
    print(f"Avg PnL per sell:     {avg_pnl:.6f} {args.base_currency}")
    if np.isinf(profit_factor):
        print("Profit factor:        inf")
    else:
        print(f"Profit factor:        {profit_factor:.4f}")
    print(f"Max DD (realized):    {max_dd_base:.6f} {args.base_currency}")
    print(f"Max DD (% of start):  {max_dd_pct:.2f}%")
    print(f"VERDICT:              {result.verdict}")
    print("\nReason(s):")
    for r in result.reasons:
        print(f"- {r}")

    # Exit codes for automation
    # GO=0, HOLD=2, NO-GO=3
    if result.verdict == "GO":
        raise SystemExit(0)
    if result.verdict == "HOLD":
        raise SystemExit(2)
    raise SystemExit(3)


if __name__ == "__main__":
    main()
