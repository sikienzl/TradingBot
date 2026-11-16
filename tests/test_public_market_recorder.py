import csv
import json
from datetime import UTC, datetime

import ccxt
import pytest

from src.public_market_recorder import PublicMarketRecorder


class FakeExchange:
    def __init__(self, tickers=None, candles=None):
        self.tickers = tickers or {}
        self.candles = candles or {}

    def fetch_tickers(self, symbols):
        assert symbols == ["ADA/EUR"]
        return self.tickers

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        assert symbol == "ADA/EUR"
        assert timeframe == "1m"
        assert limit == 720
        if since is None:
            return self.candles.get("initial", [])
        assert since == 60_000
        return self.candles.get("incremental", [])


def _recorder(tmp_path, exchange):
    return PublicMarketRecorder(
        exchange,
        tmp_path,
        ("ADA/EUR",),
    )


def test_recorder_writes_public_tickers_and_only_completed_candles(tmp_path):
    exchange = FakeExchange(
        tickers={
            "ADA/EUR": {
                "timestamp": 120_000,
                "bid": 0.49,
                "ask": 0.51,
                "last": 0.50,
                "baseVolume": 100.0,
                "quoteVolume": 50.0,
            }
        },
        candles={
            "initial": [
                [0, 0.4, 0.6, 0.3, 0.5, 10],
                [60_000, 0.5, 0.7, 0.4, 0.6, 12],
                [120_000, 0.6, 0.8, 0.5, 0.7, 14],
            ]
        },
    )
    recorder = _recorder(tmp_path, exchange)
    try:
        assert recorder.poll_once(datetime.fromtimestamp(120, UTC)) == (1, 2)
    finally:
        recorder.close()

    with (tmp_path / "tickers.csv").open(newline="", encoding="utf-8") as source:
        tickers = list(csv.DictReader(source))
    with (tmp_path / "candles_1m.csv").open(newline="", encoding="utf-8") as source:
        candles = list(csv.DictReader(source))
    metadata = json.loads((tmp_path / "metadata.json").read_text(encoding="utf-8"))

    assert len(tickers) == 1
    assert tickers[0]["bid"] == "0.49"
    assert tickers[0]["ask"] == "0.51"
    assert [row["timestamp_utc"] for row in candles] == [
        "1970-01-01T00:00:00+00:00",
        "1970-01-01T00:01:00+00:00",
    ]
    assert metadata["credentials_used"] is False
    assert metadata["orders_submitted"] is False


def test_recorder_deduplicates_candles_after_restart(tmp_path):
    first_exchange = FakeExchange(
        candles={"initial": [[0, 1, 2, 0.5, 1.5, 10]]}
    )
    first = _recorder(tmp_path, first_exchange)
    assert first.poll_once(datetime.fromtimestamp(60, UTC)) == (0, 1)
    first.close()

    second_exchange = FakeExchange(
        candles={
            "incremental": [
                [0, 1, 2, 0.5, 1.5, 10],
                [60_000, 1.5, 2, 1, 1.8, 11],
                [120_000, 1.8, 2, 1, 1.9, 12],
            ]
        }
    )
    second = _recorder(tmp_path, second_exchange)
    try:
        assert second.poll_once(datetime.fromtimestamp(180, UTC)) == (0, 2)
    finally:
        second.close()

    with (tmp_path / "candles_1m.csv").open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    assert len(rows) == 3


def test_recorder_refuses_a_second_writer(tmp_path):
    first = _recorder(tmp_path, FakeExchange())
    try:
        with pytest.raises(RuntimeError, match="another public market recorder"):
            _recorder(tmp_path, FakeExchange())
    finally:
        first.close()


def test_recorder_rejects_mismatched_metadata_or_csv_schema(tmp_path):
    first = _recorder(tmp_path, FakeExchange())
    first.close()
    metadata_path = tmp_path / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["ticker_interval_seconds"] = 30
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(ValueError, match="metadata mismatch"):
        _recorder(tmp_path, FakeExchange())

    metadata_path.unlink()
    (tmp_path / "tickers.csv").write_text("wrong,header\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected CSV header"):
        _recorder(tmp_path, FakeExchange())


def test_recorder_logs_exchange_errors_without_stopping(tmp_path, caplog):
    class FailingExchange(FakeExchange):
        def fetch_tickers(self, symbols):
            raise ccxt.NetworkError("request failed")

        def fetch_ohlcv(self, symbol, timeframe, since, limit):
            raise ccxt.NetworkError("request failed")

    recorder = _recorder(tmp_path, FailingExchange())
    try:
        with caplog.at_level("ERROR", logger="public_market_recorder"):
            assert recorder.poll_once(datetime.fromtimestamp(180, UTC)) == (0, 0)
    finally:
        recorder.close()
    assert "Ticker request failed (NetworkError)" in caplog.text
    assert "OHLCV request failed for ADA/EUR (NetworkError)" in caplog.text
