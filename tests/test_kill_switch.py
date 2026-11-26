"""Unit tests for the persistent kill switch (audit point 5).

The marker is the last independent guardrail before a live order, so it must be
visible to a fresh process and must never be created only in memory.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from src.kill_switch import activate_kill_switch, kill_switch_active


@dataclass
class _Config:
    kill_switch_file: str = ".trading_kill_switch"


def test_inactive_without_marker(tmp_path):
    assert kill_switch_active(_Config(str(tmp_path / "STOP"))) is False


def test_activation_persists_the_reason(tmp_path, caplog):
    config = _Config(str(tmp_path / "STOP"))
    with caplog.at_level(logging.CRITICAL):
        activate_kill_switch(config, "BUY BTC: timeout", logging.getLogger("t"))
    text = (tmp_path / "STOP").read_text(encoding="utf-8")
    assert "uncertain_order_outcome: BUY BTC: timeout" in text
    assert kill_switch_active(config) is True
    assert "Persistent kill switch activated" in caplog.text


def test_existing_marker_is_never_overwritten(tmp_path):
    path = tmp_path / "STOP"
    path.write_text("operator: manual stop\n", encoding="utf-8")
    activate_kill_switch(_Config(str(path)), "BUY BTC: timeout",
                         logging.getLogger("t"))
    assert path.read_text(encoding="utf-8") == "operator: manual stop\n"


@pytest.mark.parametrize("target_name", ["target", "nowhere"])
def test_marker_is_created_without_following_a_symlink(tmp_path, target_name):
    target = tmp_path / target_name
    if target_name == "target":
        target.write_text("keep", encoding="utf-8")
    link = tmp_path / "STOP"
    link.symlink_to(target)
    # A symlink (existing or dangling) already blocks trading, and activation must
    # never write through it: O_EXCL combined with O_NOFOLLOW rejects the link.
    assert kill_switch_active(_Config(str(link))) is True
    activate_kill_switch(_Config(str(link)), "reason", logging.getLogger("t"))
    assert link.is_symlink()
    assert link.readlink() == target
    if target_name == "target":
        assert target.read_text(encoding="utf-8") == "keep"


def test_unpersistable_marker_stops_the_bot(tmp_path):
    with pytest.raises(RuntimeError, match="Cannot persist live execution uncertainty"):
        activate_kill_switch(_Config(str(tmp_path / "absent" / "STOP")), "reason",
                             logging.getLogger("t"))


def test_default_path_is_used_when_config_has_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert kill_switch_active(object()) is False
    activate_kill_switch(object(), "reason", logging.getLogger("t"))
    assert (tmp_path / ".trading_kill_switch").exists()
