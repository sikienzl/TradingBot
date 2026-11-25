#!/usr/bin/env python3
"""Export bot safety, Hailo, and research-feed health to a textfile collector."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def _research_age(path: Path, now: datetime) -> float | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    timestamps = [payload.get("timestamp_utc")]
    items = payload.get("news_items", [])
    if isinstance(items, list):
        timestamps.extend(
            item.get("fetched_at") for item in items if isinstance(item, dict))
    parsed = []
    for value in timestamps:
        if not isinstance(value, str):
            continue
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            continue
        if stamp.tzinfo is not None:
            parsed.append(stamp.astimezone(UTC))
    if not parsed:
        return None
    return max(0.0, (now - max(parsed)).total_seconds())


def render_metrics(
    kill_switch_file: Path,
    hailo_health_url: str,
    research_signal_file: Path,
    *,
    now: datetime | None = None,
    health_probe=urlopen,
) -> str:
    now = now or datetime.now(UTC)
    kill_switch_active = int(os.path.lexists(kill_switch_file))
    hailo_enabled = int(bool(hailo_health_url.strip()))
    hailo_up = 0 if hailo_enabled else 1
    if hailo_enabled:
        try:
            with health_probe(hailo_health_url, timeout=3) as response:
                hailo_up = int(200 <= response.status < 300)
        except (URLError, TimeoutError, OSError, ValueError):
            hailo_up = 0
    research_age = _research_age(research_signal_file, now)
    lines = [
        "# HELP trading_kill_switch_active Whether the live trading stop marker exists.",
        "# TYPE trading_kill_switch_active gauge",
        f"trading_kill_switch_active {kill_switch_active}",
        "# HELP trading_hailo_health_check_enabled Whether a Hailo health URL is configured.",
        "# TYPE trading_hailo_health_check_enabled gauge",
        f"trading_hailo_health_check_enabled {hailo_enabled}",
        "# HELP trading_hailo_service_up Whether the configured Hailo service passed its health check.",
        "# TYPE trading_hailo_service_up gauge",
        f"trading_hailo_service_up {hailo_up}",
        "# HELP trading_research_signal_present Whether a parseable timestamped research signal exists.",
        "# TYPE trading_research_signal_present gauge",
        f"trading_research_signal_present {int(research_age is not None)}",
    ]
    if research_age is not None:
        lines.extend([
            "# HELP trading_research_signal_age_seconds Age of the latest research signal.",
            "# TYPE trading_research_signal_age_seconds gauge",
            f"trading_research_signal_age_seconds {research_age:.3f}",
        ])
    return "\n".join(lines) + "\n"


def write_metrics(output: Path, body: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent,
            prefix=f".{output.name}-", delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    root = Path(os.getenv("TRADING_DATA_DIR", "."))
    body = render_metrics(
        Path(os.getenv("TRADING_KILL_SWITCH_FILE", ".trading_kill_switch")),
        os.getenv("HAILO_HEALTH_URL", ""),
        Path(os.getenv("RESEARCH_SIGNAL_PATH", str(root / "research_signal_latest.json"))),
    )
    output = Path(os.getenv(
        "TRADING_HEALTH_METRICS_FILE",
        "results/scorecards/textfile/trading_health.prom",
    ))
    write_metrics(output, body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
