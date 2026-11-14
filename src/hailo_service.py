import hmac
import os
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

from src.api_models import HailoPredictionRequest, HailoPredictionResponse
from src.hailo_runtime import HailoRuntime


def create_app(
    predictor_factory: Callable[[], object] | None = None,
    api_token: str | None = None,
) -> FastAPI:
    service_token = (
        api_token if api_token is not None else os.getenv("HAILO_API_TOKEN", "")
    ).strip()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not service_token:
            raise RuntimeError("HAILO_API_TOKEN is required to start the Hailo service")
        predictor = (
            predictor_factory() if predictor_factory is not None
            else HailoRuntime.from_environment()
        )
        if hasattr(predictor, "load"):
            predictor.load()
        app.state.hailo_predictor = predictor
        app.state.hailo_api_token = service_token
        try:
            yield
        finally:
            if hasattr(predictor, "close"):
                predictor.close()

    app = FastAPI(
        title="Trading Bot Hailo Inference",
        version="1.0.0",
        description="Authenticated remote Hailo-8 inference service for tabular trading features.",
        lifespan=lifespan,
    )

    @app.get("/health")
    def health(request: Request) -> dict[str, str]:
        predictor = getattr(request.app.state, "hailo_predictor", None)
        metadata = getattr(predictor, "metadata", None)
        model_id = getattr(metadata, "model_id", "unknown")
        return {"status": "ok", "device": "Hailo-8", "model_id": model_id}

    @app.post("/predict/hailo", response_model=HailoPredictionResponse)
    def predict_hailo(
        payload: HailoPredictionRequest,
        request: Request,
        authorization: str = Header(default=""),
    ) -> HailoPredictionResponse:
        expected = f"Bearer {request.app.state.hailo_api_token}"
        if not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Invalid bearer token")
        predictor = request.app.state.hailo_predictor
        try:
            prediction = predictor.predict(
                payload.features,
                confidence_threshold=payload.confidence_threshold,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail="Hailo inference is unavailable",
            ) from exc
        return HailoPredictionResponse(**prediction)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "src.hailo_service:app",
        host=os.getenv("HAILO_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("HAILO_PORT", "8001")),
        log_level=os.getenv("HAILO_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
