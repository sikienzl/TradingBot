"""Unit tests for the AI co-pilot service extracted from ``CryptoTradingBot``."""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src import ai_copilot_service as svc
from src.state_store import current_day_key

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(tmp_path=None):
    return SimpleNamespace(
        performance_log_enabled=False,
        performance_log_file=str(tmp_path / "perf.csv") if tmp_path else "",
        min_entry_score=30,
        reentry_cooldown_seconds=900,
        reentry_cooldown_max_seconds=7200,
        tabular_buy_min_confidence=0.55,
        tabular_min_confidence=0.5,
        max_hold_seconds=14400,
        fallback_min_score=45,
        ai_copilot_min_entry_score_min=0,
        ai_copilot_min_entry_score_max=100,
        ai_copilot_reentry_cooldown_min=0,
        ai_copilot_reentry_cooldown_max=86400,
        ai_copilot_tabular_buy_conf_min=0.0,
        ai_copilot_tabular_buy_conf_max=1.0,
        ai_copilot_api_url="https://example.invalid/v1/chat/completions",
        ai_copilot_api_key="secret-key",
    )


def _make_portfolio():
    return SimpleNamespace(cash=1000.0, open_trades={"ETH": {}}, holdings={"BTC": 1.0})


def _make_performance():
    return {
        "buys": 4,
        "sells": 3,
        "wins": 2,
        "losses": 1,
        "break_even": 0,
        "closed_trades": 3,
        "realized_pnl_base": 12.5,
        "by_source": {
            "rules": {"buys": 2, "sells": 2, "pnl": 7.5},
            "tabular": {"buys": 2, "sells": 1, "pnl": 5.0},
        },
    }


def _variant_kwargs(**overrides):
    kwargs = {
        "label": "AI co-pilot",
        "state_file": "",
        "enabled": True,
        "model": "gpt-test",
        "shadow_mode": False,
        "max_calls_per_day": 10,
        "max_calls_per_month": 50,
        "max_budget_usd_per_month": 5.0,
        "max_output_tokens": 400,
        "temperature": 0.2,
        "max_consecutive_errors": 3,
        "cost_input_per_mtok": 1.0,
        "cost_output_per_mtok": 2.0,
        "interval_minutes": 0,
        "allow_apply": True,
    }
    kwargs.update(overrides)
    return kwargs


class _CaptureLogger:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _add(self, level, msg, *args):
        self.records.append((level, (msg % args) if args else msg))

    def info(self, msg, *args):
        self._add("INFO", msg, *args)

    def warning(self, msg, *args):
        self._add("WARNING", msg, *args)

    def error(self, msg, *args):
        self._add("ERROR", msg, *args)

    def messages(self, level=None):
        return [m for lvl, m in self.records if level is None or lvl == level]


# ---------------------------------------------------------------------------
# estimate_ai_cost_usd
# ---------------------------------------------------------------------------


def test_estimate_ai_cost_usd_basic():
    assert svc.estimate_ai_cost_usd(1_000_000, 1_000_000, 1.0, 2.0) == 3.0


def test_estimate_ai_cost_usd_clamps_negative_tokens():
    assert svc.estimate_ai_cost_usd(-50, -10, 1.0, 2.0) == 0.0


def test_estimate_ai_cost_usd_fractional():
    cost = svc.estimate_ai_cost_usd(1500, 2500, 2.0, 4.0)
    assert cost == pytest.approx(0.003 + 0.01)


# ---------------------------------------------------------------------------
# can_run_ai_copilot
# ---------------------------------------------------------------------------


def _can_run(state=None, **overrides):
    kwargs = {
        "enabled": True,
        "model": "gpt-test",
        "api_key": "k",
        "max_consecutive_errors": 3,
        "max_calls_per_day": 10,
        "max_calls_per_month": 50,
        "max_budget_usd_per_month": 5.0,
        "interval_minutes": 0,
    }
    kwargs.update(overrides)
    return svc.can_run_ai_copilot(state or {}, logger=_CaptureLogger(), **kwargs)


def test_can_run_ok():
    assert _can_run() == (True, "ok")


