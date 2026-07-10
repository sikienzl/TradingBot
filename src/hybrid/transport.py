from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.hybrid.decision_gate import AnomalyAlert


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(slots=True)
class MarketTickPayload:
    timestamp: float
    symbol: str
    bid: float
    ask: float
    mid: float
    bid_size: float
    ask_size: float
    last_trade_price: float
    last_trade_size: float

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MarketTickPayload":
        return cls(
            timestamp=float(payload.get("timestamp", 0.0)),
            symbol=str(payload.get("symbol", "")),
            bid=float(payload.get("bid", 0.0)),
            ask=float(payload.get("ask", 0.0)),
            mid=float(payload.get("mid", 0.0)),
            bid_size=float(payload.get("bid_size", 0.0)),
            ask_size=float(payload.get("ask_size", 0.0)),
            last_trade_price=float(payload.get("last_trade_price", 0.0)),
            last_trade_size=float(payload.get("last_trade_size", 0.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class MarketSnapshotPayload:
    pair: str
    ticks: list[MarketTickPayload]
    source: str = "kraken-websocket"
    emitted_at: str = field(default_factory=utc_now_iso)
    node_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_tick_dicts(
        cls,
        pair: str,
        ticks: list[dict[str, Any]],
        source: str = "kraken-websocket",
        node_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "MarketSnapshotPayload":
        return cls(
            pair=pair,
            ticks=[MarketTickPayload.from_dict(tick) for tick in ticks],
            source=source,
            node_name=node_name,
            metadata=metadata or {},
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MarketSnapshotPayload":
        return cls(
            pair=str(payload.get("pair", "")),
            ticks=[
                MarketTickPayload.from_dict(item)
                for item in payload.get("ticks", [])
            ],
            source=str(payload.get("source", "kraken-websocket")),
            emitted_at=str(payload.get("emitted_at", utc_now_iso())),
            node_name=payload.get("node_name"),
            metadata=dict(payload.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "ticks": [tick.to_dict() for tick in self.ticks],
            "source": self.source,
            "emitted_at": self.emitted_at,
            "node_name": self.node_name,
            "metadata": self.metadata,
        }

    def tick_dicts(self) -> list[dict[str, Any]]:
        return [tick.to_dict() for tick in self.ticks]


@dataclass(slots=True)
class EdgeAlertPayload:
    timestamp_utc: str
    hailo_score: float
    window_size: int
    signal_type: str
    confidence: float
    market_context: dict[str, Any]
    coin: str
    source_node: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_anomaly_alert(
        cls,
        alert: AnomalyAlert,
        source_node: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "EdgeAlertPayload":
        return cls(
            timestamp_utc=alert.timestamp.astimezone(timezone.utc).isoformat(),
            hailo_score=alert.hailo_score,
            window_size=alert.window_size,
            signal_type=alert.signal_type,
            confidence=alert.confidence,
            market_context=alert.market_context,
            coin=alert.coin,
            source_node=source_node,
            metadata=metadata or {},
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EdgeAlertPayload":
        return cls(
            timestamp_utc=str(payload.get("timestamp_utc", utc_now_iso())),
            hailo_score=float(payload.get("hailo_score", 0.0)),
            window_size=int(payload.get("window_size", 0)),
            signal_type=str(payload.get("signal_type", "unknown")),
            confidence=float(payload.get("confidence", 0.0)),
            market_context=dict(payload.get("market_context", {})),
            coin=str(payload.get("coin", "")),
            source_node=payload.get("source_node"),
            metadata=dict(payload.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_anomaly_alert(self) -> AnomalyAlert:
        return AnomalyAlert(
            timestamp=_parse_timestamp(self.timestamp_utc),
            hailo_score=self.hailo_score,
            window_size=self.window_size,
            signal_type=self.signal_type,
            confidence=self.confidence,
            market_context=self.market_context,
            coin=self.coin,
        )


@dataclass(slots=True)
class StrategistDecisionPayload:
    decision: str
    reason: str
    risk_level: float
    confidence: float
    received_at: str = field(default_factory=utc_now_iso)
    position_size_pct: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StrategistDecisionPayload":
        return cls(
            decision=str(payload.get("decision", "VETO")),
            reason=str(payload.get("reason", "Unknown")),
            risk_level=float(payload.get("risk_level", 0.0)),
            confidence=float(payload.get("confidence", 0.0)),
            received_at=str(payload.get("received_at", utc_now_iso())),
            position_size_pct=float(payload.get("position_size_pct", 0.0)),
            metadata=dict(payload.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TransportAck:
    accepted: bool
    message: str
    processed_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TransportAck":
        return cls(
            accepted=bool(payload.get("accepted", False)),
            message=str(payload.get("message", "")),
            processed_at=str(payload.get("processed_at", utc_now_iso())),
            metadata=dict(payload.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
