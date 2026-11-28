"""Durable daily-risk and loss-streak state for the trading bot.

The daily loss anchor, the loss-streak counter and the BUY pause are hard risk
guardrails: audit point 4 requires that they survive a restart instead of being
rebuilt from scratch by a fresh process. This module owns the on-disk shape, the
atomic write and the fail-closed read, so :mod:`src.trading_bot` only has to map
its own attributes to :class:`RiskState`.

Reads never raise: a missing file yields a neutral state, an unreadable or
incompatible file yields ``RiskState(unreadable=True)`` and the caller must treat
that as "risk state unknown" (new entries blocked) rather than as "no risk
state". Writes raise so a live process can latch its kill switch instead of
trading with state it could not persist.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Schema version of the risk-state file; incompatible versions fail closed.
RISK_STATE_VERSION = 1
#: Timestamps are stored in UTC ISO-8601 with an explicit offset.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S+00:00"


@dataclass
class RiskState:
    """Daily-risk and loss-streak bookkeeping that must outlive a restart."""

    anchor_date: date | None = None
    anchor_value: float = 0.0
    consecutive_losses: int = 0
    buy_pause_until_utc: datetime | None = None
    #: Timestamps of recent BUYs, used by the hourly BUY budget.
    buy_timestamps_utc: list[datetime] | None = None
    #: Last SELL per coin, used by the re-entry cooldown.
    last_sell_timestamps_utc: dict[str, datetime] | None = None
    #: True when the state file exists but cannot be trusted.
    unreadable: bool = False
    #: True when the state was loaded from disk instead of starting neutral.
    restored: bool = False


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Backward-compatible: naive timestamps were always written as UTC.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def risk_state_to_payload(state: RiskState, *, now_utc: datetime | None = None) -> dict[str, Any]:
    """Serialises ``state`` into the on-disk JSON document."""
    now = now_utc or datetime.now(UTC)
    return {
        'version': RISK_STATE_VERSION,
        'updated_utc': now.strftime(TIMESTAMP_FORMAT),
        'daily_anchor_date': (
            state.anchor_date.isoformat() if state.anchor_date else None
        ),
        'daily_anchor_value': float(state.anchor_value),
        'consecutive_losses': int(state.consecutive_losses),
        'buy_pause_until_utc': (
            state.buy_pause_until_utc.astimezone(UTC).strftime(TIMESTAMP_FORMAT)
            if state.buy_pause_until_utc else None
        ),
        'buy_timestamps_utc': [
            timestamp.astimezone(UTC).strftime(TIMESTAMP_FORMAT)
            for timestamp in (state.buy_timestamps_utc or [])
        ],
        'last_sell_utc': {
            coin: timestamp.astimezone(UTC).strftime(TIMESTAMP_FORMAT)
            for coin, timestamp in (state.last_sell_timestamps_utc or {}).items()
        },
    }


def parse_risk_state(payload: Any) -> RiskState:  # noqa: C901
    """Builds a :class:`RiskState` from a decoded JSON document.

    Raises :class:`TypeError` for a document with the wrong shape and
    :class:`ValueError` for a value that is not a trustworthy risk state, so a
    corrupt file can never silently reset the guardrails.
    """
    if not isinstance(payload, dict):
        raise TypeError("Risk state document is not an object")
    version = payload.get('version')
    if version != RISK_STATE_VERSION:
        raise ValueError(f"Unsupported risk state version: {version!r}")

    anchor_raw = payload.get('daily_anchor_value', 0.0)
    anchor_value = _finite_float(anchor_raw) if anchor_raw is not None else 0.0
    if anchor_value is None or anchor_value < 0:
        raise ValueError("Risk state has an invalid daily anchor value")

    losses_raw = payload.get('consecutive_losses', 0)
    if isinstance(losses_raw, bool) or not isinstance(losses_raw, int):
        raise TypeError("Risk state has an invalid loss streak")
    if losses_raw < 0:
        raise ValueError("Risk state has a negative loss streak")

    pause_raw = payload.get('buy_pause_until_utc')
    pause_until = None
    if pause_raw is not None:
        pause_until = _parse_datetime(pause_raw)
        if pause_until is None:
            raise ValueError("Risk state has an invalid BUY pause timestamp")

    anchor_raw_date = payload.get('daily_anchor_date')
    anchor_date = None
    if anchor_raw_date is not None:
        anchor_date = _parse_date(anchor_raw_date)
        if anchor_date is None:
            raise ValueError("Risk state has an invalid daily anchor date")

    buys_raw = payload.get('buy_timestamps_utc') or []
    if not isinstance(buys_raw, list):
        raise TypeError("Risk state has an invalid BUY budget")
    buy_timestamps: list[datetime] = []
    for entry in buys_raw:
        parsed = _parse_datetime(entry)
        if parsed is None:
            raise ValueError("Risk state has an invalid BUY budget timestamp")
        buy_timestamps.append(parsed)

    sells_raw = payload.get('last_sell_utc') or {}
    if not isinstance(sells_raw, dict):
        raise TypeError("Risk state has an invalid re-entry cooldown map")
    last_sell: dict[str, datetime] = {}
    for coin, entry in sells_raw.items():
        parsed = _parse_datetime(entry)
        if parsed is None or not isinstance(coin, str) or not coin:
            raise ValueError("Risk state has an invalid re-entry cooldown timestamp")
        last_sell[coin.upper()] = parsed

    return RiskState(
        anchor_date=anchor_date,
        anchor_value=anchor_value,
        consecutive_losses=losses_raw,
        buy_pause_until_utc=pause_until,
        buy_timestamps_utc=buy_timestamps,
        last_sell_timestamps_utc=last_sell,
    )


def read_risk_state(path: str | None, *, logger: logging.Logger = logger) -> RiskState:
    """Loads the risk state, failing closed on an unreadable file.

    A missing file is a legitimate neutral state (first run). Anything that
    exists but cannot be parsed, decoded or understood returns
    ``RiskState(unreadable=True)`` so the caller blocks new entries instead of
    starting the daily-loss budget and the loss streak from zero.
    """
    if not path:
        return RiskState()
    if not os.path.lexists(path):
        return RiskState()
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.error(
            "Risk state at %s could not be read (%s); new entries stay blocked "
            "until it is repaired or removed by an operator.",
            path, exc,
        )
        return RiskState(unreadable=True)
    try:
        state = parse_risk_state(payload)
    except (ValueError, TypeError) as exc:
        logger.error(
            "Risk state at %s is invalid (%s); new entries stay blocked until "
            "it is repaired or removed by an operator.",
            path, exc,
        )
        return RiskState(unreadable=True)
    state.restored = True
    return state


def write_risk_state(
    state: RiskState,
    path: str | None,
    *,
    now_utc: datetime | None = None,
    logger: logging.Logger = logger,
) -> bool:
    """Atomically persists the risk state (temp file, fsync, ``os.replace``).

    Raises :class:`RuntimeError` on failure: risk state that cannot be written
    must stop a live process, not be dropped with a warning.
    """
    if not path:
        return False
    directory = os.path.dirname(os.path.abspath(path))
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix='.risk-state-', suffix='.json', dir=directory)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(risk_state_to_payload(state, now_utc=now_utc), handle,
                      indent=2, ensure_ascii=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        logger.debug("Risk state saved to %s", path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.error("Risk state could not be saved to %s: %s", path, exc)
        raise RuntimeError("Cannot persist risk state; stopping") from exc
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
