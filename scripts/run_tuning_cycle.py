#!/usr/bin/env python3
"""Run full tuning cycle: recommend first, then optionally apply.

This orchestrator executes:
1) recommend_tuning_change.py
2) apply_tuning_recommendation.py

By default, apply step runs in dry-run mode.
Use --apply to enable actual env writes in step 2.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from glob import glob
from typing import Any

# The script runs from the repository root (systemd sets PYTHONPATH and the Pi
# package ships src/ next to scripts/), but make the import work when the
# script is invoked directly as well.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.scorecard_verdict_history import (
    DEFAULT_STATUS_ENV_FILE,
    DEFAULT_STATUS_JSON_FILE,
    recent_no_go_records,
    resolve_scorecard_status,
    resolve_verdict_history_path,
    status_age_seconds,
)

# A verdict older than this is not evidence about the current market: the
# scorecard timer is weekly, so one missed cycle plus slack is the budget.
DEFAULT_MAX_VERDICT_AGE_SECONDS = 9 * 24 * 60 * 60.0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def _load_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _list_recent_scorecard_reports(path: str, limit: int) -> list[str]:
    pattern = os.path.join(path, "scorecard_*.txt")
    reports = [p for p in glob(pattern) if os.path.isfile(p)]
    reports.sort(key=os.path.getmtime, reverse=True)
    return reports[: max(0, limit)]


def _report_has_no_go(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError:
        return False

    return "VERDICT:              NO-GO" in content or "Verdict summary: NO-GO" in content


def _extract_max_drawdown_pct(path: str) -> float | None:
    """Parse 'Max DD (% of start): 29.54%' from scorecard report."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "Max DD (% of start):" in line:
                    try:
                        pct_str = line.split(
                            ":", 1)[1].strip().replace("%", "")
                        return float(pct_str)
                    except (IndexError, ValueError):
                        return None
    except OSError:
        return None
    return None


