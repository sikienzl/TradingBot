"""Unit tests for :mod:`src.market_observations`.

The dashboard's market panel is pure file reading plus arithmetic, so these
tests drive the module directly: no FastAPI app, no auth, just the recorder
files in ``tmp_path`` and the returned payload.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

from src import market_observations as mo

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
SYMBOLS = ["ADA/EUR", "ETH/EUR"]


def _iso(offset_seconds: float) -> str:
    return (NOW + timedelta(seconds=offset_seconds)).isoformat()


def _ticker(
    symbol: str,
    offset: float,
    bid: str,
    ask: str,
    last: str,
    *,
    observed_at: str | None = None,
) -> dict[str, str]:
    return {
        "observed_at_utc": _iso(offset) if observed_at is None else observed_at,
        "exchange_timestamp_utc": "",
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "last": last,
        "base_volume": "",
        "quote_volume": "",
    }


def _candle(
    symbol: str,
    offset: float,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str,
    *,
    timestamp: str | None = None,
) -> dict[str, str]:
    return {
        "timestamp_utc": _iso(offset) if timestamp is None else timestamp,
        "symbol": symbol,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


@pytest.fixture
def clock(monkeypatch):
    """Pin ``datetime.now(UTC)`` inside the module under test."""

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(mo, "datetime", FixedDateTime)
    return NOW


def _write_market(
    market_dir: Path,
    *,
    metadata: object | None = None,
    metadata_raw: str | None = None,
    ticker_rows: list[dict[str, str]] | None = None,
    candle_rows: list[dict[str, str]] | None = None,
    ticker_raw_header: str | None = None,
) -> Path:
    market_dir.mkdir(parents=True, exist_ok=True)
    payload = META_DEFAULT if metadata is None else metadata
    if metadata_raw is not None:
        (market_dir / "metadata.json").write_text(metadata_raw, encoding="utf-8")
    else:
        (market_dir / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    with (market_dir / "tickers.csv").open("w", newline="", encoding="utf-8") as file:
        if ticker_raw_header is not None:
            file.write(ticker_raw_header + "\n")
            file.writelines(
                ",".join(row[name] for name in mo.MARKET_TICKER_FIELDS) + "\n"
                for row in (ticker_rows or [])
            )
        else:
            writer = csv.DictWriter(file, fieldnames=list(mo.MARKET_TICKER_FIELDS))
            writer.writeheader()
            writer.writerows(ticker_rows or [])
    with (market_dir / "candles_1m.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(mo.MARKET_CANDLE_FIELDS))
        writer.writeheader()
        writer.writerows(candle_rows or [])
    return market_dir


META_DEFAULT = {
    "exchange": "kraken",
    "symbols": list(SYMBOLS),
    "ticker_interval_seconds": 10,
    "candle_timeframe": "1m",
    "candle_backfill_limit": 720,
    "created_at_utc": _iso(-3600),
}


def _load(market_dir: Path, symbol: str | None = None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv("DASHBOARD_MARKET_DATA_DIR", str(market_dir))
    return mo.load_market_observations(symbol)


# --- shared scalar helpers --------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("nonsense", None),
        (5, None),
        ("2026-01-01T12:00:00+00:00", "2026-01-01T12:00:00+00:00"),
        ("2026-01-01T12:00:00", "2026-01-01T12:00:00+00:00"),
        ("2026-01-01 12:00:00", "2026-01-01T12:00:00+00:00"),
        ("2026-01-01T14:00:00+02:00", "2026-01-01T12:00:00+00:00"),
        ("2026-01-01T12:00:00.123456Z", "2026-01-01T12:00:00.123456+00:00"),
    ],
)
def test_parse_utc_normalises_to_utc(value, expected):
    parsed = mo._parse_utc(value)
    assert (parsed.isoformat() if parsed else None) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("abc", None),
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        ("1.5", 1.5),
        (1, 1.0),
        (-1, -1.0),
        (0, 0.0),
    ],
)
def test_number_rejects_non_finite(value, expected):
    assert mo._number(value) == expected


def test_downsample_keeps_small_series_untouched():
    points = [{"i": index} for index in range(5)]
    assert mo._downsample(points, 5) is points
    assert mo._downsample(points, 10) == points
    assert mo._downsample([], 5) == []


def test_downsample_thins_evenly_and_keeps_endpoints():
    points = [{"i": index} for index in range(11)]
    result = mo._downsample(points, 3)
    assert [point["i"] for point in result] == [0, 5, 10]
    result = mo._downsample(points, 6)
    assert [point["i"] for point in result] == [0, 2, 4, 6, 8, 10]


# --- CSV tail reader --------------------------------------------------------


def _write_tail_file(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(mo.MARKET_TICKER_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_read_csv_tail_returns_everything_when_small(tmp_path):
    path = _write_tail_file(
        tmp_path / "tickers.csv",
        [_ticker("ADA/EUR", -60 * index, "1", "2", "1.5") for index in range(1, 6)],
    )
    reader, truncated = mo._read_csv_tail(path, mo.MARKET_TICKER_FIELDS)
    rows = list(reader)
    assert not truncated
    assert len(rows) == 5
    assert rows[-1]["observed_at_utc"] == _iso(-300)


def test_read_csv_tail_trims_partial_last_line(tmp_path):
    path = _write_tail_file(
        tmp_path / "tickers.csv",
        [
            _ticker("ADA/EUR", -60, "1", "2", "1.5"),
            _ticker("ADA/EUR", -120, "1", "2", "2"),
            _ticker("ADA/EUR", -180, "1", "2", "2.5"),
        ],
    )
    text = path.read_text(encoding="utf-8").rstrip("\n")
    path.write_text(text[: len(text) - 5], encoding="utf-8")
    reader, truncated = mo._read_csv_tail(path, mo.MARKET_TICKER_FIELDS)
    rows = list(reader)
    assert not truncated
    assert len(rows) == 2
    assert [row["last"] for row in rows] == ["1.5", "2"]


def test_read_csv_tail_window_is_bounded_and_relabels(tmp_path, monkeypatch):
    path = _write_tail_file(
        tmp_path / "tickers.csv",
        [
            _ticker("ADA/EUR", -60 * index, "1", "2", f"{1.5 + index:.1f}")
            for index in range(1, 11)
        ],
    )
    monkeypatch.setattr(mo, "MARKET_DATA_READ_LIMIT", 150)
    reader, truncated = mo._read_csv_tail(path, mo.MARKET_TICKER_FIELDS)
    rows = list(reader)
    assert truncated
    assert len(rows) < 10
    assert rows[-1]["observed_at_utc"] == _iso(-600)
    assert set(rows[0]) == set(mo.MARKET_TICKER_FIELDS)


def test_read_csv_tail_rejects_unexpected_header(tmp_path):
    path = tmp_path / "tickers.csv"
    path.write_text("observed_at_utc,symbol,bid\n2026-01-01T12:00:00+00:00,ADA/EUR,1\n")
    with pytest.raises(ValueError, match="unexpected CSV header"):
        mo._read_csv_tail(path, mo.MARKET_TICKER_FIELDS)


def test_read_csv_tail_needs_a_complete_row_in_window(tmp_path, monkeypatch):
    path = tmp_path / "tickers.csv"
    row = ",".join(
        value for value in _ticker("ADA/EUR", -60, "1", "2", "1.5").values()
    )
    path.write_text(",".join(mo.MARKET_TICKER_FIELDS) + "\n" + row, encoding="utf-8")
    monkeypatch.setattr(mo, "MARKET_DATA_READ_LIMIT", 8)
    with pytest.raises(ValueError, match="no complete rows in its read window"):
        mo._read_csv_tail(path, mo.MARKET_TICKER_FIELDS)


# --- availability guards ----------------------------------------------------


def test_missing_directory_is_reported(tmp_path, monkeypatch, clock):
    payload = _load(tmp_path / "market-data", monkeypatch=monkeypatch)
    assert payload["available"] is False
    assert "not mounted" in payload["reason"]
    assert payload["window_hours"] == 24
    assert payload["symbols"] == []


def test_missing_files_are_reported(tmp_path, monkeypatch, clock):
    (tmp_path / "market-data").mkdir()
    payload = _load(tmp_path / "market-data", monkeypatch=monkeypatch)
    assert payload["available"] is False
    assert "not available yet" in payload["reason"]


@pytest.mark.parametrize(
    ("metadata_raw", "metadata"),
    [
        ("{oops", None),
        (None, [1, 2]),
        (None, {**META_DEFAULT, "exchange": "binance"}),
        (None, {**META_DEFAULT, "symbols": []}),
        (None, {**META_DEFAULT, "symbols": "ADA/EUR"}),
        (None, {**META_DEFAULT, "symbols": ["ada/eur", "ETH/EUR"]}),
        (None, {**META_DEFAULT, "symbols": ["BTC/USD"]}),
    ],
)
def test_invalid_metadata_is_unavailable(
    tmp_path, monkeypatch, clock, metadata_raw, metadata
):
    market_dir = _write_market(
        tmp_path / "market-data",
        metadata_raw=metadata_raw,
        metadata=metadata,
        ticker_rows=[_ticker("ADA/EUR", -60, "1", "2", "1.5")],
        candle_rows=[_candle("ADA/EUR", -60, "1", "2", "1", "1.5", "1")],
    )
    payload = _load(market_dir, monkeypatch=monkeypatch)
    assert payload["available"] is False
    assert payload["reason"] == (
        "Market recorder data is invalid or temporarily unavailable."
    )


def test_oversized_metadata_is_unavailable(tmp_path, monkeypatch, clock):
    market_dir = _write_market(
        tmp_path / "market-data",
        metadata={**META_DEFAULT, "pad": "x" * (1024 * 1024 + 8)},
        ticker_rows=[],
        candle_rows=[],
    )
    payload = _load(market_dir, monkeypatch=monkeypatch)
    assert payload["available"] is False


def test_bad_csv_header_is_unavailable(tmp_path, monkeypatch, clock):
    market_dir = _write_market(
        tmp_path / "market-data",
        ticker_raw_header="observed_at_utc,symbol,bid",
        ticker_rows=[_ticker("ADA/EUR", -60, "100", "101", "100")],
        candle_rows=[_candle("ADA/EUR", -60, "100", "101", "100", "100", "1")],
    )
    payload = _load(market_dir, monkeypatch=monkeypatch)
    assert payload["available"] is False


def test_duplicate_symbols_are_deduplicated_in_order(tmp_path, monkeypatch, clock):
    market_dir = _write_market(
        tmp_path / "market-data",
        metadata={**META_DEFAULT, "symbols": ["ETH/EUR", "ADA/EUR", "ETH/EUR"]},
        ticker_rows=[_ticker("ETH/EUR", -60, "1", "2", "1.5")],
        candle_rows=[_candle("ETH/EUR", -60, "1", "2", "1", "1.5", "1")],
    )
    payload = _load(market_dir, monkeypatch=monkeypatch)
    assert payload["symbols"] == ["ETH/EUR", "ADA/EUR"]
    assert payload["selected_symbol"] == "ETH/EUR"


# --- symbol selection -------------------------------------------------------


def _happy_market(tmp_path: Path) -> Path:
    return _write_market(
        tmp_path / "market-data",
        ticker_rows=[
            _ticker("ADA/EUR", -600, "99.9", "100.1", "100"),
            _ticker("ADA/EUR", -300, "100.9", "101.1", "101"),
            _ticker("ETH/EUR", -600, "1000", "1002", "1001"),
            _ticker("ETH/EUR", -1, "1200", "1203", "1201"),
        ],
        candle_rows=[
            _candle("ADA/EUR", -120, "100", "101", "99", "100", "5"),
            _candle("ADA/EUR", -60, "100", "102", "100", "101", "6"),
            _candle("ETH/EUR", -60, "1000", "1010", "990", "1005", "2"),
        ],
    )


def test_default_selection_is_first_symbol(tmp_path, monkeypatch, clock):
    payload = _load(_happy_market(tmp_path), monkeypatch=monkeypatch)
    assert payload["available"] is True
    assert payload["selected_symbol"] == "ADA/EUR"
    assert [market["symbol"] for market in payload["markets"]] == SYMBOLS
    assert len(payload["ticker_series"]) == 2
    assert payload["ticker_series"][-1]["bid"] == 100.9
    assert payload["candle_series"][-1]["close"] == 101.0


def test_explicit_symbol_selects_series(tmp_path, monkeypatch, clock):
    payload = _load(_happy_market(tmp_path), "ETH/EUR", monkeypatch=monkeypatch)
    assert payload["selected_symbol"] == "ETH/EUR"
    assert len(payload["ticker_series"]) == 2
    assert payload["ticker_series"][0]["last"] == 1001.0


@pytest.mark.parametrize("symbol", ["XRP/EUR", "../../etc/passwd", "ADA/EUR "])
def test_unrecorded_symbol_raises_404(tmp_path, monkeypatch, clock, symbol):
    with pytest.raises(HTTPException) as excinfo:
        _load(_happy_market(tmp_path), symbol, monkeypatch=monkeypatch)
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "Market is not recorded."


def test_recorded_symbol_without_rows_yields_empty_summary(tmp_path, monkeypatch, clock):
    market_dir = _write_market(
        tmp_path / "market-data", ticker_rows=[], candle_rows=[]
    )
    payload = _load(market_dir, "ADA/EUR", monkeypatch=monkeypatch)
    assert payload["available"] is True
    market = payload["markets"][0]
    assert market["symbol"] == "ADA/EUR"
    assert market["latest_observed_at"] is None
    assert market["ticker_samples_24h"] == 0
    assert market["latest_spread_bps"] is None
    assert market["candle_coverage_pct"] is None
    assert payload["ticker_series"] == []
    assert payload["latest_data_at"] is None
    assert payload["freshness_status"] == "unavailable"
    assert payload["latest_data_age_seconds"] is None


# --- freshness --------------------------------------------------------------


def test_freshness_is_fresh_then_stale(tmp_path, monkeypatch, clock):
    market_dir = _write_market(
        tmp_path / "market-data",
        ticker_rows=[_ticker("ADA/EUR", -30, "100", "101", "100")],
        candle_rows=[_candle("ADA/EUR", -30, "100", "101", "100", "100", "1")],
    )
    payload = _load(market_dir, "ADA/EUR", monkeypatch=monkeypatch)
    assert payload["freshness_status"] == "fresh"
    assert payload["latest_data_age_seconds"] == pytest.approx(30.0, abs=1.0)

    market_dir = _write_market(
        tmp_path / "market-stale",
        ticker_rows=[_ticker("ADA/EUR", -600, "100", "101", "100")],
        candle_rows=[_candle("ADA/EUR", -600, "100", "101", "100", "100", "1")],
    )
    payload = _load(market_dir, "ADA/EUR", monkeypatch=monkeypatch)
    assert payload["freshness_status"] == "stale"
    assert payload["latest_data_age_seconds"] == pytest.approx(600.0, abs=1.0)


def test_generated_at_and_window_are_reported(tmp_path, monkeypatch, clock):
    payload = _load(_happy_market(tmp_path), monkeypatch=monkeypatch)
    assert payload["generated_at"] == NOW.isoformat()
    assert payload["window_hours"] == 24
    assert payload["metadata"]["candle_timeframe"] == "1m"
    assert payload["metadata"]["ticker_interval_seconds"] == 10
    assert payload["limitations"]


# --- row validation ---------------------------------------------------------


def test_invalid_rows_are_counted_not_used(tmp_path, monkeypatch, clock):
    market_dir = _write_market(
        tmp_path / "market-data",
        ticker_rows=[
            _ticker("ADA/EUR", -600, "99.9", "100.1", "100"),
            _ticker("ADA/EUR", -500, "0", "101", "100"),
            _ticker("ADA/EUR", -400, "101", "100", "100"),
            _ticker("ADA/EUR", -300, "x", "100", "100"),
            _ticker("XXX/EUR", -200, "1", "2", "1.5"),
            _ticker("ADA/EUR", -200, "1", "2", "1.5", observed_at=""),
            _ticker("ADA/EUR", -210, "1", "2", "1.5", observed_at="not-a-time"),
            _ticker("ADA/EUR", -100, "100", "101", "100"),
        ],
        candle_rows=[
            _candle("ADA/EUR", -120, "100", "101", "99", "100", "5"),
            _candle("ADA/EUR", -60, "-1", "2", "1", "1.5", "1"),
            _candle("ADA/EUR", -30, "100", "90", "95", "100", "1"),
            _candle("ADA/EUR", -20, "100", "101", "99", "100", "-3"),
            _candle("ADA/EUR", -10, "0", "0", "0", "0", "0"),
        ],
    )
    payload = _load(market_dir, "ADA/EUR", monkeypatch=monkeypatch)
    assert payload["invalid_ticker_rows"] == 6
    assert payload["invalid_candle_rows"] == 4
    market = payload["markets"][0]
    assert market["ticker_samples_24h"] == 2
    assert market["candle_count_24h"] == 1


def test_zero_spread_and_zero_volume_are_valid(tmp_path, monkeypatch, clock):
    market_dir = _write_market(
        tmp_path / "market-data",
        ticker_rows=[_ticker("ADA/EUR", -60, "100", "100", "100")],
        candle_rows=[_candle("ADA/EUR", -60, "100", "100", "100", "100", "0")],
    )
    payload = _load(market_dir, "ADA/EUR", monkeypatch=monkeypatch)
    market = payload["markets"][0]
    assert market["latest_spread_bps"] == 0.0
    assert market["average_spread_bps"] == 0.0
    assert market["candle_coverage_pct"] == 100.0
    assert payload["invalid_ticker_rows"] == 0


# --- window / grouping ------------------------------------------------------


def test_rows_outside_window_fall_back_to_latest_older(tmp_path, monkeypatch, clock):
    market_dir = _write_market(
        tmp_path / "market-data",
        ticker_rows=[
            _ticker("ADA/EUR", -100000, "100", "101", "100"),
            _ticker("ADA/EUR", -120000, "100", "101", "99"),
            _ticker("ADA/EUR", -90000, "100", "101", "101"),
        ],
        candle_rows=[_candle("ADA/EUR", -100000, "100", "101", "100", "100", "1")],
    )
    payload = _load(market_dir, "ADA/EUR", monkeypatch=monkeypatch)
    market = payload["markets"][0]
    assert market["ticker_samples_24h"] == 0
    assert market["latest_observed_at"] == _iso(-90000)
    assert market["latest_last"] == 101.0
    assert market["first_ticker_at"] is None
    assert market["latest_candle_at"] == _iso(-100000)
    assert market["candle_count_24h"] == 0
    assert payload["ticker_series"] == []
    assert payload["latest_data_at"] == _iso(-90000)
    assert payload["freshness_status"] == "stale"


def test_window_boundary_is_inclusive(tmp_path, monkeypatch, clock):
    market_dir = _write_market(
        tmp_path / "market-data",
        ticker_rows=[
            _ticker("ADA/EUR", -24 * 3600, "100", "101", "100"),
            _ticker("ADA/EUR", -24 * 3600 - 1, "101", "102", "101"),
            _ticker("ADA/EUR", 0, "102", "103", "102"),
        ],
        candle_rows=[_candle("ADA/EUR", -24 * 3600, "100", "101", "100", "100", "1")],
    )
    payload = _load(market_dir, "ADA/EUR", monkeypatch=monkeypatch)
    market = payload["markets"][0]
    assert market["ticker_samples_24h"] == 2
    assert market["first_ticker_at"] == _iso(-24 * 3600)
    assert market["latest_observed_at"] == _iso(0)


def test_unsorted_rows_are_sorted_before_use(tmp_path, monkeypatch, clock):
    market_dir = _write_market(
        tmp_path / "market-data",
        ticker_rows=[
            _ticker("ADA/EUR", -60, "102", "103", "102"),
            _ticker("ADA/EUR", -600, "100", "101", "100"),
            _ticker("ADA/EUR", -300, "101", "102", "101"),
        ],
        candle_rows=[
            _candle("ADA/EUR", -60, "102", "103", "102", "102", "1"),
            _candle("ADA/EUR", -180, "100", "101", "100", "100", "1"),
        ],
    )
    payload = _load(market_dir, "ADA/EUR", monkeypatch=monkeypatch)
    market = payload["markets"][0]
    assert market["first_ticker_at"] == _iso(-600)
    assert market["latest_observed_at"] == _iso(-60)
    assert market["price_change_pct_observed"] == pytest.approx(2.0)
    assert [point["timestamp"] for point in payload["ticker_series"]] == [
        _iso(-600),
        _iso(-300),
        _iso(-60),
    ]


def test_candle_gaps_are_counted(tmp_path, monkeypatch, clock):
    market_dir = _write_market(
        tmp_path / "market-data",
        ticker_rows=[_ticker("ADA/EUR", -3600, "100", "101", "100")],
        candle_rows=[
            _candle("ADA/EUR", -3600, "100", "101", "100", "100", "1"),
            _candle("ADA/EUR", -3000, "100", "101", "100", "100", "1"),
            _candle("ADA/EUR", -600, "100", "101", "100", "100", "1"),
        ],
    )
    payload = _load(market_dir, "ADA/EUR", monkeypatch=monkeypatch)
    market = payload["markets"][0]
    assert market["candle_count_24h"] == 3
    assert market["expected_candles_in_observed_span"] == 51
    assert market["candle_gap_minutes"] == 48
    assert market["largest_candle_gap_minutes"] == 39
    assert market["candle_coverage_pct"] == pytest.approx(5.88, abs=0.01)


def test_single_candle_span_is_one(tmp_path, monkeypatch, clock):
    market_dir = _write_market(
        tmp_path / "market-data",
        ticker_rows=[_ticker("ADA/EUR", -60, "100", "101", "100")],
        candle_rows=[_candle("ADA/EUR", -60, "100", "101", "100", "100", "1")],
    )
    market = _load(market_dir, "ADA/EUR", monkeypatch=monkeypatch)["markets"][0]
    assert market["expected_candles_in_observed_span"] == 1
    assert market["candle_gap_minutes"] == 0
    assert market["largest_candle_gap_minutes"] == 0
    assert market["ticker_span_hours"] == 0.0


# --- summary arithmetic -----------------------------------------------------


def _grouped(tickers, candles):
    return {"ADA/EUR": tickers}, {"ADA/EUR": candles}


def test_summary_spread_and_change(tmp_path, clock):
    tickers = [
        (NOW - timedelta(seconds=600), _ticker("ADA/EUR", -600, "99.9", "100.1", "100")),
        (NOW - timedelta(seconds=300), _ticker("ADA/EUR", -300, "100.9", "101.1", "101")),
    ]
    candles = [
        (NOW - timedelta(seconds=120), _candle("ADA/EUR", -120, "1", "2", "1", "1", "1")),
    ]
    ticker_map, candle_map = _grouped(tickers, candles)
    market = mo._build_market_summary(
        "ADA/EUR", ticker_map, candle_map, ticker_map, candle_map
    )
    assert market["latest_spread_bps"] == pytest.approx(19.8, abs=0.1)
    assert market["average_spread_bps"] == pytest.approx(19.8, abs=0.1)
    assert market["price_change_pct_observed"] == 1.0
    assert market["ticker_samples_per_hour"] == 0.1
    assert market["ticker_span_hours"] == pytest.approx(0.08)
    assert market["latest_candle_at"] == _iso(-120)


def test_summary_uses_all_rows_for_latest_and_window_rows_for_first():
    first_in_window = (NOW - timedelta(seconds=100), _ticker("ADA/EUR", -100, "1", "2", "10"))
    older = (NOW - timedelta(seconds=90000), _ticker("ADA/EUR", -90000, "1", "2", "1"))
    latest = (NOW - timedelta(seconds=1), _ticker("ADA/EUR", -1, "100", "102", "20"))
    window = {"ADA/EUR": [first_in_window]}
    everything = {"ADA/EUR": [older, first_in_window, latest]}
    market = mo._build_market_summary("ADA/EUR", window, {}, everything, everything)
    assert market["first_ticker_at"] == _iso(-100)
    assert market["latest_observed_at"] == _iso(-1)
    assert market["price_change_pct_observed"] == 100.0
    assert market["ticker_samples_24h"] == 1


def test_summary_of_unknown_symbol_is_all_none():
    window = {"ADA/EUR": [(NOW, _ticker("ADA/EUR", 0, "1", "2", "1.5"))]}
    market = mo._build_market_summary("XXX/EUR", window, window, window, window)
    assert market["symbol"] == "XXX/EUR"
    assert market["latest_bid"] is None
    assert market["price_change_pct_observed"] is None
    assert market["ticker_samples_24h"] == 0


def test_summary_first_price_zero_disables_change():
    tickers = [
        (NOW - timedelta(seconds=600), _ticker("ADA/EUR", -600, "100", "101", "0")),
        (NOW - timedelta(seconds=60), _ticker("ADA/EUR", -60, "100", "101", "100")),
    ]
    ticker_map, candle_map = _grouped(tickers, [])
    market = mo._build_market_summary(
        "ADA/EUR", ticker_map, candle_map, ticker_map, candle_map
    )
    assert market["price_change_pct_observed"] is None


# --- _valid_rows directly ---------------------------------------------------


def test_valid_rows_only_collects_rows_inside_the_window():
    grouped, invalid = mo._valid_rows(
        iter(
            [
                _ticker("ADA/EUR", -100, "1", "2", "1.5"),
                _ticker("ADA/EUR", -90000, "1", "2", "1.4"),
                _ticker("ADA/EUR", -80000, "bad", "2", "1.5"),
                _ticker("XXX/EUR", -100, "1", "2", "1.5"),
            ]
        ),
        "observed_at_utc",
        ["ADA/EUR"],
        NOW - timedelta(hours=24),
        NOW,
    )
    assert invalid == 2
    assert [stamp.isoformat() for stamp, _ in grouped["ADA/EUR"]] == [_iso(-100)]
    assert grouped["ADA/EUR"][0][1]["last"] == "1.5"


def test_valid_rows_candle_numeric_gate():
    grouped, invalid = mo._valid_rows(
        iter(
            [
                _candle("ADA/EUR", -120, "100", "101", "99", "100", "5"),
                _candle("ADA/EUR", -60, "100", "99", "101", "100", "5"),
                _candle("ADA/EUR", -30, "100", "101", "99", "100", ""),
            ]
        ),
        "timestamp_utc",
        ["ADA/EUR"],
        NOW - timedelta(hours=24),
        NOW,
    )
    assert invalid == 2
    assert len(grouped["ADA/EUR"]) == 1


# --- truncation flag --------------------------------------------------------


def test_read_window_truncated_flag_is_reported(tmp_path, monkeypatch, clock):
    market_dir = _write_market(
        tmp_path / "market-data",
        ticker_rows=[
            _ticker("ADA/EUR", -60 * index, "100", "101", f"{100 + index}")
            for index in range(1, 41)
        ],
        candle_rows=[_candle("ADA/EUR", -60, "100", "101", "100", "100", "1")],
    )
    monkeypatch.setattr(mo, "MARKET_DATA_READ_LIMIT", 400)
    payload = _load(market_dir, "ADA/EUR", monkeypatch=monkeypatch)
    assert payload["available"] is True
    assert payload["read_window_truncated"] is True
    assert payload["ticker_file_bytes"] > 400
    assert payload["candle_file_bytes"] < 400


def test_read_window_not_truncated_by_default(tmp_path, monkeypatch, clock):
    payload = _load(_happy_market(tmp_path), monkeypatch=monkeypatch)
    assert payload["read_window_truncated"] is False
