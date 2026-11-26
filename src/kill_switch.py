"""Persistent trading kill switch.

The kill switch is the last independent guardrail in front of a live order: it
is checked immediately before every submission and it is never weakened by a
signal source. Activation must survive a crash, so the marker is created
atomically with ``O_EXCL`` and fsynced, and a failure to persist it stops the
process instead of letting it keep trading.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def kill_switch_active(config: Any) -> bool:
    """True when the configured kill-switch marker exists (including a dangling link)."""
    path = os.fspath(getattr(config, 'kill_switch_file', '.trading_kill_switch'))
    return os.path.lexists(path)


def activate_kill_switch(config: Any, reason: str, logger: logging.Logger = logger) -> None:
    """Creates the persistent kill-switch marker for ``reason``.

    An existing marker is left untouched. A marker that cannot be written raises
    :class:`RuntimeError`: live trading must stop rather than continue with an
    unpersisted uncertainty.
    """
    path = os.fspath(getattr(config, 'kill_switch_file', '.trading_kill_switch'))
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0),
            0o600,
        )
    except FileExistsError:
        return
    except OSError as exc:
        logger.critical(
            "Cannot persist kill switch after uncertain order outcome at %s: %s",
            path,
            exc,
        )
        raise RuntimeError(
            "Cannot persist live execution uncertainty; stopping the bot"
        ) from exc
    try:
        os.write(descriptor, f"uncertain_order_outcome: {reason}\n".encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory_descriptor = os.open(
        os.path.dirname(os.path.abspath(path)) or ".", os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    logger.critical(
        "Persistent kill switch activated after uncertain order outcome at %s",
        path,
    )
