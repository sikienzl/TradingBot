"""Unit tests for the durable risk state (audit point 4).

The read path must fail closed: a file that exists but cannot be understood may
never look like "no risk state", because that would silently reset the
daily-loss budget and the loss streak.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta

import pytest

from src.risk_state import (
    RISK_STATE_VERSION,
    RiskState,
    parse_risk_state,
    read_risk_state,
    risk_state_to_payload,
    write_risk_state,
)

NOW = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)


def _state() -> RiskState:
    return RiskState(
        anchor_date=date(2026, 3, 4),
        anchor_value=1000.0,
        consecutive_losses=2,
        buy_pause_until_utc=NOW + timedelta(minutes=5),
        buy_timestamps_utc=[NOW - timedelta(minutes=2)],
        last_sell_timestamps_utc={"BTC": NOW - timedelta(minutes=10)},
    )


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------
def test_payload_round_trips_every_counter(tmp_path):
    path = tmp_path / "risk.json"
    write_risk_state(_state(), str(path), now_utc=NOW)
    loaded = read_risk_state(str(path))
    assert loaded.restored is True
    assert loaded.unreadable is False
    assert loaded.anchor_date == date(2026, 3, 4)
    assert loaded.anchor_value == pytest.approx(1000.0)
    assert loaded.consecutive_losses == 2
    assert loaded.buy_pause_until_utc == NOW + timedelta(minutes=5)
    assert loaded.buy_timestamps_utc == [NOW - timedelta(minutes=2)]
    assert loaded.last_sell_timestamps_utc == {"BTC": NOW - timedelta(minutes=10)}


def test_write_is_atomic_and_leaves_no_temporary_files(tmp_path):
    path = tmp_path / "risk.json"
    write_risk_state(_state(), str(path), now_utc=NOW)
    write_risk_state(_state(), str(path), now_utc=NOW)
    assert list(tmp_path.glob(".risk-state-*")) == []
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == RISK_STATE_VERSION
    assert payload["updated_utc"] == "2026-03-04T12:00:00+00:00"


def test_write_without_path_is_a_no_op(tmp_path):
    assert write_risk_state(_state(), None) is False
    assert write_risk_state(_state(), "") is False
    assert list(tmp_path.iterdir()) == []


def test_payload_documents_the_schema(tmp_path):
    payload = risk_state_to_payload(RiskState(), now_utc=NOW)
    assert payload["version"] == RISK_STATE_VERSION
    assert payload["daily_anchor_date"] is None
    assert payload["daily_anchor_value"] == 0.0
    assert payload["consecutive_losses"] == 0
    assert payload["buy_pause_until_utc"] is None
    assert payload["buy_timestamps_utc"] == []
    assert payload["last_sell_utc"] == {}


# ---------------------------------------------------------------------------
# Missing file is a legitimate neutral state
# ---------------------------------------------------------------------------
def test_missing_file_starts_neutral(tmp_path):
    state = read_risk_state(str(tmp_path / "absent.json"))
    assert state.unreadable is False
    assert state.restored is False
    assert state.anchor_value == 0.0
    assert state.consecutive_losses == 0


def test_empty_path_starts_neutral():
    assert read_risk_state(None).restored is False
    assert read_risk_state("").restored is False


# ---------------------------------------------------------------------------
# Fail closed on anything unreadable
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("content", [
    "not json at all",
    "[]",
    '{"version": 999}',
    '{"version": 1, "daily_anchor_value": "lots"}',
    '{"version": 1, "daily_anchor_value": -5}',
    '{"version": 1, "consecutive_losses": -1}',
    '{"version": 1, "consecutive_losses": "two"}',
    '{"version": 1, "consecutive_losses": true}',
    '{"version": 1, "buy_pause_until_utc": "yesterday"}',
    '{"version": 1, "daily_anchor_date": "nope"}',
    '{"version": 1, "buy_timestamps_utc": "yesterday"}',
    '{"version": 1, "buy_timestamps_utc": ["yesterday"]}',
    '{"version": 1, "last_sell_utc": "BTC"}',
    '{"version": 1, "last_sell_utc": {"BTC": "yesterday"}}',
    '{"version": 1, "last_sell_utc": {"": "2026-03-04T12:00:00+00:00"}}',
    "",
])
def test_unreadable_or_invalid_state_fails_closed(tmp_path, content, caplog):
    path = tmp_path / "risk.json"
    path.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        state = read_risk_state(str(path))
    assert state.unreadable is True
    assert state.restored is False
    assert state.anchor_value == 0.0
    assert "could not be read" in caplog.text or "is invalid" in caplog.text


def test_unreadable_state_is_reported_once_per_read(tmp_path, caplog):
    path = tmp_path / "risk.json"
    path.write_text("{ broken", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        read_risk_state(str(path))
    assert caplog.text.count("new entries stay blocked") == 1


def test_unreadable_directory_is_not_fatal(tmp_path):
    directory = tmp_path / "risk.json"
    directory.mkdir()
    assert read_risk_state(str(directory)).unreadable is True


def test_parse_rejects_a_non_object():
    with pytest.raises(TypeError):
        parse_risk_state("nope")


def test_naive_timestamps_are_read_as_utc():
    state = parse_risk_state({
        "version": RISK_STATE_VERSION,
        "buy_pause_until_utc": "2026-03-04T12:00:00",
    })
    assert state.buy_pause_until_utc == NOW
    assert state.buy_pause_until_utc.tzinfo is UTC


def test_coin_keys_are_normalised():
    state = parse_risk_state({
        "version": RISK_STATE_VERSION,
        "last_sell_utc": {"btc": "2026-03-04T12:00:00+00:00"},
    })
    assert list(state.last_sell_timestamps_utc or {}) == ["BTC"]


# ---------------------------------------------------------------------------
# Write failures must stop a live process
# ---------------------------------------------------------------------------
def test_write_failure_raises_runtime_error(tmp_path):
    with pytest.raises(RuntimeError, match="Cannot persist risk state"):
        write_risk_state(_state(), str(tmp_path / "missing-dir" / "risk.json"))


def test_write_failure_cleans_up_its_temporary_file(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise TypeError("not serialisable")

    monkeypatch.setattr("src.risk_state.json.dump", boom)
    path = tmp_path / "risk.json"
    with pytest.raises(RuntimeError, match="Cannot persist risk state"):
        write_risk_state(_state(), str(path))
    assert not path.exists()
    assert list(tmp_path.glob(".risk-state-*")) == []
