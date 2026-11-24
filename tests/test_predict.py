"""Unit tests for :mod:`src.predict`.

``src.predict`` imports ``torch`` and ``transformers`` at module level, but the
CI test job deliberately installs a slim dependency set (see
``.github/workflows/ci.yml``). The stubs below are only inserted when the real
packages are missing, so the tests exercise the same decision logic in both the
slim and the full environment without pulling multi-gigabyte ML wheels into CI.

No model is ever loaded: the predictor is built with ``__new__`` and given a
fake pipeline, which keeps the tests fast and hermetic.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pandas as pd
import pytest


def _stub(name: str, **attrs: object) -> str | None:
    """Register a placeholder module for ``name`` unless the real one imports.

    Returns the module name when a stub was inserted so the caller can remove it
    from ``sys.modules`` again: leaving a fake ``torch`` behind would make
    ``pytest.importorskip("torch")`` in other test modules believe the real
    package is installed.
    """
    try:  # pragma: no cover - environment dependent
        __import__(name)
        return None
    except ImportError:
        pass
    module = types.ModuleType(name)
    module.__dict__["_copilot_test_stub"] = True
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return name


_stubbed = [
    _stub("torch", bfloat16="bfloat16"),
    _stub(
        "transformers",
        AutoModelForCausalLM=SimpleNamespace(from_pretrained=lambda *a, **k: SimpleNamespace(name="model")),
        AutoTokenizer=SimpleNamespace(
            from_pretrained=lambda *a, **k: SimpleNamespace(eos_token="</s>", eos_token_id=0)
        ),
        BitsAndBytesConfig=lambda **kwargs: SimpleNamespace(**kwargs),
        pipeline=lambda *a, **k: SimpleNamespace(name="pipeline"),
    ),
]

from src.predict import TradingModelPredictor

_predict_module = sys.modules["src.predict"]
if any(entry for entry in _stubbed):
    _predict_module.__dict__["_copilot_test_stub"] = True
# ``src.predict`` keeps direct references to the placeholder modules in its own
# namespace, so the ``sys.modules`` entries are no longer needed and are dropped
# to keep the rest of the suite honest about what is actually installed.
for _name in [entry for entry in _stubbed if entry]:
    sys.modules.pop(_name, None)


def make_row(rsi: float = 55.0, macd: float = 12.3) -> pd.DataFrame:
    return pd.DataFrame([{
        "coin": "BTC/EUR",
        "timestamp": 1_672_531_200_000,
        "open": 16500.0,
        "high": 16600.0,
        "low": 16450.0,
        "close": 16550.0,
        "volume": 1234.5,
        "rsi": rsi,
        "macd": macd,
    }])


def make_predictor(suffixes: list[str] | None = None,
                   error: Exception | None = None) -> TradingModelPredictor:
    """Build a predictor without touching the model-loading code path."""
    predictor = TradingModelPredictor.__new__(TradingModelPredictor)
    predictor.model_path = "./model/fine_tuned_trading_model"
    predictor.model = None
    predictor.tokenizer = SimpleNamespace(eos_token_id=0)

    if error is not None:
        def failing_pipe(_prompt: str, **_kwargs: object) -> list[dict[str, str]]:
            raise error

        predictor.pipe = failing_pipe
        return predictor

    chosen = suffixes if suffixes is not None else [" kaufen"]

    def fake_pipe(prompt: str, **_kwargs: object) -> list[dict[str, str]]:
        return [{"generated_text": prompt + suffix} for suffix in chosen]

    predictor.pipe = fake_pipe
    return predictor


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def test_load_model_wires_pipeline_without_trust_remote_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the deliberate decision to never enable remote code execution."""
    predict_module = sys.modules["src.predict"]
    if not getattr(predict_module, "_copilot_test_stub", False):
        pytest.skip("model wiring is covered against the real transformers package")

    model_calls: dict[str, object] = {}
    pipeline_calls: dict[str, object] = {}

    def record_model(_path: str, **kwargs: object) -> SimpleNamespace:
        model_calls.update(kwargs)
        return SimpleNamespace(name="model")

    def record_pipeline(task: str, **kwargs: object) -> SimpleNamespace:
        pipeline_calls.update({"task": task, **kwargs})
        return SimpleNamespace(name="pipeline")

    monkeypatch.setattr(
        sys.modules["src.predict"].AutoModelForCausalLM, "from_pretrained", record_model)
    monkeypatch.setattr("src.predict.pipeline", record_pipeline)

    predictor = TradingModelPredictor("model/dir")

    assert predictor.model_path == "model/dir"
    assert predictor.model is not None
    assert predictor.tokenizer.pad_token == predictor.tokenizer.eos_token
    assert predictor.tokenizer.padding_side == "left"

    # Quantisation is configured, remote code stays disabled (absent, not False).
    assert model_calls["device_map"] == "auto"
    assert "trust_remote_code" not in model_calls
    quant = model_calls["quantization_config"]
    assert getattr(quant, "load_in_4bit", None) is True
    assert getattr(quant, "bnb_4bit_quant_type", None) == "nf4"

    assert pipeline_calls["task"] == "text-generation"
    assert pipeline_calls["truncation"] is True
    assert pipeline_calls["pad_token_id"] == predictor.tokenizer.eos_token_id


