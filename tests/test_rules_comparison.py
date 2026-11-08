import json
import sys
from types import SimpleNamespace

import pytest

from scripts.rules_comparison import RulesComparisonBot, main


def bot():
    instance = RulesComparisonBot.__new__(RulesComparisonBot)
    instance.config = SimpleNamespace(
        dry_run=True, use_tabular_model=False, uptrend_entry_gate_enabled=True,
        uptrend_entry_max_rsi=72, uptrend_entry_max_rsi_by_coin={},
        uptrend_entry_min_buy_proba=0.24, uptrend_entry_min_buy_proba_by_coin={},
        uptrend_entry_max_sell_proba=0.34, uptrend_entry_max_sell_proba_by_coin={},
        uptrend_entry_min_proba_edge=-0.05, uptrend_entry_min_proba_edge_by_coin={},
    )
    return instance


def test_rules_only_uptrend_still_enforces_rsi():
    instance = bot()
    data = {"coin": "ETH", "recommendation": "HOLD (Up-Trend)",
            "signal_source": "rules", "rsi": 65}
    assert instance._passes_uptrend_entry_filter(data) == (True, "rules_comparison_no_model")
    data["rsi"] = 73
    assert instance._passes_uptrend_entry_filter(data)[1].startswith("rsi_above_uptrend_max")
    data["rsi"] = None
    assert instance._passes_uptrend_entry_filter(data) == (False, "missing_rsi")


def test_probability_bypass_only_for_rules_without_model_or_partial_probabilities():
    instance = bot()
    data = {"coin": "ETH", "recommendation": "HOLD (Up-Trend)",
            "signal_source": "rules", "rsi": 65}
    for key, value in (("signal_source", "catboost"), ("tabular_sell_proba", 0.3)):
        altered = {**data, key: value}
        assert instance._passes_uptrend_entry_filter(altered) == (False, "missing_buy_proba")
    instance.config.use_tabular_model = True
    assert instance._passes_uptrend_entry_filter(data) == (False, "missing_buy_proba")
    instance.config.use_tabular_model = False
    instance.config.dry_run = False
    assert instance._passes_uptrend_entry_filter(data) == (False, "missing_buy_proba")


def test_comparison_keeps_defensive_market_and_fallback_rsi():
    instance = bot()
    instance.config.simulate_data = False
    instance.config.fallback_max_rsi = 68
    instance.config.fallback_max_rsi_by_coin = {}
    market = {
        "ETH": {"recommendation": "HOLD (Up-Trend)"},
        **{str(i): {"recommendation": "HOLD (Down-Trend)"} for i in range(9)},
    }
    assert instance._entry_market_mode(market) == "defensive"
    assert instance._passes_fallback_entry_filter("ETH", {"rsi": 65}) == (True, "ok")
    assert instance._passes_fallback_entry_filter("ETH", {"rsi": 70})[1].startswith(
        "rsi_above_fallback_max"
    )


def test_live_order_and_missing_state_are_blocked(tmp_path):
    instance = bot()
    instance.config.dry_run = False
    with pytest.raises(RuntimeError, match="refuses live"):
        instance._execute_trade("ETH", "buy", 100, 5)
    instance.config.dry_run = True
    with pytest.raises(RuntimeError, match="5 EUR limit"):
        instance._execute_trade("ETH", "buy", 100, 6)
    instance.portfolio = SimpleNamespace(state_file=str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="private balance"):
        instance._update_portfolio_balance()


def test_initialization_is_explicit_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["comparison", "--data-dir", str(tmp_path),
                                     "--initialize", "--initial-cash", "19.52"])
    main()
    path = tmp_path / "portfolio.json"
    assert json.loads(path.read_text())["cash"] == 19.52
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        main()
    assert path.read_bytes() == before


def test_start_without_existing_state_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["comparison", "--data-dir", str(tmp_path)])
    with pytest.raises(RuntimeError, match="initialize explicitly"):
        main()


@pytest.mark.parametrize("allow", [False, True])
def test_defensive_fallback_requires_explicit_flag(tmp_path, monkeypatch, allow):
    import scripts.rules_comparison as comparison

    monkeypatch.setattr("src.trading_bot.load_dotenv", lambda **kwargs: False)
    monkeypatch.setattr(comparison, "configure_logging", lambda path: None)
    monkeypatch.setattr(sys, "argv", ["comparison", "--data-dir", str(tmp_path),
                                     "--initialize", "--initial-cash", "19.52"])
    main()
    captured = []

    class FakeBot:
        def __init__(self, config):
            self.exchange = object()
            captured.append(config)

        def run(self):
            pass

    monkeypatch.setattr(comparison, "RulesComparisonBot", FakeBot)
    argv = ["comparison", "--data-dir", str(tmp_path)]
    if allow:
        argv.append("--allow-defensive-fallback")
    monkeypatch.setattr(sys, "argv", argv)
    main()
    config = captured[0]
    assert config.defensive_entry_mode_enabled is not allow
    assert config.dry_run and not config.force_fill_slots
    assert config.uptrend_entry_gate_enabled and config.fallback_max_rsi == 68
    assert config.trade_amount == 5
