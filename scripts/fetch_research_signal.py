#!/usr/bin/env python3
"""Fetch the Crypto Fear & Greed Index and write it as a canonical
research_signal_latest.json consumed by train_catboost_model.py and
predict_catboost.py.

Endpoint (no API key required):
    https://api.alternative.me/fng/?limit=1

Usage:
    python3 scripts/fetch_research_signal.py \
        --output data/research_signal_latest.json
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

FNG_URL = "https://api.alternative.me/fng/?limit=1"
REQUEST_TIMEOUT_SEC = 15


def _fetch_fng() -> dict:
    req = urllib.request.Request(
        FNG_URL,
        headers={"User-Agent": "TradingBot/1.0 (fetch_research_signal)"},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:  # nosec B310
        raw = resp.read()
    payload = json.loads(raw)
    data = payload.get("data", [])
    if not data:
        raise ValueError("Fear & Greed API returned empty data array")
    return data[0]


def _map_fng(entry: dict) -> dict:
    """Convert a FNG entry to the canonical research signal format."""
    raw_value = int(entry.get("value", 50))
    classification = str(entry.get("value_classification", "")).lower()
    ts_epoch = entry.get("timestamp")
    if ts_epoch:
        ts_utc = datetime.fromtimestamp(
            int(ts_epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        ts_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # sentiment_score: map 0-100 → -1.0 … +1.0
    sentiment_score = round((raw_value - 50) / 50.0, 4)

    # confidence: distance from neutral normalised to 0-1
    confidence = round(abs(raw_value - 50) / 50.0, 4)

    # risk_score: inverse of sentiment (fear = high risk)
    risk_score = round(max(0.0, (50 - raw_value) / 50.0), 4)

    # market_regime derived from classification label
    if "extreme greed" in classification or classification == "greed":
        market_regime = "bull"
    elif "extreme fear" in classification or classification == "fear":
        market_regime = "bear"
    else:
        market_regime = "sideways"

    normalized_features = {
        "research_sentiment_score": max(-1.0, min(1.0, sentiment_score)),
        "research_confidence": max(0.0, min(1.0, confidence)),
        "research_risk_score": max(0.0, min(1.0, risk_score)),
        "research_regime_bull": 1.0 if market_regime == "bull" else 0.0,
        "research_regime_bear": 1.0 if market_regime == "bear" else 0.0,
        "research_regime_sideways": 1.0 if market_regime == "sideways" else 0.0,
    }

    return {
        "timestamp_utc": ts_utc,
        "sentiment_score": sentiment_score,
        "confidence": confidence,
        "risk_score": risk_score,
        "market_regime": market_regime,
        "citations": [FNG_URL],
        "source_details": {
            "provider": "alternative.me/fng",
            "raw_value": raw_value,
            "classification": entry.get("value_classification", ""),
        },
        "normalized_features": normalized_features,
        "integration": {
            "source": "fetch_research_signal",
            "written_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Crypto Fear & Greed Index and write canonical research signal JSON."
    )
    parser.add_argument(
        "--output",
        default="data/research_signal_latest.json",
        help="Output path for the canonical research signal JSON.",
    )
    args = parser.parse_args()

    output_path = args.output
    if not os.path.isabs(output_path):
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        output_path = os.path.join(root, output_path)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    try:
        entry = _fetch_fng()
    except urllib.error.URLError as exc:
        print(
            f"ERROR: Network request to Fear & Greed API failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        print(
            f"ERROR: Failed to parse Fear & Greed API response: {exc}", file=sys.stderr)
        sys.exit(1)

    signal = _map_fng(entry)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(signal, f, indent=2)
        f.write("\n")

    print(
        f"Research signal written to {output_path} "
        f"(regime={signal['market_regime']}, "
        f"sentiment={signal['sentiment_score']}, "
        f"raw_fng={signal['source_details']['raw_value']})"
    )


if __name__ == "__main__":
    main()
