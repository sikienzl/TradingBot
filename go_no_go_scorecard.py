import argparse
import json
from dataclasses import asdict, dataclass
from typing import List

import numpy as np
import pandas as pd


@dataclass
class ScorecardResult:
    verdict: str
    reasons: List[str]


@dataclass
class ScorecardMetrics:
    closed_trades: int
    realized_pnl: float
    avg_pnl: float
    win_rate: float
    gross_profit: float
    gross_loss: float
    profit_factor: float
    max_drawdown_base: float
    max_drawdown_pct: float
    recent_closed_trades: int
    recent_realized_pnl: float
    recent_win_rate: float
    catboost_closed_trades: int
    catboost_realized_pnl: float
    rules_closed_trades: int
    rules_realized_pnl: float
    catboost_vs_rules_pnl_delta: float


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
    recent_closed_trades: int = 0,
    recent_realized_pnl: float = 0.0,
    min_recent_realized_pnl: float = -1e18,
    recent_win_rate: float = 0.0,
    min_recent_win_rate: float = 0.0,
    catboost_closed_trades: int = 0,
    catboost_realized_pnl: float = 0.0,
    rules_closed_trades: int = 0,
    rules_realized_pnl: float = 0.0,
    min_catboost_vs_rules_pnl_delta: float = -1e18,
    min_source_trades_for_delta: int = 0,
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

    if recent_closed_trades > 0 and recent_realized_pnl < min_recent_realized_pnl:
        soft_fails.append(
            f"Recent PnL too low: {recent_realized_pnl:.6f} < {min_recent_realized_pnl:.6f}"
        )

    if recent_closed_trades > 0 and recent_win_rate < min_recent_win_rate:
        soft_fails.append(
            f"Recent win rate too low: {recent_win_rate:.2f}% < {min_recent_win_rate:.2f}%"
        )

    source_samples_ok = (
        catboost_closed_trades >= min_source_trades_for_delta
        and rules_closed_trades >= min_source_trades_for_delta
    )
    if source_samples_ok:
        delta = catboost_realized_pnl - rules_realized_pnl
        if delta < min_catboost_vs_rules_pnl_delta:
            soft_fails.append(
                "CatBoost underperforms rules too much: "
                f"delta={delta:.6f} < {min_catboost_vs_rules_pnl_delta:.6f}"
            )

    if soft_fails:
        return ScorecardResult(verdict="HOLD", reasons=soft_fails)

    return ScorecardResult(verdict="GO", reasons=["All defined scorecard criteria met."])


