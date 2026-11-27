"""Tests for :mod:`src.bot_bootstrap` (startup validation, logging, model loading)."""

from __future__ import annotations

import json
import logging
import sys
import types
from dataclasses import dataclass, field

import pytest

from src.bot_bootstrap import (
    evaluate_live_scorecard_gate,
    load_ml_predictor,
    load_tabular_predictor,
    log_active_release,
    log_startup_details,
    validate_live_scorecard_gate,
    validate_startup_config,
)
from src.inference_fallback import TabularInferenceFallback
from src.scorecard_gate import scorecard_gate_settings


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
# live-mode scorecard gate
# ---------------------------------------------------------------------------

def _gate_settings(tmp_path, verdict: str = "GO", *, seconds_ago: float = 60.0):
    from datetime import UTC, datetime, timedelta

    marker = tmp_path / "latest_status.env"
    stamp = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    marker.write_text(
        f"TIMESTAMP_UTC={stamp}\nRUN_MODE=standard\nVERDICT={verdict}\n"
        "FINAL_EXIT_CODE=0\n",
        encoding="utf-8",
    )
    return scorecard_gate_settings({
        "SCORECARD_STATUS_JSON": str(tmp_path / "absent.json"),
        "SCORECARD_STATUS_ENV": str(marker),
    })


def test_live_gate_is_skipped_in_dry_run(tmp_path) -> None:
    settings = _gate_settings(tmp_path)
    assert evaluate_live_scorecard_gate(_Config(), settings) is None
    assert validate_live_scorecard_gate(_Config(), settings) is None


def test_live_gate_accepts_fresh_go(tmp_path) -> None:
    status = validate_live_scorecard_gate(
        _Config(dry_run=False), _gate_settings(tmp_path))
    assert status is not None
    assert status.allowed is True


def test_startup_config_accepts_live_with_fresh_go(tmp_path) -> None:
    validate_startup_config(_Config(dry_run=False), _gate_settings(tmp_path))


@pytest.mark.parametrize("verdict", ["HOLD", "NO-GO", "ERROR"])
def test_live_gate_refuses_non_go_verdict(tmp_path, verdict) -> None:
    settings = _gate_settings(tmp_path, verdict)
    with pytest.raises(ValueError, match=f"scorecard verdict is {verdict}"):
        validate_live_scorecard_gate(_Config(dry_run=False), settings)


def test_live_gate_refuses_missing_marker(tmp_path) -> None:
    settings = scorecard_gate_settings({
        "SCORECARD_STATUS_JSON": str(tmp_path / "absent.json"),
        "SCORECARD_STATUS_ENV": str(tmp_path / "absent.env"),
    })
    with pytest.raises(ValueError, match="no scorecard status marker was found"):
        validate_live_scorecard_gate(_Config(dry_run=False), settings)


def test_live_gate_refuses_stale_go(tmp_path) -> None:
    settings = _gate_settings(tmp_path, seconds_ago=10 * 24 * 3600)
    with pytest.raises(ValueError, match="stale"):
        validate_live_scorecard_gate(_Config(dry_run=False), settings)


def test_live_gate_operator_override(tmp_path) -> None:
    settings = scorecard_gate_settings({
        "SCORECARD_GATE_ENABLED": "false",
        "SCORECARD_STATUS_JSON": str(tmp_path / "absent.json"),
        "SCORECARD_STATUS_ENV": str(tmp_path / "absent.env"),
    })
    assert evaluate_live_scorecard_gate(_Config(dry_run=False), settings) is None
    assert validate_live_scorecard_gate(_Config(dry_run=False), settings) is None


def test_live_gate_reads_process_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    marker = tmp_path / "results/scorecards/latest_status.env"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        "TIMESTAMP_UTC=2020-01-01T00:00:00Z\nRUN_MODE=standard\nVERDICT=NO-GO\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="scorecard verdict is NO-GO"):
        validate_live_scorecard_gate(_Config(dry_run=False))
    # The startup validator is the production entry point and must agree.
    with pytest.raises(ValueError, match="scorecard verdict is NO-GO"):
        validate_startup_config(_Config(dry_run=False))


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
    assert isinstance(predictor, TabularInferenceFallback)
    assert isinstance(predictor.primary, _Remote)
    assert seen == {
        "service_url": "http://hailo:8000",
        "api_token": "tok",
        "timeout_seconds": 3.5,
    }
    assert _records(logger) == [
        "INFO|Tabular inference selected backend=hailo at http://hailo:8000"
    ]


