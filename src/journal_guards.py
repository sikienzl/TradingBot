"""Pure journal-analysis helpers used by the dynamic entry risk guards.

Extracted from ``src.trading_bot`` so the guard maths can be tested without
importing the full trading bot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd


def _compute_dynamic_lossmaker_entry_pairs(
    df: pd.DataFrame,
    window: int,
    min_sells: int,
    max_win_rate_pct: float,
    min_pnl_loss: float,
    min_max_hold_exit_ratio: float,
    max_avg_hold_seconds: float,
) -> set[tuple[str, str]]:
    """Returns recent coin/source pairs that should be temporarily suppressed."""
    if df.empty:
        return set()

    required_columns = {"action", "coin", "pnl_base"}
    if not required_columns.issubset(df.columns):
        return set()

    sells = df.loc[
        df["action"].fillna("").astype(str).str.lower() == "sell"
    ].copy()
    if sells.empty:
        return set()

    recent_window = sells.tail(max(1, int(window))).copy()
    if recent_window.empty:
        return set()

    recent_window["coin"] = recent_window["coin"].fillna(
        "").astype(str).str.upper()
    recent_window["signal_source"] = recent_window.get(
        "signal_source",
        pd.Series(index=recent_window.index, dtype=object),
    ).fillna("rules").astype(str).str.lower()
    recent_window["pnl_base"] = pd.to_numeric(
        recent_window["pnl_base"], errors="coerce"
    ).fillna(0.0)
    recent_window["hold_seconds"] = pd.to_numeric(
        recent_window.get(
            "hold_seconds",
            pd.Series(index=recent_window.index, dtype=float),
        ),
        errors="coerce",
    ).fillna(0.0)
    recent_reasons = recent_window.get(
        "reason",
        pd.Series(index=recent_window.index, dtype=object),
    ).fillna("").astype(str)

    blocked_pairs: set[tuple[str, str]] = set()
    for (coin, signal_source), group in recent_window.groupby(["coin", "signal_source"], dropna=False):
        if not coin or not signal_source:
            continue
        if len(group) < max(1, int(min_sells)):
            continue

        pnl = pd.to_numeric(group["pnl_base"], errors="coerce").fillna(0.0)
        pnl_sum = float(pnl.sum())
        if pnl_sum > -float(min_pnl_loss):
            continue

        win_rate_pct = float((pnl > 0).mean() * 100.0)
        if win_rate_pct > float(max_win_rate_pct):
            continue

        avg_hold_seconds = float(
            pd.to_numeric(group["hold_seconds"],
                          errors="coerce").fillna(0.0).mean()
        )

        reasons = recent_reasons.loc[group.index]
        max_hold_exit_ratio = float(
            reasons.str.contains("MAX-HOLD-TIME", case=False, na=False).mean()
        )
        if (
            max_hold_exit_ratio < float(min_max_hold_exit_ratio)
            and avg_hold_seconds > float(max_avg_hold_seconds)
        ):
            continue

        blocked_pairs.add((str(coin).upper(), str(signal_source).lower()))

    return blocked_pairs


def _compute_degraded_entry_sources(
    df: pd.DataFrame,
    window: int,
    min_sells: int,
    min_pnl_loss: float,
    max_profit_factor: float,
    max_avg_hold_seconds: float,
) -> set[str]:
    """Returns signal sources that recently underperform with short-hold churn."""
    if df.empty:
        return set()

    required_columns = {"action", "pnl_base"}
    if not required_columns.issubset(df.columns):
        return set()

    sells = df.loc[
        df["action"].fillna("").astype(str).str.lower() == "sell"
    ].copy()
    if sells.empty:
        return set()

    recent_window = sells.tail(max(1, int(window))).copy()
    if recent_window.empty:
        return set()

    recent_window["signal_source"] = recent_window.get(
        "signal_source",
        pd.Series(index=recent_window.index, dtype=object),
    ).fillna("rules").astype(str).str.lower()
    recent_window["pnl_base"] = pd.to_numeric(
        recent_window["pnl_base"], errors="coerce"
    ).fillna(0.0)
    recent_window["hold_seconds"] = pd.to_numeric(
        recent_window.get(
            "hold_seconds",
            pd.Series(index=recent_window.index, dtype=float),
        ),
        errors="coerce",
    ).fillna(0.0)

    degraded_sources: set[str] = set()
    for source, group in recent_window.groupby("signal_source", dropna=False):
        if not source:
            continue
        if len(group) < max(1, int(min_sells)):
            continue

        pnl = pd.to_numeric(group["pnl_base"], errors="coerce").fillna(0.0)
        pnl_sum = float(pnl.sum())
        if pnl_sum > -float(min_pnl_loss):
            continue

        gross_profit = float(pnl[pnl > 0].sum())
        gross_loss = float(-pnl[pnl < 0].sum())
        profit_factor = (
            gross_profit / gross_loss) if gross_loss > 0 else 999.0
        if profit_factor > float(max_profit_factor):
            continue

        avg_hold_seconds = float(
            pd.to_numeric(group["hold_seconds"],
                          errors="coerce").fillna(0.0).mean()
        )
        if avg_hold_seconds > float(max_avg_hold_seconds):
            continue

        degraded_sources.add(str(source).lower())

    return degraded_sources


def _compute_recent_pnl_guard_state(
    df: pd.DataFrame,
    window: int,
    min_trades: int,
    min_realized_pnl: float,
    max_profit_factor: float,
    stale_after_hours: float = 24.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Returns whether recent realized performance is weak enough to tighten entries."""
    result: dict[str, Any] = {
        "active": False,
        "recent_trades": 0,
        "recent_realized_pnl": 0.0,
        "recent_profit_factor": 999.0,
        "reason": "insufficient_data",
        "latest_closed_trade_at": None,
        "journal_age_hours": None,
        "journal_stale": False,
    }

    if df.empty or "action" not in df.columns or "pnl_base" not in df.columns:
        return result

    sells = df.loc[
        df["action"].fillna("").astype(str).str.lower() == "sell"
    ].copy()
    if sells.empty:
        return result

    recent = sells.tail(max(1, int(window))).copy()
    if recent.empty:
        return result

    pnl = pd.to_numeric(recent["pnl_base"], errors="coerce").fillna(0.0)
    recent_trades = len(recent)
    recent_realized_pnl = float(pnl.sum())
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = float(-pnl[pnl < 0].sum())
    recent_profit_factor = (
        (gross_profit / gross_loss) if gross_loss > 0 else 999.0
    )
    if "timestamp" in sells.columns:
        timestamps = pd.to_datetime(
            sells["timestamp"], utc=True, errors="coerce").dropna()
        if not timestamps.empty:
            latest = timestamps.max()
            reference = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
            if reference.tzinfo is None:
                reference = reference.tz_localize("UTC")
            else:
                reference = reference.tz_convert("UTC")
            age_hours = (reference - latest).total_seconds() / 3600.0
            result["latest_closed_trade_at"] = latest.isoformat()
            result["journal_age_hours"] = age_hours
            result["journal_stale"] = age_hours > stale_after_hours

    result["recent_trades"] = recent_trades
    result["recent_realized_pnl"] = recent_realized_pnl
    result["recent_profit_factor"] = recent_profit_factor

    if recent_trades < max(1, int(min_trades)):
        result["reason"] = "below_min_trades"
        return result

    if recent_realized_pnl < float(min_realized_pnl) and recent_profit_factor <= float(max_profit_factor):
        result["active"] = True
        result["reason"] = "negative_recent_pnl_and_low_pf"
    else:
        result["reason"] = "healthy_recent_window"

    return result
