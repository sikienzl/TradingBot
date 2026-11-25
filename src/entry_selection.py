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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class EntryCandidateContext:
    """Bot-owned state the entry-candidate sweep reads but does not own."""

    config: Any
    portfolio: Any


@dataclass(frozen=True)
class EntryCandidateHooks:
    """Bot collaborators the entry-candidate sweep calls but does not own."""

    dynamic_excluded_entry_pairs: Callable[[], set[tuple[str, str]]]
    degraded_entry_sources: Callable[[], set[str]]
    recent_pnl_guard_state: Callable[[], dict[str, Any]]
    log_recent_pnl_guard_status: Callable[[dict[str, Any]], None]
    entry_market_mode: Callable[[dict[str, Any]], str]
    entry_pair_allowed: Callable[[str, dict[str, Any], Container[tuple[str, str]]], bool]
    entry_guards_allow: Callable[[str, dict[str, Any], dict[str, bool]], bool]
    passes_fallback_entry_filter: Callable[[str, dict[str, Any]], tuple[bool, Any]]
    passes_downtrend_reversal_filter: Callable[[dict[str, Any]], tuple[bool, Any]]
    passes_uptrend_entry_filter: Callable[[dict[str, Any]], tuple[bool, Any]]
    log_blocked_uptrend_candidates: Callable[..., None]
    log_blocked_downtrend_reversal_candidates: Callable[..., None]
    log_fallback_entry_diagnostics: Callable[..., None]


def entry_guards_allow(
    config: Any,
    coin: str,
    data: dict[str, Any],
    cache: dict[str, bool],
    *,
    volatility_guard: Callable[[dict[str, Any]], tuple[bool, Any]],
    log: logging.Logger = logger,
) -> bool:
    """Cached per-iteration entry guards: tabular veto + volatility proxy."""
    if coin in cache:
        return cache[coin]

    if data.get('tabular_sell_veto', False):
        log.info(
            "⛔ Confident tabular SELL vetoed new entry for %s: "
            "P(sell)=%.3f",
            coin,
            float(data.get('tabular_sell_proba') or 0.0),
        )
        cache[coin] = False
        return False

    volatility_allowed, volatility_reason = (
        volatility_guard(data)
    )
    if not volatility_allowed:
        minimum_atr_pct = config.min_entry_atr_pct
        if volatility_reason == 'atr_pct_unavailable':
            log.warning(
                "⛔ Entry withheld for %s: ATR(14)/price volatility proxy "
                "is unavailable (minimum %.3f%%)",
                coin,
                minimum_atr_pct * 100,
            )
        else:
            log.info(
                "⛔ Entry withheld for %s: ATR(14)/price volatility proxy "
                "%.3f%% is below minimum %.3f%%",
                coin,
                float(data['atr_pct']) * 100,
                minimum_atr_pct * 100,
            )
        cache[coin] = False
        return False

    cache[coin] = True
    return True


def log_no_entry_candidates(
    market_analysis: dict[str, Any],
    selection: dict[str, Any],
    downtrend_filter_results: dict[str, tuple[bool, str]],
    uptrend_filter_results: dict[str, tuple[bool, str]],
    entry_market_mode: str,
    hooks: EntryCandidateHooks,
    *,
    log: logging.Logger = logger,
) -> None:
    """Explains an empty entry shortlist: signal mix, gates and diagnostics."""
    recommendation_mix = dict(Counter(
        data.get('recommendation', 'UNKNOWN')
        for data in market_analysis.values()
    ))
    downtrend_reversal_eligible = sum(
        1
        for coin, data in market_analysis.items()
        if data.get('recommendation') == 'HOLD (Down-Trend)'
        and downtrend_filter_results.get(coin, (False, 'not_evaluated'))[0]
    )
    log.info(
        "ℹ️ No entry candidates this round: strict=%d, fallback=%d, force_fill=%d, "
        "downtrend_reversal_eligible=%d, entry_mode=%s, recommendation_mix=%s",
        len(selection['strict_buy_candidates']),
        len(selection['fallback_candidates']),
        len(selection['force_fill_candidates']),
        downtrend_reversal_eligible,
        entry_market_mode,
        recommendation_mix,
    )
    hooks.log_blocked_uptrend_candidates(
        market_analysis,
        uptrend_filter_results,
    )
    hooks.log_blocked_downtrend_reversal_candidates(
        market_analysis,
        downtrend_filter_results,
    )
    hooks.log_fallback_entry_diagnostics(
        market_analysis,
        selection['fallback_base_candidates'],
        selection['fallback_filter_results'],
        selection['fallback_allowed'],
        selection['fallback_suppression_reason'],
        entry_market_mode,
    )
    for coin, data in list(market_analysis.items())[:5]:
        signal_confidence = data.get('signal_confidence')
        confidence_text = ''
        if signal_confidence is not None:
            confidence_text = f", confidence={float(signal_confidence) * 100:.0f}%"
        log.info(
            "  - %s: rec=%s, rule=%s, score=%s, rule_score=%s, RSI=%.2f, source=%s%s",
            coin,
            data.get('recommendation', 'n/a'),
            data.get('rule_recommendation', 'n/a'),
            data.get('score', 0),
            data.get('rule_score', 0),
            float(data.get('rsi', np.nan)),
            data.get('signal_source', 'n/a'),
            confidence_text,
        )


