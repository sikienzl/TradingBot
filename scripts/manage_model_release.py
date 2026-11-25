#!/usr/bin/env python3
"""Promote or roll back an immutable paired CPU/Hailo model release."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.model_release import promote_release, rollback_release


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
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--release-root", type=Path, required=True)
    rollback.add_argument("--release-id", required=True)
    args = parser.parse_args()

    if args.command == "promote":
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