def test_load_tabular_predictor_hailo_initialization_falls_back_to_cpu(
    logger: logging.Logger, monkeypatch
) -> None:
    class _Remote:
        def __init__(self, **kwargs: object) -> None:
            raise RuntimeError("hailo down")

    module = types.ModuleType("src.predict_hailo_remote")
    module.HailoRemotePredictor = _Remote  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.predict_hailo_remote", module)

    config = _Config(use_tabular_model=True, tabular_model_backend="hailo")
    predictor = load_tabular_predictor(logger, config)
    assert isinstance(predictor, TabularInferenceFallback)
    assert predictor.primary is None
    assert "falling back to CPU: hailo down" in _records(logger)[0]


def test_tabular_fallback_uses_cpu_after_hailo_failure(logger: logging.Logger) -> None:
    class _Predictor:
        def predict(self, *_args, **_kwargs):
            raise RuntimeError("primary unavailable")

    class _Cpu:
        def predict(self, *_args, **_kwargs):
            return {"decision": "kaufen", "confidence": 0.8}

    fallback = TabularInferenceFallback(_Predictor(), _Cpu, logger)

    result = fallback.predict(object())

    assert result["decision"] == "kaufen"
    assert result["backend"] == "cpu"
    assert "falling back to CPU" in _records(logger)[0]


def test_tabular_fallback_uses_rules_when_cpu_is_unavailable(logger: logging.Logger) -> None:
    class _Predictor:
        def predict(self, *_args, **_kwargs):
            return {"decision": "buy", "confidence": 1.0}

    fallback = TabularInferenceFallback(
        _Predictor(), lambda: (_ for _ in ()).throw(FileNotFoundError("no CPU model")), logger)

    result = fallback.predict(object())

    assert result == {
        "decision": "halten", "confidence": 0.0, "proba": {}, "backend": "rules_only",
    }
    assert any("continuing with rules only" in record for record in _records(logger))


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


# ---------------------------------------------------------------------------
# active model release visibility
# ---------------------------------------------------------------------------

def _release_root(tmp_path, *, tamper: str | None = None):
    """Promote a paired release into a throwaway root and return that root.

    Built with the real promotion code so the startup log is tested against a
    manifest the runtime would actually accept, not a hand-written one.
    """
    from src.model_release import promote_release

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "cpu.cbm").write_bytes(b"cpu model")
    (artifacts / "model.hef").write_bytes(b"hailo model")
    validation = {
        "rows": 250,
        "accuracy": 0.8,
        "macro_f1": 0.7,
        "threshold_macro_f1": 0.68,
        "threshold_coverage": 0.62,
    }
    label_map = {"verkaufen": 0, "halten": 1, "kaufen": 2}
    (artifacts / "cpu.json").write_text(json.dumps({
        "feature_contract": "hailo",
        "features": ["rsi", "macd"],
        "label_map": label_map,
        "training_data_sha256": "a" * 64,
        "label_horizon": 6,
        "train_rows": 1000,
        "validation_rows": 250,
        "validation": validation,
    }), encoding="utf-8")
    (artifacts / "hailo.json").write_text(json.dumps({
        "feature_contract": "hailo",
        "features": ["rsi", "macd"],
        "label_map": label_map,
        "data_provenance": {
            "training_data_sha256": "a" * 64,
            "label_map": label_map,
            "label_horizon": 6,
            "train_rows": 1000,
            "validation_rows": 250,
        },
        "validation": validation,
    }), encoding="utf-8")
    root = tmp_path / "models"
    release = promote_release(
        root, "release-1",
        artifacts / "cpu.cbm", artifacts / "cpu.json",
        artifacts / "model.hef", artifacts / "hailo.json",
    )
    if tamper == "checksum":
        (release / "cpu" / "catboost_model.cbm").write_bytes(b"swapped")
    elif tamper == "manifest":
        (release / "manifest.json").write_text("{", encoding="utf-8")
    return root