# ---------------------------------------------------------------------------
# prepare_data
# ---------------------------------------------------------------------------
def test_prepare_data_scales_only_the_three_features() -> None:
    predictor = make_predictor()
    scaled = predictor.prepare_data(make_row(rsi=55.0, macd=12.3))
    assert scaled["rsi"].iloc[0] == pytest.approx(0.0)
    assert scaled["macd"].iloc[0] == pytest.approx(0.0)
    assert scaled["volume"].iloc[0] == pytest.approx(0.0)
    # Untouched columns keep their raw magnitude.
    assert scaled["close"].iloc[0] == pytest.approx(16550.0)


def test_prepare_data_spreads_multiple_rows_over_unit_interval() -> None:
    predictor = make_predictor()
    frame = pd.DataFrame(
        [{"rsi": 10.0, "macd": -5.0, "volume": 100.0},
         {"rsi": 60.0, "macd": 0.0, "volume": 300.0},
         {"rsi": 90.0, "macd": 25.0, "volume": 500.0}]
    )
    scaled = predictor.prepare_data(frame)
    assert scaled["rsi"].tolist() == pytest.approx([0.0, 0.625, 1.0])
    assert scaled["macd"].tolist() == pytest.approx([0.0, 5 / 30, 1.0])
    assert scaled["volume"].tolist() == pytest.approx([0.0, 0.5, 1.0])


def test_prepare_data_does_not_mutate_the_caller_frame() -> None:
    predictor = make_predictor()
    original = make_row(rsi=55.0)
    predictor.prepare_data(original)
    assert original["rsi"].iloc[0] == pytest.approx(55.0)


# ---------------------------------------------------------------------------
# create_prompt
# ---------------------------------------------------------------------------
def test_create_prompt_contains_market_data() -> None:
    predictor = make_predictor()
    prompt = predictor.create_prompt(make_row(rsi=55.2, macd=12.3))
    assert "BTC/EUR" in prompt
    assert "2023-01-01" in prompt
    assert "Open=16500.00" in prompt
    assert "RSI=55.20" in prompt
    assert "MACD=12.30" in prompt


def test_create_prompt_accepts_a_series() -> None:
    predictor = make_predictor()
    prompt = predictor.create_prompt(make_row().iloc[0])
    assert "BTC/EUR" in prompt


def test_create_prompt_asks_for_a_single_word_answer() -> None:
    predictor = make_predictor()
    prompt = predictor.create_prompt(make_row())
    assert "kaufen/verkaufen/halten" in prompt


# ---------------------------------------------------------------------------
# rule_based_decision
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rsi, expected",
    [
        (10.0, "kaufen"),
        (29.9, "kaufen"),
        (30.0, "halten"),
        (55.0, "halten"),
        (70.0, "halten"),
        (70.1, "verkaufen"),
        (95.0, "verkaufen"),
    ],
)
def test_rule_based_decision_thresholds(rsi: float, expected: str) -> None:
    predictor = make_predictor()
    assert predictor.rule_based_decision(make_row(rsi=rsi)) == expected


def test_rule_based_decision_custom_thresholds() -> None:
    predictor = make_predictor()
    assert predictor.rule_based_decision(
        make_row(rsi=40.0), rsi_buy=50, rsi_sell=90) == "kaufen"
    # 80 is above the default sell line but still below a widened one.
    assert predictor.rule_based_decision(
        make_row(rsi=80.0), rsi_buy=50, rsi_sell=90) == "halten"
    assert predictor.rule_based_decision(
        make_row(rsi=95.0), rsi_buy=50, rsi_sell=90) == "verkaufen"


def test_rule_based_decision_without_rsi_column() -> None:
    predictor = make_predictor()
    frame = pd.DataFrame([{"coin": "BTC/EUR", "close": 1.0}])
    assert predictor.rule_based_decision(frame) == "halten"


