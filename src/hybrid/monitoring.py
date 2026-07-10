from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server
    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - optional at development time
    CollectorRegistry = Counter = Gauge = Histogram = None
    PROMETHEUS_AVAILABLE = False


class NullMetric:
    def inc(self, _amount: float = 1.0) -> None:
        return

    def set(self, _value: float) -> None:
        return

    def observe(self, _value: float) -> None:
        return

    def labels(self, **_labels: str) -> "NullMetric":
        return self


class ServiceMetrics:
    def __init__(self, service_name: str, port: int | None):
        self.service_name = service_name
        self.port = port
        self.registry = CollectorRegistry() if PROMETHEUS_AVAILABLE else None
        self.enabled = PROMETHEUS_AVAILABLE and port is not None and port > 0
        self._server_started = False

        if self.registry is None:
            self.up = NullMetric()
            self.events_total = NullMetric()
            self.event_latency = NullMetric()
            self.last_value = NullMetric()
            self.build_info = NullMetric()
            return

        metric_prefix = f"trading_hybrid_{service_name}"
        self.up = Gauge(
            f"{metric_prefix}_up",
            f"Whether the {service_name} service loop is active",
            registry=self.registry,
        )
        self.events_total = Counter(
            f"{metric_prefix}_events_total",
            f"Count of processed {service_name} events",
            ["event_type"],
            registry=self.registry,
        )
        self.event_latency = Histogram(
            f"{metric_prefix}_event_latency_seconds",
            f"Latency of {service_name} event processing",
            ["event_type"],
            registry=self.registry,
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
        )
        self.last_value = Gauge(
            f"{metric_prefix}_last_value",
            f"Latest sampled value for {service_name}",
            ["name"],
            registry=self.registry,
        )
        self.build_info = Gauge(
            f"{metric_prefix}_build_info",
            f"Static build information for {service_name}",
            ["service"],
            registry=self.registry,
        )
        self.build_info.labels(service=service_name).set(1)

    def start_http_server(self) -> None:
        if not self.enabled or self._server_started:
            return
        start_http_server(self.port, addr="0.0.0.0", registry=self.registry)
        self._server_started = True
        logger.info("Started Prometheus metrics server for %s on :%s",
                    self.service_name, self.port)

    def set_up(self, value: bool) -> None:
        self.up.set(1 if value else 0)

    def observe_event(self, event_type: str, latency_seconds: float | None = None) -> None:
        self.events_total.labels(event_type=event_type).inc()
        if latency_seconds is not None:
            self.event_latency.labels(
                event_type=event_type).observe(latency_seconds)

    def set_value(self, name: str, value: float) -> None:
        self.last_value.labels(name=name).set(value)

    def publish_mapping(self, values: dict[str, Any]) -> None:
        for name, value in values.items():
            if isinstance(value, bool):
                self.set_value(name, 1.0 if value else 0.0)
            elif isinstance(value, (int, float)):
                self.set_value(name, float(value))
