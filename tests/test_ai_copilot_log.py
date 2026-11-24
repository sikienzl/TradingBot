"""Unit tests for the co-pilot log reader in :mod:`src.ai_copilot_log`.

The bot writes its co-pilot decisions as plain log lines; the exporters turn the
last few of them into Prometheus series. These tests pin the parsing of each
marker, the malformed-line handling and the recency limits.

The bot's ``logging.Formatter`` emits naive local-time timestamps, so the reader
has to interpret them as local time before comparing against
``datetime.now(UTC)`` -- see ``test_naive_log_timestamp_is_read_as_local_time``.
The helpers below render fixtures the same way the bot does.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import ai_copilot_log
from src.ai_copilot_log import (
    _parse_log_timestamp,
    _parse_suggestion_payload,
    _suggestion_confidence,
    _suggestion_series,
    read_ai_shadow_suggestions,
)

NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)


def _log_line(when: datetime, message: str) -> str:
    """Render *when* exactly like the bot does: naive local time, no offset."""
    naive_local = when.astimezone().replace(tzinfo=None)
    return (
        f"{naive_local.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} - INFO - {message}"
    )


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr(ai_copilot_log, "_now", lambda: NOW)


def _write_log(tmp_path, lines) -> str:
    path = tmp_path / "bot.log"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------- markers


def test_missing_log_returns_empty_list(tmp_path):
    assert read_ai_shadow_suggestions(str(tmp_path / "nope.log")) == {
        'ai_copilot_shadow_suggestions': []}


def test_shadow_suggestion_is_parsed(frozen_now, tmp_path):
    path = _write_log(tmp_path, [
        _log_line(NOW - timedelta(minutes=5),
                  "AI co-pilot shadow suggestion: {'stop_loss_pct': -0.045, "
                  "'take_profit_pct': 0.09} (risk=low, confidence=0.72, "
                  "reason=recent drawdown)"),
    ])

    (first, second) = read_ai_shadow_suggestions(path)[
        'ai_copilot_shadow_suggestions']

    assert first == {
        'rank': 1,
        'suggestion_id': '1_stop_loss_pct',
        'mode': 'shadow',
        'parameter': 'stop_loss_pct',
        'value': -0.045,
        'confidence': 0.72,
        'age_minutes': 5.0,
    }
    assert second['suggestion_id'] == '1_take_profit_pct'


def test_unchanged_values_marker(frozen_now, tmp_path):
    path = _write_log(tmp_path, [
        _log_line(NOW - timedelta(minutes=2),
                  "AI co-pilot suggested unchanged values: "
                  "{'trailing_stop_pct': 0.03}"),
    ])

    (only,) = read_ai_shadow_suggestions(path)['ai_copilot_shadow_suggestions']

    assert only['mode'] == 'unchanged'
    assert only['value'] == 0.03
    assert only['confidence'] == 0.0


def test_no_change_marker(frozen_now, tmp_path):
    path = _write_log(tmp_path, [
        _log_line(NOW - timedelta(minutes=1),
                  "AI co-pilot suggestion: no change (risk=medium, "
                  "confidence=0.31, reason=stable)"),
    ])

    (only,) = read_ai_shadow_suggestions(path)['ai_copilot_shadow_suggestions']

    assert only['mode'] == 'no_change'
    assert only['parameter'] == 'no_change'
    assert only['value'] == 0.0
    assert only['confidence'] == 0.31


# ---------------------------------------------------------------- recency/rank


def test_newest_suggestions_win_and_rank_stops_at_three(
        frozen_now, tmp_path):
    lines = [
        _log_line(NOW - timedelta(minutes=30 * (i + 1), seconds=1),
                  f"AI co-pilot suggestion: no change (risk=low, "
                  f"confidence=0.{i + 1}, reason=r)")
        for i in range(6)
    ]
    path = _write_log(tmp_path, lines)

    suggestions = read_ai_shadow_suggestions(path)['ai_copilot_shadow_suggestions']

    assert [s['rank'] for s in suggestions] == [1, 2, 3]
    assert [s['confidence'] for s in suggestions] == [0.6, 0.5, 0.4]


def test_only_the_log_tail_is_scanned(frozen_now, tmp_path,
                                      monkeypatch):
    monkeypatch.setattr(ai_copilot_log, 'LOG_TAIL_LINES', 3)
    path = _write_log(tmp_path, [
        _log_line(NOW - timedelta(minutes=1),
                  "AI co-pilot suggestion: no change (risk=low, "
                  "confidence=0.9, reason=r)"),
        "noise one",
        "noise two",
        "noise three",
    ])

    assert read_ai_shadow_suggestions(path)['ai_copilot_shadow_suggestions'] == []


def test_future_timestamps_age_to_zero(frozen_now, tmp_path):
    path = _write_log(tmp_path, [
        _log_line(NOW + timedelta(days=1),
                  "AI co-pilot suggestion: no change (risk=low, "
                  "confidence=0.5, reason=r)"),
    ])

    (only,) = read_ai_shadow_suggestions(path)['ai_copilot_shadow_suggestions']
    assert only['age_minutes'] == 0.0


# ------------------------------------------------------------- malformed input


MALFORMED_LINES = [
    ("2026-11-24 12:00:00,000 - INFO - AI co-pilot shadow suggestion: "
     "not-a-dict (risk=low, confidence=x)"),
    ("2026-11-24 12:00:00,000 - INFO - AI co-pilot shadow suggestion: "
     "{'a': 1} without meta"),
    ("2026-11-24 12:00:00,000 - INFO - AI co-pilot suggested unchanged "
     "values: {{{broken"),
    "2026-11-24 12:00:00,000 - INFO - AI co-pilot suggestion: no change",
    "no separator here AI co-pilot",
    ("not-a-time - INFO - AI co-pilot suggestion: no change (risk=low, "
     "confidence=0.5, reason=r)"),
]


@pytest.mark.parametrize("line", MALFORMED_LINES)
def test_malformed_lines_are_skipped(frozen_now, tmp_path, line):
    path = _write_log(tmp_path, [line])
    assert read_ai_shadow_suggestions(path)['ai_copilot_shadow_suggestions'] == []


def test_non_numeric_values_are_skipped(frozen_now, tmp_path):
    path = _write_log(tmp_path, [
        _log_line(NOW - timedelta(minutes=1),
                  "AI co-pilot shadow suggestion: {'mode': 'aggressive', "
                  "'window': 12} (risk=low, confidence=0.4, reason=r)"),
    ])

    (only,) = read_ai_shadow_suggestions(path)['ai_copilot_shadow_suggestions']
    assert only['parameter'] == 'window'


def test_unreadable_log_is_reported(tmp_path, capsys):
    directory = tmp_path / "not-a-file"
    directory.mkdir()

    assert read_ai_shadow_suggestions(str(directory)) == {
        'ai_copilot_shadow_suggestions': []}
    assert "Error reading AI co-pilot suggestions" in capsys.readouterr().err


# ----------------------------------------------------------- log clock handling


def test_naive_log_timestamp_is_read_as_local_time(frozen_now, tmp_path):
    """The bot logs naive local timestamps; the reader must not crash.

    ``logging.Formatter`` emits ``asctime`` without an offset, so a naive
    timestamp used to be subtracted from ``datetime.now(UTC)`` and raise
    ``TypeError`` straight out of the exporter request. It is now interpreted as
    local time, which keeps the age correct whatever the host timezone is.
    """
    path = _write_log(tmp_path, [
        _log_line(NOW - timedelta(minutes=7, seconds=30),
                  "AI co-pilot shadow suggestion: {'stop_loss_pct': -0.045} "
                  "(risk=low, confidence=0.72, reason=r)"),
    ])

    (only,) = read_ai_shadow_suggestions(path)['ai_copilot_shadow_suggestions']

    assert only['age_minutes'] == 7.5
    assert only['confidence'] == 0.72


# ------------------------------------------------------------- helper contracts


def test_parse_suggestion_payload_returns_none_for_other_lines():
    assert _parse_suggestion_payload("just an info line") is None


def test_parse_suggestion_payload_shapes():
    assert _parse_suggestion_payload(
        "AI co-pilot suggested unchanged values: {'a': 1}") == (
        'unchanged', {'a': 1}, 0.0)
    mode, changes, confidence = _parse_suggestion_payload(
        "AI co-pilot shadow suggestion: {'a': 1} (risk=low, confidence=0.8, "
        "reason=r)")
    assert (mode, changes, confidence) == ('shadow', {'a': 1}, 0.8)


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ("low, confidence=0.72, reason=r", 0.72),
        ("low, confidence=1.0, reason=r", 1.0),
        ("low, reason=no confidence", 0.0),
    ],
)
def test_suggestion_confidence(meta, expected):
    assert _suggestion_confidence(meta) == expected


def test_parse_log_timestamp_accepts_milliseconds():
    assert _parse_log_timestamp(" 2026-11-24 12:00:00,123 ") == datetime(
        2026, 11, 24, 12, 0, 0, 123000).astimezone(UTC)


def test_parse_log_timestamp_returns_aware_utc():
    parsed = _parse_log_timestamp("2026-11-24 12:00:00,000")
    assert parsed.tzinfo is UTC
    assert parsed == datetime(2026, 11, 24, 12, 0, 0).astimezone(UTC)


def test_suggestion_series_skips_unparsable_values():
    entries = list(_suggestion_series(
        2, 'shadow', {'a': 'x', 'b': None, 'c': '1.5', 'd': 3}, 0.5, 7.005))
    assert [e['parameter'] for e in entries] == ['c', 'd']
    assert entries[0]['suggestion_id'] == '2_c'
    assert entries[0]['age_minutes'] == 7.0