# ---------------------------------------------------------------------------
# predict - vote extraction
# ---------------------------------------------------------------------------
def test_predict_uses_first_word_of_generated_text() -> None:
    predictor = make_predictor([" Kaufen!", " kaufen.", "kaufen"])
    result = predictor.predict(make_row(rsi=20.0), n_votes=3)
    assert result["decision"] == "kaufen"
    assert result["confidence"] == pytest.approx(1.0)
    assert result["llm_votes"] == {"kaufen": 3}


def test_predict_falls_back_to_keyword_search() -> None:
    predictor = make_predictor([" ich denke, verkaufen ist richtig"])
    result = predictor.predict(make_row(rsi=85.0), n_votes=1)
    assert result["llm_votes"] == {"verkaufen": 1}
    assert result["decision"] == "verkaufen"


def test_predict_prefers_verkaufen_over_kaufen_in_same_text() -> None:
    predictor = make_predictor([" nicht kaufen, sondern verkaufen"])
    result = predictor.predict(make_row(rsi=85.0), n_votes=1)
    assert result["llm_votes"] == {"verkaufen": 1}


def test_predict_majority_vote_and_confidence() -> None:
    predictor = make_predictor(
        [" kaufen", " kaufen", " kaufen", " verkaufen", " verkaufen"])
    result = predictor.predict(make_row(rsi=20.0), n_votes=5)
    assert result["decision"] == "kaufen"
    assert result["confidence"] == pytest.approx(0.6)
    assert result["llm_votes"] == {"kaufen": 3, "verkaufen": 2}


def test_predict_ignores_unusable_votes() -> None:
    predictor = make_predictor([" kaufen", "...", " maybe"])
    result = predictor.predict(make_row(rsi=20.0), n_votes=3)
    assert result["llm_votes"] == {"kaufen": 1}
    assert result["confidence"] == pytest.approx(1 / 3)


def test_predict_without_any_usable_vote_holds() -> None:
    predictor = make_predictor([" ???", ""])
    result = predictor.predict(make_row(rsi=20.0), n_votes=2)
    assert result == {
        "decision": "halten",
        "confidence": 0.0,
        "llm_votes": {},
        "rule": "kaufen",
    }


# ---------------------------------------------------------------------------
# predict - ensemble logic
# ---------------------------------------------------------------------------
def test_predict_acts_when_llm_and_rule_agree() -> None:
    predictor = make_predictor([" kaufen"] * 4)
    result = predictor.predict(make_row(rsi=20.0), n_votes=4)
    assert result["rule"] == "kaufen"
    assert result["decision"] == "kaufen"
    assert result["confidence"] == pytest.approx(1.0)


def test_predict_holds_when_llm_and_rule_disagree() -> None:
    predictor = make_predictor([" verkaufen"] * 5)
    result = predictor.predict(make_row(rsi=20.0), n_votes=5)
    assert result["rule"] == "kaufen"
    assert result["decision"] == "halten"
    assert result["confidence"] == pytest.approx(1.0)


def test_predict_holds_below_confidence_threshold() -> None:
    predictor = make_predictor([" kaufen", " verkaufen", " halten"])
    result = predictor.predict(
        make_row(rsi=20.0), n_votes=3, confidence_threshold=0.9)
    assert result["rule"] == "kaufen"
    assert result["decision"] == "halten"
    assert result["confidence"] == pytest.approx(1 / 3)


def test_predict_confidence_uses_requested_vote_count_not_returned() -> None:
    predictor = make_predictor([" kaufen"])
    result = predictor.predict(make_row(rsi=20.0), n_votes=5)
    assert result["confidence"] == pytest.approx(0.2)
    assert result["decision"] == "halten"


# ---------------------------------------------------------------------------
# predict - failure handling
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("error", [
    RuntimeError("cuda out of memory"),
    ValueError("bad shape"),
    KeyError("rsi"),
    OSError("disk full"),
    TypeError("not callable"),
    IndexError("empty"),
])
def test_predict_degrades_to_hold_on_pipeline_error(error: Exception) -> None:
    predictor = make_predictor(error=error)
    result = predictor.predict(make_row(rsi=20.0))
    assert result == {
        "decision": "halten",
        "confidence": 0.0,
        "llm_votes": {},
        "rule": "halten",
    }


def test_predict_degrades_to_hold_on_missing_column() -> None:
    predictor = make_predictor([" kaufen"])
    frame = pd.DataFrame([{"coin": "BTC/EUR", "timestamp": 1_672_531_200_000}])
    result = predictor.predict(frame)
    assert result["decision"] == "halten"
    assert result["confidence"] == pytest.approx(0.0)
