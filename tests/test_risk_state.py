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
# Every guardrail, pinned by the message the operator sees
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("payload", [None, [], "nope", 7, 1.5, True])
def test_parse_rejects_a_document_that_is_not_an_object(payload):
    with pytest.raises(TypeError, match="Risk state document is not an object"):
        parse_risk_state(payload)


@pytest.mark.parametrize("version", [2, 0, "1", None, [], {}, ""])
def test_parse_rejects_an_unsupported_schema_version(version):
    with pytest.raises(ValueError) as caught:
        parse_risk_state({"version": version})
    assert str(caught.value) == f"Unsupported risk state version: {version!r}"


@pytest.mark.parametrize("version", [1, 1.0, True])
def test_parse_accepts_the_pinned_schema_version_only_numerically(version):
    state = parse_risk_state({"version": version})
    assert state.unreadable is False
    assert state.consecutive_losses == 0


@pytest.mark.parametrize("raw, expected", [
    (1000.0, 1000.0),
    ("1000", 1000.0),
    ("1e5", 100000.0),
    (None, 0.0),
    (True, 1.0),
    (False, 0.0),
    (-0.0, -0.0),
])
def test_parse_reads_the_daily_anchor_value_leniently(raw, expected):
    state = parse_risk_state({"version": 1, "daily_anchor_value": raw})
    assert state.anchor_value == expected


@pytest.mark.parametrize("raw", [-5, -0.5, "lots", [], {}, float("nan"),
                                 float("inf"), "-inf"])
def test_parse_rejects_an_unusable_daily_anchor_value(raw):
    with pytest.raises(ValueError, match="Risk state has an invalid daily anchor value"):
        parse_risk_state({"version": 1, "daily_anchor_value": raw})


@pytest.mark.parametrize("raw", ["0", "two", None, True, False, [], {}, 1.0, 2.5])
def test_parse_requires_a_whole_number_loss_streak(raw):
    with pytest.raises(TypeError, match="Risk state has an invalid loss streak"):
        parse_risk_state({"version": 1, "consecutive_losses": raw})


@pytest.mark.parametrize("raw", [-1, -7])
def test_parse_rejects_a_negative_loss_streak(raw):
    with pytest.raises(ValueError, match="Risk state has a negative loss streak"):
        parse_risk_state({"version": 1, "consecutive_losses": raw})


@pytest.mark.parametrize("raw, expected", [
    ("2026-03-04T12:00:00+00:00", NOW),
    ("2026-03-04T12:00:00", NOW),
    ("2026-03-04 12:00:00", NOW),
    ("2026-03-04T12:00:00+02:00", NOW - timedelta(hours=2)),
])
def test_parse_normalises_the_buy_pause_to_utc(raw, expected):
    state = parse_risk_state({"version": 1, "buy_pause_until_utc": raw})
    assert state.buy_pause_until_utc == expected
    assert state.buy_pause_until_utc.tzinfo is UTC


@pytest.mark.parametrize("raw", ["yesterday", "", 0, 1, True, False, [], {},
                                 "2026-13-45T99:99:99+00:00"])
def test_parse_rejects_an_unusable_buy_pause(raw):
    with pytest.raises(ValueError, match="Risk state has an invalid BUY pause timestamp"):
        parse_risk_state({"version": 1, "buy_pause_until_utc": raw})


def test_parse_reads_a_plain_iso_anchor_date():
    state = parse_risk_state({"version": 1, "daily_anchor_date": "2026-03-04"})
    assert state.anchor_date == date(2026, 3, 4)


@pytest.mark.parametrize("raw", ["2026-1-2", "nope", "", 0, True, "2026-13-45", [], {},
                                 "2026-03-04T00:00:00+00:00"])
def test_parse_rejects_an_unusable_anchor_date(raw):
    with pytest.raises(ValueError, match="Risk state has an invalid daily anchor date"):
        parse_risk_state({"version": 1, "daily_anchor_date": raw})


@pytest.mark.parametrize("raw", [None, [], {}, 0])
def test_parse_treats_an_absent_buy_budget_as_empty(raw):
    state = parse_risk_state({"version": 1, "buy_timestamps_utc": raw})
    assert state.buy_timestamps_utc == []


