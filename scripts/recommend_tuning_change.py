#!/usr/bin/env python3
"""Recommend one weekly tuning change from policy + latest scorecard.

This script does not modify .env automatically. It proposes at most one change
per run and writes a small recommender state for baseline tracking.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Recommendation:
    action: str
    reason: str
    parameter: Optional[str] = None
    old_value: Optional[float] = None
    new_value: Optional[float] = None
    phase: str = "phase_1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_env(path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not os.path.exists(path):
        return values

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_value_for_type(value: float, typ: str) -> float:
    if typ == "int":
        return float(int(round(value)))
    return float(value)


def _state_load(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {
            "created_utc": _utc_now(),
            "phase": "phase_1",
            "phase_1_successful_cycles": 0,
            "last_metrics": {},
            "last_recommendation": {},
            "last_stable_params": {},
        }
    try:
        return _load_json(path)
    except (OSError, json.JSONDecodeError):
        return {
            "created_utc": _utc_now(),
            "phase": "phase_1",
            "phase_1_successful_cycles": 0,
            "last_metrics": {},
            "last_recommendation": {},
            "last_stable_params": {},
        }


def _state_save(path: str, state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=True, indent=2)
        f.write("\n")


def _compare_acceptance(
    prev_metrics: Dict[str, Any],
    curr_metrics: Dict[str, Any],
    acceptance: Dict[str, Any],
) -> Tuple[Optional[bool], List[str]]:
    if not prev_metrics or not curr_metrics:
        return None, ["No previous metrics baseline available."]

    reasons: List[str] = []
    ok = True

    prev_pf = _to_float(prev_metrics.get("profit_factor"))
    curr_pf = _to_float(curr_metrics.get("profit_factor"))
    prev_avg = _to_float(prev_metrics.get("avg_pnl"))
    curr_avg = _to_float(curr_metrics.get("avg_pnl"))
    prev_dd = _to_float(prev_metrics.get("max_drawdown_pct"))
    curr_dd = _to_float(curr_metrics.get("max_drawdown_pct"))

    if None in (prev_pf, curr_pf, prev_avg, curr_avg, prev_dd, curr_dd):
        return None, ["Insufficient metrics for acceptance comparison."]

    pf_rule = acceptance.get("profit_factor_vs_previous", {})
    avg_rule = acceptance.get("avg_pnl_vs_previous_pct", {})
    dd_rule = acceptance.get("max_drawdown_pct_vs_previous", {})

    pf_delta = curr_pf - prev_pf
    pf_target = float(pf_rule.get("value", 0.0))
    pf_pass = pf_delta >= pf_target
    reasons.append(
        f"profit_factor_delta={pf_delta:.6f} (target >= {pf_target:.6f}) => {'PASS' if pf_pass else 'FAIL'}"
    )
    ok = ok and pf_pass

    if abs(prev_avg) < 1e-12:
        avg_pct = 100.0 if curr_avg > 0 else 0.0
    else:
        avg_pct = ((curr_avg - prev_avg) / abs(prev_avg)) * 100.0
    avg_target = float(avg_rule.get("value", 10.0))
    avg_pass = avg_pct >= avg_target
    reasons.append(
        f"avg_pnl_change_pct={avg_pct:.2f}% (target >= {avg_target:.2f}%) => {'PASS' if avg_pass else 'FAIL'}"
    )
    ok = ok and avg_pass

    dd_delta = curr_dd - prev_dd
    dd_target = float(dd_rule.get("value", 0.3))
    dd_pass = dd_delta <= dd_target
    reasons.append(
        f"max_drawdown_pct_delta={dd_delta:.6f} (target <= {dd_target:.6f}) => {'PASS' if dd_pass else 'FAIL'}"
    )
    ok = ok and dd_pass

    return ok, reasons


def _resolve_phase(policy: Dict[str, Any], state: Dict[str, Any]) -> str:
    phase = str(state.get("phase") or "phase_1")
    required = int(
        policy.get("activation_plan", {})
        .get("phase_1", {})
        .get("required_successful_cycles_before_next_phase", 2)
    )
    successful = int(state.get("phase_1_successful_cycles", 0) or 0)
    if phase == "phase_1" and successful >= required:
        return "phase_2"
    return phase


def _enabled_parameters(policy: Dict[str, Any], phase: str) -> List[str]:
    plan = policy.get("activation_plan", {})
    if phase == "phase_2":
        return list(plan.get("phase_2", {}).get("enabled_parameters", []))
    return list(plan.get("phase_1", {}).get("enabled_parameters", []))


def _make_change(
    param_cfg: Dict[str, Any],
    current_value: float,
    mode: str,
) -> Optional[float]:
    step = float(param_cfg.get("step", 0.0))
    pmin = float(param_cfg.get("min"))
    pmax = float(param_cfg.get("max"))
    name = str(param_cfg.get("name", ""))

    if step <= 0:
        return None

    # Conservative, explicit mapping per parameter.
    if mode == "aggressive":
        if name == "MAX_HOLD_SECONDS":
            new = current_value + step
        elif name == "REENTRY_COOLDOWN_MAX_SECONDS":
            new = current_value - step
        elif name == "ENTRY_MIN_RET_3":
            new = current_value - step
        elif name == "MIN_ENTRY_SCORE":
            new = current_value - step
        elif name == "TABULAR_BUY_MIN_CONFIDENCE":
            new = current_value - step
        else:
            return None
    else:
        if name == "MAX_HOLD_SECONDS":
            new = current_value - step
        elif name == "REENTRY_COOLDOWN_MAX_SECONDS":
            new = current_value + step
        elif name == "ENTRY_MIN_RET_3":
            new = current_value + step
        elif name == "MIN_ENTRY_SCORE":
            new = current_value + step
        elif name == "TABULAR_BUY_MIN_CONFIDENCE":
            new = current_value + step
        else:
            return None

    clipped = min(max(new, pmin), pmax)
    typed = _coerce_value_for_type(
        clipped, str(param_cfg.get("type", "float")))
    if abs(typed - current_value) < 1e-12:
        return None
    return typed


def recommend(
    policy: Dict[str, Any],
    status: Dict[str, Any],
    env_values: Dict[str, str],
    state: Dict[str, Any],
) -> Tuple[Recommendation, Dict[str, Any]]:
    phase = _resolve_phase(policy, state)
    enabled = _enabled_parameters(policy, phase)

    verdict = str(status.get("verdict", "ERROR")).upper()
    metrics = status.get("metrics", {}) or {}

    hard = policy.get("hard_guardrails", {})
    hard_dd = float(hard.get("max_drawdown_pct_hard", 3.0))
    curr_dd = _to_float(metrics.get("max_drawdown_pct"))

    state_updates: Dict[str, Any] = {
        "phase": phase,
        "last_run_utc": _utc_now(),
        "last_status_verdict": verdict,
        "last_metrics": metrics,
    }

    # Evaluate previous cycle acceptance if possible.
    acceptance = (
        policy.get("optimization_cycle", {}).get("acceptance_criteria", {})
    )
    prev_metrics = state.get("last_metrics", {}) or {}
    acceptance_ok, acceptance_reasons = _compare_acceptance(
        prev_metrics=prev_metrics,
        curr_metrics=metrics,
        acceptance=acceptance,
    )
    state_updates["last_acceptance"] = {
        "result": acceptance_ok,
        "details": acceptance_reasons,
    }

    if acceptance_ok is True:
        phase1_count = int(state.get("phase_1_successful_cycles", 0) or 0)
        if phase == "phase_1":
            state_updates["phase_1_successful_cycles"] = phase1_count + 1
        # Track stable params only when acceptance passed.
        stable: Dict[str, Any] = dict(
            state.get("last_stable_params", {}) or {})
        for p in policy.get("tunable_parameters", []):
            name = str(p.get("name"))
            v = _to_float(env_values.get(name))
            if v is not None:
                stable[name] = v
        state_updates["last_stable_params"] = stable

    if verdict != "GO":
        reason = f"No tuning: scorecard verdict is {verdict}."
        return Recommendation(action="no_change", reason=reason, phase=phase), state_updates

    if curr_dd is not None and curr_dd > hard_dd:
        reason = (
            f"No tuning: hard drawdown breach ({curr_dd:.4f}% > {hard_dd:.4f}%)."
        )
        return Recommendation(action="no_change", reason=reason, phase=phase), state_updates

    if not metrics:
        reason = "No tuning: scorecard metrics are missing in status JSON."
        return Recommendation(action="no_change", reason=reason, phase=phase), state_updates

    pf = _to_float(metrics.get("profit_factor"))
    avg = _to_float(metrics.get("avg_pnl"))
    win = _to_float(metrics.get("win_rate"))

    if None in (pf, avg, win):
        reason = "No tuning: required metrics (profit_factor/avg_pnl/win_rate) missing."
        return Recommendation(action="no_change", reason=reason, phase=phase), state_updates

    # Decide directional mode.
    if avg > 0 and pf >= 1.2 and win >= 50 and (curr_dd is None or curr_dd <= hard_dd * 0.7):
        mode = "aggressive"
        mode_reason = "Performance is healthy; suggest slight growth-oriented step."
    elif avg <= 0 or pf < 1.0:
        mode = "defensive"
        mode_reason = "Performance is weak; suggest safety-oriented step."
    else:
        return (
            Recommendation(
                action="no_change",
                reason=(
                    "No tuning: mixed metrics. Keep current settings for another cycle."
                ),
                phase=phase,
            ),
            state_updates,
        )

    param_cfg_by_name = {
        str(p.get("name")): p for p in policy.get("tunable_parameters", [])
    }

    for param_name in enabled:
        cfg = param_cfg_by_name.get(param_name)
        if not cfg:
            continue
        curr_raw = env_values.get(param_name)
        curr_val = _to_float(curr_raw)
        if curr_val is None:
            # If env value is missing, seed from min.
            curr_val = float(cfg.get("min"))
        new_val = _make_change(cfg, curr_val, mode)
        if new_val is None:
            continue

        state_updates["last_recommendation"] = {
            "parameter": param_name,
            "old_value": curr_val,
            "new_value": new_val,
            "mode": mode,
            "reason": mode_reason,
            "created_utc": _utc_now(),
        }
        return (
            Recommendation(
                action="propose_change",
                reason=mode_reason,
                parameter=param_name,
                old_value=curr_val,
                new_value=new_val,
                phase=phase,
            ),
            state_updates,
        )

    return (
        Recommendation(
            action="no_change",
            reason="No tunable parameter could be adjusted within configured bounds.",
            phase=phase,
        ),
        state_updates,
    )


def _capital_step_hint(policy: Dict[str, Any], status: Dict[str, Any]) -> Dict[str, Any]:
    step_cfg = policy.get("capital_step_up", {})
    if not step_cfg.get("enabled", False):
        return {"eligible": False, "reason": "Capital step-up disabled."}

    metrics = status.get("metrics", {}) or {}
    verdict = str(status.get("verdict", "ERROR")).upper()
    criteria = step_cfg.get("criteria", {})

    pf = _to_float(metrics.get("profit_factor"))
    dd = _to_float(metrics.get("max_drawdown_pct"))
    req_pf = float(criteria.get("min_profit_factor", 1.25))
    max_dd = float(criteria.get("max_drawdown_pct", 2.0))

    if verdict != "GO":
        return {"eligible": False, "reason": f"Verdict is {verdict}."}
    if pf is None or dd is None:
        return {"eligible": False, "reason": "Missing profit_factor or max_drawdown_pct."}
    if pf < req_pf:
        return {"eligible": False, "reason": f"profit_factor {pf:.4f} < {req_pf:.4f}."}
    if dd > max_dd:
        return {"eligible": False, "reason": f"max_drawdown_pct {dd:.4f}% > {max_dd:.4f}%."}

    return {
        "eligible": True,
        "reason": "Current metrics satisfy point-in-time step-up gates.",
        "required_go_streak": int(criteria.get("required_go_streak", 3)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recommend next weekly tuning change")
    parser.add_argument("--policy", default="tuning_policy.json")
    parser.add_argument(
        "--status-json", default="results/scorecards/latest_status.json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--state-file", default="results/scorecards/tuning_recommender_state.json"
    )
    parser.add_argument("--output-json", default="")
    args = parser.parse_args()

    policy = _load_json(args.policy)
    status = _load_json(args.status_json)
    env_values = _parse_env(args.env_file)
    state = _state_load(args.state_file)

    rec, updates = recommend(policy, status, env_values, state)
    state.update(updates)
    _state_save(args.state_file, state)

    capital_hint = _capital_step_hint(policy, status)

    result = {
        "generated_at_utc": _utc_now(),
        "policy": args.policy,
        "status_json": args.status_json,
        "env_file": args.env_file,
        "state_file": args.state_file,
        "phase": rec.phase,
        "recommendation": {
            "action": rec.action,
            "reason": rec.reason,
            "parameter": rec.parameter,
            "old_value": rec.old_value,
            "new_value": rec.new_value,
        },
        "capital_step_hint": capital_hint,
        "acceptance_check": state.get("last_acceptance", {}),
    }

    text_lines = [
        "=== Tuning Recommendation ===",
        f"generated_at_utc: {result['generated_at_utc']}",
        f"phase: {rec.phase}",
        f"action: {rec.action}",
        f"reason: {rec.reason}",
    ]

    if rec.action == "propose_change":
        text_lines.append(
            f"proposed_change: {rec.parameter} {rec.old_value} -> {rec.new_value}"
        )
        text_lines.append(
            f"env_patch: {rec.parameter}={rec.new_value}"
        )

    text_lines.append(
        f"capital_step_eligible: {capital_hint.get('eligible')} ({capital_hint.get('reason')})"
    )

    print("\n".join(text_lines))

    if args.output_json:
        os.makedirs(os.path.dirname(args.output_json), exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=True, indent=2)
            f.write("\n")
        print(f"Recommendation JSON written: {args.output_json}")


if __name__ == "__main__":
    main()