def test_log_active_release_is_silent_without_a_release_root(
    logger: logging.Logger, monkeypatch
) -> None:
    monkeypatch.delenv("TRADING_MODEL_RELEASE_ROOT", raising=False)
    assert log_active_release(logger, _Config()) is None
    assert _records(logger) == []


def test_log_active_release_warns_when_no_release_is_active(
    logger: logging.Logger, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_MODEL_RELEASE_ROOT", str(tmp_path / "models"))
    assert log_active_release(logger, _Config()) is None
    assert _records(logger) == [
        (
            "WARNING|No valid active model release under "
            f"{tmp_path / 'models'}: the tabular model is not backed by a "
            "validated paired release. Run scripts/manage_model_release.py "
            "validate to inspect it."
        )
    ]


def test_log_active_release_reports_a_verified_release(
    logger: logging.Logger, tmp_path, monkeypatch
) -> None:
    root = _release_root(tmp_path)
    monkeypatch.setenv("TRADING_MODEL_RELEASE_ROOT", str(root))
    config = _Config(tabular_model_path=str(root / "active" / "cpu"))

    active = log_active_release(logger, config)

    assert active is not None and active.release_id == "release-1"
    assert _records(logger) == [
        (
            "INFO|Active model release: id=release-1 contract=hailo features=2 "
            "training_data_sha256=" + "a" * 64 + " artifacts verified"
        )
    ]


def test_log_active_release_flags_a_tampered_release(
    logger: logging.Logger, tmp_path, monkeypatch
) -> None:
    root = _release_root(tmp_path, tamper="checksum")
    monkeypatch.setenv("TRADING_MODEL_RELEASE_ROOT", str(root))

    active = log_active_release(logger, _Config(tabular_model_path=""))

    assert active is not None
    assert active.artifacts_verified is False
    assert _records(logger) == [
        (
            "ERROR|Active model release release-1 failed artifact verification "
            "(cpu_model checksum mismatch). Roll back to a known-good release "
            "before trading on this model."
        )
    ]


def test_log_active_release_warns_when_model_path_escapes_the_release(
    logger: logging.Logger, tmp_path, monkeypatch
) -> None:
    """The release root and TABULAR_MODEL_PATH can disagree after an edit."""
    root = _release_root(tmp_path)
    monkeypatch.setenv("TRADING_MODEL_RELEASE_ROOT", str(root))

    log_active_release(logger, _Config(tabular_model_path="model/catboost_trading_model"))

    assert "is outside the active model release" in _records(logger)[1]


def test_load_tabular_predictor_logs_the_active_release(
    logger: logging.Logger, tmp_path, monkeypatch
) -> None:
    root = _release_root(tmp_path)
    monkeypatch.setenv("TRADING_MODEL_RELEASE_ROOT", str(root))

    class _CatBoost:
        def __init__(self, model_path: str, *, research_signal_path: str) -> None:
            pass

    module = types.ModuleType("src.predict_catboost")
    module.CatBoostTradingPredictor = _CatBoost  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.predict_catboost", module)

    config = _Config(
        use_tabular_model=True,
        tabular_model_backend="catboost",
        tabular_model_path=str(root / "active" / "cpu"),
    )
    assert load_tabular_predictor(logger, config) is not None
    assert _records(logger)[0].startswith("INFO|Active model release: id=release-1")


def test_load_tabular_predictor_stays_silent_without_releases(
    logger: logging.Logger, monkeypatch
) -> None:
    monkeypatch.delenv("TRADING_MODEL_RELEASE_ROOT", raising=False)

    class _CatBoost:
        def __init__(self, model_path: str, *, research_signal_path: str) -> None:
            pass

    module = types.ModuleType("src.predict_catboost")
    module.CatBoostTradingPredictor = _CatBoost  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "src.predict_catboost", module)

    assert load_tabular_predictor(logger, _Config(use_tabular_model=True)) is not None
    assert _records(logger) == [
        "INFO|\U0001f4ca CatBoost model loaded from 'model/catboost_trading_model'."
    ]