def _build_safety_guard_result(args: argparse.Namespace, status: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
    verdict = str(status.get("verdict", "ERROR")).upper()
    reasons: list[str] = []
    recent_reports_checked: list[str] = []
    recent_no_go_reports: list[str] = []
    recent_no_go_verdicts: list[str] = []
    recent_drawdown_violations: list[str] = []
    allow_apply = True

    if args.require_go_verdict and verdict != "GO":
        allow_apply = False
        reasons.append(
            f"Apply blocked: latest verdict is {verdict}, required GO.")

    age_limit = float(args.max_verdict_age_seconds)
    age = status_age_seconds(status)
    if age is None:
        allow_apply = False
        reasons.append(
            "Apply blocked: the scorecard evidence has no usable timestamp.")
    elif age_limit > 0 and age > age_limit:
        allow_apply = False
        reasons.append(
            "Apply blocked: the newest scorecard verdict is "
            f"{age:.0f}s old, above the {age_limit:.0f}s limit."
        )

    if args.block_on_recent_no_go:
        history = str(args.verdict_history or "").strip()
        if history:
            # The verdict history is authoritative: one record per attempted
            # run with the verdict already parsed. The report glob below only
            # exists for deployments that keep the history disabled.
            recent_no_go_verdicts = [
                f"{record.get('timestamp_utc', '?')}:{record.get('verdict', '')}"
                for record in recent_no_go_records(history, args.recent_scorecard_limit)
            ]
            if recent_no_go_verdicts:
                allow_apply = False
                reasons.append(
                    "Apply blocked: recent NO-GO verdict found in the verdict history."
                )
        else:
            recent_reports_checked = _list_recent_scorecard_reports(
                args.scorecard_reports_dir,
                args.recent_scorecard_limit,
            )
            recent_no_go_reports = [
                report for report in recent_reports_checked if _report_has_no_go(report)
            ]
            if recent_no_go_reports:
                allow_apply = False
                reasons.append(
                    "Apply blocked: recent NO-GO scorecard found in checked reports."
                )

    # Drawdown guard: block if any recent report exceeds threshold
    if args.max_drawdown_pct_limit is not None:
        for report in recent_reports_checked or _list_recent_scorecard_reports(
            args.scorecard_reports_dir, args.recent_scorecard_limit
        ):
            dd = _extract_max_drawdown_pct(report)
            if dd is not None and dd > args.max_drawdown_pct_limit:
                recent_drawdown_violations.append(
                    f"{report}: {dd:.2f}% > {args.max_drawdown_pct_limit:.2f}%")
        if recent_drawdown_violations:
            allow_apply = False
            reasons.append(
                f"Apply blocked: max_drawdown_pct exceeds limit in recent reports: {recent_drawdown_violations}"
            )

    if allow_apply:
        reasons.append("Safety guard passed.")

    return {
        "allow_apply": allow_apply,
        "reasons": reasons,
        "require_go_verdict": bool(args.require_go_verdict),
        "block_on_recent_no_go": bool(args.block_on_recent_no_go),
        "recent_scorecard_limit": int(args.recent_scorecard_limit),
        "recent_reports_checked": recent_reports_checked,
        "recent_no_go_reports": recent_no_go_reports,
        "recent_no_go_verdicts": recent_no_go_verdicts,
        "verdict_history": str(args.verdict_history or ""),
        "verdict_age_seconds": age,
        "max_verdict_age_seconds": age_limit,
        "max_drawdown_pct_limit": args.max_drawdown_pct_limit,
        "recent_drawdown_violations": recent_drawdown_violations,
    }


def main() -> None:  # noqa: C901
    parser = argparse.ArgumentParser(
        description="Run recommendation + apply tuning cycle")
    parser.add_argument("--policy", default="tuning_policy.json")
    parser.add_argument(
        "--status-json", default=DEFAULT_STATUS_JSON_FILE)
    parser.add_argument(
        "--status-env",
        default=DEFAULT_STATUS_ENV_FILE,
        help=(
            "Uppercase KEY=value scorecard marker, used when the JSON marker is "
            "absent (the single-Pi profile keeps STATUS_JSON_ENABLED off)."
        ),
    )
    parser.add_argument(
        "--verdict-history",
        default=None,
        help=(
            "Scorecard verdict history JSONL. Last evidence fallback and the "
            "source of the recent NO-GO guard. Defaults to "
            "$SCORECARD_VERDICT_HISTORY_FILE; 'off' disables it."
        ),
    )
    parser.add_argument(
        "--max-verdict-age-seconds",
        type=float,
        default=DEFAULT_MAX_VERDICT_AGE_SECONDS,
        help=(
            "Block real apply when the newest scorecard verdict is older than "
            "this. 0 disables the age guard."
        ),
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--recommender-state",
        default="results/scorecards/tuning_recommender_state.json",
    )
    parser.add_argument(
        "--recommendation-json",
        default="results/scorecards/latest_tuning_recommendation.json",
    )
    parser.add_argument(
        "--apply-summary-json",
        default="results/scorecards/latest_tuning_apply_plan.json",
    )
    parser.add_argument(
        "--cycle-summary-json",
        default="results/scorecards/latest_tuning_cycle_summary.json",
    )
    parser.add_argument(
        "--backup-dir",
        default="results/scorecards/env_backups",
    )
    parser.add_argument("--apply", action="store_true",
                        help="Enable env write in apply step")
    parser.add_argument(
        "--require-go-verdict",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Block real apply unless latest status verdict is GO.",
    )
    parser.add_argument(
        "--block-on-recent-no-go",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Block real apply if any recent scorecard report is NO-GO.",
    )
    parser.add_argument(
        "--recent-scorecard-limit",
        type=int,
        default=3,
        help="How many recent scorecard reports to inspect for NO-GO before allowing apply.",
    )
    parser.add_argument(
        "--scorecard-reports-dir",
        default="results/scorecards",
        help="Directory containing scorecard_*.txt reports for recent NO-GO safety checks.",
    )
    parser.add_argument(
        "--max-drawdown-pct-limit",
        type=float,
        default=None,
        help="Block real apply if any recent scorecard report has max_drawdown_pct above this threshold.",
    )
    args = parser.parse_args()

    if args.verdict_history is None:
        args.verdict_history = resolve_verdict_history_path()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    recommend_script = os.path.join(script_dir, "recommend_tuning_change.py")
    apply_script = os.path.join(script_dir, "apply_tuning_recommendation.py")

    recommend_cmd = [
        sys.executable,
        recommend_script,
        "--policy",
        args.policy,
        "--status-json",
        args.status_json,
        "--status-env",
        args.status_env,
        "--verdict-history",
        args.verdict_history or "off",
        "--env-file",
        args.env_file,
        "--state-file",
        args.recommender_state,
        "--output-json",
        args.recommendation_json,
    ]

    apply_cmd = [
        sys.executable,
        apply_script,
        "--recommendation-json",
        args.recommendation_json,
        "--env-file",
        args.env_file,
        "--backup-dir",
        args.backup_dir,
        "--output-json",
        args.apply_summary_json,
    ]
    os.makedirs(os.path.dirname(args.recommendation_json), exist_ok=True)
    os.makedirs(os.path.dirname(args.apply_summary_json), exist_ok=True)
    os.makedirs(os.path.dirname(args.cycle_summary_json), exist_ok=True)

    rec_proc = _run(recommend_cmd)
    if rec_proc.stdout:
        print(rec_proc.stdout.rstrip())
    if rec_proc.stderr:
        print(rec_proc.stderr.rstrip(), file=sys.stderr)

    if rec_proc.returncode != 0:
        summary = {
            "generated_at_utc": _utc_now(),
            "status": "failed",
            "step": "recommend",
            "returncode": rec_proc.returncode,
            "apply_mode": bool(args.apply),
            "recommend_command": recommend_cmd,
            "stderr": rec_proc.stderr,
        }
        with open(args.cycle_summary_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=True, indent=2)
            f.write("\n")
        print(f"Cycle summary written: {args.cycle_summary_json}")
        raise SystemExit(rec_proc.returncode)

    recommendation = _load_json(args.recommendation_json)
    rec_action = str(recommendation.get(
        "recommendation", {}).get("action") or "no_change")

    status_payload = resolve_scorecard_status(
        status_json_file=args.status_json,
        status_env_file=args.status_env,
        history_file=args.verdict_history or "",
    )
    if status_payload is None:
        # No marker and no history: there is no evidence about the last
        # scorecard, so the cycle stops without touching .env. Fail closed with
        # a summary an operator can read instead of a traceback.
        summary = {
            "generated_at_utc": _utc_now(),
            "status": "blocked",
            "step": "safety_guard",
            "reason": "no scorecard verdict evidence",
            "status_json": args.status_json,
            "status_env": args.status_env,
            "verdict_history": args.verdict_history,
            "apply_requested": bool(args.apply),
            "apply_effective": False,
            "recommendation_action": rec_action,
            "recommend_returncode": rec_proc.returncode,
        }
        with open(args.cycle_summary_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=True, indent=2)
            f.write("\n")
        print(
            "Apply blocked: no scorecard verdict evidence in "
            f"{args.status_json}, {args.status_env} or {args.verdict_history}."
        )
        print(f"Cycle summary written: {args.cycle_summary_json}")
        raise SystemExit(3)

    safety_guard = _build_safety_guard_result(args, status_payload)
    apply_effective = bool(args.apply and safety_guard["allow_apply"])

    if args.apply and not safety_guard["allow_apply"]:
        for reason in safety_guard["reasons"]:
            print(reason)

    if apply_effective:
        apply_cmd.append("--apply")

    apply_proc = _run(apply_cmd)
    if apply_proc.stdout:
        print(apply_proc.stdout.rstrip())
    if apply_proc.stderr:
        print(apply_proc.stderr.rstrip(), file=sys.stderr)

    status = "ok" if apply_proc.returncode == 0 else "failed"
    summary = {
        "generated_at_utc": _utc_now(),
        "status": status,
        "apply_requested": bool(args.apply),
        "apply_effective": apply_effective,
        "recommendation_action": rec_action,
        "status_source": str(status_payload.get("source", "")),
        "status_verdict": str(status_payload.get("verdict", "")),
        "recommendation_json": args.recommendation_json,
        "apply_summary_json": args.apply_summary_json,
        "cycle_summary_json": args.cycle_summary_json,
        "recommend_returncode": rec_proc.returncode,
        "apply_returncode": apply_proc.returncode,
        "recommend_command": recommend_cmd,
        "apply_command": apply_cmd,
        "safety_guard": safety_guard,
    }

    with open(args.cycle_summary_json, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=True, indent=2)
        f.write("\n")

    print(f"Cycle summary written: {args.cycle_summary_json}")

    if apply_proc.returncode != 0:
        raise SystemExit(apply_proc.returncode)


if __name__ == "__main__":
    main()
