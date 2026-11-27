#!/usr/bin/env python3
"""Promote, validate or roll back an immutable paired CPU/Hailo model release.

Promotion is gated on a fresh ``GO`` scorecard verdict: the scorecard is the
only evidence this repository produces that the recent trading record is
healthy, so swapping the active model without it is not allowed. Rollback is
deliberately **not** gated - reverting to a previously validated release must
always stay possible, including immediately after a ``NO-GO``.

``validate`` is the read-only inspection of that same gate's subject: it checks a
stored release's manifest shape, its full validation reports and every artifact
checksum without touching the ``active`` symlink.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from src.model_release import (
    promote_release,
    release_directory,
    rollback_release,
    validate_release,
)
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


def describe_release(manifest: dict[str, Any]) -> str:
    """Operator-facing summary of a validated release manifest.

    The point of ``validate`` is to answer "what is this release, and what
    evidence backs it?" without opening four files by hand, so the summary keeps
    the contract, the provenance and the per-side validation numbers.
    """
    lines = [
        f"Release: {manifest['release_id']}",
        (
            f"Feature contract: {manifest['feature_contract']} "
            f"({len(manifest['features'])} features)"
        ),
        f"Label map: {json.dumps(manifest['label_map'], sort_keys=True)}",
        f"Training data sha256: {manifest['training_data_sha256']}",
    ]
    for side_name in ("cpu", "hailo"):
        report = manifest["validation_reports"][side_name]
        validation = report["validation"]
        provenance = report["provenance"]
        lines.append(
            f"{side_name}: rows={validation['rows']} "
            f"accuracy={float(validation['accuracy']):.4f} "
            f"macro_f1={float(validation['macro_f1']):.4f} "
            f"threshold_macro_f1={float(validation['threshold_macro_f1']):.4f} "
            f"threshold_coverage={float(validation['threshold_coverage']):.4f} "
            f"train_rows={provenance['train_rows']} "
            f"label_horizon={provenance['label_horizon']}"
        )
        folds = report.get("walk_forward") or []
        if folds:
            summary = report.get("walk_forward_summary") or {}
            lines.append(
                f"{side_name} walk-forward: folds={len(folds)} "
                f"accuracy={float(summary.get('accuracy', 0.0)):.4f} "
                f"macro_f1={float(summary.get('macro_f1', 0.0)):.4f}"
            )
        else:
            lines.append(f"{side_name} walk-forward: not recorded")
    for name, artifact in sorted(manifest["artifacts"].items()):
        lines.append(f"artifact {name}: {artifact['path']} sha256={artifact['sha256']}")
    return "\n".join(lines)


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
    validate = subparsers.add_parser(
        "validate",
        help="Check a stored release manifest, reports and artifact checksums "
             "without changing the active release",
    )
    validate.add_argument("--release-root", type=Path, required=True)
    validate.add_argument("--release-id", required=True)
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
    elif args.command == "validate":
        release = release_directory(args.release_root, args.release_id)
        try:
            manifest = validate_release(release)
        except (OSError, TypeError, ValueError) as exc:
            # A release that cannot be inspected must not look like a success:
            # operators run this before trusting a rollback target.
            print(f"Model release is not valid: {exc}", file=sys.stderr)
            return 1
        print(describe_release(manifest))
        print(f"Model release validated: {release}")
        return 0
    else:
        path = rollback_release(args.release_root, args.release_id)
    print(f"Active model release: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
