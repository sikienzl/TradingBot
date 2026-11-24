"""Unit tests for the exchange market-data helpers in ``src.market_data``."""

from __future__ import annotations

import ccxt
import pandas as pd
import pytest

from src.market_data import fetch_market_data, fetch_ohlcv


class _RecordingSleep:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> _RecordingSleep:
    recorder = _RecordingSleep()
    monkeypatch.setattr("src.market_data.time.sleep", recorder)
    return recorder


def _ohlcv_rows(rows: int) -> list[list[float]]:
    base = 1_700_000_000_000
    return [
        [base + i * 3_600_000, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 12.5]
        for i in range(rows)
    ]


class _OkExchange:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, limit=100):
        self.calls += 1
        return _ohlcv_rows(limit)


class _FlakyExchange:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, limit=100):
        self.calls += 1
        if self.calls <= self.failures:
            raise ccxt.NetworkError("temporary outage")
        return _ohlcv_rows(limit)


# ---------------------------------------------------------------------------
# fetch_ohlcv
# ---------------------------------------------------------------------------

def test_fetch_ohlcv_returns_dataframe_with_expected_columns() -> None:
    df = fetch_ohlcv(_OkExchange(), "BTC/EUR", "1h", limit=50)
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == [
        "timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 50
    assert df["timestamp"].iloc[0] == pd.Timestamp("2023-11-14 22:13:20")


def test_fetch_ohlcv_returns_none_for_empty_answer(caplog) -> None:
    class Empty:
        def fetch_ohlcv(self, symbol, timeframe, limit=100):
            return []

    with caplog.at_level("WARNING"):
        assert fetch_ohlcv(Empty(), "BTC/EUR") is None
    assert "No OHLCV data available for BTC/EUR." in caplog.text


def test_fetch_ohlcv_retries_then_succeeds(_no_sleep: _RecordingSleep) -> None:
    exchange = _FlakyExchange(failures=2)
    df = fetch_ohlcv(
        exchange,
        "BTC/EUR",
        retries=3,
        delay_seconds=2.0,
    )
    assert df is not None and len(df) == 100
    assert exchange.calls == 3
    assert _no_sleep.slept == [2.0, 4.0]


def test_fetch_ohlcv_gives_up_after_retries(_no_sleep: _RecordingSleep, caplog) -> None:
    exchange = _FlakyExchange(failures=99)
    with caplog.at_level("ERROR"):
        assert fetch_ohlcv(
            exchange, "BTC/EUR", retries=2, delay_seconds=1.0) is None
    assert exchange.calls == 3
    assert _no_sleep.slept == [1.0, 2.0]
    assert "Error fetching OHLCV data for BTC/EUR" in caplog.text


def test_fetch_ohlcv_does_not_sleep_when_retry_delay_is_zero(
    _no_sleep: _RecordingSleep,
) -> None:
    exchange = _FlakyExchange(failures=1)
    assert fetch_ohlcv(exchange, "BTC/EUR", retries=1) is not None
    assert _no_sleep.slept == []


def test_fetch_ohlcv_warns_on_slow_fetch(monkeypatch, caplog) -> None:
    clock = iter([0.0, 9.0])
    monkeypatch.setattr(
        "src.market_data.time.monotonic", lambda: next(clock))
    with caplog.at_level("WARNING"):
        df = fetch_ohlcv(
            _OkExchange(), "BTC/EUR", limit=10, slow_warning_seconds=8.0)
    assert df is not None
    assert "Slow OHLCV fetch for BTC/EUR: 9.0s" in caplog.text


# ---------------------------------------------------------------------------
# fetch_market_data
# ---------------------------------------------------------------------------

class _TickerExchange:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def fetch_tickers(self, symbols):
        self.batches.append(list(symbols))
        return {
            symbol: {"last": 10.0, "quoteVolume": 1000.0}
            for symbol in symbols
        }


def test_fetch_market_data_without_exchange_returns_empty(caplog) -> None:
    with caplog.at_level("ERROR"):
        assert fetch_market_data(None, ["BTC/EUR"]) == {}
    assert "Exchange is not initialised" in caplog.text


def test_fetch_market_data_without_symbols_returns_empty() -> None:
    assert fetch_market_data(_TickerExchange(), []) == {}


def test_fetch_market_data_keys_by_base_coin() -> None:
    exchange = _TickerExchange()
    data = fetch_market_data(
        exchange, ["BTC/EUR", "ETH/EUR"], batch_size=10)
    assert data == {
        "BTC": {"price": 10.0, "volume": 1000.0},
        "ETH": {"price": 10.0, "volume": 1000.0},
    }
    assert exchange.batches == [["BTC/EUR", "ETH/EUR"]]


def test_fetch_market_data_splits_into_batches() -> None:
    exchange = _TickerExchange()
    symbols = [f"{c}/EUR" for c in ("BTC", "ETH", "XMR", "ADA")]
    data = fetch_market_data(exchange, symbols, batch_size=3)
    assert len(data) == 4
    assert exchange.batches == [["BTC/EUR", "ETH/EUR", "XMR/EUR"],
                                ["ADA/EUR"]]


def test_fetch_market_data_skips_incomplete_tickers() -> None:
    class Partial:
        def fetch_tickers(self, symbols):
            return {
                "BTC/EUR": {"last": 10.0, "quoteVolume": 1000.0},
                "ETH/EUR": {"last": None, "quoteVolume": 1000.0},
                "XMR/EUR": {"last": 10.0, "quoteVolume": None},
            }

    data = fetch_market_data(Partial(), ["BTC/EUR", "ETH/EUR", "XMR/EUR"])
    assert list(data) == ["BTC"]


def test_fetch_market_data_retries_failed_batch(
    _no_sleep: _RecordingSleep,
) -> None:
    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_tickers(self, symbols):
            self.calls += 1
            if self.calls == 1:
                raise ccxt.ExchangeError("rate limited")
            return {"BTC/EUR": {"last": 1.0, "quoteVolume": 2.0}}

    exchange = Flaky()
    data = fetch_market_data(
        exchange, ["BTC/EUR"], retries=2, delay_seconds=3.0)
    assert data == {"BTC": {"price": 1.0, "volume": 2.0}}
    assert exchange.calls == 2
    assert _no_sleep.slept == [3.0]


def test_fetch_market_data_skips_batch_that_keeps_failing(
    _no_sleep: _RecordingSleep, caplog
) -> None:
    class Broken:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_tickers(self, symbols):
            self.calls += 1
            raise ccxt.NetworkError("down")

    exchange = Broken()
    with caplog.at_level("ERROR"):
        assert fetch_market_data(
            exchange, ["BTC/EUR"], retries=1, delay_seconds=1.0) == {}
    assert exchange.calls == 2
    assert _no_sleep.slept == [1.0]
    assert "Error fetching ticker data for batch 1" in caplog.text
