"""Tests for :mod:`src.bot_bootstrap` (startup validation, logging, model loading)."""

from __future__ import annotations

import logging
import sys
import types
from dataclasses import dataclass, field

import pytest

from src.bot_bootstrap import (
    load_ml_predictor,
    load_tabular_predictor,
    log_startup_details,
    validate_startup_config,
)


@dataclass
class _Config:
    # startup validation
    simulation_initial_balance_from_kraken: bool = False
    dry_run: bool = True
    exchange_name: str = "kraken"
    base_currency: str = "EUR"
    simulate_data: bool = True
    api_key: str = ""
    api_secret: str = ""
    # startup logging
    dry_run_costs_enabled: bool = False
    dry_run_fee_rate: float = 0.001
    dry_run_slippage_rate: float = 0.0005
    # model loading
    use_ml_model: bool = False
    model_path: str = "model/dir"
    use_tabular_model: bool = False
    tabular_model_backend: str = "catboost"
    hailo_service_url: str = "http://127.0.0.1:8001"
    hailo_api_token: str = "token"
    hailo_timeout_seconds: float = 5.0
    tabular_model_path: str = "model/catboost_trading_model"
    tabular_research_signal_path: str = "data/research_signal_latest.json"
    extra: dict = field(default_factory=dict)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(f"{record.levelname}|{record.getMessage()}")


@pytest.fixture()
def logger() -> logging.Logger:
    log = logging.getLogger("test.bot_bootstrap")
    log.handlers.clear()
    log.propagate = False
    log.setLevel(logging.DEBUG)
    handler = _Records()
    log.addHandler(handler)
    return log


def _records(logger: logging.Logger) -> list[str]:
    handler = logger.handlers[0]
    assert isinstance(handler, _Records)
    return handler.records


# ---------------------------------------------------------------------------
# validate_startup_config
# ---------------------------------------------------------------------------

def test_validate_accepts_plain_simulation_config() -> None:
    validate_startup_config(_Config())


def test_validate_rejects_kraken_bootstrap_without_dry_run() -> None:
    config = _Config(simulation_initial_balance_from_kraken=True, dry_run=False)
    with pytest.raises(ValueError, match="requires DRY_RUN, Kraken and EUR"):
        validate_startup_config(config)


def test_validate_rejects_kraken_bootstrap_with_wrong_exchange() -> None:
    config = _Config(simulation_initial_balance_from_kraken=True, exchange_name="coinbase")
    with pytest.raises(ValueError, match="requires DRY_RUN, Kraken and EUR"):
        validate_startup_config(config)


def test_validate_rejects_kraken_bootstrap_with_wrong_base_currency() -> None:
    config = _Config(simulation_initial_balance_from_kraken=True, base_currency="USD")
    with pytest.raises(ValueError, match="requires DRY_RUN, Kraken and EUR"):
        validate_startup_config(config)


def test_validate_rejects_public_paper_with_api_credentials() -> None:
    config = _Config(
        simulation_initial_balance_from_kraken=True,
        simulate_data=False,
        api_key="key",
    )
    with pytest.raises(ValueError, match="no Kraken API credentials"):
        validate_startup_config(config)


def test_validate_rejects_public_paper_with_api_secret() -> None:
    config = _Config(
        simulation_initial_balance_from_kraken=True,
        simulate_data=False,
        api_secret="secret",
    )
    with pytest.raises(ValueError, match="no Kraken API credentials"):
        validate_startup_config(config)


def test_validate_allows_public_paper_without_credentials() -> None:
    validate_startup_config(
        _Config(simulation_initial_balance_from_kraken=True, simulate_data=False)
    )


# ---------------------------------------------------------------------------
# log_startup_details
# ---------------------------------------------------------------------------

def test_log_startup_details_emits_run_banner(logger: logging.Logger) -> None:
    log_startup_details(logger, _Config(), "run-1", "hash-1")
    assert _records(logger) == [
        (
            "INFO|Trading run started: run_id=run-1 config_hash=hash-1 "
            "dry_run=True simulated_data=True"
        ),
        (
            "INFO|DRY-RUN execution cost mode=gross_legacy fee_rate=0.000000 "
            "slippage_rate=0.000000"
        ),
    ]


def test_log_startup_details_gross_legacy_costs(logger: logging.Logger) -> None:
    log_startup_details(logger, _Config(dry_run_costs_enabled=False), "r", "h")
    assert _records(logger)[1] == (
        "INFO|DRY-RUN execution cost mode=gross_legacy fee_rate=0.000000 "
        "slippage_rate=0.000000"
    )


def test_log_startup_details_net_costs(logger: logging.Logger) -> None:
    config = _Config(dry_run_costs_enabled=True, dry_run_fee_rate=0.002,
                     dry_run_slippage_rate=0.0007)
    log_startup_details(logger, config, "r", "h")
    assert _records(logger)[1] == (
        "INFO|DRY-RUN execution cost mode=net_costs fee_rate=0.002000 "
        "slippage_rate=0.000700"
    )


def test_log_startup_details_skips_costs_outside_dry_run(logger: logging.Logger) -> None:
    log_startup_details(logger, _Config(dry_run=False, dry_run_costs_enabled=True), "r", "h")
    assert len(_records(logger)) == 1


# ---------------------------------------------------------------------------
# load_ml_predictor
# ---------------------------------------------------------------------------

def test_load_ml_predictor_disabled(logger: logging.Logger) -> None:
    assert load_ml_predictor(logger, _Config(use_ml_model=False)) is None
    assert _records(logger) == []


