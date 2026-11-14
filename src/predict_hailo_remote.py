import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

import pandas as pd

from src.api_models import HailoPredictionResponse
from src.hailo_contract import HAILO_FEATURES


class HailoInferenceError(RuntimeError):
    """Raised when the remote Hailo service cannot provide a valid prediction."""


class HailoRemotePredictor:
    def __init__(
        self,
        service_url: str,
        api_token: str,
        timeout_seconds: float = 3.0,
    ):
        parsed_url = urlsplit(service_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("HAILO_SERVICE_URL must be an absolute HTTP(S) URL")
        if parsed_url.username or parsed_url.password:
            raise ValueError("HAILO_SERVICE_URL must not contain credentials")
        if parsed_url.query or parsed_url.fragment:
            raise ValueError("HAILO_SERVICE_URL must not contain a query or fragment")
        if not api_token.strip():
            raise ValueError("HAILO_API_TOKEN is required when the Hailo backend is enabled")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("HAILO_TIMEOUT_SECONDS must be a positive finite number")
        self.endpoint = urljoin(service_url.rstrip("/") + "/", "predict/hailo")
        self.api_token = api_token.strip()
        self.timeout_seconds = timeout_seconds

    def predict(
        self,
        row_df: pd.DataFrame,
        confidence_threshold: float | None = None,
    ) -> dict:
        if row_df.empty:
            raise ValueError("Hailo prediction requires exactly one feature row")
        if len(row_df) != 1:
            raise ValueError("Hailo prediction accepts exactly one feature row")

        row = row_df.iloc[0]
        features: dict[str, float] = {}
        for name in HAILO_FEATURES:
            if name not in row_df.columns or pd.isna(row[name]):
                continue
            value = float(row[name])
            if math.isfinite(value):
                features[name] = value
        payload = {"features": features}
        if confidence_threshold is not None:
            payload["confidence_threshold"] = float(confidence_threshold)
        request = Request(
            self.endpoint,
            data=json.dumps(payload, allow_nan=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read(64 * 1024)
        except HTTPError as exc:
            raise HailoInferenceError(
                f"Hailo service returned HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise HailoInferenceError("Hailo service request failed") from exc

        try:
            result = HailoPredictionResponse.model_validate(json.loads(body))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise HailoInferenceError("Hailo service returned an invalid response") from exc
        return {
            "decision": result.decision.value,
            "confidence": result.confidence,
            "proba": result.proba,
            "threshold_used": result.threshold_used,
            "margin": result.margin,
        }
