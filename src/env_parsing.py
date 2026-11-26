"""Reusable environment-variable parsers for bot configuration.

Extracted from :mod:`src.bot_config` so the parsing rules live in one place and
stay unit-testable without constructing a :class:`~src.bot_config.BotConfig`.
Every helper reads the real process environment; nothing here knows about
trading, so the module stays dependency-free.
"""
from __future__ import annotations

import math
import os


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def env_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().strip('"').strip("'")


def env_optional_float(name: str) -> float | None:
    """Reads an optional float. Empty means 'not configured'."""
    raw = env_str(name, "")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def symbol_int_map(name: str, default: str = "") -> dict[str, int]:
    raw = env_str(name, default)
    if not raw:
        return {}

    parsed: dict[str, int] = {}
    for item in raw.split(','):
        token = item.strip()
        if not token or ':' not in token:
            continue
        symbol, value = token.split(':', 1)
        symbol = symbol.strip().upper()
        value = value.strip()
        if not symbol or not value:
            continue
        try:
            parsed[symbol] = int(value)
        except ValueError:
            continue
    return parsed


def symbol_float_map(name: str, default: str = "") -> dict[str, float]:
    raw = env_str(name, default)
    if not raw:
        return {}

    parsed: dict[str, float] = {}
    for item in raw.split(','):
        token = item.strip()
        if not token or ':' not in token:
            continue
        symbol, value = token.split(':', 1)
        symbol = symbol.strip().upper()
        value = value.strip()
        if not symbol or not value:
            continue
        try:
            parsed[symbol] = float(value)
        except ValueError:
            continue
    return parsed


def threshold_float_pairs(name: str, default: str = "") -> list[tuple[float, float]]:
    raw = env_str(name, default)
    if not raw:
        return []

    parsed: list[tuple[float, float]] = []
    for item in raw.split(','):
        token = item.strip()
        if not token or ':' not in token:
            continue
        threshold, value = token.split(':', 1)
        try:
            parsed.append(
                (float(threshold.strip()), float(value.strip())))
        except ValueError:
            continue
    return sorted(parsed, key=lambda entry: entry[0])


def threshold_int_pairs(name: str, default: str = "") -> list[tuple[float, int]]:
    raw = env_str(name, default)
    if not raw:
        return []

    parsed: list[tuple[float, int]] = []
    for item in raw.split(','):
        token = item.strip()
        if not token or ':' not in token:
            continue
        threshold, value = token.split(':', 1)
        try:
            parsed.append(
                (float(threshold.strip()), int(value.strip())))
        except ValueError:
            continue
    return sorted(parsed, key=lambda entry: entry[0])
