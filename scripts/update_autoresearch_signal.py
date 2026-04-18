import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


def _discover_repo_candidates() -> List[str]:
    """Returns likely local AutoResearch repository paths."""
    home = os.path.expanduser("~")
    hints = [
        os.path.join(home, "AutoResearch"),
        os.path.join(home, "autoresearch"),
        os.path.join(home, "code", "AutoResearch"),
        os.path.join(home, "code", "autoresearch"),
        os.path.join(home, "projects", "AutoResearch"),
        os.path.join(home, "projects", "autoresearch"),
    ]

    candidates: List[str] = []
    for path in hints:
        if os.path.isdir(path):
            candidates.append(path)

    # Shallow scan in common dev folders to avoid expensive full-home traversal.
    scan_roots = [
        os.path.join(home, "code"),
        os.path.join(home, "projects"),
        home,
    ]
    keywords = ("autoresearch", "auto-research", "karpathy")
    for base in scan_roots:
        if not os.path.isdir(base):
            continue
        try:
            for entry in os.scandir(base):
                if not entry.is_dir():
                    continue
                name_l = entry.name.lower()
                if any(k in name_l for k in keywords):
                    candidates.append(entry.path)
        except PermissionError:
            continue

    # De-duplicate while preserving order.
    seen = set()
    unique = []
    for path in candidates:
        norm = os.path.abspath(path)
        if norm in seen:
            continue
        seen.add(norm)
        unique.append(norm)
    return unique


def _suggest_commands(repo_path: str, output_path: str) -> List[str]:
    """Builds a short list of probable start commands."""
    py = os.path.join(ROOT_DIR, ".venv", "bin", "python")
    python_cmd = py if os.path.exists(py) else "python3"
    candidates: List[str] = []

    if os.path.exists(os.path.join(repo_path, "main.py")):
        candidates.append(f"{python_cmd} main.py --output {output_path}")
    if os.path.exists(os.path.join(repo_path, "run.py")):
        candidates.append(f"{python_cmd} run.py --output {output_path}")

    candidates.append(f"{python_cmd} -m autoresearch --output {output_path}")
    candidates.append(f"{python_cmd} -m auto_research --output {output_path}")

    if os.path.exists(os.path.join(repo_path, "package.json")):
        candidates.append(f"npm run research -- --output {output_path}")
        candidates.append(f"npm start -- --output {output_path}")

    return candidates


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_research_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, float]:
    raw = payload or {}
    regime = str(raw.get("market_regime", "sideways")
                 or "sideways").strip().lower()

    features = {
        "research_sentiment_score": _to_float(raw.get("sentiment_score"), 0.0),
        "research_confidence": _to_float(raw.get("confidence"), 0.0),
        "research_risk_score": _to_float(raw.get("risk_score"), 0.0),
        "research_regime_bull": 1.0 if regime == "bull" else 0.0,
        "research_regime_bear": 1.0 if regime == "bear" else 0.0,
        "research_regime_sideways": 1.0 if regime not in {"bull", "bear"} else 0.0,
    }

    features["research_sentiment_score"] = max(
        -1.0, min(1.0, features["research_sentiment_score"]))
    features["research_confidence"] = max(
        0.0, min(1.0, features["research_confidence"]))
    features["research_risk_score"] = max(
        0.0, min(1.0, features["research_risk_score"]))
    return features


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _is_stale(payload: Dict[str, Any], max_age_minutes: int) -> bool:
    if max_age_minutes <= 0:
        return False
    ts = _parse_timestamp(payload.get("timestamp_utc"))
    if ts is None:
        return True
    age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
    return age_seconds > (max_age_minutes * 60)


