import argparse

import numpy as np
import pandas as pd


def safe_float(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").fillna(0.0)


def compute_max_drawdown_base(equity_curve: pd.Series) -> float:
    if equity_curve.empty:
        return 0.0
    running_max = equity_curve.cummax()
    drawdown_base = equity_curve - running_max
    return float(drawdown_base.min()) if not drawdown_base.empty else 0.0


def report_journal_freshness(
    df: pd.DataFrame,
    as_of: pd.Timestamp | None = None,
    stale_after_hours: float = 24.0,
) -> None:
    print("\n--- Journal Freshness ---")
    if "timestamp" not in df.columns:
        print("No timestamp column; journal age cannot be evaluated.")
        return

    timestamps = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").dropna()
    if timestamps.empty:
        print("No valid timestamps; journal age cannot be evaluated.")
        return

    latest = timestamps.max()
    reference = pd.Timestamp.now(tz="UTC") if as_of is None else pd.Timestamp(as_of)
    if reference.tzinfo is None:
        reference = reference.tz_localize("UTC")
    else:
        reference = reference.tz_convert("UTC")
    age_hours = (reference - latest).total_seconds() / 3600.0
    print(f"Latest journal event: {latest.isoformat()}")
    print(f"Journal age:          {age_hours:.1f} hours")
    if age_hours < 0:
        print("WARNING: Latest journal timestamp is in the future.")
    elif age_hours > stale_after_hours:
        print(
            f"WARNING: No journal event for more than {stale_after_hours:g} hours; "
            "do not treat the latest trade metrics as current."
        )


def report_daily(sells: pd.DataFrame, base_currency: str) -> None:
    if sells.empty or "timestamp" not in sells.columns:
        print("\n--- Daily Report ---")
        print("No daily data available.")
        return

    daily = sells.copy()
    daily = daily[daily["timestamp"].notna()].copy()
    if daily.empty:
        print("\n--- Daily Report ---")
        print("No valid timestamps for daily report.")
        return

    daily["date"] = daily["timestamp"].dt.date
    grouped = daily.groupby("date", dropna=False).agg(
        sells=("action", "count"),
        wins=("pnl_base", lambda x: int((x > 0).sum())),
        losses=("pnl_base", lambda x: int((x < 0).sum())),
        pnl=("pnl_base", "sum"),
        avg_pnl=("pnl_base", "mean"),
        avg_hold_s=("hold_seconds", "mean"),
    )
    grouped["win_rate"] = np.where(
        grouped["sells"] > 0,
        grouped["wins"] / grouped["sells"] * 100.0,
        0.0,
    )
    grouped = grouped.sort_index()

    print("\n--- Daily Report ---")
    print(grouped.to_string(float_format=lambda v: f"{v:.4f}"))

    best_day = grouped["pnl"].idxmax()
    worst_day = grouped["pnl"].idxmin()
    print(
        f"Best day:  {best_day} | pnl={grouped.loc[best_day, 'pnl']:.6f} {base_currency}")
    print(
        f"Worst day: {worst_day} | pnl={grouped.loc[worst_day, 'pnl']:.6f} {base_currency}")


def _print_trade_slice_summary(label: str, trades: pd.DataFrame, base_currency: str) -> None:
    if trades.empty:
        return

    pnl = float(trades["pnl_base"].sum())
    win_rate = float((trades["pnl_base"] > 0).mean() * 100.0)
    avg_pnl = float(trades["pnl_base"].mean())
    avg_hold = float(trades["hold_seconds"].mean()
                     ) if "hold_seconds" in trades.columns else 0.0
    print(
        f"{label:16s} trades={len(trades):4d} pnl={pnl:+.6f} {base_currency} "
        f"win_rate={win_rate:6.2f}% avg_pnl={avg_pnl:+.6f} avg_hold={avg_hold:7.2f}s"
    )


def report_coin_source_breakdown(sells: pd.DataFrame, base_currency: str) -> None:
    if sells.empty or "coin" not in sells.columns or "signal_source" not in sells.columns:
        return

    print("\n--- By Coin / Signal Source ---")
    work = sells.copy()
    work["coin"] = work["coin"].fillna("unknown").astype(str)
    work["signal_source"] = work["signal_source"].fillna(
        "unknown").astype(str).str.lower()

    grouped = work.groupby(["coin", "signal_source"],
                           dropna=False, observed=True)
    for (coin, source), part in sorted(grouped, key=lambda item: (item[0][0], item[0][1])):
        _print_trade_slice_summary(f"{coin}/{source}", part, base_currency)
        if len(part) < 20:
            print("  Small sample (<20 trades); descriptive only.")


def report_run_config_breakdown(sells: pd.DataFrame, base_currency: str) -> None:
    if sells.empty or not {"run_id", "config_hash"}.issubset(sells.columns):
        return

    work = sells.copy()
    work["run_id"] = work["run_id"].fillna("").replace("", "legacy").astype(str)
    work["config_hash"] = (
        work["config_hash"].fillna("").replace("", "legacy").astype(str))
    print("\n--- By Run / Configuration ---")
    grouped = work.groupby(["run_id", "config_hash"], dropna=False)
    for (run_id, config_hash), part in grouped:
        print(
            f"run_id={run_id} config_hash={config_hash[:12]} "
            f"trades={len(part)} pnl={part['pnl_base'].sum():+.6f} {base_currency}"
        )


def report_exit_excursions(sells: pd.DataFrame) -> None:
    required = {"mfe_pct", "mae_pct", "reason", "pnl_pct"}
    if sells.empty or not required.issubset(sells.columns):
        return

    work = sells.copy()
    work["mfe_pct"] = pd.to_numeric(work["mfe_pct"], errors="coerce")
    work["mae_pct"] = pd.to_numeric(work["mae_pct"], errors="coerce")
    work = work.dropna(subset=["mfe_pct", "mae_pct"])
    if work.empty:
        return

    print("\n--- Observed Excursions by Exit Reason ---")
    work["reason"] = work["reason"].fillna("unknown").astype(str)
    grouped = work.groupby("reason", dropna=False).agg(
        trades=("mfe_pct", "size"),
        avg_pnl_pct=("pnl_pct", "mean"),
        avg_mfe_pct=("mfe_pct", "mean"),
        avg_mae_pct=("mae_pct", "mean"),
    )
    print(grouped.to_string(float_format=lambda value: f"{value:.3f}"))


def report_hold_time_bands(sells: pd.DataFrame, base_currency: str) -> None:
    if sells.empty or "hold_seconds" not in sells.columns:
        return

    print("\n--- By Hold Time ---")
    bins = [-0.1, 30, 60, 120, 240, 420, 720, 1200, np.inf]
    labels = ["<=30s", "31-60s", "61-120s", "121-240s",
              "241-420s", "421-720s", "721-1200s", ">1200s"]
    work = sells.copy()
    work["hold_bin"] = pd.cut(work["hold_seconds"], bins=bins, labels=labels)
    grouped = work.groupby("hold_bin", dropna=False, observed=True).agg(
        trades=("pnl_base", "size"),
        pnl=("pnl_base", "sum"),
        win_rate=("pnl_base", lambda x: float((x > 0).mean() * 100.0)),
        avg_pnl=("pnl_base", "mean"),
    ).reset_index()
    print(grouped.to_string(index=False, float_format=lambda v: f"{v:.6f}"))


def report_confidence_bands(sells: pd.DataFrame, base_currency: str) -> None:
    if sells.empty or "signal_confidence" not in sells.columns:
        return

    work = sells.copy()
    work["signal_confidence"] = pd.to_numeric(
        work["signal_confidence"], errors="coerce")
    work = work[work["signal_confidence"].notna()].copy()
    if work.empty:
        return

    print("\n--- By Signal Confidence ---")
    bins = [0.0, 0.25, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0]
    labels = ["0.00-0.25", "0.25-0.40", "0.40-0.50",
              "0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-1.00"]
    work["conf_bin"] = pd.cut(work["signal_confidence"], bins=bins,
                              labels=labels, include_lowest=True, right=False)
    grouped = work.groupby("conf_bin", dropna=False, observed=True).agg(
        trades=("pnl_base", "size"),
        pnl=("pnl_base", "sum"),
        win_rate=("pnl_base", lambda x: float((x > 0).mean() * 100.0)),
        avg_pnl=("pnl_base", "mean"),
    ).reset_index()
    print(grouped.to_string(index=False, float_format=lambda v: f"{v:.6f}"))


def report_recent_windows(sells: pd.DataFrame, base_currency: str, windows: tuple[int, ...] = (50, 100)) -> None:
    if sells.empty:
        return

    print("\n--- Recent Windows ---")
    for window in windows:
        recent = sells.tail(window)
        _print_trade_slice_summary(f"last_{window}", recent, base_currency)


def optimize_confidence_threshold(
    sells: pd.DataFrame,
    base_currency: str,
    threshold_min: float,
    threshold_max: float,
    threshold_step: float,
    min_trades: int,
) -> None:
    print("\n--- Confidence Threshold Sweep ---")
    if sells.empty or "signal_confidence" not in sells.columns:
        print("No confidence data available.")
        return

    work = sells.copy()
    work["signal_confidence"] = pd.to_numeric(
        work["signal_confidence"], errors="coerce")
    work = work[work["signal_confidence"].notna()].copy()
    if work.empty:
        print("No numeric signal_confidence values available.")
        return

    if "timestamp" in work.columns:
        work = work.sort_values("timestamp").reset_index(drop=True)
    split_index = int(len(work) * 0.7)
    training = work.iloc[:split_index]
    holdout = work.iloc[split_index:]
    if training.empty or holdout.empty:
        print("Not enough chronological data for a training/holdout split.")
        return

    thresholds = np.arange(
        threshold_min,
        threshold_max + (threshold_step * 0.5),
        threshold_step,
    )

    rows = []
    for th in thresholds:
        filt = training[training["signal_confidence"] >= th]
        n = len(filt)
        if n == 0:
            continue
        pnl = float(filt["pnl_base"].sum())
        wr = float((filt["pnl_base"] > 0).mean() * 100.0)
        avg = float(filt["pnl_base"].mean())
        rows.append({
            "threshold": float(th),
            "trades": n,
            "win_rate": wr,
            "pnl": pnl,
            "avg_pnl": avg,
        })

    if not rows:
        print("No results in the selected threshold range.")
        return

    res = pd.DataFrame(rows).sort_values("threshold").reset_index(drop=True)
    eligible = res[res["trades"] >= min_trades].copy()
    if eligible.empty:
        print(
            f"No training threshold has at least {min_trades} trades; "
            "no candidate selected."
        )
        return

    best = eligible.sort_values(
        ["avg_pnl", "pnl"], ascending=False).iloc[0]
    selected_holdout = holdout[
        holdout["signal_confidence"] >= best["threshold"]]
    print(
        f"Training-selected candidate (first 70%, not a deployment recommendation): "
        f"threshold={best['threshold']:.2f} | trades={int(best['trades'])} | "
        f"avg_pnl={best['avg_pnl']:.6f} {base_currency}"
    )
    if selected_holdout.empty:
        print("Chronological holdout (last 30%): no trades met the selected threshold.")
    else:
        holdout_pnl = float(selected_holdout["pnl_base"].sum())
        holdout_avg = float(selected_holdout["pnl_base"].mean())
        holdout_win_rate = float(
            (selected_holdout["pnl_base"] > 0).mean() * 100.0)
        print(
            f"Chronological holdout (last 30%): trades={len(selected_holdout)} | "
            f"win_rate={holdout_win_rate:.2f}% | pnl={holdout_pnl:.6f} "
            f"{base_currency} | avg_pnl={holdout_avg:.6f} {base_currency}"
        )


def summarize(df: pd.DataFrame, base_currency: str) -> None:
    if df.empty:
        print("No trades found.")
        return

    if "action" not in df.columns:
        print("Missing column: action")
        return

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.sort_values("timestamp").reset_index(drop=True)

    df["pnl_base"] = safe_float(df.get("pnl_base", pd.Series(dtype=float)))
    df["pnl_pct"] = safe_float(df.get("pnl_pct", pd.Series(dtype=float)))
    df["hold_seconds"] = safe_float(
        df.get("hold_seconds", pd.Series(dtype=float)))

    buys = df[df["action"] == "buy"].copy()
    sells = df[df["action"] == "sell"].copy()

    closed = len(sells)
    wins = (sells["pnl_base"] > 0).sum()
    losses = (sells["pnl_base"] < 0).sum()
    win_rate = (wins / closed * 100.0) if closed > 0 else 0.0

    total_realized = float(sells["pnl_base"].sum()) if closed > 0 else 0.0
    avg_pnl = float(sells["pnl_base"].mean()) if closed > 0 else 0.0
    avg_hold = float(sells["hold_seconds"].mean()) if closed > 0 else 0.0

    gross_profit = float(sells.loc[sells["pnl_base"] > 0, "pnl_base"].sum())
    gross_loss = float(-sells.loc[sells["pnl_base"] < 0, "pnl_base"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else np.inf

    # Equity curve based on realized PnL
    equity = pd.Series(dtype=float)
    if closed > 0:
        equity = sells["pnl_base"].cumsum()
    max_dd_base = compute_max_drawdown_base(
        equity) if not equity.empty else 0.0

    print("=== Trade Journal Report ===")
    print(f"Rows total:          {len(df)}")
    print(f"Buys:                {len(buys)}")
    print(f"Sells (closed):      {closed}")
    print(f"Wins / Losses:       {wins} / {losses}")
    print(f"Win rate:            {win_rate:.2f}%")
    print(f"Realized PnL:        {total_realized:.6f} {base_currency}")
    print(f"Avg PnL per sell:    {avg_pnl:.6f} {base_currency}")
    print(f"Avg hold time:       {avg_hold:.2f} s")
    if np.isinf(profit_factor):
        print("Profit factor:       inf")
    else:
        print(f"Profit factor:       {profit_factor:.4f}")
    print(f"Max drawdown (realized equity): {max_dd_base:.6f} {base_currency}")

    # Breakdown by signal source
    if "signal_source" in sells.columns and not sells.empty:
        print("\n--- By Signal Source ---")
        source_grp = sells.groupby("signal_source", dropna=False)
        for source, part in source_grp:
            src = str(source) if pd.notna(source) else "unknown"
            src_closed = len(part)
            src_wins = int((part["pnl_base"] > 0).sum())
            src_win_rate = (src_wins / src_closed *
                            100.0) if src_closed > 0 else 0.0
            src_pnl = float(part["pnl_base"].sum())
            print(
                f"{src:12s} closed={src_closed:4d} win_rate={src_win_rate:6.2f}% pnl={src_pnl:.6f} {base_currency}"
            )

    # Breakdown by coin
    if "coin" in sells.columns and not sells.empty:
        print("\n--- By Coin ---")
        coin_grp = sells.groupby("coin", dropna=False)
        for coin, part in coin_grp:
            c = str(coin) if pd.notna(coin) else "unknown"
            c_closed = len(part)
            c_pnl = float(part["pnl_base"].sum())
            c_wr = (part["pnl_base"] > 0).mean() * 100.0
            print(
                f"{c:8s} closed={c_closed:4d} win_rate={c_wr:6.2f}% pnl={c_pnl:.6f} {base_currency}")

    # Exit reasons
    if "reason" in sells.columns and not sells.empty:
        print("\n--- Exit Reasons ---")
        reason_counts = sells["reason"].fillna("unknown").value_counts()
        for reason, cnt in reason_counts.items():
            print(f"{cnt:4d}  {reason}")

    # Last trades
    print("\n--- Last 10 Rows ---")
    cols = [c for c in ["timestamp", "coin", "action", "price", "pnl_base",
                        "pnl_pct", "signal_source", "signal_confidence", "reason"] if c in df.columns]
    print(df[cols].tail(10).to_string(index=False))


def run_full_report(
    df: pd.DataFrame,
    base_currency: str,
    threshold_min: float,
    threshold_max: float,
    threshold_step: float,
    min_trades: int,
) -> None:
    report_journal_freshness(df)
    summarize(df, base_currency)
    sells = df[df["action"] == "sell"].copy(
    ) if "action" in df.columns else pd.DataFrame()
    if "timestamp" in sells.columns:
        sells = sells.sort_values("timestamp").reset_index(drop=True)
    report_daily(sells, base_currency)
    report_coin_source_breakdown(sells, base_currency)
    report_run_config_breakdown(sells, base_currency)
    report_exit_excursions(sells)
    report_hold_time_bands(sells, base_currency)
    report_confidence_bands(sells, base_currency)
    report_recent_windows(sells, base_currency)
    optimize_confidence_threshold(
        sells,
        base_currency,
        threshold_min=threshold_min,
        threshold_max=threshold_max,
        threshold_step=threshold_step,
        min_trades=min_trades,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analysis of trade_journal.csv")
    parser.add_argument("--file", default="trade_journal.csv",
                        help="Path to the journal file")
    parser.add_argument("--base-currency", default="EUR",
                        help="Display base currency")
    parser.add_argument("--threshold-min", type=float,
                        default=0.45, help="Minimum confidence for sweep")
    parser.add_argument("--threshold-max", type=float,
                        default=0.90, help="Maximum confidence for sweep")
    parser.add_argument("--threshold-step", type=float,
                        default=0.02, help="Step size for sweep")
    parser.add_argument("--min-trades", type=int, default=5,
                        help="Minimum number of trades for threshold recommendation")
    args = parser.parse_args()

    try:
        df = pd.read_csv(args.file)
    except FileNotFoundError:
        print(f"Datei nicht gefunden: {args.file}")
        return
    except (
        OSError,
        UnicodeError,
        pd.errors.ParserError,
        pd.errors.EmptyDataError,
    ) as exc:
        print(f"Fehler beim Lesen: {exc}")
        return

    run_full_report(
        df,
        args.base_currency,
        threshold_min=args.threshold_min,
        threshold_max=args.threshold_max,
        threshold_step=args.threshold_step,
        min_trades=args.min_trades,
    )


if __name__ == "__main__":
    main()
