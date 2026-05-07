#!/usr/bin/env python3
"""Apply one tuning recommendation to an env file with automatic backup.

Default behavior is dry-run (no file modifications).
Use --apply to actually write changes.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _format_env_value(value: Any) -> str:
    try:
        num = float(value)
        if abs(num - round(num)) < 1e-12:
            return str(int(round(num)))
        text = f"{num:.8f}".rstrip("0").rstrip(".")
        return text if text else "0"
    except (TypeError, ValueError):
        return str(value)


def _replace_or_append(lines: List[str], key: str, value: str) -> Tuple[List[str], Optional[str], bool]:
    old_value: Optional[str] = None
    replaced = False

    prefixes = (f"{key}=", f"export {key}=")
    out: List[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue

        if stripped.startswith(prefixes):
            if old_value is None:
                old_value = stripped.split(
                    "=", 1)[1].strip().strip('"').strip("'")
            if not replaced:
                out.append(f"{key}={value}\n")
                replaced = True
            else:
                # Drop duplicate active definitions after first replacement.
                continue
        else:
            out.append(line)

    if not replaced:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        out.append(f"{key}={value}\n")

    return out, old_value, replaced


def _backup_file(src: str, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"{os.path.basename(src)}.{ts}.bak")
    with open(src, "r", encoding="utf-8", errors="ignore") as fin, open(
        backup_path, "w", encoding="utf-8"
    ) as fout:
        fout.write(fin.read())
    return backup_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply tuning recommendation to env file")
    parser.add_argument(
        "--recommendation-json",
        default="results/scorecards/latest_tuning_recommendation.json",
        help="Path to recommendation JSON produced by recommend_tuning_change.py",
    )
    parser.add_argument("--env-file", default=".env", help="Target env file")
    parser.add_argument(
        "--backup-dir",
        default="results/scorecards/env_backups",
        help="Directory for env backups before write",
    )
    parser.add_argument("--apply", action="store_true",
                        help="Actually write to env file")
    parser.add_argument("--output-json", default="",
                        help="Optional operation summary JSON")
    args = parser.parse_args()

    payload = _load_json(args.recommendation_json)
    rec = payload.get("recommendation", {}) or {}

    action = str(rec.get("action") or "no_change")
    parameter = rec.get("parameter")
    new_value_raw = rec.get("new_value")
    reason = str(rec.get("reason") or "")

    summary: Dict[str, Any] = {
        "generated_at_utc": _utc_now(),
        "recommendation_json": args.recommendation_json,
        "env_file": args.env_file,
        "apply_mode": bool(args.apply),
        "recommendation_action": action,
        "status": "skipped",
        "message": "",
        "backup_file": "",
        "parameter": parameter,
        "old_value": None,
        "new_value": new_value_raw,
    }

    if action != "propose_change":
        summary["message"] = f"No env change: recommendation action is {action}."
        print(summary["message"])
        if reason:
            print(f"reason: {reason}")
    elif not parameter or new_value_raw is None:
        summary["message"] = "No env change: recommendation is missing parameter/new_value."
        print(summary["message"])
    elif not os.path.exists(args.env_file):
        summary["message"] = f"No env change: env file not found: {args.env_file}"
        print(summary["message"])
    else:
        new_value = _format_env_value(new_value_raw)
        with open(args.env_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        updated_lines, old_value, replaced_existing = _replace_or_append(
            lines=lines, key=str(parameter), value=new_value
        )

        summary["old_value"] = old_value
        summary["new_value"] = new_value
        summary["status"] = "planned"
        summary["message"] = (
            f"Planned change: {parameter}={new_value} "
            f"({'replace' if replaced_existing else 'append'})"
        )
        print(summary["message"])

        if args.apply:
            backup_file = _backup_file(args.env_file, args.backup_dir)
            with open(args.env_file, "w", encoding="utf-8") as f:
                f.writelines(updated_lines)
            summary["status"] = "applied"
            summary["backup_file"] = backup_file
            summary["message"] = (
                f"Applied change: {parameter}={new_value}; backup={backup_file}"
            )
            print(summary["message"])
        else:
            print("dry_run: true (use --apply to write changes)")

    if args.output_json:
        out_dir = os.path.dirname(args.output_json)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=True, indent=2)
            f.write("\n")
        print(f"Summary JSON written: {args.output_json}")


if __name__ == "__main__":
    main()