def test_can_run_disabled():
    assert _can_run(enabled=False) == (False, "disabled")


def test_can_run_missing_model():
    assert _can_run(model="") == (False, "missing_model")


def test_can_run_missing_api_key():
    assert _can_run(api_key="") == (False, "missing_api_key")


def test_can_run_suspended_after_errors():
    state = {"consecutive_errors": 3, "last_suspended_at": "2026-01-01T00:00:00+00:00"}
    assert _can_run(state) == (False, "suspended_after_errors")


def test_can_run_errors_without_suspension_marker_still_run():
    state = {"consecutive_errors": 5}
    assert _can_run(state, max_consecutive_errors=3)[0] is True


def test_can_run_zero_error_threshold_disables_suspension():
    state = {"consecutive_errors": 9, "last_suspended_at": "2026-01-01T00:00:00+00:00"}
    assert _can_run(state, max_consecutive_errors=0)[0] is True


def test_can_run_daily_call_limit():
    assert _can_run({"daily_calls": 10}) == (False, "daily_call_limit")


def test_can_run_monthly_call_limit():
    assert _can_run({"monthly_calls": 50}) == (False, "monthly_call_limit")


def test_can_run_monthly_budget_limit():
    assert _can_run({"monthly_spend_usd": 5.0}) == (False, "monthly_budget_limit")


def test_can_run_interval_not_reached():
    recent = datetime.now(UTC).isoformat()
    assert _can_run({"last_attempt_at": recent}, interval_minutes=60) == (
        False,
        "interval_not_reached",
    )


def test_can_run_interval_reached():
    old = (datetime.now(UTC) - timedelta(minutes=61)).isoformat()
    assert _can_run({"last_attempt_at": old}, interval_minutes=60)[0] is True


def test_can_run_interval_treats_naive_timestamp_as_utc():
    old = (datetime.now(UTC) - timedelta(minutes=120)).replace(tzinfo=None).isoformat()
    assert _can_run({"last_attempt_at": old}, interval_minutes=60)[0] is True


def test_can_run_falls_back_to_last_run_at():
    recent = datetime.now(UTC).isoformat()
    assert _can_run({"last_run_at": recent}, interval_minutes=60)[1] == (
        "interval_not_reached"
    )


def test_can_run_invalid_attempt_timestamp_blocks_and_logs():
    logger = _CaptureLogger()
    ok, reason = svc.can_run_ai_copilot(
        {"last_attempt_at": "not-a-timestamp"},
        enabled=True,
        model="m",
        api_key="k",
        max_consecutive_errors=0,
        max_calls_per_day=0,
        max_calls_per_month=0,
        max_budget_usd_per_month=0,
        interval_minutes=30,
        logger=logger,
    )
    assert (ok, reason) == (False, "invalid_attempt_timestamp")
    assert any("invalid attempt timestamp" in m for m in logger.messages("WARNING"))


# ---------------------------------------------------------------------------
# extract_json_object
# ---------------------------------------------------------------------------


def test_extract_json_object_empty_text():
    assert svc.extract_json_object("") is None


def test_extract_json_object_no_braces():
    assert svc.extract_json_object("no json here") is None


def test_extract_json_object_unbalanced():
    assert svc.extract_json_object("} {") is None


def test_extract_json_object_plain():
    assert svc.extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_object_embedded_in_prose():
    text = 'Sure! Here you go: {"proposed_changes": {}} thanks'
    assert svc.extract_json_object(text) == {"proposed_changes": {}}


def test_extract_json_object_nested():
    payload = {"a": {"b": [1, 2, {"c": 3}]}}
    assert svc.extract_json_object(f"prefix {json.dumps(payload)} suffix") == payload


def test_extract_json_object_invalid_json():
    assert svc.extract_json_object("{not valid json}") is None


def test_extract_json_object_non_dict_payload():
    assert svc.extract_json_object("[1, 2, 3]") is None


# ---------------------------------------------------------------------------
# clamp_ai_changes
# ---------------------------------------------------------------------------


def test_clamp_ai_changes_non_dict_returns_empty():
    assert svc.clamp_ai_changes("nope", _make_config()) == {}


