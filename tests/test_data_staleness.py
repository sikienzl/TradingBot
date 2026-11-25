"""Tests for the bounded staleness probes in :mod:`src.data_staleness`."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from src.data_staleness import (
    STALENESS_READ_LIMIT,
    _read_tail_lines,
    latest_journal_timestamp,
    latest_log_timestamp,
    latest_market_timestamp,
    measure_staleness,
    resolve_log_timezone,
)

JOURNAL_HEADER = "timestamp,iteration,coin,action,price"
MARKET_HEADER = "timestamp_utc,symbol,price"
NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _journal(rows: list[str], header: str = JOURNAL_HEADER) -> str:
    return "\n".join([header, *rows]) + "\n"


# ---------------------------------------------------------------------------
# trade journal
# ---------------------------------------------------------------------------

def test_journal_uses_newest_timestamp_not_last_row(tmp_path):
    journal = tmp_path / "trade_journal.csv"
    _write(journal, _journal([
        "2026-11-25T11:00:00+00:00,3,BTC/EUR,buy,10",
        "2026-11-24T09:00:00+00:00,4,BTC/EUR,sell,11",
        "2026-11-25T11:30:00+00:00,5,ETH/EUR,buy,20",
    ]))

    assert latest_journal_timestamp(journal) == datetime(
        2026, 11, 25, 11, 30, tzinfo=UTC)


def test_journal_accepts_naive_and_zulu_timestamps(tmp_path):
    journal = tmp_path / "trade_journal.csv"
    _write(journal, _journal([
        "2026-11-25T10:15:00Z,1,BTC/EUR,buy,10",
        "2026-11-25T10:45:00,2,BTC/EUR,sell,11",
    ]))

    assert latest_journal_timestamp(journal) == datetime(
        2026, 11, 25, 10, 45, tzinfo=UTC)


def test_journal_missing_file_or_column_is_none(tmp_path):
    assert latest_journal_timestamp(tmp_path / "absent.csv") is None

    no_column = tmp_path / "no_column.csv"
    _write(no_column, "iteration,coin\n3,BTC/EUR\n")
    assert latest_journal_timestamp(no_column) is None


def test_journal_only_unparseable_timestamps_is_none(tmp_path):
    journal = tmp_path / "trade_journal.csv"
    _write(journal, _journal(["not-a-time,1,BTC/EUR,buy,10", ",2,BTC/EUR,buy,10"]))

    assert latest_journal_timestamp(journal) is None


def test_journal_reads_only_the_tail_window(tmp_path):
    journal = tmp_path / "trade_journal.csv"
    # The newest timestamp sits at the top of the file: scanning everything would
    # return it, the bounded tail window only ever sees the older last row.
    filler = [f"2026-01-01T00:00:00+00:00,{i},BTC/EUR,hold,1" for i in range(30000)]
    _write(journal, _journal(["2026-11-25T11:59:00+00:00,9,BTC/EUR,buy,1", *filler]))
    assert journal.stat().st_size > STALENESS_READ_LIMIT

    assert latest_journal_timestamp(journal) == datetime(2026, 1, 1, tzinfo=UTC)


def test_journal_row_without_timestamp_column_is_skipped(tmp_path):
    journal = tmp_path / "trade_journal.csv"
    _write(journal, "iteration,coin,action,price\n9,BTC/EUR,buy,1\n")

    assert latest_journal_timestamp(journal) is None


def test_journal_row_with_blank_timestamp_is_skipped(tmp_path):
    journal = tmp_path / "trade_journal.csv"
    _write(journal, _journal([",9,BTC/EUR,buy,1"]))

    assert latest_journal_timestamp(journal) is None


def test_journal_unreadable_file_is_none(tmp_path):
    journal = tmp_path / "trade_journal.csv"
    _write(journal, _journal(["2026-11-25T11:00:00+00:00,1,BTC/EUR,buy,10"]))
    os.chmod(journal, 0o000)
    try:
        assert latest_journal_timestamp(journal) is None
    finally:
        os.chmod(journal, 0o644)


def test_journal_blank_line_is_ignored(tmp_path):
    journal = tmp_path / "trade_journal.csv"
    _write(journal, _journal([
        "2026-11-25T11:00:00+00:00,1,BTC/EUR,buy,10", "",
    ]))

    assert latest_journal_timestamp(journal) == datetime(
        2026, 11, 25, 11, 0, tzinfo=UTC)


def test_tail_window_without_newline_returns_nothing(tmp_path):
    # A window that starts inside the only line has no complete line to parse.
    path = tmp_path / "single_line.csv"
    _write(path, "x" * 200)

    assert _read_tail_lines(path, 64) == []


def test_tail_window_drops_truncated_last_line(tmp_path):
    path = tmp_path / "partial.csv"
    _write(path, "first\nsecond\nthird")

    assert _read_tail_lines(path, 64) == ["first", "second"]


def test_tail_window_keeps_everything_below_the_limit(tmp_path):
    path = tmp_path / "small.csv"
    _write(path, "first\nsecond\n")

    assert _read_tail_lines(path, STALENESS_READ_LIMIT) == ["first", "second"]


# ---------------------------------------------------------------------------
# market recorder
# ---------------------------------------------------------------------------

def test_market_uses_newest_row_across_both_files(tmp_path):
    market = tmp_path / "market"
    _write(market / "tickers.csv",
           f"{MARKET_HEADER}\n2026-11-25T11:58:00+00:00,BTC/EUR,10\n")
    _write(market / "candles_1m.csv",
           f"{MARKET_HEADER}\n2026-11-25T11:59:30+00:00,BTC/EUR,10\n")

    assert latest_market_timestamp(market) == datetime(
        2026, 11, 25, 11, 59, 30, tzinfo=UTC)


def test_market_missing_directory_is_none(tmp_path):
    assert latest_market_timestamp(tmp_path / "absent") is None


def test_market_one_usable_file_is_enough(tmp_path):
    market = tmp_path / "market"
    _write(market / "tickers.csv",
           f"{MARKET_HEADER}\n2026-11-25T11:58:00+00:00,BTC/EUR,10\n")

    assert latest_market_timestamp(market) == datetime(
        2026, 11, 25, 11, 58, tzinfo=UTC)


# ---------------------------------------------------------------------------
# bot log
# ---------------------------------------------------------------------------

def test_log_uses_newest_asctime_including_milliseconds(tmp_path):
    log = tmp_path / "bot.log"
    _write(log,
           "2026-11-25 11:00:00,123 - INFO - first\n"
           "2026-11-25 11:59:59,987 - INFO - 🕒 Iteration 42\n"
           "traceback noise without a timestamp\n")

    assert latest_log_timestamp(log) == datetime(
        2026, 11, 25, 11, 59, 59, 987000, tzinfo=UTC)


def test_log_without_timestamps_is_none(tmp_path):
    log = tmp_path / "bot.log"
    _write(log, "no timestamps here\n\n")

    assert latest_log_timestamp(log) is None


def test_log_missing_file_is_none(tmp_path):
    assert latest_log_timestamp(tmp_path / "absent.log") is None


def test_log_unreadable_file_is_none(tmp_path):
    log = tmp_path / "bot.log"
    _write(log, "2026-11-25 11:00:00 - INFO - hello\n")
    os.chmod(log, 0o000)
    try:
        assert latest_log_timestamp(log) is None
    finally:
        os.chmod(log, 0o644)


def test_log_rejects_impossible_calendar_values(tmp_path):
    # The asctime pattern matches, but the calendar value cannot exist.
    log = tmp_path / "bot.log"
    _write(log, "2026-13-45 99:99:99 - INFO - impossible\n")

    assert latest_log_timestamp(log) is None


def test_log_ignores_timestamps_inside_messages(tmp_path):
    log = tmp_path / "bot.log"
    _write(log, "2026-11-25 11:00:00 - INFO - saw 2020-01-01 00:00:00 in payload\n")

    assert latest_log_timestamp(log) == datetime(2026, 11, 25, 11, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# measure_staleness
# ---------------------------------------------------------------------------

def _healthy(tmp_path):
    journal = tmp_path / "trade_journal.csv"
    _write(journal, _journal(["2026-11-25T11:50:00+00:00,1,BTC/EUR,buy,10"]))
    log = tmp_path / "bot.log"
    _write(log, "2026-11-25 11:59:00 - INFO - 🕒 Iteration 7\n")
    market = tmp_path / "market"
    _write(market / "tickers.csv",
           f"{MARKET_HEADER}\n2026-11-25T11:59:30+00:00,BTC/EUR,10\n")
    return journal, log, market


def test_measure_staleness_reports_all_three_ages(tmp_path):
    journal, log, market = _healthy(tmp_path)

    result = measure_staleness(journal, log, market, now=NOW)

    assert result["journal_present"] == 1.0
    assert result["journal_age_seconds"] == 600.0
    assert result["bot_log_present"] == 1.0
    assert result["bot_log_age_seconds"] == 60.0
    assert result["market_data_present"] == 1.0
    assert result["market_data_age_seconds"] == 30.0


def test_measure_staleness_omits_market_when_not_configured(tmp_path):
    journal, log, _market = _healthy(tmp_path)

    result = measure_staleness(journal, log, None, now=NOW)

    assert result["market_data_present"] == 0.0
    assert result["market_data_age_seconds"] is None
    assert result["bot_log_age_seconds"] == 60.0


def test_measure_staleness_shifts_log_age_by_timezone(tmp_path):
    log = tmp_path / "bot.log"
    _write(log, "2026-11-25 11:59:00 - INFO - hello\n")

    utc = measure_staleness(tmp_path / "absent.csv", log, None, now=NOW)
    berlin = measure_staleness(
        tmp_path / "absent.csv", log, None,
        log_timezone=resolve_log_timezone("Europe/Berlin"), now=NOW,
    )

    assert utc["bot_log_age_seconds"] == 60.0
    # CET is UTC+1, so the naive stamp is an hour earlier in UTC.
    assert berlin["bot_log_age_seconds"] == 3660.0


def test_resolve_log_timezone_falls_back_to_utc():
    assert resolve_log_timezone("not a zone") is UTC
    assert resolve_log_timezone("") is UTC


def test_measure_staleness_fails_closed_for_missing_sources(tmp_path):
    result = measure_staleness(
        tmp_path / "absent.csv", tmp_path / "absent.log", tmp_path / "absent",
        now=NOW,
    )

    assert result == {
        "journal_present": 0.0,
        "journal_age_seconds": None,
        "bot_log_present": 0.0,
        "bot_log_age_seconds": None,
        "market_data_present": 0.0,
        "market_data_age_seconds": None,
    }


def test_measure_staleness_clamps_future_timestamps_to_zero(tmp_path):
    journal = tmp_path / "trade_journal.csv"
    _write(journal, _journal([
        (NOW + timedelta(hours=3)).isoformat() + ",1,BTC/EUR,buy,10",
    ]))
    log = tmp_path / "bot.log"
    _write(log, "2026-11-25 11:59:00 - INFO - hello\n")

    result = measure_staleness(journal, log, tmp_path / "absent", now=NOW)

    assert result["journal_age_seconds"] == 0.0


def test_measure_staleness_uses_wall_clock_when_now_omitted(tmp_path):
    journal, log, market = _healthy(tmp_path)

    result = measure_staleness(journal, log, market)

    # Real rows are from 2026-11-25, so against the real clock they are old but
    # still present; the ages must be non-negative.
    assert result["journal_present"] == 1.0
    assert result["journal_age_seconds"] >= 0.0
