"""Unit tests for the layer-1 market analysis service.

The service receives the config and the bot-owned callables explicitly, so
these tests drive the real code paths with stubs and never import
:mod:`src.trading_bot`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from src.market_analysis import (
    analyze_markets,
    dynamic_excluded_coins,
    select_analysis_coins,
)

JOURNAL_HEADER = (
    "timestamp,iteration,coin,action,price,amount_coin,amount_base,pnl_base,"
    "pnl_pct,hold_seconds,signal_source,signal_confidence,recommendation,"
    "reason,dry_run"
)


@dataclass
class _Config:
    base_currency: str = "EUR"
    excluded_coins: set[str] = field(default_factory=set)
    min_volume_base: float = 0.0
    top_n_for_analysis: int = 10
    slow_analysis_warning_seconds: float = 8.0
    dynamic_lossmaker_exclusion_enabled: bool = False
    performance_log_enabled: bool = False
    performance_log_file: str = "trade_journal.csv"
    dynamic_lossmaker_window: int = 50
    dynamic_lossmaker_min_sells: int = 3
    dynamic_lossmaker_min_pnl_loss: float = 0.003
    dynamic_lossmaker_max_win_rate_pct: float = 45.0
    dynamic_lossmaker_min_max_hold_exit_ratio: float = 0.5


def _capture(
    monkeypatch: pytest.MonkeyPatch, logger_name: str = "src.market_analysis"
) -> list[tuple[int, str]]:
    """Collect the records the service logs, formatted exactly as emitted.

    ``scripts/collect_hailo_training`` calls ``logging.disable(CRITICAL)`` at
    import time, which silences every logger in the process for the rest of the
    pytest session. Re-arming the global switch keeps this file independent of
    collection order; ``monkeypatch`` restores it afterwards.
    """
    records: list[tuple[int, str]] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append((record.levelno, record.getMessage()))

    handler = _Handler()
    target = logging.getLogger(logger_name)
    monkeypatch.setattr(logging.Logger.manager, "disable", logging.NOTSET)
    monkeypatch.setattr(target, "handlers", [handler])
    monkeypatch.setattr(target, "level", logging.DEBUG)
    return records


def _journal(tmp_path, rows: list[str], name: str = "trade_journal.csv"):
    path = tmp_path / name
    path.write_text(
        "\n".join([JOURNAL_HEADER, *rows]) + "\n", encoding="utf-8")
    return str(path)


SELL_TIMEOUT = (
    "2026-05-20T10:0{idx}:00,{idx},SUI,sell,1,1,1,{pnl},-0.2,420,rules,,BUY,"
    "MAX-HOLD-TIME reached (420s >= 420s),true"
)


def _lossmaker_rows() -> list[str]:
    return [SELL_TIMEOUT.format(idx=idx, pnl=pnl)
            for idx, pnl in enumerate((-0.0020, -0.0015, -0.0010), start=1)]


def _lossmaker_config(tmp_path, rows=None, name="trade_journal.csv", **overrides) -> _Config:
    config = _Config(
        dynamic_lossmaker_exclusion_enabled=True,
        performance_log_enabled=True,
        performance_log_file=_journal(
            tmp_path, _lossmaker_rows() if rows is None else rows, name),
    )
    for attr, value in overrides.items():
        setattr(config, attr, value)
    return config


# ---------------------------------------------------------------------------
# dynamic_excluded_coins
# ---------------------------------------------------------------------------
def test_dynamic_exclusions_disabled_by_feature_flag(tmp_path):
    config = _lossmaker_config(
        tmp_path, dynamic_lossmaker_exclusion_enabled=False)
    assert dynamic_excluded_coins(config) == set()


def test_dynamic_exclusions_disabled_by_journal_flag(tmp_path):
    config = _lossmaker_config(tmp_path, performance_log_enabled=False)
    assert dynamic_excluded_coins(config) == set()


def test_dynamic_exclusions_without_journal_file():
    config = _Config(
        dynamic_lossmaker_exclusion_enabled=True,
        performance_log_enabled=True,
        performance_log_file="does-not-exist.csv",
    )
    assert dynamic_excluded_coins(config) == set()


def test_dynamic_exclusions_survive_an_unreadable_journal(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    config = _Config(
        dynamic_lossmaker_exclusion_enabled=True,
        performance_log_enabled=True,
        performance_log_file=str(tmp_path),
    )
    records = _capture(monkeypatch)
    assert dynamic_excluded_coins(config) == set()
    assert len(records) == 1
    level, message = records[0]
    assert level == logging.WARNING
    assert message.startswith("Journal could not be read for dynamic exclusions:")


def test_dynamic_exclusions_ignore_journal_without_required_columns(tmp_path):
    path = tmp_path / "trade_journal.csv"
    path.write_text("coin,action\nSUI,sell\n", encoding="utf-8")
    config = _Config(
        dynamic_lossmaker_exclusion_enabled=True,
        performance_log_enabled=True,
        performance_log_file=str(path),
    )
    assert dynamic_excluded_coins(config) == set()


def test_dynamic_exclusions_ignore_an_empty_journal(tmp_path):
    path = tmp_path / "trade_journal.csv"
    path.write_text(JOURNAL_HEADER + "\n", encoding="utf-8")
    config = _Config(
        dynamic_lossmaker_exclusion_enabled=True,
        performance_log_enabled=True,
        performance_log_file=str(path),
    )
    assert dynamic_excluded_coins(config) == set()


def test_dynamic_exclusions_ignore_buy_only_journal(tmp_path):
    config = _lossmaker_config(
        tmp_path,
        rows=["2026-05-20T10:00:00,1,SUI,buy,1,1,1,0,0,0,rules,,BUY,ENTRY,true"],
    )
    assert dynamic_excluded_coins(config) == set()


def test_dynamic_exclusions_detect_recent_timeout_losers(tmp_path):
    assert dynamic_excluded_coins(_lossmaker_config(tmp_path)) == {"SUI"}


def test_dynamic_exclusions_upper_case_coin_names(tmp_path):
    config = _lossmaker_config(
        tmp_path, rows=[row.replace(",SUI,", ",sui,")
                        for row in _lossmaker_rows()])
    assert dynamic_excluded_coins(config) == {"SUI"}


def test_dynamic_exclusions_need_enough_sells(tmp_path):
    config = _lossmaker_config(tmp_path, dynamic_lossmaker_min_sells=4)
    assert dynamic_excluded_coins(config) == set()


def test_dynamic_exclusions_need_a_real_loss(tmp_path):
    config = _lossmaker_config(
        tmp_path, dynamic_lossmaker_min_pnl_loss=0.01)
    assert dynamic_excluded_coins(config) == set()


def test_dynamic_exclusions_reject_a_high_win_rate(tmp_path):
    rows = [
        SELL_TIMEOUT.format(idx=1, pnl=-0.0040),
        SELL_TIMEOUT.format(idx=2, pnl=-0.0040),
        SELL_TIMEOUT.format(idx=3, pnl=0.0010),
    ]
    config = _lossmaker_config(
        tmp_path, rows=rows, dynamic_lossmaker_max_win_rate_pct=20.0)
    assert dynamic_excluded_coins(config) == set()
    # Sanity check: the same journal passes once the win-rate gate is relaxed.
    relaxed = _lossmaker_config(
        tmp_path, rows=rows, dynamic_lossmaker_max_win_rate_pct=50.0,
        name="relaxed.csv")
    assert dynamic_excluded_coins(relaxed) == {"SUI"}


def test_dynamic_exclusions_reject_other_exit_reasons(tmp_path):
    config = _lossmaker_config(
        tmp_path,
        rows=[row.replace("MAX-HOLD-TIME reached (420s >= 420s)",
                          "ATR-TAKE-PROFIT")
              for row in _lossmaker_rows()],
    )
    assert dynamic_excluded_coins(config) == set()


def test_dynamic_exclusions_only_look_at_the_recent_window(tmp_path):
    config = _lossmaker_config(
        tmp_path,
        dynamic_lossmaker_window=1,
    )
    assert dynamic_excluded_coins(config) == set()


def test_dynamic_exclusions_empty_window_returns_empty(tmp_path):
    config = _lossmaker_config(tmp_path, dynamic_lossmaker_window=0)
    assert dynamic_excluded_coins(config) == set()


# ---------------------------------------------------------------------------
# select_analysis_coins
# ---------------------------------------------------------------------------
def test_select_coins_applies_the_volume_gate(monkeypatch: pytest.MonkeyPatch):
    config = _Config(min_volume_base=100.0)
    records = _capture(monkeypatch)
    market_data = {
        "BTC": {"price": 1.0, "volume": 500.0},
        "DOGE": {"price": 1.0, "volume": 99.0},
    }
    assert select_analysis_coins(config, market_data, set()) == ["BTC"]
    assert (
        logging.INFO,
        "Coins filtered by volume (100 EUR minimum): 1",
    ) in records


def test_select_coins_skips_static_and_dynamic_exclusions():
    config = _Config(excluded_coins={"LUNA"})
    market_data = {
        "BTC": {"price": 1.0, "volume": 500.0},
        "LUNA": {"price": 1.0, "volume": 400.0},
        "SUI": {"price": 1.0, "volume": 300.0},
    }
    assert select_analysis_coins(config, market_data, {"SUI"}) == ["BTC"]


def test_select_coins_keeps_the_top_n_by_volume(monkeypatch: pytest.MonkeyPatch):
    config = _Config(top_n_for_analysis=2)
    records = _capture(monkeypatch)
    market_data = {
        "ADA": {"price": 1.0, "volume": 100.0},
        "BTC": {"price": 1.0, "volume": 900.0},
        "ETH": {"price": 1.0, "volume": 500.0},
    }
    assert select_analysis_coins(config, market_data, set()) == ["BTC", "ETH"]
    assert (
        logging.INFO,
        "Running detailed analysis for top 2 coins (by volume).",
    ) in records


def test_select_coins_analyses_everything_below_top_n(
    monkeypatch: pytest.MonkeyPatch
):
    config = _Config(top_n_for_analysis=10)
    records = _capture(monkeypatch)
    market_data = {
        "ADA": {"price": 1.0, "volume": 100.0},
        "BTC": {"price": 1.0, "volume": 900.0},
    }
    assert select_analysis_coins(config, market_data, set()) == ["ADA", "BTC"]
    assert (
        logging.INFO,
        "Running detailed analysis for all 2 high-volume coins.",
    ) in records


def test_select_coins_appends_extra_coins_that_are_not_in_the_top_n():
    config = _Config(top_n_for_analysis=1)
    market_data = {
        "BTC": {"price": 1.0, "volume": 900.0},
        "ETH": {"price": 1.0, "volume": 500.0},
        "SUI": {"price": 1.0, "volume": 10.0},
    }
    assert select_analysis_coins(
        config, market_data, set(), extra_coins=["SUI", "BTC"]) == [
            "BTC", "SUI"]


def test_select_coins_ignores_extra_coins_without_market_data():
    config = _Config(top_n_for_analysis=5)
    market_data = {"BTC": {"price": 1.0, "volume": 900.0}}
    assert select_analysis_coins(
        config, market_data, set(), extra_coins=["DOGE"]) == ["BTC"]


# ---------------------------------------------------------------------------
# analyze_markets
# ---------------------------------------------------------------------------
def _analysis(score: int, recommendation: str = "BUY") -> dict[str, Any]:
    return {
        "recommendation": recommendation,
        "score": score,
        "signal_source": "rules",
        "signal_confidence": None,
    }


def test_analyze_markets_sorts_by_score_and_merges_market_fields(
    monkeypatch: pytest.MonkeyPatch
):
    config = _Config(top_n_for_analysis=5)
    _capture(monkeypatch)
    market_data = {
        "ADA": {"price": 3.0, "volume": 100.0},
        "BTC": {"price": 1.0, "volume": 900.0},
        "ETH": {"price": 2.0, "volume": 500.0},
    }

    def _analyze_coin(coin, current_price):
        return _analysis({"ADA": 40, "BTC": 90, "ETH": 60}[coin])

    analysis = analyze_markets(
        config, lambda: set(), _analyze_coin, market_data)
    assert list(analysis) == ["BTC", "ETH", "ADA"]
    assert analysis["ETH"] == {
        "coin": "ETH",
        "price": 2.0,
        "volume": 500.0,
        "recommendation": "BUY",
        "score": 60,
        "signal_source": "rules",
        "signal_confidence": None,
    }


def test_analyze_markets_drops_coins_without_analysis(
    monkeypatch: pytest.MonkeyPatch
):
    config = _Config(top_n_for_analysis=5)
    _capture(monkeypatch)
    market_data = {
        "ADA": {"price": 3.0, "volume": 100.0},
        "BTC": {"price": 1.0, "volume": 900.0},
    }

    def _analyze_coin(coin, current_price):
        return None if coin == "ADA" else _analysis(90)

    assert list(analyze_markets(
        config, lambda: set(), _analyze_coin, market_data)) == ["BTC"]


def test_analyze_markets_logs_dynamic_exclusions(
    monkeypatch: pytest.MonkeyPatch
):
    config = _Config(top_n_for_analysis=5)
    records = _capture(monkeypatch)
    market_data = {"BTC": {"price": 1.0, "volume": 900.0}}
    analyze_markets(
        config, lambda: {"SUI", "LUNA"}, lambda *_a: _analysis(10), market_data)
    assert (
        logging.INFO,
        "Dynamically excluded lossmaker coins: LUNA, SUI",
    ) in records
    assert not any("Dynamically excluded" in message
                   for _level, message in records[1:])


def test_analyze_markets_logs_progress_in_analysis_order(
    monkeypatch: pytest.MonkeyPatch
):
    config = _Config(top_n_for_analysis=5)
    records = _capture(monkeypatch)
    market_data = {
        "ADA": {"price": 3.0, "volume": 100.0},
        "BTC": {"price": 1.0, "volume": 900.0},
    }
    analyze_markets(config, lambda: set(), lambda *_a: _analysis(50), market_data)
    assert [
        (logging.INFO, "🔎 Analysis progress: 1/2 - ADA"),
        (logging.INFO, "🔎 Analysis progress: 2/2 - BTC"),
    ] == [record for record in records if "Analysis progress" in record[1]]


def test_analyze_markets_warns_about_a_slow_coin_analysis(
    monkeypatch: pytest.MonkeyPatch
):
    config = _Config(top_n_for_analysis=5, slow_analysis_warning_seconds=2.0)
    records = _capture(monkeypatch)
    ticks = iter([10.0, 13.5])
    monkeypatch.setattr(
        "src.market_analysis.time", SimpleNamespace(monotonic=lambda: next(ticks)))
    market_data = {"BTC": {"price": 1.0, "volume": 900.0}}
    analyze_markets(config, lambda: set(), lambda *_a: _analysis(50), market_data)
    assert (logging.WARNING, "Slow coin analysis for BTC: 3.5s") in records


def test_analyze_markets_keeps_fast_coins_quiet(monkeypatch: pytest.MonkeyPatch):
    config = _Config(top_n_for_analysis=5, slow_analysis_warning_seconds=8.0)
    records = _capture(monkeypatch)
    ticks = iter([10.0, 11.0])
    monkeypatch.setattr(
        "src.market_analysis.time", SimpleNamespace(monotonic=lambda: next(ticks)))
    market_data = {"BTC": {"price": 1.0, "volume": 900.0}}
    analyze_markets(config, lambda: set(), lambda *_a: _analysis(50), market_data)
    assert not [record for record in records if record[0] == logging.WARNING]


def test_analyze_markets_includes_extra_coins_for_open_positions(
    monkeypatch: pytest.MonkeyPatch
):
    config = _Config(top_n_for_analysis=1, min_volume_base=100.0)
    _capture(monkeypatch)
    market_data = {
        "BTC": {"price": 1.0, "volume": 900.0},
        "ETH": {"price": 2.0, "volume": 500.0},
        "SUI": {"price": 3.0, "volume": 10.0},
    }
    analysed: list[str] = []

    def _analyze_coin(coin, current_price):
        analysed.append(coin)
        return _analysis(50)

    analysis = analyze_markets(
        config, lambda: set(), _analyze_coin, market_data, extra_coins=["SUI"])
    assert analysed == ["BTC", "SUI"]
    assert set(analysis) == {"BTC", "SUI"}