@pytest.mark.parametrize("raw", ["yesterday", True, 1, 2.5])
def test_parse_rejects_a_buy_budget_that_is_not_a_list(raw):
    with pytest.raises(TypeError, match="Risk state has an invalid BUY budget"):
        parse_risk_state({"version": 1, "buy_timestamps_utc": raw})


@pytest.mark.parametrize("raw", [["yesterday"], [None], [[]], [{}],
                                 ["2026-03-04T12:00:00+00:00", "yesterday"]])
def test_parse_rejects_each_unusable_buy_budget_timestamp(raw):
    with pytest.raises(ValueError, match="Risk state has an invalid BUY budget timestamp"):
        parse_risk_state({"version": 1, "buy_timestamps_utc": raw})


@pytest.mark.parametrize("raw", [None, {}, [], 0])
def test_parse_treats_an_absent_cooldown_map_as_empty(raw):
    state = parse_risk_state({"version": 1, "last_sell_utc": raw})
    assert state.last_sell_timestamps_utc == {}


@pytest.mark.parametrize("raw", ["BTC", True, 1, 2.5])
def test_parse_rejects_a_cooldown_map_that_is_not_an_object(raw):
    with pytest.raises(TypeError, match="Risk state has an invalid re-entry cooldown map"):
        parse_risk_state({"version": 1, "last_sell_utc": raw})


@pytest.mark.parametrize("raw", [
    {"BTC": "yesterday"},
    {"": "2026-03-04T12:00:00+00:00"},
    {"BTC": None},
    {"BTC": []},
    {1: "2026-03-04T12:00:00+00:00"},
    {"ETH": "2026-03-04T12:00:00+00:00", "XRP": "yesterday"},
])
def test_parse_rejects_unusable_cooldown_entries(raw):
    with pytest.raises(ValueError,
                       match="Risk state has an invalid re-entry cooldown timestamp"):
        parse_risk_state({"version": 1, "last_sell_utc": raw})


def test_parse_upper_cases_every_cooldown_coin():
    state = parse_risk_state({
        "version": 1,
        "last_sell_utc": {
            "btc": "2026-03-04T12:00:00+00:00",
            "eTc": "2026-03-04T11:00:00+00:00",
        },
    })
    assert state.last_sell_timestamps_utc == {
        "BTC": NOW,
        "ETC": NOW - timedelta(hours=1),
    }


def test_parse_reports_the_first_defect_in_document_order():
    broken = {
        "version": 1,
        "daily_anchor_value": -5,
        "consecutive_losses": "two",
        "buy_pause_until_utc": "yesterday",
        "daily_anchor_date": "nope",
        "buy_timestamps_utc": ["yesterday"],
        "last_sell_utc": {"BTC": "yesterday"},
    }
    with pytest.raises(ValueError, match="Risk state has an invalid daily anchor value"):
        parse_risk_state(dict(broken))
    without_anchor = {key: value for key, value in broken.items()
                      if key != "daily_anchor_value"}
    with pytest.raises(TypeError, match="Risk state has an invalid loss streak"):
        parse_risk_state(without_anchor)
    only_the_last = {key: value for key, value in broken.items()
                     if key not in ("daily_anchor_value", "consecutive_losses",
                                    "buy_pause_until_utc", "daily_anchor_date",
                                    "buy_timestamps_utc")}
    with pytest.raises(ValueError,
                       match="Risk state has an invalid re-entry cooldown timestamp"):
        parse_risk_state(only_the_last)


def test_parse_ignores_fields_it_does_not_understand():
    state = parse_risk_state({
        "version": 1,
        "written_by": "a future release",
        "daily_anchor_value": 250.0,
    })
    assert state.anchor_value == 250.0
    assert state.unreadable is False


@pytest.mark.parametrize("document", [
    {"version": 1, "daily_anchor_value": "lots"},
    {"version": 1, "consecutive_losses": 1.0},
    {"version": 1, "buy_pause_until_utc": ""},
    {"version": 1, "daily_anchor_date": "2026-13-45"},
    {"version": 1, "buy_timestamps_utc": True},
    {"version": 1, "last_sell_utc": "BTC"},
    {"version": 2},
])
def test_read_fails_closed_for_each_field_defect(tmp_path, caplog, document):
    path = tmp_path / "risk.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        state = read_risk_state(str(path))
    assert state.unreadable is True
    assert state.restored is False
    assert "is invalid" in caplog.text


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
