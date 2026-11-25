"""Opt-in structured news veto with a durable daily API-call budget."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, date, datetime
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from src.api_models import NewsVetoPayload
from src.research_signal import load_research_signal_payload


def evaluate_news_veto(
    config: Any,
    coin: str,
    candidate: dict[str, Any],
) -> tuple[bool, str]:
    if not config.news_veto_enabled:
        return False, "disabled"
    if not config.news_veto_api_url or not config.news_veto_api_key or not config.news_veto_model:
        return True, "provider_not_configured"

    try:
        signal = load_research_signal_payload(config.tabular_research_signal_path)
        items = signal.source_details.get("news_items", [])
        now = datetime.now(UTC)
        fresh_items = [item for item in items if _item_is_fresh(item, now)]
        if not fresh_items:
            return True, "no_fresh_news"
        if not _reserve_daily_call(
            config.news_veto_state_file,
            config.news_veto_max_calls_per_day,
            now.date(),
        ):
            return True, "daily_call_budget_exhausted"
        result = _call_provider(config, coin, candidate, fresh_items)
        payload = NewsVetoPayload.model_validate(result)
        return payload.veto_buy, payload.reason
    except (AttributeError, KeyError, OSError, TypeError, ValueError, URLError) as exc:
        return True, f"provider_error:{type(exc).__name__}"


def _item_is_fresh(item: Any, now: datetime) -> bool:
    if not isinstance(item, dict):
        return False
    published = _parse_timestamp(item.get("published_at"))
    fetched = _parse_timestamp(item.get("fetched_at"))
    return bool(published and fetched and published <= now and fetched <= now)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _reserve_daily_call(path: str, maximum: int, today: date) -> bool:
    if maximum <= 0:
        return False
    try:
        with open(path, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
        if not isinstance(state, dict):
            return False
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError):
        return False

    day = today.isoformat()
    count = int(state.get("day_count", 0)) if state.get("day") == day else 0
    if count >= maximum:
        return False
    _atomic_write(path, {"day": day, "day_count": count + 1})
    return True


def _atomic_write(path: str, state: dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory,
            prefix=".news-veto-", delete=False,
        ) as handle:
            temporary_path = handle.name
            json.dump(state, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _call_provider(
    config: Any,
    coin: str,
    candidate: dict[str, Any],
    news_items: list[dict[str, Any]],
) -> dict[str, Any]:
    request_payload = {
        "model": config.news_veto_model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Return only JSON with sentiment_score (-1..1), event_risk (0..1), "
                    "veto_buy (boolean), and reason (non-empty string). News may veto, "
                    "never authorize, a trade."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "coin": coin,
                    "candidate": candidate,
                    "news_items": news_items,
                }),
            },
        ],
    }
    request = Request(
        config.news_veto_api_url,
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.news_veto_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=10) as response:  # nosec B310
        body = json.loads(response.read())
    content = body["choices"][0]["message"]["content"]
    result = json.loads(content) if isinstance(content, str) else content
    if not isinstance(result, dict):
        raise TypeError("News veto response is not a JSON object")
    return result
