"""Backend discovery and selection for the optional tabular model.

Layer-1 collaborator: the two heavy backends - the remote Hailo service and the
local CatBoost model - arrive as injected factories, so the whole
``hailo -> CPU -> rules_only`` cascade can be exercised without a Hailo package,
a Hailo device, or a reachable inference endpoint. :mod:`src.bot_bootstrap`
keeps a thin delegator that supplies the real factories, which import their
hardware or model dependencies lazily so the single-node Raspberry Pi profile
never imports a Hailo-only package on the general bot path.

Selection order, which is the order the decision pyramid requires:

1. ``tabular_model_backend == 'hailo'`` builds the remote predictor. A backend
   that cannot be constructed is logged and replaced by ``None``; the cascade
   still runs.
2. Predictions go through :class:`src.inference_fallback.TabularInferenceFallback`,
   which validates every result and falls back to the local CPU model.
3. When the CPU model is also unavailable the cascade returns a neutral
   ``rules_only`` result that preserves the rules signal instead of inventing a
   decision.

A construction error that is not one of :data:`BACKEND_ERRORS` is a programming
error and propagates, exactly as before.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

from src.inference_fallback import TabularInferenceFallback

HAILO_BACKEND = "hailo"

# Errors that mean "this backend cannot serve a prediction here". Anything else
# is a bug and must not be swallowed by the fallback cascade.
CONSTRUCTION_ERRORS: tuple[type[Exception], ...] = (
    ImportError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)
HAILO_CONSTRUCTION_ERRORS: tuple[type[Exception], ...] = CONSTRUCTION_ERRORS
CPU_CONSTRUCTION_ERRORS: tuple[type[Exception], ...] = CONSTRUCTION_ERRORS + (KeyError,)


class TabularPredictor(Protocol):
    """The injected inference interface every tabular backend implements."""

    def predict(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...


def default_hailo_factory(config: Any) -> Any:
    """Builds the remote Hailo predictor, importing the client lazily."""
    from src.predict_hailo_remote import HailoRemotePredictor

    return HailoRemotePredictor(
        service_url=config.hailo_service_url,
        api_token=config.hailo_api_token,
        timeout_seconds=config.hailo_timeout_seconds,
    )


def default_cpu_factory(config: Any) -> Callable[[], Any]:
    """Returns a thunk that loads the local CatBoost model on first use."""

    def load_cpu_predictor() -> Any:
        from src.predict_catboost import CatBoostTradingPredictor

        return CatBoostTradingPredictor(
            config.tabular_model_path,
            research_signal_path=config.tabular_research_signal_path,
        )

    return load_cpu_predictor


def build_tabular_predictor(
    logger: logging.Logger,
    config: Any,
    *,
    hailo_factory: Callable[[Any], Any] = default_hailo_factory,
    cpu_factory: Callable[[Any], Callable[[], Any]] = default_cpu_factory,
) -> TabularPredictor | None:
    """Select the tabular backend for ``config`` and return its predictor.

    Returns ``None`` when the tabular model is disabled or a non-Hailo backend
    cannot be loaded, which leaves the bot on rules-only signals.
    """
    if not config.use_tabular_model:
        return None

    if config.tabular_model_backend == HAILO_BACKEND:
        return build_hailo_cascade(logger, config, hailo_factory, cpu_factory)

    try:
        predictor = cpu_factory(config)()
        logger.info(
            f"\U0001f4ca CatBoost model loaded from '{config.tabular_model_path}'.")
        return predictor
    except CPU_CONSTRUCTION_ERRORS as exc:
        logger.warning(f"CatBoost model could not be loaded: {exc}.")
        return None


def build_hailo_cascade(
    logger: logging.Logger,
    config: Any,
    hailo_factory: Callable[[Any], Any],
    cpu_factory: Callable[[Any], Callable[[], Any]],
) -> TabularInferenceFallback:
    """Wrap the remote Hailo backend with the CPU and rules-only fallbacks."""
    try:
        hailo_predictor = hailo_factory(config)
        logger.info(
            "Tabular inference selected backend=hailo at %s",
            config.hailo_service_url,
        )
    except HAILO_CONSTRUCTION_ERRORS as exc:
        hailo_predictor = None
        logger.warning("Hailo backend unavailable; falling back to CPU: %s", exc)

    return TabularInferenceFallback(
        hailo_predictor, cpu_factory(config), logger)
