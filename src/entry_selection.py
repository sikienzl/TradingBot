"""Pure entry-selection rules for the trading bot.

Layer-1 collaborator: no exchange, database or model access, so the entry
gate can be unit-tested in isolation (the CI test job has no TA-Lib).
:class:`CryptoTradingBot` keeps thin delegators for every function here.
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from collections.abc import Callable, Container, Iterable, Sequence
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: Severity ordering used to pick the most restrictive entry market mode.
ENTRY_MODE_SEVERITY_RANK: dict[str, int] = {
    "normal": 0,
    "cautious": 1,
    "defensive": 2,
}

BEARISH_ENTRY_SIGNALS = {"SELL", "WEAK SELL", "HOLD (Down-Trend)"}
BULLISH_ENTRY_SIGNALS = {"BUY", "STRONG BUY", "HOLD (Up-Trend)"}

DEFENSIVE_SIMULATION_REGIMES = {"crash", "downtrend"}
CAUTIOUS_SIMULATION_REGIMES = {"recovery", "sideways", "mixed"}


def _fmt(value: Any, spec: str) -> str:
    """Format an optional metric for the diagnostics logs."""
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if math.isnan(number):
        return "n/a"
    return format(number, spec)


def filter_reason_code(reason: Any) -> str:
    """Normalize verbose filter reasons into stable summary keys."""
    return str(reason).split(" ", 1)[0]


def reason_mix(reasons: Iterable[Any]) -> dict[str, int]:
    """Count blocked reasons by their stable leading token."""
    return dict(Counter(filter_reason_code(reason) for reason in reasons))


def classify_entry_market_mode(
    market_analysis: dict[str, dict[str, Any]],
    *,
    simulate_data: bool,
    simulation_regime: str | None,
    severity_rank: dict[str, int] | None = None,
) -> str:
    """Classify how aggressive new entries should be for the current signal mix.

    Weak environments suppress the fallback and force-fill entry paths. In
    simulation the configured regime seeds the classification; live trading
    classifies from the bearish/bullish signal counts alone.
    """
    if not market_analysis:
        return "normal"

    ranks = severity_rank or ENTRY_MODE_SEVERITY_RANK

    regime_mode = "normal"
    if simulate_data:
        regime = (simulation_regime or "neutral").lower()
        if regime in DEFENSIVE_SIMULATION_REGIMES:
            regime_mode = "defensive"
        elif regime in CAUTIOUS_SIMULATION_REGIMES:
            regime_mode = "cautious"

    bearish = sum(
        1 for data in market_analysis.values()
        if data.get("recommendation") in BEARISH_ENTRY_SIGNALS
    )
    bullish = sum(
        1 for data in market_analysis.values()
        if data.get("recommendation") in BULLISH_ENTRY_SIGNALS
    )
    total = max(1, len(market_analysis))
    bearish_share = bearish / total

    signal_mode = "normal"
    if bearish_share >= 0.60 or bearish >= bullish + 2:
        signal_mode = "defensive"
    elif bearish_share >= 0.40 or bearish > bullish:
        signal_mode = "cautious"

    return max((regime_mode, signal_mode), key=ranks.__getitem__)


def allows_entry_signal(
    recommendation: Any,
    *,
    downtrend_reversal_allowed: bool,
    excluded_signals: Container[str],
) -> bool:
    """Keep explicit downtrend-reversal approvals eligible even when the
    generic exit rules block down-trend entries."""
    if recommendation != "HOLD (Down-Trend)":
        return recommendation not in excluded_signals
    return downtrend_reversal_allowed


def log_blocked_gate_candidates(
    market_analysis: dict[str, dict[str, Any]],
    filter_results: dict[str, tuple[bool, Any]],
    *,
    recommendation: str,
    gate_label: str,
    limit: int = 5,
    logger: logging.Logger = logger,
) -> None:
    """Summarise why entry-gate candidates were rejected.

    Shared by the up-trend gate and the down-trend reversal gate, which differ
    only in the gate label and the recommendation they filtered for.
    """
    blocked_candidates = []
    for coin, data in market_analysis.items():
        if data.get("recommendation") != recommendation:
            continue
        passes_filter, filter_reason = filter_results.get(coin, (True, "not_evaluated"))
        if passes_filter:
            continue
        buy_proba = data.get("tabular_buy_proba")
        sell_proba = data.get("tabular_sell_proba")
        proba_edge = None
        if buy_proba is not None and sell_proba is not None:
            try:
                if not np.isnan(buy_proba) and not np.isnan(sell_proba):
                    proba_edge = float(buy_proba) - float(sell_proba)
            except TypeError:
                proba_edge = None
        blocked_candidates.append({
            "coin": coin,
            "reason": filter_reason,
            "reason_code": filter_reason_code(filter_reason),
            "rsi": data.get("rsi"),
            "buy_proba": buy_proba,
            "sell_proba": sell_proba,
            "proba_edge": proba_edge,
            "signal_source": data.get("signal_source", "n/a"),
            "score": data.get("score", 0),
            "rule_score": data.get("rule_score", 0),
        })

    if not blocked_candidates:
        return

    logger.info(
        "  \U0001f6a7 %s blocked %d candidate(s): %s",
        gate_label,
        len(blocked_candidates),
        reason_mix(candidate["reason_code"] for candidate in blocked_candidates),
    )
    for candidate in blocked_candidates[:limit]:
        logger.info(
            "    - %s: reason=%s, RSI=%s, buy_proba=%s, sell_proba=%s, edge=%s, "
            "source=%s, score=%s, rule_score=%s",
            candidate["coin"],
            candidate["reason"],
            _fmt(candidate["rsi"], ".2f"),
            _fmt(candidate["buy_proba"], ".3f"),
            _fmt(candidate["sell_proba"], ".3f"),
            _fmt(candidate["proba_edge"], ".3f"),
            candidate["signal_source"],
            candidate["score"],
            candidate["rule_score"],
        )


def log_fallback_entry_diagnostics(
    market_analysis: dict[str, dict[str, Any]],
    fallback_base_candidates: Sequence[str],
    fallback_filter_results: dict[str, tuple[bool, Any]],
    fallback_allowed: bool,
    fallback_suppression_reason: str,
    entry_market_mode: str,
    limit: int = 5,
    logger: logging.Logger = logger,
) -> None:
    """Log why fallback entries were blocked individually or fully suppressed."""
    blocked_candidates = []
    for coin in fallback_base_candidates:
        passes_filter, filter_reason = fallback_filter_results.get(
            coin, (True, "not_evaluated"))
        if passes_filter:
            continue
        data = market_analysis.get(coin, {})
        blocked_candidates.append({
            "coin": coin,
            "reason": filter_reason,
            "reason_code": filter_reason_code(filter_reason),
            "rsi": data.get("rsi"),
            "signal_source": data.get("signal_source", "n/a"),
            "score": data.get("score", 0),
            "rule_score": data.get("rule_score", 0),
        })

    if blocked_candidates:
        logger.info(
            "  \U0001f6a7 Fallback RSI gate blocked %d candidate(s): %s",
            len(blocked_candidates),
            reason_mix(candidate["reason_code"] for candidate in blocked_candidates),
        )
        for candidate in blocked_candidates[:limit]:
            logger.info(
                "    - %s: reason=%s, RSI=%s, source=%s, score=%s, rule_score=%s",
                candidate["coin"],
                candidate["reason"],
                _fmt(candidate["rsi"], ".2f"),
                candidate["signal_source"],
                candidate["score"],
                candidate["rule_score"],
            )

    if fallback_allowed or not fallback_base_candidates:
        return

    logger.info(
        "  \U0001f6d1 Fallback entry suppressed (%s): %d candidate(s) met all "
        "non-RSI fallback conditions in entry_mode=%s.",
        fallback_suppression_reason,
        len(fallback_base_candidates),
        entry_market_mode,
    )
    for coin in fallback_base_candidates[:limit]:
        data = market_analysis.get(coin, {})
        signal_confidence = data.get("signal_confidence")
        confidence_text = ""
        if signal_confidence is not None:
            confidence_text = f", confidence={float(signal_confidence) * 100:.0f}%"
        logger.info(
            "    - %s: rec=%s, score=%s, rule_score=%s, RSI=%s, source=%s%s",
            coin,
            data.get("recommendation", "n/a"),
            data.get("score", 0),
            data.get("rule_score", 0),
            _fmt(data.get("rsi"), ".2f"),
            data.get("signal_source", "n/a"),
            confidence_text,
        )


def log_blocked_buy_attempt_candidates(
    blocked_attempts: Sequence[dict[str, Any]],
    limit: int = 5,
    logger: logging.Logger = logger,
) -> None:
    """Log why shortlisted buy candidates still failed on the execution path."""
    if not blocked_attempts:
        return

    logger.info(
        "  \U0001f6ab Buy attempt blocked %d candidate(s): %s",
        len(blocked_attempts),
        reason_mix(attempt.get("reason", "unknown") for attempt in blocked_attempts),
    )
    for attempt in blocked_attempts[:limit]:
        logger.info(
            "    - %s: reason=%s, source=%s, score=%s, recommendation=%s, "
            "position_size=%s, cash=%s, cooldown_remaining=%s",
            attempt.get("coin", "n/a"),
            attempt.get("reason", "unknown"),
            attempt.get("signal_source", "n/a"),
            attempt.get("score", "n/a"),
            attempt.get("recommendation", "n/a"),
            attempt.get("position_size_text", "n/a"),
            attempt.get("cash_text", "n/a"),
            attempt.get("cooldown_text", "n/a"),
        )


def _strict_buy_candidates(
    market_analysis: dict[str, dict[str, Any]],
    *,
    min_entry_score: float,
    recent_pnl_guard_active: bool,
    occupied_positions: Container[str],
    dynamic_excluded_entry_pairs: Container[tuple[str, str]],
    entry_pair_allowed: Callable[[str, dict[str, Any], Container[tuple[str, str]]], bool],
    entry_guards_allow: Callable[[str, dict[str, Any], dict[str, bool]], bool],
    entry_guard_results: dict[str, bool],
) -> tuple[list[str], list[str]]:
    """Coins with a strict BUY signal, before and after the recent-PnL guard.

    Returns ``(before_guard, after_guard)`` so the caller can still report the
    candidates the guard withheld.
    """
    before_guard = [
        coin for coin, data in market_analysis.items()
        if coin not in occupied_positions
        and data.get("recommendation") in ["STRONG BUY", "BUY"]
        and data.get("score", 0) >= min_entry_score
        and entry_pair_allowed(coin, data, dynamic_excluded_entry_pairs)
        and entry_guards_allow(coin, data, entry_guard_results)
    ]
    after_guard = [
        coin for coin in before_guard
        if not recent_pnl_guard_active
        or str(market_analysis[coin].get("signal_source", "rules")).strip().lower()
        == "catboost"
    ]
    return before_guard, after_guard


def _candidates_within_filters(
    market_analysis: dict[str, dict[str, Any]],
    *,
    min_score: float,
    already_selected: Sequence[str],
    occupied_positions: Container[str],
    degraded_entry_sources: Container[str],
    excluded_signals: Sequence[str],
    downtrend_filter_results: dict[str, tuple[bool, Any]],
    uptrend_filter_results: dict[str, tuple[bool, Any]],
    dynamic_excluded_entry_pairs: Container[tuple[str, str]],
    entry_pair_allowed: Callable[[str, dict[str, Any], Container[tuple[str, str]]], bool],
    entry_guards_allow: Callable[[str, dict[str, Any], dict[str, bool]], bool],
    entry_guard_results: dict[str, bool],
) -> list[str]:
    """Coins passing the shared entry filters for a given minimum score."""
    return [
        coin for coin, data in market_analysis.items()
        if coin not in occupied_positions
        and coin not in already_selected
        and data.get("score", 0) >= min_score
        and entry_guards_allow(coin, data, entry_guard_results)
        and str(data.get("signal_source", "rules")).strip().lower()
        not in degraded_entry_sources
        and allows_entry_signal(
            str(data.get("recommendation", "")),
            downtrend_reversal_allowed=downtrend_filter_results.get(
                coin, (False, "not_evaluated"))[0],
            excluded_signals=excluded_signals,
        )
        and downtrend_filter_results.get(coin, (False, "not_evaluated"))[0]
        and uptrend_filter_results.get(coin, (False, "not_evaluated"))[0]
        and entry_pair_allowed(coin, data, dynamic_excluded_entry_pairs)
    ]


def _fallback_suppression_reason(
    *,
    enable_fallback_entry: bool,
    recent_pnl_guard_active: bool,
    available_trade_slots: int,
    selected_count: int,
    defensive_entry_mode_enabled: bool,
    entry_market_mode: str,
) -> str:
    """Why the fallback entry path is unavailable this round ('allowed' if not)."""
    if not enable_fallback_entry:
        return "fallback_disabled"
    if recent_pnl_guard_active:
        return "recent_pnl_guard"
    if available_trade_slots <= selected_count:
        return "no_free_slots"
    if defensive_entry_mode_enabled and entry_market_mode == "defensive":
        return "entry_mode_defensive"
    return "allowed"


def select_entry_candidates(
    market_analysis: dict[str, dict[str, Any]],
    *,
    config: Any,
    occupied_positions: Container[str],
    available_trade_slots: int,
    dynamic_excluded_entry_pairs: Container[tuple[str, str]],
    degraded_entry_sources: Container[str],
    recent_pnl_guard_active: bool,
    downtrend_filter_results: dict[str, tuple[bool, Any]],
    uptrend_filter_results: dict[str, tuple[bool, Any]],
    entry_market_mode: str,
    entry_pair_allowed: Callable[[str, dict[str, Any], Container[tuple[str, str]]], bool],
    entry_guards_allow: Callable[[str, dict[str, Any], dict[str, bool]], bool],
    fallback_entry_filter: Callable[[str, dict[str, Any]], tuple[bool, Any]],
    logger: logging.Logger = logger,
) -> dict[str, Any]:
    """Rank entry candidates and fill trade slots: strict, fallback, force-fill.

    The bot supplies the config-dependent predicates; the slot arithmetic,
    suppression reasons and candidate ranking here are pure and testable.
    """
    entry_guard_results: dict[str, bool] = {}

    strict_before_guard, strict_buy_candidates = _strict_buy_candidates(
        market_analysis,
        min_entry_score=config.min_entry_score,
        recent_pnl_guard_active=recent_pnl_guard_active,
        occupied_positions=occupied_positions,
        dynamic_excluded_entry_pairs=dynamic_excluded_entry_pairs,
        entry_pair_allowed=entry_pair_allowed,
        entry_guards_allow=entry_guards_allow,
        entry_guard_results=entry_guard_results,
    )
    selected = strict_buy_candidates[:min(
        config.top_n_for_analysis, available_trade_slots)]

    # Fallback: fill remaining slots with solid non-SELL candidates. When
    # exit_on_downtrend is active, down-trend coins are excluded from entries.
    excluded_signals_fallback = ["SELL", "WEAK SELL"]
    if config.exit_on_downtrend:
        excluded_signals_fallback.append("HOLD (Down-Trend)")

    fallback_base_candidates = _candidates_within_filters(
        market_analysis,
        min_score=config.fallback_min_score,
        already_selected=selected,
        excluded_signals=excluded_signals_fallback,
        occupied_positions=occupied_positions,
        degraded_entry_sources=degraded_entry_sources,
        downtrend_filter_results=downtrend_filter_results,
        uptrend_filter_results=uptrend_filter_results,
        dynamic_excluded_entry_pairs=dynamic_excluded_entry_pairs,
        entry_pair_allowed=entry_pair_allowed,
        entry_guards_allow=entry_guards_allow,
        entry_guard_results=entry_guard_results,
    )
    fallback_filter_results = {
        coin: fallback_entry_filter(coin, market_analysis.get(coin, {}))
        for coin in fallback_base_candidates
    }
    fallback_suppression_reason = _fallback_suppression_reason(
        enable_fallback_entry=config.enable_fallback_entry,
        recent_pnl_guard_active=recent_pnl_guard_active,
        available_trade_slots=available_trade_slots,
        selected_count=len(selected),
        defensive_entry_mode_enabled=config.defensive_entry_mode_enabled,
        entry_market_mode=entry_market_mode,
    )
    fallback_allowed = fallback_suppression_reason == "allowed"

    fallback_candidates: list[str] = []
    if fallback_allowed:
        fallback_candidates = [
            coin for coin in fallback_base_candidates
            if fallback_filter_results.get(coin, (False, "not_evaluated"))[0]
        ]
        if fallback_candidates:
            missing_slots = available_trade_slots - len(selected)
            selected.extend(fallback_candidates[:missing_slots])
            best_data = market_analysis.get(selected[0], {})
            logger.info(
                "\u2139\ufe0f Fallback entry activated (slots filled): lead candidate "
                "%s (%s, score %s).",
                selected[0],
                best_data.get("recommendation", "n/a"),
                best_data.get("score", 0),
            )

    # Optional more aggressive slot filling: neutral candidates without the RSI
    # filter; down-trend coins stay excluded while exit_on_downtrend is active.
    force_fill_candidates: list[str] = []
    force_fill_allowed = (
        config.force_fill_slots
        and not recent_pnl_guard_active
        and available_trade_slots > len(selected)
        and (
            not config.defensive_entry_mode_enabled
            or entry_market_mode == "normal"
        )
    )
    if force_fill_allowed:
        force_fill_candidates = _candidates_within_filters(
            market_analysis,
            min_score=config.force_fill_min_score,
            already_selected=selected,
            excluded_signals=excluded_signals_fallback,
            occupied_positions=occupied_positions,
            degraded_entry_sources=degraded_entry_sources,
            downtrend_filter_results=downtrend_filter_results,
            uptrend_filter_results=uptrend_filter_results,
            dynamic_excluded_entry_pairs=dynamic_excluded_entry_pairs,
            entry_pair_allowed=entry_pair_allowed,
            entry_guards_allow=entry_guards_allow,
            entry_guard_results=entry_guard_results,
        )
        if force_fill_candidates:
            missing_slots = available_trade_slots - len(selected)
            selected.extend(force_fill_candidates[:missing_slots])
            logger.info(
                "\u2699\ufe0f Force-fill active: %d additional candidates added "
                "(min score %s).",
                min(len(force_fill_candidates), missing_slots),
                config.force_fill_min_score,
            )

    return {
        "selected": selected,
        "strict_candidates_before_guard": strict_before_guard,
        "strict_buy_candidates": strict_buy_candidates,
        "fallback_candidates": fallback_candidates,
        "fallback_base_candidates": fallback_base_candidates,
        "fallback_filter_results": fallback_filter_results,
        "fallback_allowed": fallback_allowed,
        "fallback_suppression_reason": fallback_suppression_reason,
        "force_fill_candidates": force_fill_candidates,
        "entry_guard_results": entry_guard_results,
    }
