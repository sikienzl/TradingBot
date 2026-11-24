"""Unit tests for :mod:`src.get_data`.

The fetcher talks to a live exchange, so every test drives it through a fake
ccxt exchange. No network access and no TA-Lib C library assumptions beyond
what ``requirements.txt`` already pins.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import ccxt
import pandas as pd
import pytest

# src/get_data.py imports TA-Lib at module level, and the C library is not part of
# the slim CI test environment - skip the whole module instead of failing collection.
pytest.importorskip("talib")

from src.get_data import CryptoDataFetcher


def make_ohlcv(rows: int = 260, start_ms: int = 1_700_000_000_000) -> list[list[float]]:
    """Deterministic OHLCV series long enough for the slow indicators."""
    series = []
    for i in range(rows):
        close = 100.0 + (i % 17) - (i % 5)
        series.append(
            [
                start_ms + i * 86_400_000,
                close - 1.0,
                close + 2.0,
                close - 2.0,
                close,
                10.0 + (i % 7),
            ]
        )
    return series


class FakeExchange:
    """Minimal stand-in for a ccxt exchange object."""

    def __init__(
        self,
        markets: dict[str, dict[str, object]],
        ohlcv: list[list[float]] | None = None,
        fetch_error: Exception | None = None,
    ) -> None:
        self.markets = markets
        self.symbols = list(markets)
        self.ohlcv = ohlcv if ohlcv is not None else make_ohlcv()
        self.fetch_error = fetch_error
        self.fetch_calls: list[tuple[str, str, int]] = []
        self.loaded_markets = 0

    def load_markets(self) -> dict[str, dict[str, object]]:
        self.loaded_markets += 1
        return self.markets

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        self.fetch_calls.append((symbol, timeframe, limit))
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.ohlcv


def make_markets() -> dict[str, dict[str, object]]:
    return {
        "BTC/EUR": {"active": True},
        "ETH/EUR": {"active": True},
        "ADA/USD": {"active": True},
        "DOGE/EUR": {"active": False},
    }


# ---------------------------------------------------------------------------
# Pair selection
# ---------------------------------------------------------------------------
def test_get_available_pairs_filters_quote_and_inactive() -> None:
    fetcher = CryptoDataFetcher(quote="EUR")
    pairs = fetcher.get_available_pairs(FakeExchange(make_markets()))
    assert pairs == ["BTC/EUR", "ETH/EUR"]


def test_get_available_pairs_is_sorted() -> None:
    markets = {"ZET/EUR": {"active": True}, "ABC/EUR": {"active": True}}
    fetcher = CryptoDataFetcher(quote="EUR")
    assert fetcher.get_available_pairs(FakeExchange(markets)) == ["ABC/EUR", "ZET/EUR"]


# ---------------------------------------------------------------------------
# Exchange initialisation
# ---------------------------------------------------------------------------
def test_initialize_exchange_returns_none_for_unknown_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = CryptoDataFetcher(exchange="definitely-not-an-exchange")
    assert fetcher.initialize_exchange() is None


def test_initialize_exchange_swallows_ccxt_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken:
        def __init__(self, _config: dict) -> None:
            raise ccxt.ExchangeError("boom")

    monkeypatch.setattr(ccxt, "kraken", Broken, raising=False)
    fetcher = CryptoDataFetcher(exchange="kraken")
    assert fetcher.initialize_exchange() is None


def test_initialize_exchange_loads_markets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[FakeExchange] = []

    class FakeFactory:
        def __init__(self, config: dict) -> None:
            assert config["enableRateLimit"] is True
            self._inner = FakeExchange(make_markets())
            created.append(self._inner)

        def load_markets(self) -> dict:
            return self._inner.load_markets()

    monkeypatch.setattr(ccxt, "kraken", FakeFactory, raising=False)
    fetcher = CryptoDataFetcher(exchange="kraken")
    exchange = fetcher.initialize_exchange()
    assert exchange is not None
    assert created and created[0].loaded_markets == 1


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------
def test_fetch_ohlcv_with_retry_returns_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.get_data.time.sleep", lambda _s: None)
    fetcher = CryptoDataFetcher(days=50, timeframe="1h")
    exchange = FakeExchange({"BTC/EUR": {"active": True}})
    result = fetcher.fetch_ohlcv_with_retry(exchange, "BTC/EUR")
    assert result is not None
    assert len(result) == 260
    assert exchange.fetch_calls == [("BTC/EUR", "1h", 50)]


def test_fetch_ohlcv_with_retry_gives_up_and_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[int] = []
    monkeypatch.setattr("src.get_data.time.sleep", slept.append)
    fetcher = CryptoDataFetcher()
    exchange = FakeExchange(
        {"BTC/EUR": {"active": True}},
        fetch_error=ccxt.NetworkError("offline"),
    )
    result = fetcher.fetch_ohlcv_with_retry(exchange, "BTC/EUR", max_retries=3)
    assert result is None
    assert len(exchange.fetch_calls) == 3
    assert slept == [5, 10]
    assert fetcher.error_log == [("BTC/EUR", "offline")]


def test_fetch_ohlcv_with_retry_recovers_on_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.get_data.time.sleep", lambda _s: None)
    calls: list[int] = []

    class Flaky:
        def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int):
            calls.append(1)
            if len(calls) == 1:
                raise ccxt.ExchangeError("transient")
            return make_ohlcv(rows=3)

    fetcher = CryptoDataFetcher()
    result = fetcher.fetch_ohlcv_with_retry(Flaky(), "BTC/EUR", max_retries=3)
    assert result is not None
    assert len(result) == 3
    assert fetcher.error_log == []


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def test_calculate_indicators_adds_expected_columns() -> None:
    fetcher = CryptoDataFetcher()
    frame = pd.DataFrame(make_ohlcv(), columns=[
        "timestamp", "open", "high", "low", "close", "volume"])
    assert fetcher.calculate_indicators(frame) is True
    for column in (
        "rsi", "macd", "macd_signal", "macd_hist", "sma_50", "sma_200",
        "ema_20", "ema_50", "ema_200", "atr_14", "stoch_k", "stoch_d",
        "cci_20", "obv", "bb_upper", "bb_middle", "bb_lower",
    ):
        assert column in frame.columns
    # A 260-row series is long enough for the 200-period SMA to become finite.
    assert pd.notna(frame["sma_200"].iloc[-1])


def test_calculate_indicators_reports_failure() -> None:
    fetcher = CryptoDataFetcher()
    frame = pd.DataFrame({"close": []})
    assert fetcher.calculate_indicators(frame) is False


# ---------------------------------------------------------------------------
# process_symbol / fetch_all_data
# ---------------------------------------------------------------------------
def test_process_symbol_stores_frame_with_identity_columns() -> None:
    fetcher = CryptoDataFetcher()
    exchange = FakeExchange({"BTC/EUR": {"active": True}})
    assert fetcher.process_symbol(exchange, "BTC/EUR") is True
    frame = fetcher.data["BTC/EUR"]
    assert frame["symbol"].unique().tolist() == ["BTC/EUR"]
    assert frame["base"].unique().tolist() == ["BTC"]
    assert frame["quote"].unique().tolist() == ["EUR"]
    assert pd.api.types.is_datetime64_any_dtype(frame["timestamp"])


def test_process_symbol_returns_false_when_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.get_data.time.sleep", lambda _s: None)
    fetcher = CryptoDataFetcher()
    exchange = FakeExchange(
        {"BTC/EUR": {"active": True}}, fetch_error=ccxt.NetworkError("offline"))
    assert fetcher.process_symbol(exchange, "BTC/EUR") is False
    assert fetcher.data == {}


def test_fetch_all_data_without_exchange_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetcher = CryptoDataFetcher(exchange="not-an-exchange")
    assert fetcher.fetch_all_data() is False


def test_fetch_all_data_processes_only_matching_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.get_data.time.sleep", lambda _s: None)
    fetcher = CryptoDataFetcher(quote="EUR")
    exchange = FakeExchange(make_markets())
    monkeypatch.setattr(fetcher, "initialize_exchange", lambda: exchange)

    assert fetcher.fetch_all_data() is True
    assert sorted(fetcher.data) == ["BTC/EUR", "ETH/EUR"]
    assert [call[0] for call in exchange.fetch_calls] == ["BTC/EUR", "ETH/EUR"]


def test_fetch_all_data_keeps_going_after_a_failing_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.get_data.time.sleep", lambda _s: None)
    fetcher = CryptoDataFetcher(quote="EUR")

    class HalfBroken(FakeExchange):
        def fetch_ohlcv(self, symbol, timeframe, limit):
            if symbol == "BTC/EUR":
                raise ccxt.NetworkError("offline")
            return super().fetch_ohlcv(symbol, timeframe, limit)

    exchange = HalfBroken(make_markets())
    monkeypatch.setattr(fetcher, "initialize_exchange", lambda: exchange)

    assert fetcher.fetch_all_data() is True
    assert list(fetcher.data) == ["ETH/EUR"]
    assert fetcher.error_log == [("BTC/EUR", "offline")]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def seeded_fetcher() -> CryptoDataFetcher:
    fetcher = CryptoDataFetcher()
    fetcher.data = {
        "BTC/EUR": pd.DataFrame(
            make_ohlcv(rows=5),
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
    }
    fetcher.data["BTC/EUR"]["symbol"] = "BTC/EUR"
    return fetcher


def test_save_to_csv_without_data_returns_false(tmp_path) -> None:
    fetcher = CryptoDataFetcher()
    assert fetcher.save_to_csv(tmp_path / "out.csv") is False


def test_save_to_csv_writes_combined_rows(tmp_path) -> None:
    fetcher = seeded_fetcher()
    target = tmp_path / "out.csv"
    assert fetcher.save_to_csv(target) is True
    saved = pd.read_csv(target)
    assert len(saved) == 5
    assert "symbol" in saved.columns


def test_save_to_sqlite_creates_table(tmp_path) -> None:
    fetcher = seeded_fetcher()
    target = tmp_path / "out.db"
    assert fetcher.save_to_sqlite(target) is True
    with sqlite3.connect(target) as conn:
        count = conn.execute("SELECT COUNT(*) FROM crypto_data").fetchone()[0]
    assert count == 5


def test_save_to_sqlite_reports_failure_for_bad_path(tmp_path) -> None:
    fetcher = seeded_fetcher()
    # A directory cannot be opened as a SQLite database file.
    assert fetcher.save_to_sqlite(tmp_path) is False


# ---------------------------------------------------------------------------
# Sample access
# ---------------------------------------------------------------------------
def test_get_sample_data_defaults_to_first_symbol() -> None:
    fetcher = seeded_fetcher()
    sample = fetcher.get_sample_data()
    assert sample is not None
    assert len(sample) == 5


def test_get_sample_data_returns_none_for_unknown_symbol() -> None:
    fetcher = seeded_fetcher()
    assert fetcher.get_sample_data("NOPE/EUR") is None


def test_get_sample_data_without_any_data() -> None:
    assert CryptoDataFetcher().get_sample_data() is None


def test_fetcher_defaults() -> None:
    fetcher = CryptoDataFetcher()
    assert fetcher.exchange == "kraken"
    assert fetcher.quote == "EUR"
    assert fetcher.timeframe == "1d"
    assert fetcher.data == {}
    assert fetcher.error_log == []
    assert isinstance(fetcher, CryptoDataFetcher)


def test_process_symbol_handles_symbol_without_slash() -> None:
    fetcher = CryptoDataFetcher()
    exchange = FakeExchange({"BTC": {"active": True}})
    assert fetcher.process_symbol(exchange, "BTC") is True
    frame = fetcher.data["BTC"]
    assert frame["base"].unique().tolist() == ["BTC"]
    assert frame["quote"].unique().tolist() == [""]


def test_fake_exchange_helper_is_sane() -> None:
    exchange = FakeExchange(make_markets())
    assert exchange.symbols == ["BTC/EUR", "ETH/EUR", "ADA/USD", "DOGE/EUR"]
    assert len(make_ohlcv(rows=3)) == 3
    assert isinstance(SimpleNamespace(), object)
