"""Durable per-decision inference backend counters.

Audit point 2 requires that a silent cluster fallback to CPU or rules-only be
visible in monitoring. These tests cover the counter store itself - the
normalisation, the fail-closed read, the atomic write, the write throttle, and
the fact that :func:`src.coin_analysis.analyze_coin` records the backend that
served each decision - with no Hailo package, no model and no exchange, so they
also run in the slim CI inference-matrix environment.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from src.coin_analysis import analyze_coin
from src.inference_decision_stats import (
    BACKEND_STATS_VERSION,
    CPU_BACKEND,
    DEFAULT_MIN_WRITE_INTERVAL_SECONDS,
    DEFAULT_STATS_PATH,
    HAILO_BACKEND,
    OTHER_BACKEND,
    RULES_ONLY_BACKEND,
    BackendStats,
    BackendStatsRecorder,
    backend_stats_to_payload,
    normalize_backend,
    read_backend_stats,
    record_backend_selection,
    resolve_min_write_interval_seconds,
    resolve_stats_path,
    write_backend_stats,
)

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Path and throttle resolution
# ---------------------------------------------------------------------------

def test_stats_path_defaults_to_the_shared_repository_path(monkeypatch) -> None:
    monkeypatch.delenv("TRADING_INFERENCE_STATS_FILE", raising=False)
    assert resolve_stats_path() == DEFAULT_STATS_PATH


def test_stats_path_honours_the_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_INFERENCE_STATS_FILE", "/tmp/backend.json")
    assert resolve_stats_path() == "/tmp/backend.json"


def test_stats_path_can_be_disabled(monkeypatch) -> None:
    for value in ("", "off", "none", "false", "no", "disabled", " OFF "):
        monkeypatch.setenv("TRADING_INFERENCE_STATS_FILE", value)
        assert resolve_stats_path() == ""


def test_min_write_interval_defaults_and_ignores_nonsense(monkeypatch) -> None:
    monkeypatch.delenv("TRADING_INFERENCE_STATS_MIN_WRITE_SECONDS", raising=False)
    assert resolve_min_write_interval_seconds() == DEFAULT_MIN_WRITE_INTERVAL_SECONDS
    for value in ("not-a-number", "-5", "nan", "inf"):
        monkeypatch.setenv("TRADING_INFERENCE_STATS_MIN_WRITE_SECONDS", value)
        assert (
            resolve_min_write_interval_seconds()
            == DEFAULT_MIN_WRITE_INTERVAL_SECONDS
        )
    monkeypatch.setenv("TRADING_INFERENCE_STATS_MIN_WRITE_SECONDS", "0")
    assert resolve_min_write_interval_seconds() == 0.0


# ---------------------------------------------------------------------------
# Backend normalisation
# ---------------------------------------------------------------------------

def test_normalize_backend_maps_the_labels_the_cascade_produces() -> None:
    assert normalize_backend("hailo") == HAILO_BACKEND
    assert normalize_backend("cpu") == CPU_BACKEND
    assert normalize_backend("rules_only") == RULES_ONLY_BACKEND
    # The catboost-only profile reports its configured backend name instead.
    assert normalize_backend("catboost") == CPU_BACKEND
    assert normalize_backend("Hailo") == HAILO_BACKEND
    assert normalize_backend(None) == OTHER_BACKEND
    assert normalize_backend("quantum_chip") == OTHER_BACKEND


# ---------------------------------------------------------------------------
# Reads fail closed, never raise
# ---------------------------------------------------------------------------

def test_read_backend_stats_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    stats = read_backend_stats(tmp_path / "absent.json")
    assert stats.counts == {}
    assert stats.total == 0
    assert stats.last_backend is None
    assert not stats.unreadable


def test_read_backend_stats_of_unparseable_content_is_unreadable(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert read_backend_stats(broken).unreadable

    wrong_version = tmp_path / "version.json"
    wrong_version.write_text(
        json.dumps({"version": 99, "decisions": {"cpu": 1}}), encoding="utf-8")
    assert read_backend_stats(wrong_version).unreadable

    negative = tmp_path / "negative.json"
    negative.write_text(
        json.dumps({"version": BACKEND_STATS_VERSION, "decisions": {"cpu": -3}}),
        encoding="utf-8",
    )
    assert read_backend_stats(negative).unreadable


def test_read_backend_stats_counts_a_valid_document(tmp_path: Path) -> None:
    path = tmp_path / "backend_stats.json"
    write_backend_stats(
        path,
        BackendStats(
            counts={HAILO_BACKEND: 7, CPU_BACKEND: 2, RULES_ONLY_BACKEND: 1},
            last_backend=CPU_BACKEND,
            updated_utc=NOW,
        ),
        now_utc=NOW,
    )
    stats = read_backend_stats(path)
    assert stats.counts == {HAILO_BACKEND: 7, CPU_BACKEND: 2, RULES_ONLY_BACKEND: 1}
    assert stats.total == 10
    assert stats.last_backend == CPU_BACKEND
    assert stats.updated_utc == NOW
    assert stats.share(CPU_BACKEND) == 0.2
    assert stats.share(RULES_ONLY_BACKEND) == 0.1
    assert stats.share("absent") == 0.0


def test_share_of_an_empty_store_is_zero() -> None:
    assert BackendStats().share(HAILO_BACKEND) == 0.0


# ---------------------------------------------------------------------------
# Writes are atomic and create their directory
# ---------------------------------------------------------------------------

def test_write_backend_stats_creates_parent_and_leaves_no_temp_files(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested" / "deeper" / "backend_stats.json"
    write_backend_stats(
        path,
        BackendStats(counts={CPU_BACKEND: 1}, last_backend=CPU_BACKEND),
        now_utc=NOW,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == BACKEND_STATS_VERSION
    assert payload["decisions"] == {CPU_BACKEND: 1}
    assert payload["last_backend"] == CPU_BACKEND
    assert payload["updated_utc"] == NOW.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    assert list(path.parent.glob(".backend_stats.json-*")) == []


def test_payload_only_contains_known_backends_present_in_counts() -> None:
    payload = backend_stats_to_payload(
        BackendStats(counts={CPU_BACKEND: 3, OTHER_BACKEND: 1}), now_utc=NOW)
    assert payload["decisions"] == {CPU_BACKEND: 3, OTHER_BACKEND: 1}


# ---------------------------------------------------------------------------
# Recording decisions
# ---------------------------------------------------------------------------

def test_record_backend_selection_accumulates_across_calls(tmp_path: Path) -> None:
    path = tmp_path / "backend_stats.json"
    for backend in (HAILO_BACKEND, HAILO_BACKEND, CPU_BACKEND):
        assert record_backend_selection(
            backend, path=path, now=NOW, min_write_interval_seconds=0.0)

    stats = read_backend_stats(path)
    assert stats.counts == {HAILO_BACKEND: 2, CPU_BACKEND: 1}
    assert stats.last_backend == CPU_BACKEND


def test_record_backend_selection_keeps_counters_across_a_restart(
    tmp_path: Path,
) -> None:
    """The recorder is stateless: previous totals always come from the file."""
    path = tmp_path / "backend_stats.json"
    record_backend_selection(
        HAILO_BACKEND, path=path, now=NOW, min_write_interval_seconds=0.0)
    # A new process would build a new recorder; nothing is kept in memory.
    record_backend_selection(
        RULES_ONLY_BACKEND, path=path, now=NOW, min_write_interval_seconds=0.0)

    assert read_backend_stats(path).counts == {
        HAILO_BACKEND: 1, RULES_ONLY_BACKEND: 1,
    }


def test_record_backend_selection_throttles_a_steady_backend(tmp_path: Path) -> None:
    path = tmp_path / "backend_stats.json"
    record_backend_selection(
        HAILO_BACKEND, path=path, now=NOW, min_write_interval_seconds=60.0)
    assert not record_backend_selection(
        HAILO_BACKEND, path=path, now=NOW + timedelta(seconds=5),
        min_write_interval_seconds=60.0)
    assert read_backend_stats(path).counts == {HAILO_BACKEND: 1}

    assert record_backend_selection(
        HAILO_BACKEND, path=path, now=NOW + timedelta(seconds=61),
        min_write_interval_seconds=60.0)
    assert read_backend_stats(path).counts == {HAILO_BACKEND: 2}


def test_a_backend_change_is_written_immediately(tmp_path: Path) -> None:
    """The fallback transition is the event the alert watches for."""
    path = tmp_path / "backend_stats.json"
    record_backend_selection(
        HAILO_BACKEND, path=path, now=NOW, min_write_interval_seconds=3600.0)
    assert record_backend_selection(
        CPU_BACKEND, path=path, now=NOW + timedelta(seconds=1),
        min_write_interval_seconds=3600.0)
    stats = read_backend_stats(path)
    assert stats.counts == {HAILO_BACKEND: 1, CPU_BACKEND: 1}
    assert stats.last_backend == CPU_BACKEND


def test_record_backend_selection_is_a_noop_when_disabled(tmp_path: Path) -> None:
    assert not record_backend_selection(
        HAILO_BACKEND, path="", now=NOW, min_write_interval_seconds=0.0)
    assert list(tmp_path.iterdir()) == []


def test_record_backend_selection_survives_an_unwritable_path(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """A lost metric must never change a trading decision."""
    monkeypatch.setattr("src.inference_decision_stats._write_error_logged", False)
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    path = blocked / "backend_stats.json"

    with caplog.at_level(logging.WARNING, logger="src.inference_decision_stats"):
        assert not record_backend_selection(
            HAILO_BACKEND, path=path, now=NOW, min_write_interval_seconds=0.0)

    assert "could not be written" in caplog.text
    # A second failure is not logged again - the bot loop must not be flooded.
    caplog.clear()
    assert not record_backend_selection(
        CPU_BACKEND, path=path, now=NOW, min_write_interval_seconds=0.0)
    assert caplog.text == ""


def test_record_backend_selection_uses_the_environment_path(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "from_env.json"
    monkeypatch.setenv("TRADING_INFERENCE_STATS_FILE", str(path))
    monkeypatch.setenv("TRADING_INFERENCE_STATS_MIN_WRITE_SECONDS", "0")

    assert BackendStatsRecorder().record(HAILO_BACKEND, now=NOW)
    assert read_backend_stats(path).counts == {HAILO_BACKEND: 1}


def test_recorder_honours_an_explicit_path(tmp_path: Path) -> None:
    path = tmp_path / "explicit.json"
    recorder = BackendStatsRecorder(path)
    assert recorder.path == str(path)
    assert recorder.record("catboost", now=NOW)
    assert read_backend_stats(path).counts == {CPU_BACKEND: 1}


# ---------------------------------------------------------------------------
# The analysis path records the backend that served the decision
# ---------------------------------------------------------------------------

@dataclass
class _AnalysisConfig:
    base_currency: str = "EUR"
    ohlcv_timeframe: str = "1h"
    ohlcv_limit: int = 60
    rsi_period: int = 14
    sma_period_short: int = 20
    sma_period_long: int = 50
    ml_min_confidence: float = 0.6
    tabular_min_confidence: float = 0.5
    tabular_buy_min_confidence: float = 0.55
    tabular_model_backend: str = "hailo"
    tabular_block_on_sell: bool = True
    tabular_sell_block_min_proba: float = 0.6
    tabular_allow_buy_entries: bool = True
    tabular_source_gate_enabled: bool = True
    tabular_override_min_confidence: float = 0.8
    tabular_override_margin: float = 0.2
    min_entry_atr_pct: float = 0.0


def _ohlcv(rows: int = 60) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=rows, freq="h")
    close = 100.0 + 0.1 * np.arange(rows, dtype=float)
    return pd.DataFrame(
        {
            "timestamp": index,
            "open": close - 0.05,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.linspace(10.0, 20.0, rows),
        }
    )


class _Predictor:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result

    def predict(self, row_data: pd.DataFrame, confidence_threshold: float) -> dict[str, object]:
        return self.result


def _fetcher(frame: pd.DataFrame):
    def fetch(symbol: str, **_kwargs: object) -> pd.DataFrame:
        return frame

    return fetch


def _rules(recommendation: str = "HOLD", score: int = 50):
    def rule_recommender(rsi: float, sma_short: float, sma_long: float, price: float):
        return recommendation, score

    return rule_recommender


def test_analyze_coin_records_the_backend_that_served_the_decision(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "backend_stats.json"
    monkeypatch.setenv("TRADING_INFERENCE_STATS_FILE", str(path))
    monkeypatch.setenv("TRADING_INFERENCE_STATS_MIN_WRITE_SECONDS", "0")
    predictor = _Predictor({
        "decision": "kaufen", "confidence": 0.9,
        "proba": {"kaufen": 0.9, "halten": 0.08, "verkaufen": 0.02},
        "backend": "cpu",
    })

    result = analyze_coin(
        _AnalysisConfig(), _fetcher(_ohlcv()), None, predictor, "BTC", 100.0,
        rule_recommender=_rules("HOLD", 50),
    )

    assert result is not None
    assert result["signal_source"] == "cpu"
    stats = read_backend_stats(path)
    assert stats.counts == {CPU_BACKEND: 1}
    assert stats.last_backend == CPU_BACKEND


def test_analyze_coin_records_a_rules_only_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    """The cluster failure mode the audit cares about: no model served it."""
    path = tmp_path / "backend_stats.json"
    monkeypatch.setenv("TRADING_INFERENCE_STATS_FILE", str(path))
    monkeypatch.setenv("TRADING_INFERENCE_STATS_MIN_WRITE_SECONDS", "0")
    predictor = _Predictor({
        "decision": "halten", "confidence": 0.0, "proba": {},
        "backend": RULES_ONLY_BACKEND,
    })

    result = analyze_coin(
        _AnalysisConfig(), _fetcher(_ohlcv()), None, predictor, "BTC", 100.0,
        rule_recommender=_rules("HOLD", 50),
    )

    assert result is not None
    assert read_backend_stats(path).counts == {RULES_ONLY_BACKEND: 1}


def test_analyze_coin_falls_back_to_the_configured_backend_label(
    tmp_path: Path, monkeypatch
) -> None:
    """A predictor that does not tag its result still produces a counter."""
    path = tmp_path / "backend_stats.json"
    monkeypatch.setenv("TRADING_INFERENCE_STATS_FILE", str(path))
    monkeypatch.setenv("TRADING_INFERENCE_STATS_MIN_WRITE_SECONDS", "0")
    predictor = _Predictor({
        "decision": "halten", "confidence": 0.7, "proba": {"halten": 0.7},
    })

    analyze_coin(
        _AnalysisConfig(tabular_model_backend="hailo"), _fetcher(_ohlcv()), None,
        predictor, "BTC", 100.0, rule_recommender=_rules("HOLD", 50),
    )

    assert read_backend_stats(path).counts == {HAILO_BACKEND: 1}


def test_analyze_coin_records_nothing_when_the_atr_gate_rejects(
    tmp_path: Path, monkeypatch
) -> None:
    """No prediction, no counter - the pyramid must stay ordered."""
    path = tmp_path / "backend_stats.json"
    monkeypatch.setenv("TRADING_INFERENCE_STATS_FILE", str(path))
    monkeypatch.setenv("TRADING_INFERENCE_STATS_MIN_WRITE_SECONDS", "0")
    monkeypatch.setattr("src.coin_analysis.calculate_atr", lambda *_a, **_k: 0.05)

    result = analyze_coin(
        _AnalysisConfig(min_entry_atr_pct=0.01), _fetcher(_ohlcv()), None,
        _Predictor({"decision": "kaufen", "confidence": 0.9, "proba": {},
                    "backend": CPU_BACKEND}),
        "BTC", 100.0, rule_recommender=_rules("HOLD", 50),
    )

    assert result is not None
    assert not path.exists()