def test_clamp_ai_changes_empty():
    assert svc.clamp_ai_changes({}, _make_config()) == {}


def test_clamp_ai_changes_min_entry_score_rounded():
    assert svc.clamp_ai_changes({"min_entry_score": 42.6}, _make_config()) == {
        "min_entry_score": 43
    }


def test_clamp_ai_changes_min_entry_score_clamped_both_sides():
    cfg = _make_config()
    assert svc.clamp_ai_changes({"min_entry_score": -20}, cfg)["min_entry_score"] == 0
    assert svc.clamp_ai_changes({"min_entry_score": 999}, cfg)["min_entry_score"] == 100


def test_clamp_ai_changes_reentry_cooldown():
    cfg = _make_config()
    assert svc.clamp_ai_changes({"reentry_cooldown_seconds": 1200.4}, cfg) == {
        "reentry_cooldown_seconds": 1200
    }
    assert svc.clamp_ai_changes({"reentry_cooldown_seconds": -5}, cfg)[
        "reentry_cooldown_seconds"
    ] == 0
    assert svc.clamp_ai_changes({"reentry_cooldown_seconds": 10 ** 9}, cfg)[
        "reentry_cooldown_seconds"
    ] == 86400


def test_clamp_ai_changes_tabular_confidence_rounded():
    cfg = _make_config()
    assert svc.clamp_ai_changes(
        {"tabular_buy_min_confidence": 0.123456}, cfg
    ) == {"tabular_buy_min_confidence": 0.1235}


def test_clamp_ai_changes_tabular_confidence_clamped():
    cfg = _make_config()
    assert svc.clamp_ai_changes({"tabular_buy_min_confidence": -0.5}, cfg)[
        "tabular_buy_min_confidence"
    ] == 0.0
    assert svc.clamp_ai_changes({"tabular_buy_min_confidence": 2.0}, cfg)[
        "tabular_buy_min_confidence"
    ] == 1.0


def test_clamp_ai_changes_non_numeric_swallowed():
    cfg = _make_config()
    raw = {
        "min_entry_score": "abc",
        "reentry_cooldown_seconds": None,
        "tabular_buy_min_confidence": {},
    }
    assert svc.clamp_ai_changes(raw, cfg) == {}


def test_clamp_ai_changes_keeps_only_allowed_keys():
    cfg = _make_config()
    out = svc.clamp_ai_changes({"max_hold_seconds": 10, "min_entry_score": 5}, cfg)
    assert out == {"min_entry_score": 5}


# ---------------------------------------------------------------------------
# apply_ai_changes
# ---------------------------------------------------------------------------


def test_apply_ai_changes_min_entry_score():
    cfg = _make_config()
    applied = svc.apply_ai_changes({"min_entry_score": 42}, cfg)
    assert applied == {"min_entry_score": (30, 42)}
    assert cfg.min_entry_score == 42


def test_apply_ai_changes_unchanged_values_not_applied():
    cfg = _make_config()
    assert svc.apply_ai_changes({"min_entry_score": 30}, cfg) == {}
    assert cfg.min_entry_score == 30


def test_apply_ai_changes_confidence_epsilon():
    cfg = _make_config()
    assert svc.apply_ai_changes({"tabular_buy_min_confidence": 0.55}, cfg) == {}
    applied = svc.apply_ai_changes({"tabular_buy_min_confidence": 0.56}, cfg)
    assert applied == {"tabular_buy_min_confidence": (0.55, 0.56)}
    assert cfg.tabular_buy_min_confidence == 0.56


def test_apply_ai_changes_reentry_cooldown():
    cfg = _make_config()
    applied = svc.apply_ai_changes({"reentry_cooldown_seconds": 1800}, cfg)
    assert applied == {"reentry_cooldown_seconds": (900, 1800)}
    assert cfg.reentry_cooldown_seconds == 1800


def test_apply_ai_changes_empty():
    cfg = _make_config()
    assert svc.apply_ai_changes({}, cfg) == {}