def _compute_metrics(df: pd.DataFrame, starting_capital: float, recent_trades_window: int = 100) -> ScorecardMetrics:
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
    max_dd_pct = abs(max_dd_base) / max(starting_capital, 1e-9) * 100.0

    recent = sells.tail(max(1, int(recent_trades_window))).copy()
    recent_closed = len(recent)
    recent_realized_pnl = float(
        recent["pnl_base"].sum()) if recent_closed > 0 else 0.0
    recent_win_rate = float(
        (recent["pnl_base"] > 0).mean() * 100.0) if recent_closed > 0 else 0.0

    signal_source = sells.get("signal_source", pd.Series(
        index=sells.index, dtype=object)).fillna("").astype(str).str.strip().str.lower()
    catboost_sells = sells[signal_source == "catboost"]
    rules_sells = sells[signal_source == "rules"]
    catboost_closed = len(catboost_sells)
    rules_closed = len(rules_sells)
    catboost_realized_pnl = float(
        catboost_sells["pnl_base"].sum()) if catboost_closed > 0 else 0.0
    rules_realized_pnl = float(
        rules_sells["pnl_base"].sum()) if rules_closed > 0 else 0.0
    catboost_vs_rules_pnl_delta = catboost_realized_pnl - rules_realized_pnl

    return ScorecardMetrics(
        closed_trades=closed,
        realized_pnl=realized_pnl,
        avg_pnl=avg_pnl,
        win_rate=win_rate,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        max_drawdown_base=max_dd_base,
        max_drawdown_pct=max_dd_pct,
        recent_closed_trades=recent_closed,
        recent_realized_pnl=recent_realized_pnl,
        recent_win_rate=recent_win_rate,
        catboost_closed_trades=catboost_closed,
        catboost_realized_pnl=catboost_realized_pnl,
        rules_closed_trades=rules_closed,
        rules_realized_pnl=rules_realized_pnl,
        catboost_vs_rules_pnl_delta=catboost_vs_rules_pnl_delta,
    )


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
    parser.add_argument("--recent-trades-window", type=int, default=100,
                        help="Number of most recent sells used for rolling metrics")
    parser.add_argument("--min-recent-realized-pnl", type=float, default=0.0,
                        help="Minimum realized PnL across recent-trades-window sells")
    parser.add_argument("--min-recent-win-rate", type=float, default=45.0,
                        help="Minimum win rate across recent-trades-window sells")
    parser.add_argument("--min-catboost-vs-rules-pnl-delta", type=float, default=-0.05,
                        help="Minimum allowed CatBoost-vs-Rules realized-PnL delta (catboost - rules)")
    parser.add_argument("--min-source-trades-for-delta", type=int, default=50,
                        help="Minimum sells per source required before applying CatBoost-vs-Rules delta gate")
    parser.add_argument("--metrics-json", default="",
                        help="Optional path to write structured scorecard metrics as JSON")

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
        print("=== Go/No-Go Scorecard ===")
        print(f"File:                 {args.file}")
        print("Closed trades:        0")
        print(f"VERDICT:              HOLD")
        print("\nReason(s):")
        if args.lookback_days > 0:
            print(
                f"- No data in the selected lookback window (last {args.lookback_days} day(s)); collect more recent trades or use --lookback-days 0."
            )
        else:
            print(
                "- No data available in the journal yet; collect trades before evaluating the scorecard.")
        raise SystemExit(2)

    df["pnl_base"] = _safe_float(df.get("pnl_base", pd.Series(dtype=float)))

    metrics = _compute_metrics(
        df,
        args.starting_capital,
        recent_trades_window=args.recent_trades_window,
    )

    result = _evaluate_verdict(
        closed_trades=metrics.closed_trades,
        min_closed_trades=args.min_closed_trades,
        realized_pnl=metrics.realized_pnl,
        win_rate=metrics.win_rate,
        min_win_rate=args.min_win_rate,
        profit_factor=metrics.profit_factor,
        min_profit_factor=args.min_profit_factor,
        avg_pnl=metrics.avg_pnl,
        min_avg_pnl=args.min_avg_pnl,
        max_drawdown_pct=metrics.max_drawdown_pct,
        max_allowed_drawdown_pct=args.max_drawdown_pct,
        recent_closed_trades=metrics.recent_closed_trades,
        recent_realized_pnl=metrics.recent_realized_pnl,
        min_recent_realized_pnl=args.min_recent_realized_pnl,
        recent_win_rate=metrics.recent_win_rate,
        min_recent_win_rate=args.min_recent_win_rate,
        catboost_closed_trades=metrics.catboost_closed_trades,
        catboost_realized_pnl=metrics.catboost_realized_pnl,
        rules_closed_trades=metrics.rules_closed_trades,
        rules_realized_pnl=metrics.rules_realized_pnl,
        min_catboost_vs_rules_pnl_delta=args.min_catboost_vs_rules_pnl_delta,
        min_source_trades_for_delta=args.min_source_trades_for_delta,
    )

    if args.metrics_json:
        payload = {
            "metrics": asdict(metrics),
            "verdict": result.verdict,
            "reasons": result.reasons,
            "thresholds": {
                "min_closed_trades": args.min_closed_trades,
                "min_win_rate": args.min_win_rate,
                "min_profit_factor": args.min_profit_factor,
                "min_avg_pnl": args.min_avg_pnl,
                "max_drawdown_pct": args.max_drawdown_pct,
                "recent_trades_window": args.recent_trades_window,
                "min_recent_realized_pnl": args.min_recent_realized_pnl,
                "min_recent_win_rate": args.min_recent_win_rate,
                "min_catboost_vs_rules_pnl_delta": args.min_catboost_vs_rules_pnl_delta,
                "min_source_trades_for_delta": args.min_source_trades_for_delta,
                "starting_capital": args.starting_capital,
                "lookback_days": args.lookback_days,
            },
        }
        with open(args.metrics_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=True, indent=2)

    print("=== Go/No-Go Scorecard ===")
    print(f"File:                 {args.file}")
    print(f"Closed trades:        {metrics.closed_trades}")
    print(f"Win rate:             {metrics.win_rate:.2f}%")
    print(
        f"Realized PnL:         {metrics.realized_pnl:.6f} {args.base_currency}")
    print(f"Avg PnL per sell:     {metrics.avg_pnl:.6f} {args.base_currency}")
    if np.isinf(metrics.profit_factor):
        print("Profit factor:        inf")
    else:
        print(f"Profit factor:        {metrics.profit_factor:.4f}")
    print(
        f"Max DD (realized):    {metrics.max_drawdown_base:.6f} {args.base_currency}")
    print(f"Max DD (% of start):  {metrics.max_drawdown_pct:.2f}%")
    print(
        f"Recent PnL ({args.recent_trades_window}): {metrics.recent_realized_pnl:.6f} {args.base_currency}"
    )
    print(
        f"Recent win rate:      {metrics.recent_win_rate:.2f}%"
    )
    print(
        "CatBoost vs rules Δ:  "
        f"{metrics.catboost_vs_rules_pnl_delta:.6f} {args.base_currency} "
        f"(catboost={metrics.catboost_realized_pnl:.6f}, rules={metrics.rules_realized_pnl:.6f})"
    )
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
