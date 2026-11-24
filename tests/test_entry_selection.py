"""Unit tests for the pure entry-selection collaborator (layer 1)."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.entry_selection import (
    allows_entry_signal,
    classify_entry_market_mode,
    filter_reason_code,
    log_blocked_buy_attempt_candidates,
    log_blocked_gate_candidates,
    log_fallback_entry_diagnostics,
    reason_mix,
    select_entry_candidates,
)


def _config(**overrides):
    base = {
        "min_entry_score": 50.0,
        "top_n_for_analysis": 5,
        "fallback_min_score": 40.0,
        "force_fill_min_score": 30.0,
        "enable_fallback_entry": True,
        "force_fill_slots": False,
        "exit_on_downtrend": True,
        "defensive_entry_mode_enabled": True,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _always_allow_pair(coin, data, pairs):
    return True


def _always_allow_guard(coin, data, cache):
    return True


def _select(market_analysis, **kwargs):
    kwargs.setdefault("config", _config())
    kwargs.setdefault("occupied_positions", [])
    kwargs.setdefault("available_trade_slots", 3)
    kwargs.setdefault("dynamic_excluded_entry_pairs", set())
    kwargs.setdefault("degraded_entry_sources", set())
    kwargs.setdefault("recent_pnl_guard_active", False)
    kwargs.setdefault("downtrend_filter_results", {})
    kwargs.setdefault("uptrend_filter_results", {})
    kwargs.setdefault("entry_market_mode", "normal")
    kwargs.setdefault("entry_pair_allowed", _always_allow_pair)
    kwargs.setdefault("entry_guards_allow", _always_allow_guard)
    kwargs.setdefault("fallback_entry_filter",
                      lambda coin, data: (True, "ok"))
    return select_entry_candidates(market_analysis, **kwargs)


# ---------------------------------------------------------------------------
# reason helpers
# ---------------------------------------------------------------------------

def test_filter_reason_code_keeps_leading_token():
    assert filter_reason_code("rsi_above_max (72.19 > 72.00)") == "rsi_above_max"
    assert filter_reason_code("ok") == "ok"


def test_reason_mix_counts_by_code():
    mix = reason_mix([
        "rsi_above_max (72.19 > 72.00)",
        "rsi_above_max (75.00 > 72.00)",
        "missing_rsi",
    ])
    assert mix == {"rsi_above_max": 2, "missing_rsi": 1}


# ---------------------------------------------------------------------------
# market mode classification
# ---------------------------------------------------------------------------

def test_entry_market_mode_empty_analysis_is_normal():
    assert classify_entry_market_mode(
        {}, simulate_data=False, simulation_regime=None) == "normal"


def test_entry_market_mode_crash_regime_is_defensive_in_simulation():
    mode = classify_entry_market_mode(
        {"BTC": {"recommendation": "BUY"}},
        simulate_data=True,
        simulation_regime="crash",
    )
    assert mode == "defensive"


def test_entry_market_mode_sideways_regime_is_cautious_in_simulation():
    mode = classify_entry_market_mode(
        {"BTC": {"recommendation": "BUY"}, "ETH": {"recommendation": "BUY"}},
        simulate_data=True,
        simulation_regime="sideways",
    )
    assert mode == "cautious"


def test_entry_market_mode_live_bearish_majority_is_defensive():
    analysis = {
        "BTC": {"recommendation": "SELL"},
        "ETH": {"recommendation": "WEAK SELL"},
        "SOL": {"recommendation": "HOLD (Down-Trend)"},
        "XRP": {"recommendation": "BUY"},
    }
    mode = classify_entry_market_mode(
        analysis, simulate_data=False, simulation_regime=None)
    assert mode == "defensive"


def test_entry_market_mode_live_mildly_bearish_is_cautious():
    analysis = {
        "BTC": {"recommendation": "SELL"},
        "ETH": {"recommendation": "WEAK SELL"},
        "SOL": {"recommendation": "BUY"},
        "XRP": {"recommendation": "BUY"},
    }
    mode = classify_entry_market_mode(
        analysis, simulate_data=False, simulation_regime=None)
    assert mode == "cautious"


def test_entry_market_mode_live_all_bullish_is_normal():
    analysis = {
        "BTC": {"recommendation": "BUY"},
        "ETH": {"recommendation": "STRONG BUY"},
        "SOL": {"recommendation": "HOLD (Up-Trend)"},
        "XRP": {"recommendation": "BUY"},
    }
    mode = classify_entry_market_mode(
        analysis, simulate_data=False, simulation_regime=None)
    assert mode == "normal"


def test_entry_market_mode_regime_never_downgrades_signal_mode():
    mode = classify_entry_market_mode(
        {"BTC": {"recommendation": "SELL"}, "ETH": {"recommendation": "SELL"},
         "SOL": {"recommendation": "SELL"}},
        simulate_data=True,
        simulation_regime="recovery",
    )
    assert mode == "defensive"


# ---------------------------------------------------------------------------
# signal allowance
# ---------------------------------------------------------------------------

def test_allows_entry_signal_blocks_excluded_signals():
    assert allows_entry_signal(
        "SELL",
        downtrend_reversal_allowed=True,
        excluded_signals={"SELL", "WEAK SELL"},
    ) is False


def test_allows_entry_signal_keeps_approved_reversal():
    assert allows_entry_signal(
        "HOLD (Down-Trend)",
        downtrend_reversal_allowed=True,
        excluded_signals={"SELL", "WEAK SELL", "HOLD (Down-Trend)"},
    ) is True
    assert allows_entry_signal(
        "HOLD (Down-Trend)",
        downtrend_reversal_allowed=False,
        excluded_signals={"SELL", "WEAK SELL", "HOLD (Down-Trend)"},
    ) is False


def test_allows_entry_signal_passes_unknown_signals():
    assert allows_entry_signal(
        "HOLD",
        downtrend_reversal_allowed=False,
        excluded_signals={"SELL"},
    ) is True


# ---------------------------------------------------------------------------
# diagnostics logging
# ---------------------------------------------------------------------------

def test_log_blocked_gate_covers_both_gates(caplog):
    market = {
        "TRX": {
            "recommendation": "HOLD (Up-Trend)",
            "rsi": 72.19,
            "tabular_buy_proba": 0.55,
            "tabular_sell_proba": 0.30,
            "signal_source": "rules",
            "score": 60,
            "rule_score": 60,
        },
        "ADA": {
            "recommendation": "HOLD (Down-Trend)",
            "rsi": 28.0,
            "signal_source": "rules",
            "score": 55,
            "rule_score": 55,
        },
    }
    results = {
        "TRX": (False, "rsi_above_max (72.19 > 72.00)"),
        "ADA": (False, "missing_tabular_proba"),
    }
    with caplog.at_level(logging.INFO):
        log_blocked_gate_candidates(
            market, results,
            recommendation="HOLD (Up-Trend)", gate_label="Uptrend entry gate")
    assert "Uptrend entry gate blocked 1 candidate(s)" in caplog.text
    assert "rsi_above_max" in caplog.text
    assert "TRX" in caplog.text
    assert "ADA" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO):
        log_blocked_gate_candidates(
            market, results,
            recommendation="HOLD (Down-Trend)",
            gate_label="Downtrend reversal gate")
    assert "Downtrend reversal gate blocked 1 candidate(s)" in caplog.text
    assert "missing_tabular_proba" in caplog.text
    assert "ADA" in caplog.text
    assert "TRX" not in caplog.text


def test_log_blocked_gate_edge_and_missing_metrics(caplog):
    market = {"XRP": {"recommendation": "HOLD (Up-Trend)",
                      "tabular_buy_proba": 0.4, "tabular_sell_proba": 0.25}}
    with caplog.at_level(logging.INFO):
        log_blocked_gate_candidates(
            market, {"XRP": (False, "score_below_min (10 < 50)")},
            recommendation="HOLD (Up-Trend)", gate_label="Uptrend entry gate")
    assert "edge=0.150" in caplog.text
    assert "RSI=n/a" in caplog.text


def test_log_blocked_gate_no_blockers_is_silent(caplog):
    with caplog.at_level(logging.INFO):
        log_blocked_gate_candidates(
            {"BTC": {"recommendation": "HOLD (Up-Trend)"}},
            {"BTC": (True, "ok")},
            recommendation="HOLD (Up-Trend)", gate_label="Uptrend entry gate")
    assert caplog.text == ""


def test_log_fallback_diagnostics_blocked_and_suppressed(caplog):
    market = {
        "TRX": {"recommendation": "HOLD (Up-Trend)", "rsi": 72.19,
                "signal_source": "rules", "score": 60, "rule_score": 60},
    }
    with caplog.at_level(logging.INFO):
        log_fallback_entry_diagnostics(
            market,
            fallback_base_candidates=["TRX"],
            fallback_filter_results={"TRX": (
                False, "rsi_above_fallback_max (72.19 > 72.00)")},
            fallback_allowed=True,
            fallback_suppression_reason="allowed",
            entry_market_mode="cautious",
        )
    assert "Fallback RSI gate blocked 1 candidate(s)" in caplog.text
    assert "rsi_above_fallback_max" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO):
        log_fallback_entry_diagnostics(
            {"TRX": {"recommendation": "HOLD (Up-Trend)", "rsi": 71.88,
                     "signal_source": "catboost", "score": 60,
                     "rule_score": 60, "signal_confidence": 0.57}},
            fallback_base_candidates=["TRX"],
            fallback_filter_results={"TRX": (True, "ok")},
            fallback_allowed=False,
            fallback_suppression_reason="entry_mode_defensive",
            entry_market_mode="defensive",
        )
    assert "Fallback entry suppressed (entry_mode_defensive)" in caplog.text
    assert "entry_mode=defensive" in caplog.text
    assert "confidence=57%" in caplog.text


def test_log_blocked_buy_attempts_summary(caplog):
    with caplog.at_level(logging.INFO):
        log_blocked_buy_attempt_candidates([{
            "coin": "TRX",
            "reason": "ret_3_below_min (-0.0300 < -0.0100)",
            "signal_source": "rules",
            "score": 60,
            "recommendation": "BUY",
            "position_size_text": "5.00",
            "cash_text": "19.85",
            "cooldown_text": "n/a",
        }])
    assert "Buy attempt blocked 1 candidate(s)" in caplog.text
    assert "ret_3_below_min" in caplog.text


# ---------------------------------------------------------------------------
# candidate selection
# ---------------------------------------------------------------------------

def test_select_strict_buy_candidates_ranked_and_capped():
    market = {
        "BTC": {"recommendation": "BUY", "score": 80},
        "ETH": {"recommendation": "STRONG BUY", "score": 70},
        "SOL": {"recommendation": "BUY", "score": 30},
    }
    result = _select(market, available_trade_slots=2)
    assert result["selected"] == ["BTC", "ETH"]
    assert result["strict_buy_candidates"] == ["BTC", "ETH"]
    assert result["fallback_candidates"] == []


def test_select_recent_pnl_guard_keeps_only_catboost():
    market = {
        "BTC": {"recommendation": "BUY", "score": 80, "signal_source": "rules"},
        "ETH": {"recommendation": "BUY", "score": 70, "signal_source": "catboost"},
    }
    result = _select(market, recent_pnl_guard_active=True)
    assert result["strict_candidates_before_guard"] == ["BTC", "ETH"]
    assert result["selected"] == ["ETH"]
    assert result["fallback_allowed"] is False
    assert result["fallback_suppression_reason"] == "recent_pnl_guard"


def test_select_fallback_fills_free_slots():
    market = {
        "BTC": {"recommendation": "BUY", "score": 80},
        "ADA": {"recommendation": "HOLD", "score": 45},
    }
    downtrend = {"BTC": (True, "not_downtrend"), "ADA": (True, "not_downtrend")}
    uptrend = {"BTC": (True, "ok"), "ADA": (True, "ok")}
    result = _select(
        market,
        available_trade_slots=3,
        downtrend_filter_results=downtrend,
        uptrend_filter_results=uptrend,
    )
    assert result["selected"] == ["BTC", "ADA"]
    assert result["fallback_allowed"] is True
    assert result["fallback_suppression_reason"] == "allowed"


def test_select_fallback_suppressed_in_defensive_mode():
    market = {"ADA": {"recommendation": "HOLD", "score": 45}}
    gates = {"ADA": (True, "ok")}
    result = _select(
        market,
        available_trade_slots=3,
        entry_market_mode="defensive",
        downtrend_filter_results=gates,
        uptrend_filter_results=gates,
    )
    assert result["selected"] == []
    assert result["fallback_allowed"] is False
    assert result["fallback_suppression_reason"] == "entry_mode_defensive"
    assert result["fallback_base_candidates"] == ["ADA"]


def test_select_fallback_blocked_by_rsi_filter():
    market = {"ADA": {"recommendation": "HOLD", "score": 45}}
    gates = {"ADA": (True, "ok")}
    result = _select(
        market,
        available_trade_slots=3,
        downtrend_filter_results=gates,
        uptrend_filter_results=gates,
        fallback_entry_filter=lambda coin, data: (
            False, "rsi_above_fallback_max (72.19 > 72.00)"),
    )
    assert result["selected"] == []
    assert result["fallback_base_candidates"] == ["ADA"]
    assert result["fallback_candidates"] == []
    assert result["fallback_filter_results"]["ADA"][0] is False


def test_select_no_free_slots_suppresses_fallback():
    market = {
        "BTC": {"recommendation": "BUY", "score": 80},
        "ADA": {"recommendation": "HOLD", "score": 45},
    }
    gates = {coin: (True, "ok") for coin in market}
    result = _select(
        market,
        available_trade_slots=1,
        downtrend_filter_results=gates,
        uptrend_filter_results=gates,
    )
    assert result["selected"] == ["BTC"]
    assert result["fallback_suppression_reason"] == "no_free_slots"


def test_select_force_fill_only_in_normal_mode():
    market = {"ADA": {"recommendation": "HOLD", "score": 35}}
    gates = {"ADA": (True, "ok")}
    kwargs = {
        "available_trade_slots": 2,
        "downtrend_filter_results": gates,
        "uptrend_filter_results": gates,
        "config": _config(force_fill_slots=True),
    }
    cautious = _select(market, entry_market_mode="cautious", **kwargs)
    assert cautious["selected"] == []
    assert cautious["force_fill_candidates"] == []

    normal = _select(market, entry_market_mode="normal", **kwargs)
    assert normal["selected"] == ["ADA"]
    assert normal["force_fill_candidates"] == ["ADA"]


def test_select_skips_degraded_sources_and_occupied_positions():
    market = {
        "BTC": {"recommendation": "HOLD", "score": 60, "signal_source": "hailo"},
        "ETH": {"recommendation": "HOLD", "score": 60, "signal_source": "rules"},
    }
    gates = {coin: (True, "ok") for coin in market}
    result = _select(
        market,
        available_trade_slots=3,
        occupied_positions=["ETH"],
        degraded_entry_sources={"hailo"},
        downtrend_filter_results=gates,
        uptrend_filter_results=gates,
    )
    # ETH is already occupied and BTC's signal source is degraded, so the
    # shared fallback/force-fill filter has nothing left to select.
    assert result["fallback_base_candidates"] == []
    assert result["selected"] == []


def test_select_downtrend_reversal_candidate_needs_both_gates():
    market = {"ADA": {"recommendation": "HOLD (Down-Trend)", "score": 60}}
    result = _select(
        market,
        available_trade_slots=3,
        downtrend_filter_results={"ADA": (True, "downtrend_reversal_ok")},
        uptrend_filter_results={"ADA": (True, "ok")},
    )
    assert result["selected"] == ["ADA"]

    blocked = _select(
        market,
        available_trade_slots=3,
        downtrend_filter_results={"ADA": (False, "rsi_above_reversal_max")},
        uptrend_filter_results={"ADA": (True, "ok")},
    )
    assert blocked["selected"] == []


def test_select_respects_entry_pair_exclusion():
    market = {"BTC": {"recommendation": "BUY", "score": 80}}

    def deny_pair(coin, data, pairs):
        return False

    result = _select(market, entry_pair_allowed=deny_pair)
    assert result["selected"] == []
    assert result["strict_candidates_before_guard"] == []


def test_select_entry_guard_cache_is_shared():
    market = {
        "BTC": {"recommendation": "BUY", "score": 80},
        "ADA": {"recommendation": "HOLD", "score": 45},
    }
    gates = {coin: (True, "ok") for coin in market}
    seen = {}

    def guard(coin, data, cache):
        seen.setdefault(coin, []).append(len(cache))
        if coin in cache:
            return cache[coin]
        cache[coin] = coin != "ADA"
        return cache[coin]

    result = _select(
        market,
        available_trade_slots=3,
        downtrend_filter_results=gates,
        uptrend_filter_results=gates,
        entry_guards_allow=guard,
    )
    assert result["selected"] == ["BTC"]
    assert result["entry_guard_results"] == {"BTC": True, "ADA": False}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
