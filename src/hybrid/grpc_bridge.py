from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from src.hybrid.transport import (
    EdgeAlertPayload,
    MarketSnapshotPayload,
    StrategistDecisionPayload,
    TransportAck,
)

try:
    import grpc
except ImportError:  # pragma: no cover - exercised only on minimal installs
    grpc = None


EDGE_FILTER_SERVICE = "trading.hybrid.EdgeFilterService"
STRATEGIST_SERVICE = "trading.hybrid.StrategistService"
SUBMIT_MARKET_SNAPSHOT = "/trading.hybrid.EdgeFilterService/SubmitMarketSnapshot"
PROCESS_ANOMALY_ALERT = "/trading.hybrid.StrategistService/ProcessAnomalyAlert"

T = TypeVar("T")


def grpc_available() -> bool:
    return grpc is not None


def ensure_grpc_available() -> None:
    if grpc is None:
        raise RuntimeError(
            "grpcio is required for hybrid cluster transport. "
            "Install the package with the k3s or hybrid extras."
        )


def _json_dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _json_loads(payload: bytes) -> dict[str, Any]:
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8"))


def serialize_message(message: Any) -> bytes:
    return _json_dumps(message.to_dict())


def deserialize_message(payload: bytes, factory: Callable[[dict[str, Any]], T]) -> T:
    return factory(_json_loads(payload))


class EdgeFilterRpcServer:
    def __init__(
        self,
        processor: Callable[[MarketSnapshotPayload], Awaitable[TransportAck]],
        host: str = "0.0.0.0",
        port: int = 50051,
    ):
        ensure_grpc_available()
        self.processor = processor
        self.host = host
        self.port = port
        self.server = grpc.aio.server()
        handler = grpc.method_handlers_generic_handler(
            EDGE_FILTER_SERVICE,
            {
                "SubmitMarketSnapshot": grpc.unary_unary_rpc_method_handler(
                    self._submit_market_snapshot,
                    request_deserializer=lambda payload: deserialize_message(
                        payload, MarketSnapshotPayload.from_dict
                    ),
                    response_serializer=serialize_message,
                )
            },
        )
        self.server.add_generic_rpc_handlers((handler,))
        self.server.add_insecure_port(f"{self.host}:{self.port}")

    async def _submit_market_snapshot(
        self,
        request: MarketSnapshotPayload,
        _context: Any,
    ) -> TransportAck:
        return await self.processor(request)

    async def start(self) -> None:
        await self.server.start()

    async def wait_for_termination(self) -> None:
        await self.server.wait_for_termination()

    async def stop(self, grace: float = 5.0) -> None:
        await self.server.stop(grace)


class StrategistRpcServer:
    def __init__(
        self,
        processor: Callable[[EdgeAlertPayload], Awaitable[StrategistDecisionPayload]],
        host: str = "0.0.0.0",
        port: int = 50052,
    ):
        ensure_grpc_available()
        self.processor = processor
        self.host = host
        self.port = port
        self.server = grpc.aio.server()
        handler = grpc.method_handlers_generic_handler(
            STRATEGIST_SERVICE,
            {
                "ProcessAnomalyAlert": grpc.unary_unary_rpc_method_handler(
                    self._process_anomaly_alert,
                    request_deserializer=lambda payload: deserialize_message(
                        payload, EdgeAlertPayload.from_dict
                    ),
                    response_serializer=serialize_message,
                )
            },
        )
        self.server.add_generic_rpc_handlers((handler,))
        self.server.add_insecure_port(f"{self.host}:{self.port}")

    async def _process_anomaly_alert(
        self,
        request: EdgeAlertPayload,
        _context: Any,
    ) -> StrategistDecisionPayload:
        return await self.processor(request)

    async def start(self) -> None:
        await self.server.start()

    async def wait_for_termination(self) -> None:
        await self.server.wait_for_termination()

    async def stop(self, grace: float = 5.0) -> None:
        await self.server.stop(grace)


class EdgeFilterRpcClient:
    def __init__(self, target: str):
        ensure_grpc_available()
        self.target = target

    async def submit_market_snapshot(
        self,
        snapshot: MarketSnapshotPayload,
        timeout: float = 5.0,
    ) -> TransportAck:
        async with grpc.aio.insecure_channel(self.target) as channel:
            method = channel.unary_unary(
                SUBMIT_MARKET_SNAPSHOT,
                request_serializer=serialize_message,
                response_deserializer=lambda payload: deserialize_message(
                    payload, TransportAck.from_dict
                ),
            )
            return await method(snapshot, timeout=timeout)


class StrategistRpcClient:
    def __init__(self, target: str):
        ensure_grpc_available()
        self.target = target

    async def process_anomaly_alert(
        self,
        alert: EdgeAlertPayload,
        timeout: float = 10.0,
    ) -> StrategistDecisionPayload:
        async with grpc.aio.insecure_channel(self.target) as channel:
            method = channel.unary_unary(
                PROCESS_ANOMALY_ALERT,
                request_serializer=serialize_message,
                response_deserializer=lambda payload: deserialize_message(
                    payload, StrategistDecisionPayload.from_dict
                ),
            )
            return await method(alert, timeout=timeout)
