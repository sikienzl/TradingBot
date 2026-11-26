"""Backend-selection matrix for the optional tabular model.

The audit requires a tested matrix for the inference cascade: no Hailo
package/device, a failing Hailo endpoint, CPU fallback, and rules-only fallback.
These tests drive :mod:`src.inference_backend` with injected backends, so they
run in the slim CI environment where no Hailo package, device or reachable
inference service exists.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.inference_backend import (
    HAILO_BACKEND,
    build_hailo_cascade,
    build_tabular_predictor,
    default_cpu_factory,
    default_hailo_factory,
)
from src.inference_fallback import TabularInferenceFallback


@dataclass
class _Config:
    use_tabular_model: bool = True
    tabular_model_backend: str = HAILO_BACKEND
    hailo_service_url: str = "http://127.0.0.1:8001"
    hailo_api_token: str = "token"
    hailo_timeout_seconds: float = 5.0
    tabular_model_path: str = "model/catboost_trading_model"
    tabular_research_signal_path: str = "data/research_signal_latest.json"


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(f"{record.levelname}|{record.getMessage()}")


@pytest.fixture()
def logger(monkeypatch: pytest.MonkeyPatch) -> logging.Logger:
    # ``scripts/collect_hailo_training`` calls ``logging.disable(CRITICAL)`` at
    # import time, which silences every later capture in the same pytest session.
    monkeypatch.setattr(logging.Logger.manager, "disable", logging.NOTSET)
    log = logging.getLogger("test.inference_backend")
    log.handlers.clear()
    log.propagate = False
    log.setLevel(logging.DEBUG)
    log.addHandler(_Records())
    return log


def _records(logger: logging.Logger) -> list[str]:
    handler = logger.handlers[0]
    assert isinstance(handler, _Records)
    return handler.records


class _Predictor:
    """Backend stub returning a valid, tagged prediction."""

    def __init__(self, tag: str, error: Exception | None = None) -> None:
        self.tag = tag
        self.error = error
        self.calls: list[tuple[object, dict[str, object]]] = []

    def predict(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return {
            "decision": "kaufen",
            "confidence": 0.9,
            "proba": {"kaufen": 0.9, "halten": 0.05, "verkaufen": 0.05},
        }


def _cpu_factory(predictor: _Predictor):
    calls: list[int] = []

    def factory() -> _Predictor:
        calls.append(1)
        return predictor

    factory.calls = calls  # type: ignore[attr-defined]
    return factory


# ---------------------------------------------------------------------------
# Selection: which backend does a configuration produce?
# ---------------------------------------------------------------------------

def test_tabular_model_disabled_selects_nothing(logger: logging.Logger) -> None:
    assert build_tabular_predictor(
        logger, _Config(use_tabular_model=False)) is None
    assert _records(logger) == []


def test_hailo_backend_selects_the_fallback_cascade(logger: logging.Logger) -> None:
    primary = _Predictor("hailo")
    predictor = build_tabular_predictor(
        logger, _Config(), hailo_factory=lambda _config: primary,
        cpu_factory=lambda _config: _cpu_factory(_Predictor("cpu")))

    assert isinstance(predictor, TabularInferenceFallback)
    assert predictor.primary is primary
    assert _records(logger) == [
        "INFO|Tabular inference selected backend=hailo at http://127.0.0.1:8001"
    ]


def test_catboost_backend_returns_the_cpu_predictor(logger: logging.Logger) -> None:
    cpu = _Predictor("cpu")

    def cpu_factory(_config: object):
        return lambda: cpu

    predictor = build_tabular_predictor(
        logger, _Config(tabular_model_backend="catboost"), cpu_factory=cpu_factory)

    assert predictor is cpu
    assert _records(logger) == [
        "INFO|\U0001f4ca CatBoost model loaded from 'model/catboost_trading_model'."
    ]


@pytest.mark.parametrize(
    "error",
    [ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError],
)
def test_catboost_backend_unavailable_is_rules_only(
    logger: logging.Logger, error: type[Exception]
) -> None:
    def cpu_factory(_config: object):
        return lambda: (_ for _ in ()).throw(error("no catboost"))

    assert build_tabular_predictor(
        logger, _Config(tabular_model_backend="catboost"),
        cpu_factory=cpu_factory) is None
    assert _records(logger) == [
        f"WARNING|CatBoost model could not be loaded: {error('no catboost')}."
    ]


def test_catboost_backend_unexpected_error_propagates(logger: logging.Logger) -> None:
    def cpu_factory(_config: object):
        return lambda: (_ for _ in ()).throw(ZeroDivisionError("bug"))

    with pytest.raises(ZeroDivisionError):
        build_tabular_predictor(
            logger, _Config(tabular_model_backend="catboost"),
            cpu_factory=cpu_factory)


# ---------------------------------------------------------------------------
# Matrix: hailo available -> hailo, failing endpoint -> CPU, CPU gone -> rules
# ---------------------------------------------------------------------------

def test_matrix_hailo_available_uses_hailo(logger: logging.Logger) -> None:
    primary = _Predictor("hailo")
    cpu = _cpu_factory(_Predictor("cpu"))
    cascade = build_hailo_cascade(logger, _Config(), lambda _c: primary, lambda _c: cpu)

    result = cascade.predict("row", confidence_threshold=0.6)

    assert result["backend"] == "hailo"
    assert result["decision"] == "kaufen"
    assert cpu.calls == []
    assert primary.calls == [(("row",), {"confidence_threshold": 0.6})]


@pytest.mark.parametrize(
    "error",
    [
        ImportError("hailo_platform is not installed"),
        OSError("connection refused"),
        RuntimeError("Hailo service request failed"),
        ValueError("Hailo service returned an invalid response"),
    ],
)
def test_matrix_hailo_endpoint_failure_uses_cpu(
    logger: logging.Logger, error: Exception
) -> None:
    primary = _Predictor("hailo", error=error)
    cpu = _cpu_factory(_Predictor("cpu"))
    cascade = build_hailo_cascade(logger, _Config(), lambda _c: primary, lambda _c: cpu)

    result = cascade.predict("row")

    assert result["backend"] == "cpu"
    assert cpu.calls == [1]
    assert any("falling back to CPU" in record for record in _records(logger))


def test_matrix_hailo_invalid_prediction_uses_cpu(logger: logging.Logger) -> None:
    class _BadPrediction:
        def predict(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            return {"decision": "buy", "confidence": 0.9}

    cascade = build_hailo_cascade(
        logger, _Config(), lambda _c: _BadPrediction(),
        lambda _c: _cpu_factory(_Predictor("cpu")))

    assert cascade.predict("row")["backend"] == "cpu"


def test_matrix_no_hailo_device_uses_cpu(logger: logging.Logger) -> None:
    """A Hailo backend that cannot even be constructed still serves CPU."""

    def hailo_factory(_config: object):
        raise ImportError("no module named hailo_platform")

    cpu = _cpu_factory(_Predictor("cpu"))
    cascade = build_hailo_cascade(logger, _Config(), hailo_factory, lambda _c: cpu)
    assert cascade.primary is None

    result = cascade.predict("row")

    assert result["backend"] == "cpu"
    assert cpu.calls == [1]
    assert _records(logger)[0].startswith(
        "WARNING|Hailo backend unavailable; falling back to CPU: ")


def test_matrix_cpu_unavailable_falls_back_to_rules_only(logger: logging.Logger) -> None:
    primary = _Predictor("hailo", error=RuntimeError("endpoint down"))
    cpu = _cpu_factory(_Predictor("cpu", error=FileNotFoundError("model missing")))
    cascade = build_hailo_cascade(logger, _Config(), lambda _c: primary, lambda _c: cpu)

    result = cascade.predict("row")

    assert result == {
        "decision": "halten",
        "confidence": 0.0,
        "proba": {},
        "backend": "rules_only",
    }
    assert any("continuing with rules only" in r for r in _records(logger))


def test_matrix_rules_only_result_preserves_the_rules_signal(
    logger: logging.Logger
) -> None:
    """The neutral result must never look like a model BUY on either failure path."""
    for cascade in (
        build_hailo_cascade(
            logger, _Config(),
            lambda _c: _Predictor("hailo", error=ImportError("no device")),
            lambda _c: _cpu_factory(
                _Predictor("cpu", error=RuntimeError("no model")))),
        build_hailo_cascade(
            logger, _Config(), lambda _c: None,
            lambda _c: _cpu_factory(
                _Predictor("cpu", error=RuntimeError("no model")))),
    ):
        result = cascade.predict("row")
        assert result["backend"] == "rules_only"
        assert result["decision"] != "kaufen"
        assert result["confidence"] == 0.0


def test_hailo_construction_unexpected_error_propagates(logger: logging.Logger) -> None:
    def hailo_factory(_config: object):
        raise ZeroDivisionError("bug")

    with pytest.raises(ZeroDivisionError):
        build_hailo_cascade(
            logger, _Config(), hailo_factory,
            lambda _c: _cpu_factory(_Predictor("cpu")))


# ---------------------------------------------------------------------------
# The real factories stay lazy: no Hailo or CatBoost import at module import
# ---------------------------------------------------------------------------

def test_importing_the_selection_module_pulls_in_no_backend() -> None:
    """A fresh interpreter must not import a backend from the selection module.

    This is the single-Pi guarantee: importing the cascade never imports the
    Hailo client, so a host without the Hailo packages still starts the bot.
    """
    code = (
        "import sys\n"
        "import src.inference_backend\n"
        "blocked = [name for name in sys.modules\n"
        "           if name in ('src.predict_hailo_remote', 'src.predict_catboost')]\n"
        "print(blocked)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == "[]"


def test_default_hailo_factory_passes_config_through(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class _Remote:
        def __init__(self, *, service_url: str, api_token: str,
                     timeout_seconds: float) -> None:
            seen.update(service_url=service_url, api_token=api_token,
                        timeout_seconds=timeout_seconds)

    module = types.ModuleType("src.predict_hailo_remote")
    module.HailoRemotePredictor = _Remote  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.predict_hailo_remote", module)

    config = _Config(hailo_service_url="http://hailo:8000",
                     hailo_api_token="tok", hailo_timeout_seconds=2.5)
    assert isinstance(default_hailo_factory(config), _Remote)
    assert seen == {"service_url": "http://hailo:8000", "api_token": "tok",
                    "timeout_seconds": 2.5}


def test_default_cpu_factory_is_lazy(monkeypatch) -> None:
    constructed: list[str] = []

    class _CatBoost:
        def __init__(self, model_path: str, *, research_signal_path: str) -> None:
            constructed.append(model_path)

    module = types.ModuleType("src.predict_catboost")
    module.CatBoostTradingPredictor = _CatBoost  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.predict_catboost", module)

    load = default_cpu_factory(_Config(tabular_model_path="models/cb"))
    assert constructed == []
    assert isinstance(load(), _CatBoost)
    assert constructed == ["models/cb"]