def test_load_ml_predictor_success(logger: logging.Logger, monkeypatch) -> None:
    calls: list[str] = []

    class _Predictor:
        def __init__(self, model_path: str) -> None:
            calls.append(model_path)

    module = types.ModuleType("src.predict")
    module.TradingModelPredictor = _Predictor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.predict", module)

    predictor = load_ml_predictor(logger, _Config(use_ml_model=True, model_path="models/m"))
    assert isinstance(predictor, _Predictor)
    assert calls == ["models/m"]
    assert _records(logger) == ["INFO|\U0001f916 ML model loaded from 'models/m'."]


@pytest.mark.parametrize(
    "error",
    [ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError],
)
def test_load_ml_predictor_swallows_expected_errors(
    logger: logging.Logger, monkeypatch, error: type[Exception]
) -> None:
    class _Predictor:
        def __init__(self, model_path: str) -> None:
            raise error("boom")

    module = types.ModuleType("src.predict")
    module.TradingModelPredictor = _Predictor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.predict", module)

    assert load_ml_predictor(logger, _Config(use_ml_model=True)) is None
    assert _records(logger) == [
        (
            f"WARNING|ML model could not be loaded: {error('boom')}. "
            "Using rule-based analysis only."
        )
    ]


def test_load_ml_predictor_propagates_unexpected_errors(
    logger: logging.Logger, monkeypatch
) -> None:
    class _Predictor:
        def __init__(self, model_path: str) -> None:
            raise ZeroDivisionError("nope")

    module = types.ModuleType("src.predict")
    module.TradingModelPredictor = _Predictor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.predict", module)

    with pytest.raises(ZeroDivisionError):
        load_ml_predictor(logger, _Config(use_ml_model=True))


# ---------------------------------------------------------------------------
# load_tabular_predictor
# ---------------------------------------------------------------------------

def test_load_tabular_predictor_disabled(logger: logging.Logger) -> None:
    assert load_tabular_predictor(logger, _Config(use_tabular_model=False)) is None
    assert _records(logger) == []


def test_load_tabular_predictor_hailo_remote(logger: logging.Logger, monkeypatch) -> None:
    seen: dict = {}

    class _Remote:
        def __init__(self, *, service_url: str, api_token: str, timeout_seconds: float) -> None:
            seen["service_url"] = service_url
            seen["api_token"] = api_token
            seen["timeout_seconds"] = timeout_seconds

    module = types.ModuleType("src.predict_hailo_remote")
    module.HailoRemotePredictor = _Remote  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.predict_hailo_remote", module)

    config = _Config(
        use_tabular_model=True,
        tabular_model_backend="hailo",
        hailo_service_url="http://hailo:8000",
        hailo_api_token="tok",
        hailo_timeout_seconds=3.5,
    )
    predictor = load_tabular_predictor(logger, config)
    assert isinstance(predictor, _Remote)
    assert seen == {
        "service_url": "http://hailo:8000",
        "api_token": "tok",
        "timeout_seconds": 3.5,
    }
    assert _records(logger) == [
        "INFO|Remote Hailo inference configured at http://hailo:8000"
    ]


def test_load_tabular_predictor_hailo_failure_propagates(
    logger: logging.Logger, monkeypatch
) -> None:
    class _Remote:
        def __init__(self, **kwargs: object) -> None:
            raise RuntimeError("hailo down")

    module = types.ModuleType("src.predict_hailo_remote")
    module.HailoRemotePredictor = _Remote  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.predict_hailo_remote", module)

    config = _Config(use_tabular_model=True, tabular_model_backend="hailo")
    with pytest.raises(RuntimeError, match="hailo down"):
        load_tabular_predictor(logger, config)


def test_load_tabular_predictor_catboost_success(logger: logging.Logger, monkeypatch) -> None:
    seen: dict = {}

    class _CatBoost:
        def __init__(self, model_path: str, *, research_signal_path: str) -> None:
            seen["model_path"] = model_path
            seen["research_signal_path"] = research_signal_path

    module = types.ModuleType("src.predict_catboost")
    module.CatBoostTradingPredictor = _CatBoost  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.predict_catboost", module)

    config = _Config(
        use_tabular_model=True,
        tabular_model_backend="catboost",
        tabular_model_path="models/cb",
        tabular_research_signal_path="data/signal.json",
    )
    predictor = load_tabular_predictor(logger, config)
    assert isinstance(predictor, _CatBoost)
    assert seen == {"model_path": "models/cb", "research_signal_path": "data/signal.json"}
    assert _records(logger) == [
        "INFO|\U0001f4ca CatBoost model loaded from 'models/cb'."
    ]


@pytest.mark.parametrize(
    "error",
    [ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError],
)
def test_load_tabular_predictor_catboost_swallows_expected_errors(
    logger: logging.Logger, monkeypatch, error: type[Exception]
) -> None:
    class _CatBoost:
        def __init__(self, model_path: str, *, research_signal_path: str) -> None:
            raise error("broken model")

    module = types.ModuleType("src.predict_catboost")
    module.CatBoostTradingPredictor = _CatBoost  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.predict_catboost", module)

    config = _Config(use_tabular_model=True, tabular_model_backend="catboost")
    assert load_tabular_predictor(logger, config) is None
    assert _records(logger) == [
        f"WARNING|CatBoost model could not be loaded: {error('broken model')}."
    ]


def test_load_tabular_predictor_catboost_propagates_unexpected_errors(
    logger: logging.Logger, monkeypatch
) -> None:
    class _CatBoost:
        def __init__(self, model_path: str, *, research_signal_path: str) -> None:
            raise ZeroDivisionError("nope")

    module = types.ModuleType("src.predict_catboost")
    module.CatBoostTradingPredictor = _CatBoost  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.predict_catboost", module)

    config = _Config(use_tabular_model=True, tabular_model_backend="anything")
    with pytest.raises(ZeroDivisionError):
        load_tabular_predictor(logger, config)
