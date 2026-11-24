"""Unit tests for ``scripts/recommend_tuning_change.py``.

The recommender decides whether the bot may change its own parameters, so the
guardrails matter more than the happy path: every "no_change" branch and every
bounds clip is pinned here.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from scripts.recommend_tuning_change import (
    Recommendation,
    _capital_step_hint,
    _coerce_value_for_type,
    _compare_acceptance,
    _enabled_parameters,
    _make_change,
    _parse_env,
    _resolve_phase,
    _state_load,
    _state_save,
    _to_float,
    main,
    recommend,
)

POLICY: dict[str, Any] = {
    "hard_guardrails": {"max_drawdown_pct_hard": 3.0},
    "tunable_parameters": [
        {"name": "MAX_HOLD_SECONDS", "type": "int", "min": 240, "max": 600, "step": 60},
        {"name": "MIN_ENTRY_SCORE", "type": "int", "min": 45, "max": 65, "step": 2},
        {"name": "ENTRY_MIN_RET_3", "type": "float", "min": -0.02, "max": -0.005, "step": 0.0025},
    ],
    "optimization_cycle": {
        "acceptance_criteria": {
            "profit_factor_vs_previous": {"value": 0.0},
            "avg_pnl_vs_previous_pct": {"value": 10.0},
            "max_drawdown_pct_vs_previous": {"value": 0.3},
        }
    },
    "activation_plan": {
        "phase_1": {
            "enabled_parameters": ["MAX_HOLD_SECONDS"],
            "required_successful_cycles_before_next_phase": 2,
        },
        "phase_2": {
            "enabled_parameters": ["MAX_HOLD_SECONDS", "MIN_ENTRY_SCORE"],
        },
    },
    "capital_step_up": {
        "enabled": True,
        "criteria": {
            "required_go_streak": 3,
            "min_profit_factor": 1.25,
            "max_drawdown_pct": 2.0,
        },
    },
}

HEALTHY_METRICS = {
    "profit_factor": 1.5,
    "avg_pnl": 5.0,
    "win_rate": 55.0,
    "max_drawdown_pct": 1.0,
}

WEAK_METRICS = {
    "profit_factor": 0.8,
    "avg_pnl": -2.0,
    "win_rate": 40.0,
    "max_drawdown_pct": 2.5,
}


def status(verdict: str = "GO", metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"verdict": verdict, "metrics": HEALTHY_METRICS if metrics is None else metrics}


def fresh_state(**overrides: Any) -> dict[str, Any]:
    base = {
        "phase": "phase_1",
        "phase_1_successful_cycles": 0,
        "last_metrics": {},
        "last_recommendation": {},
        "last_stable_params": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def test_to_float_accepts_numeric_strings_and_rejects_junk() -> None:
    assert _to_float("12.5") == 12.5
    assert _to_float(3) == 3.0
    assert _to_float(None) is None
    assert _to_float("abc") is None
    assert _to_float("") is None


def test_coerce_value_for_type_rounds_ints_only() -> None:
    assert _coerce_value_for_type(269.7, "int") == 270.0
    assert _coerce_value_for_type(269.7, "float") == 269.7
    assert _coerce_value_for_type(0.0025, "float") == 0.0025


def test_parse_env_handles_comments_exports_and_quotes(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "MAX_HOLD_SECONDS=300\n"
        "export MIN_ENTRY_SCORE=50\n"
        'QUOTED="60"\n'
        "SINGLE='70'\n"
        "no_equals_line\n"
        "\n"
        "SPACED =  80 \n",
        encoding="utf-8",
    )
    values = _parse_env(str(env_file))
    assert values["MAX_HOLD_SECONDS"] == "300"
    assert values["MIN_ENTRY_SCORE"] == "50"
    assert values["QUOTED"] == "60"
    assert values["SINGLE"] == "70"
    assert values["SPACED"] == "80"
    assert "no_equals_line" not in values


def test_parse_env_missing_file_returns_empty(tmp_path) -> None:
    assert _parse_env(str(tmp_path / "absent.env")) == {}


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
def test_state_load_creates_default_for_missing_file(tmp_path) -> None:
    state = _state_load(str(tmp_path / "nested" / "state.json"))
    assert state["phase"] == "phase_1"
    assert state["phase_1_successful_cycles"] == 0
    assert state["last_metrics"] == {}
    assert state["created_utc"].endswith("Z")


def test_state_load_recovers_from_corrupt_json(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    state = _state_load(str(path))
    assert state["phase"] == "phase_1"
    assert state["phase_1_successful_cycles"] == 0


def test_state_load_returns_saved_state(tmp_path) -> None:
    path = tmp_path / "state.json"
    _state_save(str(path), {"phase": "phase_2", "phase_1_successful_cycles": 4})
    state = _state_load(str(path))
    assert state["phase"] == "phase_2"
    assert state["phase_1_successful_cycles"] == 4


def test_state_save_creates_parent_directory(tmp_path) -> None:
    path = tmp_path / "deep" / "dir" / "state.json"
    _state_save(str(path), {"phase": "phase_1"})
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == {"phase": "phase_1"}


# ---------------------------------------------------------------------------
# Acceptance comparison
# ---------------------------------------------------------------------------
def test_compare_acceptance_without_baseline() -> None:
    ok, reasons = _compare_acceptance({}, HEALTHY_METRICS, {})
    assert ok is None
    assert reasons == ["No previous metrics baseline available."]


def test_compare_acceptance_with_missing_fields() -> None:
    ok, reasons = _compare_acceptance(
        {"profit_factor": 1.2}, HEALTHY_METRICS, {})
    assert ok is None
    assert "Insufficient metrics" in reasons[0]


def test_compare_acceptance_passes() -> None:
    previous = {"profit_factor": 1.2, "avg_pnl": 4.0, "max_drawdown_pct": 1.5}
    current = {"profit_factor": 1.4, "avg_pnl": 5.0, "max_drawdown_pct": 1.4}
    ok, reasons = _compare_acceptance(
        previous, current, POLICY["optimization_cycle"]["acceptance_criteria"])
    assert ok is True
    assert all("PASS" in line for line in reasons)


def test_compare_acceptance_fails_on_profit_factor() -> None:
    previous = {"profit_factor": 1.5, "avg_pnl": 4.0, "max_drawdown_pct": 1.0}
    current = {"profit_factor": 1.4, "avg_pnl": 6.0, "max_drawdown_pct": 1.0}
    ok, reasons = _compare_acceptance(
        previous, current, POLICY["optimization_cycle"]["acceptance_criteria"])
    assert ok is False
    assert any("profit_factor_delta" in line and "FAIL" in line for line in reasons)


def test_compare_acceptance_fails_on_drawdown() -> None:
    previous = {"profit_factor": 1.2, "avg_pnl": 4.0, "max_drawdown_pct": 1.0}
    current = {"profit_factor": 1.4, "avg_pnl": 6.0, "max_drawdown_pct": 2.0}
    ok, reasons = _compare_acceptance(
        previous, current, POLICY["optimization_cycle"]["acceptance_criteria"])
    assert ok is False
    assert any("max_drawdown_pct_delta" in line and "FAIL" in line for line in reasons)


def test_compare_acceptance_from_zero_avg_pnl() -> None:
    previous = {"profit_factor": 1.0, "avg_pnl": 0.0, "max_drawdown_pct": 1.0}
    current = {"profit_factor": 1.1, "avg_pnl": 2.0, "max_drawdown_pct": 1.0}
    ok, reasons = _compare_acceptance(
        previous, current, POLICY["optimization_cycle"]["acceptance_criteria"])
    assert ok is True
    assert any("avg_pnl_change_pct=100.00%" in line for line in reasons)


def test_compare_acceptance_zero_to_zero_avg_pnl() -> None:
    previous = {"profit_factor": 1.0, "avg_pnl": 0.0, "max_drawdown_pct": 1.0}
    current = {"profit_factor": 1.1, "avg_pnl": 0.0, "max_drawdown_pct": 1.0}
    ok, _reasons = _compare_acceptance(
        previous, current, {"avg_pnl_vs_previous_pct": {"value": 0.0}})
    assert ok is True


# ---------------------------------------------------------------------------
# Phase resolution
# ---------------------------------------------------------------------------
def test_resolve_phase_stays_in_phase_1_below_requirement() -> None:
    assert _resolve_phase(POLICY, fresh_state(phase_1_successful_cycles=1)) == "phase_1"


def test_resolve_phase_advances_at_requirement() -> None:
    assert _resolve_phase(POLICY, fresh_state(phase_1_successful_cycles=2)) == "phase_2"


def test_resolve_phase_keeps_an_explicit_phase_2() -> None:
    assert _resolve_phase(POLICY, fresh_state(phase="phase_2")) == "phase_2"


def test_resolve_phase_tolerates_missing_state() -> None:
    assert _resolve_phase({}, {}) == "phase_1"


def test_enabled_parameters_per_phase() -> None:
    assert _enabled_parameters(POLICY, "phase_1") == ["MAX_HOLD_SECONDS"]
    assert _enabled_parameters(POLICY, "phase_2") == [
        "MAX_HOLD_SECONDS", "MIN_ENTRY_SCORE"]
    assert _enabled_parameters({}, "phase_1") == []


# ---------------------------------------------------------------------------
# Parameter stepping
# ---------------------------------------------------------------------------
def test_make_change_requires_positive_step() -> None:
    cfg = {"name": "MAX_HOLD_SECONDS", "min": 240, "max": 600, "step": 0}
    assert _make_change(cfg, 300.0, "aggressive") is None


def test_make_change_expands_hold_time_when_aggressive() -> None:
    cfg = {"name": "MAX_HOLD_SECONDS", "type": "int", "min": 240, "max": 600, "step": 60}
    assert _make_change(cfg, 300.0, "aggressive") == 360.0


def test_make_change_shrinks_hold_time_when_defensive() -> None:
    cfg = {"name": "MAX_HOLD_SECONDS", "type": "int", "min": 240, "max": 600, "step": 60}
    assert _make_change(cfg, 300.0, "defensive") == 240.0


def test_make_change_lowers_entry_score_when_aggressive() -> None:
    cfg = {"name": "MIN_ENTRY_SCORE", "type": "int", "min": 45, "max": 65, "step": 2}
    assert _make_change(cfg, 50.0, "aggressive") == 48.0


def test_make_change_raises_entry_score_when_defensive() -> None:
    cfg = {"name": "MIN_ENTRY_SCORE", "type": "int", "min": 45, "max": 65, "step": 2}
    assert _make_change(cfg, 50.0, "defensive") == 52.0


def test_make_change_clips_at_upper_bound() -> None:
    cfg = {"name": "MAX_HOLD_SECONDS", "type": "int", "min": 240, "max": 400, "step": 60}
    assert _make_change(cfg, 380.0, "aggressive") == 400.0


def test_make_change_clips_at_lower_bound() -> None:
    cfg = {"name": "MIN_ENTRY_SCORE", "type": "int", "min": 46, "max": 65, "step": 2}
    # 47 - 2 would be 45, which is below the configured minimum.
    assert _make_change(cfg, 47.0, "aggressive") == 46.0
    assert _make_change(cfg, 46.0, "aggressive") is None


def test_make_change_returns_none_at_bound() -> None:
    cfg = {"name": "MAX_HOLD_SECONDS", "type": "int", "min": 240, "max": 600, "step": 60}
    assert _make_change(cfg, 600.0, "aggressive") is None
    assert _make_change(cfg, 240.0, "defensive") is None


def test_make_change_ignores_unknown_parameter() -> None:
    cfg = {"name": "SOMETHING_ELSE", "type": "int", "min": 1, "max": 10, "step": 1}
    assert _make_change(cfg, 5.0, "aggressive") is None
    assert _make_change(cfg, 5.0, "defensive") is None


def test_make_change_rounds_integer_parameters() -> None:
    cfg = {"name": "ENTRY_MIN_RET_3", "type": "float", "min": -0.02, "max": -0.005, "step": 0.0025}
    assert _make_change(cfg, -0.01, "defensive") == pytest.approx(-0.0075)


# ---------------------------------------------------------------------------
# recommend() - guard rails
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("verdict", ["NO_GO", "HOLD", "ERROR", "error"])
def test_recommend_refuses_without_go_verdict(verdict: str) -> None:
    rec, updates = recommend(POLICY, status(verdict), {}, fresh_state())
    assert rec.action == "no_change"
    assert verdict.upper() in rec.reason
    assert updates["last_status_verdict"] == verdict.upper()


def test_recommend_refuses_on_hard_drawdown_breach() -> None:
    metrics = dict(HEALTHY_METRICS, max_drawdown_pct=3.5)
    rec, _updates = recommend(POLICY, status(metrics=metrics), {}, fresh_state())
    assert rec.action == "no_change"
    assert "hard drawdown breach" in rec.reason


def test_recommend_refuses_without_metrics() -> None:
    rec, _updates = recommend(POLICY, status(metrics={}), {}, fresh_state())
    assert rec.action == "no_change"
    assert "metrics are missing" in rec.reason


@pytest.mark.parametrize("missing", ["profit_factor", "avg_pnl", "win_rate"])
def test_recommend_refuses_on_incomplete_metrics(missing: str) -> None:
    metrics = {k: v for k, v in HEALTHY_METRICS.items() if k != missing}
    rec, _updates = recommend(POLICY, status(metrics=metrics), {}, fresh_state())
    assert rec.action == "no_change"
    assert "required metrics" in rec.reason


def test_recommend_holds_on_mixed_metrics() -> None:
    metrics = {"profit_factor": 1.1, "avg_pnl": 1.0, "win_rate": 45.0,
               "max_drawdown_pct": 1.0}
    rec, _updates = recommend(POLICY, status(metrics=metrics), {}, fresh_state())
    assert rec.action == "no_change"
    assert "mixed metrics" in rec.reason


# ---------------------------------------------------------------------------
# recommend() - directional proposals
# ---------------------------------------------------------------------------
def test_recommend_proposes_growth_step_when_healthy() -> None:
    env = {"MAX_HOLD_SECONDS": "300"}
    rec, updates = recommend(POLICY, status(), env, fresh_state())
    assert rec.action == "propose_change"
    assert rec.parameter == "MAX_HOLD_SECONDS"
    assert rec.old_value == 300.0
    assert rec.new_value == 360.0
    assert rec.phase == "phase_1"
    assert updates["last_recommendation"]["mode"] == "aggressive"


def test_recommend_proposes_safety_step_when_weak() -> None:
    env = {"MAX_HOLD_SECONDS": "300"}
    rec, _updates = recommend(
        POLICY, status(metrics=WEAK_METRICS), env, fresh_state())
    assert rec.action == "propose_change"
    assert rec.new_value == 240.0


def test_recommend_seeds_missing_env_value_from_minimum() -> None:
    rec, _updates = recommend(POLICY, status(), {}, fresh_state())
    assert rec.action == "propose_change"
    assert rec.old_value == 240.0
    assert rec.new_value == 300.0


def test_recommend_skips_parameters_that_cannot_move() -> None:
    phase_policy = json.loads(json.dumps(POLICY))
    phase_policy["activation_plan"]["phase_1"]["enabled_parameters"] = [
        "UNKNOWN_PARAMETER", "MAX_HOLD_SECONDS"]
    env = {"MAX_HOLD_SECONDS": "300"}
    rec, _updates = recommend(phase_policy, status(), env, fresh_state())
    assert rec.parameter == "MAX_HOLD_SECONDS"


def test_recommend_reports_no_adjustable_parameter() -> None:
    env = {"MAX_HOLD_SECONDS": "600"}
    rec, _updates = recommend(POLICY, status(), env, fresh_state())
    assert rec.action == "no_change"
    assert "within configured bounds" in rec.reason


def test_recommend_uses_phase_2_parameters_after_advancement() -> None:
    env = {"MAX_HOLD_SECONDS": "600", "MIN_ENTRY_SCORE": "50"}
    rec, _updates = recommend(
        POLICY, status(), env, fresh_state(phase_1_successful_cycles=2))
    assert rec.phase == "phase_2"
    assert rec.parameter == "MIN_ENTRY_SCORE"
    assert rec.new_value == 48.0


# ---------------------------------------------------------------------------
# recommend() - acceptance bookkeeping
# ---------------------------------------------------------------------------
def test_recommend_counts_successful_cycle_and_stores_stable_params() -> None:
    state = fresh_state(last_metrics={"profit_factor": 1.2, "avg_pnl": 4.0,
                                      "max_drawdown_pct": 1.5})
    metrics = {"profit_factor": 1.4, "avg_pnl": 5.0, "win_rate": 55.0,
               "max_drawdown_pct": 1.4}
    env = {"MAX_HOLD_SECONDS": "300", "MIN_ENTRY_SCORE": "50"}
    _rec, updates = recommend(POLICY, status(metrics=metrics), env, state)
    assert updates["last_acceptance"]["result"] is True
    assert updates["phase_1_successful_cycles"] == 1
    assert updates["last_stable_params"] == {
        "MAX_HOLD_SECONDS": 300.0, "MIN_ENTRY_SCORE": 50.0}


def test_recommend_does_not_count_a_rejected_cycle() -> None:
    state = fresh_state(last_metrics={"profit_factor": 1.5, "avg_pnl": 4.0,
                                      "max_drawdown_pct": 1.0})
    metrics = {"profit_factor": 1.4, "avg_pnl": 5.0, "win_rate": 55.0,
               "max_drawdown_pct": 1.0}
    _rec, updates = recommend(POLICY, status(metrics=metrics), {}, state)
    assert updates["last_acceptance"]["result"] is False
    assert "phase_1_successful_cycles" not in updates
    assert "last_stable_params" not in updates


def test_recommend_records_phase_in_updates() -> None:
    _rec, updates = recommend(POLICY, status(), {}, fresh_state())
    assert updates["phase"] == "phase_1"
    assert updates["last_run_utc"].endswith("Z")
    assert updates["last_metrics"] == HEALTHY_METRICS


def test_recommendation_dataclass_defaults() -> None:
    rec = Recommendation(action="no_change", reason="because")
    assert rec.parameter is None
    assert rec.old_value is None
    assert rec.new_value is None
    assert rec.phase == "phase_1"


# ---------------------------------------------------------------------------
# Capital step-up hint
# ---------------------------------------------------------------------------
def test_capital_step_hint_disabled() -> None:
    policy = json.loads(json.dumps(POLICY))
    policy["capital_step_up"]["enabled"] = False
    hint = _capital_step_hint(policy, status())
    assert hint == {"eligible": False, "reason": "Capital step-up disabled."}


def test_capital_step_hint_requires_go() -> None:
    hint = _capital_step_hint(POLICY, status(verdict="NO_GO"))
    assert hint["eligible"] is False
    assert "NO_GO" in hint["reason"]


def test_capital_step_hint_requires_metrics() -> None:
    hint = _capital_step_hint(POLICY, status(metrics={"win_rate": 50.0}))
    assert hint["eligible"] is False
    assert "Missing profit_factor" in hint["reason"]


def test_capital_step_hint_rejects_low_profit_factor() -> None:
    metrics = dict(HEALTHY_METRICS, profit_factor=1.1)
    hint = _capital_step_hint(POLICY, status(metrics=metrics))
    assert hint["eligible"] is False
    assert "profit_factor" in hint["reason"]


def test_capital_step_hint_rejects_high_drawdown() -> None:
    metrics = dict(HEALTHY_METRICS, max_drawdown_pct=2.5)
    hint = _capital_step_hint(POLICY, status(metrics=metrics))
    assert hint["eligible"] is False
    assert "max_drawdown_pct" in hint["reason"]


def test_capital_step_hint_eligible() -> None:
    hint = _capital_step_hint(POLICY, status())
    assert hint["eligible"] is True
    assert hint["required_go_streak"] == 3


# ---------------------------------------------------------------------------
# main() end to end
# ---------------------------------------------------------------------------
def run_main(tmp_path, extra_args: list[str] | None = None) -> tuple[int, dict[str, Any]]:
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(POLICY), encoding="utf-8")
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps(status()), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("MAX_HOLD_SECONDS=300\n", encoding="utf-8")
    state_file = tmp_path / "state" / "recommender.json"
    output_file = tmp_path / "out" / "recommendation.json"

    args = [
        "--policy", str(policy_file),
        "--status-json", str(status_file),
        "--env-file", str(env_file),
        "--state-file", str(state_file),
        "--output-json", str(output_file),
    ]
    if extra_args:
        args = extra_args

    saved = sys.argv
    sys.argv = ["recommend_tuning_change.py", *args]
    try:
        main()
    finally:
        sys.argv = saved

    return 0, json.loads(output_file.read_text(encoding="utf-8"))


def test_main_writes_recommendation_and_state(tmp_path, capsys) -> None:
    _code, result = run_main(tmp_path)
    assert result["recommendation"]["action"] == "propose_change"
    assert result["recommendation"]["parameter"] == "MAX_HOLD_SECONDS"
    assert result["recommendation"]["new_value"] == 360.0
    assert result["phase"] == "phase_1"
    assert result["capital_step_hint"]["eligible"] is True
    assert result["generated_at_utc"].endswith("Z")

    captured = capsys.readouterr().out
    assert "=== Tuning Recommendation ===" in captured
    assert "env_patch: MAX_HOLD_SECONDS=360.0" in captured

    state_file = tmp_path / "state" / "recommender.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["last_recommendation"]["parameter"] == "MAX_HOLD_SECONDS"


def test_main_second_run_uses_persisted_state(tmp_path) -> None:
    _code, first = run_main(tmp_path)
    assert first["acceptance_check"]["result"] is None  # no baseline yet

    _code, second = run_main(tmp_path)
    # Same metrics twice: profit factor delta 0 passes, avg pnl change 0 fails.
    assert second["acceptance_check"]["result"] is False


def test_main_reports_no_change_for_no_go(tmp_path, capsys) -> None:
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(POLICY), encoding="utf-8")
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps(status(verdict="NO_GO")), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("MAX_HOLD_SECONDS=300\n", encoding="utf-8")

    saved = sys.argv
    sys.argv = [
        "recommend_tuning_change.py",
        "--policy", str(policy_file),
        "--status-json", str(status_file),
        "--env-file", str(env_file),
        "--state-file", str(tmp_path / "state.json"),
    ]
    try:
        main()
    finally:
        sys.argv = saved

    captured = capsys.readouterr().out
    assert "action: no_change" in captured
    assert "NO_GO" in captured
