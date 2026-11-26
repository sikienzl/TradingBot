#!/usr/bin/env python3
"""Promote or roll back an immutable paired CPU/Hailo model release.

Promotion is gated on a fresh ``GO`` scorecard verdict: the scorecard is the
only evidence this repository produces that the recent trading record is
healthy, so swapping the active model without it is not allowed. Rollback is
deliberately **not** gated - reverting to a previously validated release must
always stay possible, including immediately after a ``NO-GO``.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from src.model_release import promote_release, rollback_release
from src.scorecard_gate import (
    DEFAULT_MAX_AGE_SECONDS,
    ScorecardGateSettings,
    scorecard_gate_refusal,
    scorecard_gate_settings,
)

PROMOTION_ACTION = "Model promotion"


def resolve_gate_settings(args: argparse.Namespace) -> ScorecardGateSettings | None:
    """Gate configuration for the CLI, or ``None`` when the operator opted out."""
    if args.no_require_go_verdict:
        return None
    settings = scorecard_gate_settings(
        default_max_age_seconds=args.scorecard_max_age_seconds,
    )
    if args.scorecard_status_json is not None:
        settings = replace(settings, status_json_file=args.scorecard_status_json)
    if args.scorecard_status_env is not None:
        settings = replace(settings, status_env_file=args.scorecard_status_env)
    return settings


def require_scorecard_go(
    settings: ScorecardGateSettings | None,
    *,
    allow_maintenance_override: bool = False,
) -> None:
    """Refuse promotion unless a fresh ``GO`` scorecard marker is on disk."""
    if settings is None:
        print(
            "WARNING: scorecard GO gate disabled by --no-require-go-verdict; "
            "promoting without scorecard evidence.",
        )
        return
    status = settings.evaluate(allow_maintenance_override=allow_maintenance_override)
    if not status.allowed:
        raise SystemExit(scorecard_gate_refusal(PROMOTION_ACTION, status, settings))
    print(
        f"Scorecard gate accepted: verdict={status.verdict} "
        f"age={status.age_seconds or 0.0:.0f}s limit={settings.max_age_seconds:.0f}s"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    promote = subparsers.add_parser("promote")
    promote.add_argument("--release-root", type=Path, required=True)
    promote.add_argument("--release-id", required=True)
    promote.add_argument("--cpu-model", type=Path, required=True)
    promote.add_argument("--cpu-metadata", type=Path, required=True)
    promote.add_argument("--hailo-hef", type=Path, required=True)
    promote.add_argument("--hailo-metadata", type=Path, required=True)
    promote.add_argument(
        "--scorecard-status-json",
        type=Path,
        default=None,
        help="Scorecard status JSON marker to gate on "
        "(default: $SCORECARD_STATUS_JSON)",
    )
    promote.add_argument(
        "--scorecard-status-env",
        type=Path,
        default=None,
        help="Scorecard status env marker to gate on "
        "(default: $SCORECARD_STATUS_ENV)",
    )
    promote.add_argument(
        "--scorecard-max-age-seconds",
        type=float,
        default=DEFAULT_MAX_AGE_SECONDS,
        help="Maximum accepted age of the scorecard verdict in seconds",
    )
    promote.add_argument(
        "--allow-maintenance-override",
        action="store_true",
        help="Accept a GO verdict produced by a maintenance-override run",
    )
    promote.add_argument(
        "--no-require-go-verdict",
        action="store_true",
        help="Operator override: promote without a fresh GO scorecard verdict",
    )
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--release-root", type=Path, required=True)
    rollback.add_argument("--release-id", required=True)
    args = parser.parse_args()

    if args.command == "promote":
        require_scorecard_go(
            resolve_gate_settings(args),
            allow_maintenance_override=args.allow_maintenance_override,
        )
        path = promote_release(
            args.release_root,
            args.release_id,
            args.cpu_model,
            args.cpu_metadata,
            args.hailo_hef,
            args.hailo_metadata,
        )
    else:
        path = rollback_release(args.release_root, args.release_id)
    print(f"Active model release: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
