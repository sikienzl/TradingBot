from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from src.news_veto import evaluate_news_veto


def _config(tmp_path, **overrides):
    values = {
        "news_veto_enabled": True,
        "news_veto_api_url": "https://provider.test/v1/chat/completions",
        "news_veto_api_key": "test-key",
        "news_veto_model": "test-model",
        "news_veto_state_file": str(tmp_path / "budget.json"),
        "news_veto_max_calls_per_day": 2,
        "tabular_research_signal_path": str(tmp_path / "signal.json"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _signal(tmp_path, *, published_at=None, fetched_at=None):
    now = datetime.now(UTC).isoformat()
    payload = {
        "source_details": {
            "news_items": [{
                "title": "Exchange security update",
                "published_at": published_at or now,
                "fetched_at": fetched_at or now,
            }],
        },
    }
    (tmp_path / "signal.json").write_text(json.dumps(payload), encoding="utf-8")


def test_news_veto_is_disabled_by_default(tmp_path):
    config = _config(tmp_path, news_veto_enabled=False)
    assert evaluate_news_veto(config, "BTC", {"recommendation": "BUY"}) == (False, "disabled")


def test_news_veto_fails_closed_when_no_timestamped_news(tmp_path):
    _signal(tmp_path, published_at=None)
    payload = json.loads((tmp_path / "signal.json").read_text())
    payload["source_details"]["news_items"][0]["published_at"] = ""
    (tmp_path / "signal.json").write_text(json.dumps(payload), encoding="utf-8")

    assert evaluate_news_veto(_config(tmp_path), "BTC", {}) == (True, "no_fresh_news")


def test_news_veto_fails_closed_without_provider_configuration(tmp_path):
    _signal(tmp_path)
    config = _config(tmp_path, news_veto_api_key="")
    assert evaluate_news_veto(config, "BTC", {}) == (True, "provider_not_configured")


def test_news_veto_fails_closed_when_daily_budget_is_exhausted(tmp_path):
    _signal(tmp_path)
    from datetime import date

    (tmp_path / "budget.json").write_text(
        json.dumps({"day": date.today().isoformat(), "day_count": 2}), encoding="utf-8")

    assert evaluate_news_veto(_config(tmp_path), "BTC", {}) == (
        True, "daily_call_budget_exhausted")


def test_news_veto_validates_structured_response_and_reserves_budget(tmp_path, monkeypatch):
    _signal(tmp_path)
    monkeypatch.setattr(
        "src.news_veto._call_provider",
        lambda *_args: {
            "sentiment_score": -0.5,
            "event_risk": 0.8,
            "veto_buy": True,
            "reason": "material security incident",
        },
    )

    assert evaluate_news_veto(_config(tmp_path), "BTC", {"recommendation": "BUY"}) == (
        True, "material security incident")
    budget = json.loads((tmp_path / "budget.json").read_text(encoding="utf-8"))
    assert budget["day_count"] == 1


def test_news_veto_fails_closed_on_invalid_provider_schema(tmp_path, monkeypatch):
    _signal(tmp_path)
    monkeypatch.setattr(
        "src.news_veto._call_provider",
        lambda *_args: {"sentiment_score": 2.0, "event_risk": 1.5, "veto_buy": "no"},
    )

    blocked, reason = evaluate_news_veto(_config(tmp_path), "BTC", {})

    assert blocked is True
    assert reason.startswith("provider_error:")