# ---------------------------------------------------------------------------
# call_ai_copilot (transport faked)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, payload):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResponse(payload)

    monkeypatch.setattr(svc.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_call_ai_copilot_parses_result_and_usage(monkeypatch):
    captured = _patch_urlopen(
        monkeypatch,
        {
            "usage": {"prompt_tokens": 120, "completion_tokens": 45},
            "choices": [
                {"message": {"content": '```json\n{"proposed_changes": {"min_entry_score": 40}}\n```'}}
            ],
        },
    )
    cfg = _make_config()
    result, prompt_tokens, completion_tokens = svc.call_ai_copilot(
        cfg, {"iteration": 1}, model="m1", temperature=0.1, max_output_tokens=200
    )
    assert result == {"proposed_changes": {"min_entry_score": 40}}
    assert (prompt_tokens, completion_tokens) == (120, 45)
    assert captured["timeout"] == 25
    body = json.loads(captured["req"].data.decode("utf-8"))
    assert body["model"] == "m1"
    assert body["max_tokens"] == 200
    assert captured["req"].headers["Authorization"] == "Bearer secret-key"


def test_call_ai_copilot_missing_usage_defaults_zero(monkeypatch):
    _patch_urlopen(monkeypatch, {"choices": [{"message": {"content": '{"a": 1}'}}]})
    result, prompt_tokens, completion_tokens = svc.call_ai_copilot(
        _make_config(), {}, model="m", temperature=0.0, max_output_tokens=10
    )
    assert result == {"a": 1}
    assert (prompt_tokens, completion_tokens) == (0, 0)


def test_call_ai_copilot_no_choices_returns_none(monkeypatch):
    _patch_urlopen(monkeypatch, {"usage": {"prompt_tokens": 5}})
    result, prompt_tokens, _completion_tokens = svc.call_ai_copilot(
        _make_config(), {}, model="m", temperature=0.0, max_output_tokens=10
    )
    assert result is None
    assert prompt_tokens == 5


def test_call_ai_copilot_non_dict_response(monkeypatch):
    _patch_urlopen(monkeypatch, ["unexpected", "list"])
    result, prompt_tokens, completion_tokens = svc.call_ai_copilot(
        _make_config(), {}, model="m", temperature=0.0, max_output_tokens=10
    )
    assert result is None
    assert (prompt_tokens, completion_tokens) == (0, 0)


# ---------------------------------------------------------------------------
# ai_copilot_snapshot
# ---------------------------------------------------------------------------


def _snapshot(config, performance=None, consecutive_losses=0, iteration=7, portfolio=None):
    return svc.ai_copilot_snapshot(
        config,
        _CaptureLogger(),
        performance=performance or _make_performance(),
        consecutive_losses=consecutive_losses,
        iteration=iteration,
        portfolio=portfolio or _make_portfolio(),
    )


def test_snapshot_config_block(tmp_path):
    cfg = _make_config(tmp_path)
    snap = _snapshot(cfg)
    assert snap["config"]["min_entry_score"] == 30
    assert snap["config"]["reentry_cooldown_seconds"] == 900
    assert snap["config"]["reentry_cooldown_max_seconds"] == 7200
    assert snap["config"]["tabular_buy_min_confidence"] == 0.55
    assert snap["config"]["max_hold_seconds"] == 14400
    assert snap["config"]["fallback_min_score"] == 45


def test_snapshot_performance_block(tmp_path):
    snap = _snapshot(_make_config(tmp_path), consecutive_losses=2)
    perf = snap["performance"]
    assert perf["buys"] == 4
    assert perf["sells"] == 3
    assert perf["closed_trades"] == 3
    assert perf["win_rate_pct"] == pytest.approx((2 / 3) * 100.0)
    assert perf["consecutive_losses"] == 2
    assert perf["by_source"]["rules"] == {"buys": 2, "sells": 2, "pnl": 7.5}


def test_snapshot_performance_zero_closed_trades_win_rate(tmp_path):
    perf = _make_performance()
    perf["closed_trades"] = 0
    snap = _snapshot(_make_config(tmp_path), performance=perf)
    assert snap["performance"]["win_rate_pct"] == 0.0


def test_snapshot_portfolio_block(tmp_path):
    portfolio = SimpleNamespace(
        cash=250.5,
        open_trades={"ETH": {}, "ADA": {}},
        holdings={"BTC": 1.0, "DOT": 2.0},
    )
    snap = _snapshot(_make_config(tmp_path), portfolio=portfolio)
    assert snap["portfolio"]["cash"] == 250.5
    assert snap["portfolio"]["open_trades_count"] == 2
    assert snap["portfolio"]["open_trade_coins"] == ["ADA", "ETH"]
    assert snap["portfolio"]["holding_coins"] == ["BTC", "DOT"]


def test_snapshot_guardrails_block(tmp_path):
    snap = _snapshot(_make_config(tmp_path))
    guard = snap["guardrails"]
    assert guard["min_entry_score_min"] == 0
    assert guard["min_entry_score_max"] == 100
    assert guard["reentry_cooldown_max"] == 86400
    assert guard["tabular_buy_conf_max"] == 1.0


def test_snapshot_without_journal_file(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.performance_log_enabled = True
    snap = _snapshot(cfg)
    assert snap["recent_trades"] == []
    assert snap["recent_trade_summary"]["window_rows"] == 0


def test_snapshot_disabled_logging_skips_file(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.performance_log_enabled = False
    (tmp_path / "perf.csv").write_text("this is not,csv\n,,,\n", encoding="utf-8")
    snap = _snapshot(cfg)
    assert snap["recent_trades"] == []


def test_snapshot_recent_trade_summary(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.performance_log_enabled = True
    pd.DataFrame(
        [
            {"action": "buy", "coin": "ETH", "pnl_base": 0.0, "hold_seconds": 0, "reason": "entry"},
            {"action": "sell", "coin": "ETH", "pnl_base": 1.0, "hold_seconds": 10, "reason": "take-profit"},
            {"action": "sell", "coin": "ETH", "pnl_base": -1.0, "hold_seconds": 20, "reason": "MAX-HOLD-TIME"},
            {"action": "sell", "coin": "BTC", "pnl_base": 0.0, "hold_seconds": 30, "reason": "stop-loss"},
        ]
    ).to_csv(cfg.performance_log_file, index=False)
    summary = _snapshot(cfg)["recent_trade_summary"]
    assert summary["window_rows"] == 4
    assert summary["recent_buy_count"] == 1
    assert summary["recent_sell_count"] == 3
    assert summary["recent_break_even_count"] == 1
    assert summary["recent_win_rate_pct"] == pytest.approx(33.33)
    assert summary["recent_avg_pnl_base"] == pytest.approx(0.0)
    assert summary["recent_avg_hold_seconds"] == pytest.approx(20.0)
    assert summary["recent_max_hold_exit_ratio"] == pytest.approx(1 / 3, abs=1e-4)
    assert summary["recent_exit_reasons"]["take-profit"] == 1
    eth = summary["coin_sell_summary"]["ETH"]
    assert eth["sell_count"] == 2
    assert eth["pnl_sum"] == pytest.approx(0.0)
    assert eth["max_hold_exit_ratio"] == pytest.approx(0.5)
    assert summary["top_losers"][0]["coin"] == "BTC"
    assert {entry["coin"] for entry in summary["top_winners"]} == {"BTC", "ETH"}


def test_snapshot_recent_trades_rows(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.performance_log_enabled = True
    pd.DataFrame(
        [
            {
                "timestamp": "t1", "coin": "ETH", "action": "buy",
                "pnl_base": 0.0, "hold_seconds": 0.0, "reason": "entry",
            },
            {
                "timestamp": "t2", "coin": "ETH", "action": "sell",
                "pnl_base": 2.0, "hold_seconds": 5.0, "reason": "take-profit",
            },
        ]
    ).to_csv(cfg.performance_log_file, index=False)
    rows = _snapshot(cfg)["recent_trades"]
    assert len(rows) == 2
    assert rows[1]["action"] == "sell"
    assert rows[1]["pnl_base"] == pytest.approx(2.0)
    assert rows[1]["reason"] == "take-profit"


def test_snapshot_parse_error_resets_and_logs(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.performance_log_enabled = True
    Path(cfg.performance_log_file).write_text('"unterminated,csv\n', encoding="utf-8")
    logger = _CaptureLogger()
    snap = svc.ai_copilot_snapshot(
        cfg,
        logger,
        performance=_make_performance(),
        consecutive_losses=0,
        iteration=1,
        portfolio=_make_portfolio(),
    )
    assert snap["recent_trades"] == []
    assert snap["recent_trade_summary"]["window_rows"] == 0
    assert any("Unable to build AI co-pilot snapshot" in m for m in logger.messages("WARNING"))


def test_snapshot_empty_csv(tmp_path):
    cfg = _make_config(tmp_path)
    cfg.performance_log_enabled = True
    Path(cfg.performance_log_file).write_text("", encoding="utf-8")
    snap = _snapshot(cfg)
    assert snap["recent_trades"] == []
    assert snap["recent_trade_summary"]["window_rows"] == 0


# ---------------------------------------------------------------------------
# maybe_run_ai_copilot_variant (orchestration)
# ---------------------------------------------------------------------------


def _run_variant(monkeypatch, tmp_path, *, call_result=None, call_error=None, **overrides):
    cfg = _make_config(tmp_path)
    logger = _CaptureLogger()
    state_file = str(tmp_path / "ai_state.json")
    calls = []

    def fake_call(config, snapshot, *, model, temperature, max_output_tokens):
        calls.append({"model": model, "temperature": temperature, "max_output_tokens": max_output_tokens})
        if call_error is not None:
            raise call_error
        return call_result

    monkeypatch.setattr(svc, "call_ai_copilot", fake_call)
    kwargs = _variant_kwargs(state_file=state_file, **overrides)
    svc.maybe_run_ai_copilot_variant(cfg, logger, {"iteration": 1}, **kwargs)
    state = json.loads(Path(state_file).read_text(encoding="utf-8")) if Path(state_file).exists() else None
    return cfg, logger, state, calls


def test_variant_disabled_writes_state_without_call(monkeypatch, tmp_path):
    _cfg, logger, state, calls = _run_variant(monkeypatch, tmp_path, enabled=False)
    assert calls == []
    assert state["model"] == "gpt-test"
    assert logger.messages() == []


def test_variant_missing_api_key_logs_skip(monkeypatch, tmp_path):
    cfg = _make_config(tmp_path)
    cfg.ai_copilot_api_key = ""
    logger = _CaptureLogger()
    state_file = str(tmp_path / "ai_state_missing_key.json")
    calls = []

    def fake_call(*args, **kwargs):
        calls.append(args)
        return (None, 0, 0)

    monkeypatch.setattr(svc, "call_ai_copilot", fake_call)
    svc.maybe_run_ai_copilot_variant(
        cfg, logger, {}, **_variant_kwargs(state_file=state_file)
    )
    assert calls == []
    assert logger.messages("INFO") == ["AI co-pilot skipped: missing_api_key"]
    state = json.loads(Path(state_file).read_text(encoding="utf-8"))
    assert state["model"] == "gpt-test"


def test_variant_interval_skip_is_silent(monkeypatch, tmp_path):
    state_file = str(tmp_path / "ai_state.json")
    Path(state_file).write_text(
        json.dumps({"last_attempt_at": datetime.now(UTC).isoformat()}), encoding="utf-8"
    )
    _cfg, logger, _state, calls = _run_variant(monkeypatch, tmp_path, interval_minutes=60)
    assert calls == []
    assert logger.messages() == []


def test_variant_daily_limit_logs_skip(monkeypatch, tmp_path):
    state_file = str(tmp_path / "ai_state.json")
    Path(state_file).write_text(
        json.dumps({"day_key": current_day_key(), "daily_calls": 10}), encoding="utf-8"
    )
    _cfg, logger, _state, calls = _run_variant(monkeypatch, tmp_path, max_calls_per_day=10)
    assert calls == []
    assert logger.messages("INFO") == ["AI co-pilot skipped: daily_call_limit"]


def test_variant_budget_cap_after_call_ignores_result(monkeypatch, tmp_path):
    result = ({"proposed_changes": {"min_entry_score": 40}, "reason": "r", "confidence": 0.5, "risk_level": "low"}, 1_000_000, 0)
    _, logger, state, calls = _run_variant(
        monkeypatch, tmp_path, call_result=result, max_budget_usd_per_month=0.5
    )
    assert len(calls) == 1
    assert state.get("monthly_calls", 0) == 0
    assert "last_attempt_at" in state
    warnings = logger.messages("WARNING")
    assert any("monthly budget cap" in m for m in warnings)


def test_variant_no_valid_json_payload(monkeypatch, tmp_path):
    _, logger, state, calls = _run_variant(monkeypatch, tmp_path, call_result=(None, 10, 5))
    assert len(calls) == 1
    assert state["monthly_calls"] == 1
    assert state["daily_calls"] == 1
    assert state["last_prompt_tokens"] == 10
    assert state["last_completion_tokens"] == 5
    assert state["last_estimated_cost_usd"] == pytest.approx(0.00002)
    assert logger.messages("WARNING") == ["AI co-pilot returned no valid JSON payload"]


def test_variant_no_change_suggestion(monkeypatch, tmp_path):
    result = ({"proposed_changes": {}, "reason": "weak evidence", "confidence": 0.2, "risk_level": "low"}, 10, 5)
    cfg, logger, state, _ = _run_variant(monkeypatch, tmp_path, call_result=result)
    assert cfg.min_entry_score == 30
    assert state["last_suggestion"]["mode"] == "no_change"
    info = logger.messages("INFO")
    assert any("suggestion: no change" in m for m in info)


def test_variant_shadow_mode_does_not_apply(monkeypatch, tmp_path):
    result = ({"proposed_changes": {"min_entry_score": 40}, "reason": "r", "confidence": 0.7, "risk_level": "medium"}, 10, 5)
    cfg, logger, state, _ = _run_variant(monkeypatch, tmp_path, call_result=result, shadow_mode=True)
    assert cfg.min_entry_score == 30
    assert state["last_suggestion"]["mode"] == "shadow"
    assert state["last_suggestion"]["changes"] == {"min_entry_score": 40}
    assert any("shadow suggestion" in m for m in logger.messages("INFO"))


def test_variant_allow_apply_false_is_shadow(monkeypatch, tmp_path):
    result = ({"proposed_changes": {"min_entry_score": 40}, "reason": "r", "confidence": 0.7, "risk_level": "medium"}, 10, 5)
    cfg, _, state, _ = _run_variant(monkeypatch, tmp_path, call_result=result, allow_apply=False)
    assert cfg.min_entry_score == 30
    assert state["last_suggestion"]["mode"] == "shadow"


def test_variant_applies_single_change(monkeypatch, tmp_path):
    result = (
        {
            "proposed_changes": {"min_entry_score": 40, "reentry_cooldown_seconds": 1200},
            "reason": "reduce churn",
            "confidence": 0.8,
            "risk_level": "low",
        },
        20,
        8,
    )
    cfg, logger, state, _ = _run_variant(monkeypatch, tmp_path, call_result=result)
    assert cfg.min_entry_score == 40
    assert cfg.reentry_cooldown_seconds == 900  # only one parameter per run
    assert state["last_suggestion"]["mode"] == "applied"
    assert state["last_applied_changes"] == {"min_entry_score": {"old": 30, "new": 40}}
    assert "last_applied_at" in state
    assert any("applied:" in m for m in logger.messages("INFO"))


def test_variant_suggested_unchanged_values(monkeypatch, tmp_path):
    result = ({"proposed_changes": {"min_entry_score": 30}, "reason": "r", "confidence": 0.5, "risk_level": "low"}, 1, 1)
    cfg, logger, state, _ = _run_variant(monkeypatch, tmp_path, call_result=result)
    assert cfg.min_entry_score == 30
    assert "last_applied_changes" not in state
    assert any("suggested unchanged values" in m for m in logger.messages("INFO"))


def test_variant_call_error_records_and_warns(monkeypatch, tmp_path):
    _, logger, state, _ = _run_variant(
        monkeypatch, tmp_path, call_error=urllib.error.URLError("boom")
    )
    assert state["consecutive_errors"] == 1
    assert "boom" in state["last_error"]
    assert "last_error_at" in state
    assert any("call failed" in m and "boom" in m for m in logger.messages("WARNING"))
    assert "last_suspended_at" not in state


def test_variant_error_suspends_after_threshold(monkeypatch, tmp_path):
    state_file = str(tmp_path / "ai_state.json")
    Path(state_file).write_text(
        json.dumps({"day_key": current_day_key(), "consecutive_errors": 2}), encoding="utf-8"
    )
    _, logger, state, _ = _run_variant(
        monkeypatch, tmp_path, call_error=TimeoutError("slow"), max_consecutive_errors=3
    )
    assert state["consecutive_errors"] == 3
    assert "last_suspended_at" in state
    assert any("suspended after 3 consecutive errors" in m for m in logger.messages("ERROR"))


def test_variant_successful_call_clears_error_state(monkeypatch, tmp_path):
    state_file = str(tmp_path / "ai_state.json")
    Path(state_file).write_text(
        json.dumps(
            {
                "day_key": current_day_key(),
                "consecutive_errors": 2,
                "last_suspended_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    _, _, state, _ = _run_variant(monkeypatch, tmp_path, call_result=(None, 1, 1))
    assert state["consecutive_errors"] == 0
    assert "last_suspended_at" not in state


def test_variant_resets_stale_month_counters(monkeypatch, tmp_path):
    state_file = str(tmp_path / "ai_state.json")
    Path(state_file).write_text(
        json.dumps({"month_key": "2000-01", "monthly_calls": 40, "monthly_spend_usd": 4.0}),
        encoding="utf-8",
    )
    _, _, state, _ = _run_variant(monkeypatch, tmp_path, call_result=(None, 1, 1))
    assert state["monthly_calls"] == 1
    assert state["monthly_spend_usd"] == pytest.approx(0.000003)


# ---------------------------------------------------------------------------
# Bot delegators
# ---------------------------------------------------------------------------


def _make_bot(tmp_path):
    """A CryptoTradingBot instance with just the AI co-pilot collaborator state."""
    from src.trading_bot import CryptoTradingBot

    bot = CryptoTradingBot.__new__(CryptoTradingBot)
    bot.config = _make_config(tmp_path)
    bot.config.ai_copilot_state_file = str(tmp_path / "configured_state.json")
    bot.performance = _make_performance()
    bot.consecutive_losses = 0
    bot.iteration = 3
    bot.portfolio = _make_portfolio()
    return bot


def test_delegator_snapshot_matches_service(tmp_path):
    from src import trading_bot as tb

    bot = _make_bot(tmp_path)
    snapshot = bot._ai_copilot_snapshot()
    expected = svc.ai_copilot_snapshot(
        bot.config,
        tb.logger,
        performance=bot.performance,
        consecutive_losses=bot.consecutive_losses,
        iteration=bot.iteration,
        portfolio=bot.portfolio,
    )
    assert snapshot.keys() == expected.keys()
    assert snapshot["config"] == expected["config"]
    assert snapshot["guardrails"] == expected["guardrails"]


def test_delegator_variant_empty_state_file_falls_back_to_config(monkeypatch, tmp_path):
    bot = _make_bot(tmp_path)
    configured = bot.config.ai_copilot_state_file

    def fake_call(config, snapshot, *, model, temperature, max_output_tokens):
        return {"proposed_changes": {"min_entry_score": 44}, "reason": "r",
                "confidence": 0.5, "risk_level": "low"}, 10, 5

    monkeypatch.setattr(svc, "call_ai_copilot", fake_call)
    bot._maybe_run_ai_copilot_variant(
        {"iteration": 3}, **_variant_kwargs(state_file="")
    )

    state = json.loads(Path(configured).read_text(encoding="utf-8"))
    assert state["monthly_calls"] == 1
    assert bot.config.min_entry_score == 44
