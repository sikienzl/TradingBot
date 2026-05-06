#!/usr/bin/env python3
"""Fetch a modular research signal and write canonical JSON.

Providers are modular and can be enabled/disabled independently:
- Fear & Greed Index (fng)
- RSS news sentiment (news)

Output remains backward-compatible with the existing
research_signal_latest.json format consumed by train/predict.
"""

import argparse
import email.utils
import json
import os
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

FNG_URL = "https://api.alternative.me/fng/?limit=1"
REQUEST_TIMEOUT_SEC = 15

DEFAULT_NEWS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
]

POSITIVE_NEWS_TERMS = {
    "etf approved",
    "adoption",
    "partnership",
    "institutional inflow",
    "record high",
    "bullish",
    "upgrade",
    "breakout",
    "surge",
    "growth",
    "expands",
    "inflows",
}

NEGATIVE_NEWS_TERMS = {
    "hack",
    "exploit",
    "lawsuit",
    "ban",
    "bankruptcy",
    "liquidation",
    "outflow",
    "bearish",
    "crash",
    "plunge",
    "investigation",
    "fine",
    "fraud",
    "security breach",
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _env_csv(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_iso_utc(value: str) -> Optional[datetime]:
    if not value:
        return None
    txt = value.strip()
    if not txt:
        return None
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_any_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    txt = value.strip()
    if not txt:
        return None
    dt = _parse_iso_utc(txt)
    if dt is not None:
        return dt
    try:
        parsed = email.utils.parsedate_to_datetime(txt)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_market_regime(sentiment_score: float) -> str:
    if sentiment_score >= 0.15:
        return "bull"
    if sentiment_score <= -0.15:
        return "bear"
    return "sideways"


def _headline_sentiment(title: str) -> Tuple[float, int, int]:
    text = (title or "").lower()
    pos_hits = sum(1 for term in POSITIVE_NEWS_TERMS if term in text)
    neg_hits = sum(1 for term in NEGATIVE_NEWS_TERMS if term in text)
    raw = (pos_hits - neg_hits) / 3.0
    return _clamp(raw, -1.0, 1.0), pos_hits, neg_hits


def _fetch_json(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "TradingBot/1.0 (fetch_research_signal)"},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:  # nosec B310
        raw = resp.read()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON payload at {url} is not an object")
    return payload


def _fetch_fng_signal() -> Dict[str, Any]:
    payload = _fetch_json(FNG_URL)
    data = payload.get("data", [])
    if not data:
        raise ValueError("Fear & Greed API returned empty data array")
    entry = data[0]

    raw_value = int(entry.get("value", 50))
    classification = str(entry.get("value_classification", "")).lower()
    ts_epoch = entry.get("timestamp")
    if ts_epoch:
        ts_utc = datetime.fromtimestamp(
            int(ts_epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        ts_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    sentiment_score = _clamp((raw_value - 50) / 50.0, -1.0, 1.0)
    confidence = _clamp(abs(raw_value - 50) / 50.0, 0.0, 1.0)
    risk_score = _clamp((50 - raw_value) / 50.0, 0.0, 1.0)

    if "extreme greed" in classification or classification == "greed":
        market_regime = "bull"
    elif "extreme fear" in classification or classification == "fear":
        market_regime = "bear"
    else:
        market_regime = "sideways"

    return {
        "provider": "fng",
        "timestamp_utc": ts_utc,
        "sentiment_score": round(sentiment_score, 4),
        "confidence": round(confidence, 4),
        "risk_score": round(risk_score, 4),
        "market_regime": market_regime,
        "citations": [FNG_URL],
        "source_details": {
            "provider": "alternative.me/fng",
            "raw_value": raw_value,
            "classification": entry.get("value_classification", ""),
        },
    }


def _extract_rss_items(xml_bytes: bytes) -> List[Dict[str, str]]:
    root = ET.fromstring(xml_bytes)
    items: List[Dict[str, str]] = []

    # RSS format
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        if title:
            items.append({"title": title, "link": link, "published": published})

    # Atom format fallback
    if not items:
        atom_entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for entry in atom_entries:
            title = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            published = (
                entry.findtext("{http://www.w3.org/2005/Atom}updated")
                or entry.findtext("{http://www.w3.org/2005/Atom}published")
                or ""
            ).strip()
            link = ""
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            if link_el is not None:
                link = (link_el.attrib.get("href") or "").strip()
            if title:
                items.append({"title": title, "link": link, "published": published})
    return items


def _fetch_news_signal(
    feeds: List[str],
    lookback_hours: int,
    max_items_per_feed: int,
) -> Dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=max(1, lookback_hours))

    scored: List[float] = []
    citations: List[str] = []
    recent_titles: List[str] = []
    positive_hits = 0
    negative_hits = 0

    for url in feeds:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "TradingBot/1.0 (fetch_research_signal)"},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:  # nosec B310
            raw = resp.read()
        items = _extract_rss_items(raw)

        used = 0
        for item in items:
            if used >= max_items_per_feed:
                break
            published = _parse_any_datetime(item.get("published", ""))
            if published is not None and published < cutoff:
                continue

            title = item.get("title", "")
            score, pos_hits, neg_hits = _headline_sentiment(title)
            scored.append(score)
            positive_hits += pos_hits
            negative_hits += neg_hits
            used += 1

            link = item.get("link", "")
            if link:
                citations.append(link)
            if len(recent_titles) < 12:
                recent_titles.append(title)

    if not scored:
        raise ValueError("No recent RSS news items available for sentiment scoring")

    sentiment_score = _clamp(sum(scored) / len(scored), -1.0, 1.0)
    signal_strength = min(1.0, abs(sentiment_score) * 1.8)
    coverage = min(1.0, len(scored) / 30.0)
    confidence = _clamp(0.35 * signal_strength + 0.65 * coverage, 0.0, 1.0)
    risk_score = _clamp(0.5 - 0.5 * sentiment_score, 0.0, 1.0)

    return {
        "provider": "news",
        "timestamp_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "sentiment_score": round(sentiment_score, 4),
        "confidence": round(confidence, 4),
        "risk_score": round(risk_score, 4),
        "market_regime": _to_market_regime(sentiment_score),
        "citations": citations[:25],
        "source_details": {
            "provider": "rss_news",
            "feeds": feeds,
            "lookback_hours": lookback_hours,
            "items_scored": len(scored),
            "positive_term_hits": positive_hits,
            "negative_term_hits": negative_hits,
            "recent_titles": recent_titles,
        },
    }


def _combine_provider_signals(
    signals: List[Dict[str, Any]],
    provider_weights: Dict[str, float],
) -> Dict[str, Any]:
    if not signals:
        raise ValueError("No provider signals available")

    weighted_sent = 0.0
    weighted_conf = 0.0
    weighted_risk = 0.0
    weight_total = 0.0
    citations: List[str] = []
    details: Dict[str, Any] = {
        "providers": {},
        "provider_weights": provider_weights,
    }

    for signal in signals:
        provider = str(signal.get("provider", "unknown"))
        raw_weight = float(provider_weights.get(provider, 1.0))
        confidence = _clamp(float(signal.get("confidence", 0.0)), 0.0, 1.0)
        effective_weight = max(0.0, raw_weight) * max(0.2, confidence)
        if effective_weight <= 0:
            continue

        sent = _clamp(float(signal.get("sentiment_score", 0.0)), -1.0, 1.0)
        risk = _clamp(float(signal.get("risk_score", 0.0)), 0.0, 1.0)
        weighted_sent += sent * effective_weight
        weighted_conf += confidence * effective_weight
        weighted_risk += risk * effective_weight
        weight_total += effective_weight

        details["providers"][provider] = {
            "effective_weight": round(effective_weight, 4),
            "raw_weight": round(raw_weight, 4),
            "sentiment_score": round(sent, 4),
            "confidence": round(confidence, 4),
            "risk_score": round(risk, 4),
            "market_regime": signal.get("market_regime", "sideways"),
            "source_details": signal.get("source_details", {}),
        }

        for citation in signal.get("citations", []):
            if citation and citation not in citations:
                citations.append(citation)

    if weight_total <= 0:
        raise ValueError("Provider weights collapsed to zero")

    sentiment_score = _clamp(weighted_sent / weight_total, -1.0, 1.0)
    confidence = _clamp(weighted_conf / weight_total, 0.0, 1.0)
    risk_score = _clamp(weighted_risk / weight_total, 0.0, 1.0)
    market_regime = _to_market_regime(sentiment_score)

    normalized_features = {
        "research_sentiment_score": round(sentiment_score, 4),
        "research_confidence": round(confidence, 4),
        "research_risk_score": round(risk_score, 4),
        "research_regime_bull": 1.0 if market_regime == "bull" else 0.0,
        "research_regime_bear": 1.0 if market_regime == "bear" else 0.0,
        "research_regime_sideways": 1.0 if market_regime == "sideways" else 0.0,
    }

    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sentiment_score": normalized_features["research_sentiment_score"],
        "confidence": normalized_features["research_confidence"],
        "risk_score": normalized_features["research_risk_score"],
        "market_regime": market_regime,
        "citations": citations[:50],
        "source_details": details,
        "normalized_features": normalized_features,
        "integration": {
            "source": "fetch_research_signal",
            "written_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch modular research signal (FNG + optional RSS news) and write canonical JSON."
    )
    parser.add_argument(
        "--output",
        default="data/research_signal_latest.json",
        help="Output path for the canonical research signal JSON.",
    )
    parser.add_argument(
        "--providers",
        default=os.getenv("RESEARCH_PROVIDERS", "fng"),
        help="Comma-separated provider list: fng,news",
    )
    parser.add_argument(
        "--news-feeds",
        default=os.getenv("RESEARCH_NEWS_FEEDS", ",".join(DEFAULT_NEWS_FEEDS)),
        help="Comma-separated RSS feed URLs for news provider.",
    )
    parser.add_argument(
        "--news-lookback-hours",
        type=int,
        default=int(os.getenv("RESEARCH_NEWS_LOOKBACK_HOURS", "24")),
        help="Consider only news newer than this lookback window.",
    )
    parser.add_argument(
        "--news-max-items-per-feed",
        type=int,
        default=int(os.getenv("RESEARCH_NEWS_MAX_ITEMS_PER_FEED", "30")),
        help="Max recent RSS items scored per feed.",
    )
    parser.add_argument(
        "--fng-weight",
        type=float,
        default=float(os.getenv("RESEARCH_FNG_WEIGHT", "1.0")),
        help="Weight for Fear & Greed provider in final blend.",
    )
    parser.add_argument(
        "--news-weight",
        type=float,
        default=float(os.getenv("RESEARCH_NEWS_WEIGHT", "0.6")),
        help="Weight for RSS news provider in final blend.",
    )
    args = parser.parse_args()

    output_path = args.output
    if not os.path.isabs(output_path):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        output_path = os.path.join(root, output_path)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    providers = [p.strip().lower() for p in args.providers.split(",") if p.strip()]
    if not providers:
        print("ERROR: No providers configured. Use --providers fng,news", file=sys.stderr)
        sys.exit(1)

    news_feeds = [u.strip() for u in args.news_feeds.split(",") if u.strip()]
    if not news_feeds:
        news_feeds = DEFAULT_NEWS_FEEDS

    provider_weights = {
        "fng": max(0.0, args.fng_weight),
        "news": max(0.0, args.news_weight),
    }

    provider_signals: List[Dict[str, Any]] = []
    provider_errors: Dict[str, str] = {}

    for provider in providers:
        try:
            if provider == "fng":
                provider_signals.append(_fetch_fng_signal())
            elif provider == "news":
                provider_signals.append(
                    _fetch_news_signal(
                        feeds=news_feeds,
                        lookback_hours=max(1, args.news_lookback_hours),
                        max_items_per_feed=max(1, args.news_max_items_per_feed),
                    )
                )
            else:
                provider_errors[provider] = "unknown_provider"
        except urllib.error.URLError as exc:
            provider_errors[provider] = f"network_error: {exc}"
        except (ValueError, KeyError, json.JSONDecodeError, ET.ParseError) as exc:
            provider_errors[provider] = f"parse_error: {exc}"

    if not provider_signals:
        print("ERROR: All configured providers failed", file=sys.stderr)
        for provider, reason in provider_errors.items():
            print(f"  - {provider}: {reason}", file=sys.stderr)
        sys.exit(1)

    signal = _combine_provider_signals(provider_signals, provider_weights)
    if provider_errors:
        signal.setdefault("source_details", {})["provider_errors"] = provider_errors

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(signal, f, indent=2)
        f.write("\n")

    active = ",".join([p.get("provider", "?") for p in provider_signals])
    print(
        f"Research signal written to {output_path} "
        f"(providers={active}, regime={signal['market_regime']}, "
        f"sentiment={signal['sentiment_score']}, "
        f"confidence={signal['confidence']})"
    )


if __name__ == "__main__":
    main()
