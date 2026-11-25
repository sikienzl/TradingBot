"""Validated tabular inference fallback from remote Hailo to local CPU."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import Any


class TabularInferenceFallback:
    def __init__(
        self,
        primary: Any | None,
        cpu_factory: Callable[[], Any],
        logger: logging.Logger,
    ) -> None:
        self.primary = primary
        self.cpu_factory = cpu_factory
        self.logger = logger
        self.cpu_predictor: Any | None = None
        self.cpu_loaded = False

    def predict(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if self.primary is not None:
            try:
                result = self._validated(self.primary.predict(*args, **kwargs))
                return {**result, "backend": "hailo"}
            except (ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
                self.logger.warning(
                    "Hailo inference failed or returned an invalid result; falling back to CPU: %s",
                    exc,
                )

        try:
            if not self.cpu_loaded:
                self.cpu_predictor = self.cpu_factory()
                self.cpu_loaded = True
            if self.cpu_predictor is None:
                raise RuntimeError("CPU tabular model is unavailable")
            result = self._validated(self.cpu_predictor.predict(*args, **kwargs))
            self.logger.info("Tabular inference selected backend=cpu")
            return {**result, "backend": "cpu"}
        except (ImportError, OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            self.logger.warning(
                "CPU inference unavailable; continuing with rules only: %s", exc)
            return {
                "decision": "halten",
                "confidence": 0.0,
                "proba": {},
                "backend": "rules_only",
            }

    @staticmethod
    def _validated(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise TypeError("Tabular prediction must be an object")
        if result.get("decision") not in {"verkaufen", "halten", "kaufen"}:
            raise ValueError("Tabular prediction has an invalid decision")
        try:
            confidence = float(result["confidence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Tabular prediction has invalid confidence") from exc
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("Tabular prediction has invalid confidence")
        probabilities = result.get("proba", {})
        if not isinstance(probabilities, dict):
            raise TypeError("Tabular prediction probabilities must be an object")
        return {**result, "confidence": confidence, "proba": probabilities}