def _run_command(command: str, cwd: str) -> None:
    completed = subprocess.run(
        command,
        shell=True,
        cwd=cwd,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"AutoResearch command failed with exit code {completed.returncode}"
        )


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Research payload must be a JSON object")
    return data


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _build_output_payload(raw_payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized = _normalize_research_payload(raw_payload)
    out: Dict[str, Any] = {
        "timestamp_utc": raw_payload.get("timestamp_utc")
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sentiment_score": _as_float(raw_payload.get("sentiment_score"), 0.0),
        "confidence": _as_float(raw_payload.get("confidence"), 0.0),
        "risk_score": _as_float(raw_payload.get("risk_score"), 0.0),
        "market_regime": str(raw_payload.get("market_regime", "sideways")),
        "citations": raw_payload.get("citations", []),
        "normalized_features": normalized,
        "integration": {
            "source": "autoresearch",
            "written_at_utc": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        },
    }
    return out


def _build_neutral_payload(reason: str = "") -> Dict[str, Any]:
    now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {
        "timestamp_utc": now_utc,
        "sentiment_score": 0.0,
        "confidence": 0.0,
        "risk_score": 0.0,
        "market_regime": "sideways",
        "citations": [],
    }
    out = _build_output_payload(payload)
    out["integration"]["source"] = "autoresearch_fallback"
    if reason:
        out["integration"]["fallback_reason"] = reason
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run AutoResearch and write canonical signal JSON for model features."
    )
    parser.add_argument(
        "--output",
        default="data/research_signal_latest.json",
        help="Canonical output JSON consumed by train/predict.",
    )
    parser.add_argument(
        "--command",
        default="",
        help="Optional AutoResearch command to run before reading JSON.",
    )
    parser.add_argument(
        "--source",
        default="",
        help="Optional source JSON path produced by command. If empty, output path is used.",
    )
    parser.add_argument(
        "--max-age-minutes",
        type=int,
        default=180,
        help="Maximum allowed payload age from timestamp_utc (<=0 disables).",
    )
    parser.add_argument(
        "--allow-stale",
        action="store_true",
        help="Do not fail when payload is older than max-age-minutes.",
    )
    parser.add_argument(
        "--repo-path",
        default="",
        help="Optional AutoResearch repository path used for discovery/help text.",
    )
    parser.add_argument(
        "--fallback-neutral",
        action="store_true",
        help="Write neutral fallback signal on failure instead of raising an error.",
    )
    parser.add_argument(
        "--fallback-reason",
        default="",
        help="Optional custom reason string persisted in fallback metadata.",
    )
    args = parser.parse_args()

    output_path = os.path.join(ROOT_DIR, args.output) if not os.path.isabs(
        args.output) else args.output
    source_path_arg = args.source.strip()
    source_path = source_path_arg or output_path
    if source_path_arg and not os.path.isabs(source_path_arg):
        source_path = os.path.join(ROOT_DIR, source_path_arg)

    repo_path = args.repo_path.strip() or os.getenv(
        "AUTORESEARCH_REPO_PATH", "").strip()
    if repo_path and not os.path.isabs(repo_path):
        repo_path = os.path.join(ROOT_DIR, repo_path)

    command = args.command.strip() or os.getenv("AUTORESEARCH_CMD", "").strip()
    fallback_neutral = args.fallback_neutral or _env_bool(
        "AUTORESEARCH_WRITE_NEUTRAL_FALLBACK", False)
    fallback_reason = args.fallback_reason.strip() or os.getenv(
        "AUTORESEARCH_FALLBACK_REASON", "").strip()

    try:
        if not command and not os.path.exists(source_path):
            candidate_repos = [
                repo_path] if repo_path else _discover_repo_candidates()
            help_lines = [
                "No AutoResearch command configured and no source JSON found.",
                "Set AUTORESEARCH_CMD or pass --command.",
            ]
            if candidate_repos:
                chosen = os.path.abspath(candidate_repos[0])
                help_lines.append(f"Candidate repo: {chosen}")
                help_lines.append("Try one of these commands:")
                for cmd in _suggest_commands(chosen, source_path):
                    help_lines.append(f"  - {cmd}")
            else:
                help_lines.append(
                    "No local AutoResearch repo candidate found. Clone it first, then set AUTORESEARCH_CMD."
                )
            raise RuntimeError("\n".join(help_lines))

        if command:
            run_cwd = os.path.abspath(repo_path) if repo_path else ROOT_DIR
            _run_command(command=command, cwd=run_cwd)

        if not os.path.exists(source_path):
            raise FileNotFoundError(
                f"Research source JSON not found: {source_path}")

        if source_path != output_path:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            shutil.copyfile(source_path, output_path)

        payload = _load_json(output_path)
        if _is_stale(payload, args.max_age_minutes) and not args.allow_stale:
            raise RuntimeError(
                "Research payload is stale. Increase --max-age-minutes or pass --allow-stale."
            )

        canonical_payload = _build_output_payload(payload)
    except Exception as exc:
        if not fallback_neutral:
            raise
        reason = fallback_reason or str(exc)
        canonical_payload = _build_neutral_payload(reason=reason)
        print("AutoResearch unavailable, writing neutral fallback signal.")
        print(f"Reason: {reason}")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(canonical_payload, f, ensure_ascii=True, indent=2)

    features = canonical_payload.get("normalized_features", {})
    print("AutoResearch signal updated")
    print(f"Output: {output_path}")
    print(f"Sentiment: {features.get('research_sentiment_score', 0.0):.3f}")
    print(f"Confidence: {features.get('research_confidence', 0.0):.3f}")
    print(f"Risk: {features.get('research_risk_score', 0.0):.3f}")


if __name__ == "__main__":
    main()
