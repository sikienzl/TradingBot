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