def select_entry_candidate_shortlist(
    context: EntryCandidateContext,
    hooks: EntryCandidateHooks,
    market_analysis: dict[str, Any],
    *,
    log: logging.Logger = logger,
) -> list[str]:
    """Rank entry candidates and fill trade slots (strict, fallback, force-fill)."""
    occupied_positions = set(context.portfolio.open_trades.keys()) | set(
        context.portfolio.holdings.keys())
    available_trade_slots = max(
        0, context.config.max_open_trades - len(context.portfolio.open_trades))
    dynamic_excluded_entry_pairs = hooks.dynamic_excluded_entry_pairs()
    if dynamic_excluded_entry_pairs:
        log.info(
            "Dynamically excluded lossmaker entry pairs: %s",
            ', '.join(sorted(
                f"{coin}/{source}" for coin, source in dynamic_excluded_entry_pairs)),
        )
    degraded_entry_sources = hooks.degraded_entry_sources()
    if degraded_entry_sources:
        log.info(
            "Dynamically degraded entry sources (fallback/force-fill blocked): %s",
            ', '.join(sorted(degraded_entry_sources)),
        )
    recent_pnl_guard = hooks.recent_pnl_guard_state()
    recent_pnl_guard_active = bool(recent_pnl_guard.get('active', False))
    hooks.log_recent_pnl_guard_status(recent_pnl_guard)

    downtrend_filter_results = {
        coin: hooks.passes_downtrend_reversal_filter(data)
        for coin, data in market_analysis.items()
    }
    uptrend_filter_results = {
        coin: hooks.passes_uptrend_entry_filter(data)
        for coin, data in market_analysis.items()
    }
    entry_market_mode = hooks.entry_market_mode(market_analysis)

    selection = select_entry_candidates(
        market_analysis,
        config=context.config,
        occupied_positions=occupied_positions,
        available_trade_slots=available_trade_slots,
        dynamic_excluded_entry_pairs=dynamic_excluded_entry_pairs,
        degraded_entry_sources=degraded_entry_sources,
        recent_pnl_guard_active=recent_pnl_guard_active,
        downtrend_filter_results=downtrend_filter_results,
        uptrend_filter_results=uptrend_filter_results,
        entry_market_mode=entry_market_mode,
        entry_pair_allowed=hooks.entry_pair_allowed,
        entry_guards_allow=hooks.entry_guards_allow,
        fallback_entry_filter=hooks.passes_fallback_entry_filter,
        logger=log,
    )
    top_buy_recommendations = selection['selected']

    if recent_pnl_guard_active:
        blocked_by_recent_guard = [
            coin for coin in selection['strict_candidates_before_guard']
            if str(market_analysis[coin].get(
                'signal_source', 'rules')).strip().lower() != 'catboost'
        ]
        log_blocked_candidates = (
            log.warning if blocked_by_recent_guard else log.info
        )
        log_blocked_candidates(
            "Recent PnL guard withheld %d strict BUY candidates: %s",
            len(blocked_by_recent_guard),
            ', '.join(blocked_by_recent_guard[:10]) or "none",
        )

    log.info(
        f"🔍 Market analysis complete. Top {len(top_buy_recommendations)} buy recommendations:")

    if not top_buy_recommendations and market_analysis:
        log_no_entry_candidates(
            market_analysis,
            selection,
            downtrend_filter_results,
            uptrend_filter_results,
            entry_market_mode,
            hooks,
        )

    for i, coin in enumerate(top_buy_recommendations):
        data = market_analysis.get(coin, {})
        log.info(f"  {i+1}. {coin}: {data.get('recommendation', 'HOLD')} "
                    f"(Price: {data.get('price', 0):.4f} {context.config.base_currency}, "
                    f"Score: {data.get('score', 0)}%, "
                    f"Volumen: {data.get('volume', 0):,.0f} {context.config.base_currency}, "
                    f"RSI: {data.get('rsi', np.nan):.2f})")
    return top_buy_recommendations
