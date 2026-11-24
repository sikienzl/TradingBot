"""Read AI co-pilot tuning suggestions from the bot log for monitoring.

The bot logs its co-pilot decisions as plain text lines; the PnL exporters turn
the last few of them into Prometheus series. This module owns that parsing: it
reads the tail of the log and returns plain data, leaving the exposition format
to :mod:`src.pnl_prometheus_format`.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from collections import deque
from datetime import UTC, datetime
from typing import Any

from .exporter_paths import BOT_LOG_PATH

SHADOW_SUGGESTION_META_RE = re.compile(
    r'^([a-z]+),\s+confidence=([0-9.]+),\s+reason='
)
SHADOW_SUGGESTION_MARKER = 'AI co-pilot shadow suggestion:'
UNCHANGED_VALUES_MARKER = 'AI co-pilot suggested unchanged values:'
NO_CHANGE_MARKER = 'AI co-pilot suggestion: no change'
LOG_TIME_FORMAT = '%Y-%m-%d %H:%M:%S,%f'
LOG_TAIL_LINES = 4000
MAX_SUGGESTION_RANK = 3


def read_ai_shadow_suggestions(log_path=BOT_LOG_PATH):
    """Return the newest co-pilot suggestions found in the tail of *log_path*."""
    result = {'ai_copilot_shadow_suggestions': []}

    if not os.path.exists(log_path):
        return result

    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            tail_lines = list(deque(f, maxlen=LOG_TAIL_LINES))
        result['ai_copilot_shadow_suggestions'] = _collect_suggestions(tail_lines)
    except (OSError, UnicodeError) as exc:
        print(f"Error reading AI co-pilot suggestions: {exc}", file=sys.stderr)

    return result


def _collect_suggestions(tail_lines: list[str]) -> list[dict[str, Any]]:
    """Walk the log tail backwards and collect at most three suggestions."""
    now = _now()
    suggestions: list[dict[str, Any]] = []
    rank = 1

    for line in reversed(tail_lines):
        if 'AI co-pilot' not in line:
            continue

        try:
            timestamp_raw, remainder = line.split(' - ', 1)
            parsed = _parse_suggestion_payload(remainder)
            if parsed is None:
                continue
            mode, changes, confidence = parsed
            ts = _parse_log_timestamp(timestamp_raw)
            age_minutes = max(0.0, (now - ts).total_seconds() / 60.0)
        except (IndexError, SyntaxError, ValueError):
            continue

        if not isinstance(changes, dict):
            continue

        suggestions.extend(
            _suggestion_series(rank, mode, changes, confidence, age_minutes))
        rank += 1
        if rank > MAX_SUGGESTION_RANK:
            break

    return suggestions


def _parse_suggestion_payload(
    remainder: str,
) -> tuple[str, Any, float] | None:
    """Parse one co-pilot log line into ``(mode, changes, confidence)``.

    Returns ``None`` when the line is not a suggestion at all. Raises
    ``IndexError``/``SyntaxError``/``ValueError`` for a malformed line, which
    the caller treats the same way as "skip this line".
    """
    confidence = 0.0
    mode = 'shadow'

    if SHADOW_SUGGESTION_MARKER in remainder:
        payload = remainder.split(SHADOW_SUGGESTION_MARKER, 1)[1].strip()
        changes_text, meta_text = payload.split(' (risk=', 1)
        changes = ast.literal_eval(changes_text.strip())
        confidence = _suggestion_confidence(meta_text.rstrip(')'))
    elif UNCHANGED_VALUES_MARKER in remainder:
        payload = remainder.split(UNCHANGED_VALUES_MARKER, 1)[1].strip()
        changes = ast.literal_eval(payload)
        mode = 'unchanged'
    elif NO_CHANGE_MARKER in remainder:
        payload = remainder.split('AI co-pilot suggestion:', 1)[1].strip()
        meta_text = payload.split(' (risk=', 1)[1].rstrip(')')
        confidence = _suggestion_confidence(meta_text)
        changes = {'no_change': 0.0}
        mode = 'no_change'
    else:
        return None

    return mode, changes, confidence


def _suggestion_confidence(meta_text: str) -> float:
    """Pull ``confidence=`` out of the trailing ``(risk=..., confidence=...)``."""
    meta_match = SHADOW_SUGGESTION_META_RE.match(meta_text)
    return float(meta_match.group(2)) if meta_match else 0.0


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_log_timestamp(timestamp_raw: str) -> datetime:
    """Parse a bot log timestamp into an aware UTC datetime.

    ``logging.Formatter`` writes ``asctime`` in the bot's local time with no
    offset, so the naive value is interpreted as local time and converted --
    the same assumption ``pnl_exporter._extract_log_line_timestamp()`` makes
    when it calls ``timestamp()`` on it.
    """
    return datetime.strptime(timestamp_raw.strip(), LOG_TIME_FORMAT).astimezone(UTC)


def _suggestion_series(
    rank: int,
    mode: str,
    changes: dict[Any, Any],
    confidence: float,
    age_minutes: float,
):
    """Yield one entry per numeric parameter of a suggestion."""
    for parameter, value in changes.items():
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        yield {
            'rank': rank,
            'suggestion_id': f'{rank}_{parameter}',
            'mode': mode,
            'parameter': str(parameter),
            'value': numeric_value,
            'confidence': confidence,
            'age_minutes': round(age_minutes, 2),
        }
