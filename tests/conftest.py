import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def isolated_risk_state_file(tmp_path, monkeypatch):
    """Keep the durable risk-state file inside the test's own tmp directory.

    ``BotConfig`` defaults it to a repository-relative path, so without this
    redirect a test that closes a trade would leave risk state behind for the
    next test's bot to restore. Tests that exercise persistence explicitly set
    the environment variable themselves, which overrides this default.
    """
    monkeypatch.setenv(
        "TRADING_RISK_STATE_FILE", str(tmp_path / "trading_risk_state.json"))


@pytest.fixture(autouse=True)
def isolated_inference_stats_file(tmp_path, monkeypatch):
    """Keep the inference backend counters inside the test's own tmp directory.

    ``src.inference_decision_stats`` defaults to a repository-relative path and
    :func:`src.coin_analysis.analyze_coin` records a decision on every tabular
    prediction, so without this redirect unrelated tests would accumulate
    counters in the working tree and a counter test could read another test's
    decisions.
    """
    monkeypatch.setenv(
        "TRADING_INFERENCE_STATS_FILE", str(tmp_path / "backend_stats.json"))


@pytest.fixture(autouse=True)
def isolated_model_release_root(monkeypatch):
    """Start every test with no paired model release root configured.

    ``TRADING_MODEL_RELEASE_ROOT`` makes both the bot and the operational-health
    exporter look for an ``active`` release, so a developer or CI environment
    that happens to set it would change what unrelated tests observe. Tests that
    exercise release visibility set it themselves, which overrides this default.
    """
    monkeypatch.delenv("TRADING_MODEL_RELEASE_ROOT", raising=False)
