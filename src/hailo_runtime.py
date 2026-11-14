import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import numpy as np

from src.hailo_contract import (
    HAILO_FEATURES,
    HAILO_INPUT_NAME,
    HAILO_LABEL_MAP,
    HAILO_OUTPUT_NAME,
)


@dataclass(frozen=True)
class HailoModelMetadata:
    model_id: str
    means: np.ndarray
    scales: np.ndarray
    confidence_threshold: float
    margin_threshold: float

    @classmethod
    def load(cls, path: str | Path) -> "HailoModelMetadata":
        try:
            with open(path, encoding="utf-8") as metadata_file:
                raw = json.load(metadata_file)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read Hailo model metadata: {path}") from exc
        if not isinstance(raw, dict):
            raise TypeError("Hailo model metadata must be a JSON object")
        if raw.get("features") != list(HAILO_FEATURES):
            raise ValueError("Hailo metadata feature order does not match the runtime contract")
        if raw.get("label_map") != HAILO_LABEL_MAP:
            raise ValueError("Hailo metadata labels do not match the runtime contract")
        if raw.get("input_name") != HAILO_INPUT_NAME or raw.get("output_name") != HAILO_OUTPUT_NAME:
            raise ValueError("Hailo metadata tensor names do not match the runtime contract")
        if raw.get("output_kind") != "logits":
            raise ValueError("Hailo model must output three class logits")

        means = np.asarray(raw.get("means"), dtype=np.float64)
        scales = np.asarray(raw.get("scales"), dtype=np.float64)
        if means.shape != (len(HAILO_FEATURES),) or scales.shape != means.shape:
            raise ValueError("Hailo normalization arrays have an invalid shape")
        if not np.isfinite(means).all() or not np.isfinite(scales).all() or (scales <= 0).any():
            raise ValueError("Hailo normalization values must be finite and scales positive")

        threshold = raw.get("confidence_threshold")
        margin = raw.get("margin_threshold")
        if (
            not isinstance(threshold, (int, float))
            or not math.isfinite(threshold)
            or not 0 <= threshold <= 1
        ):
            raise ValueError("Hailo confidence_threshold must be between zero and one")
        if not isinstance(margin, (int, float)) or not math.isfinite(margin) or margin < 0:
            raise ValueError("Hailo margin_threshold must be finite and non-negative")
        model_id = raw.get("model_id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("Hailo metadata requires a non-empty model_id")
        return cls(
            model_id=model_id,
            means=means,
            scales=scales,
            confidence_threshold=float(threshold),
            margin_threshold=float(margin),
        )


def prediction_from_logits(
    logits: np.ndarray,
    metadata: HailoModelMetadata,
    confidence_threshold: float | None = None,
) -> dict:
    values = np.asarray(logits, dtype=np.float64).reshape(-1)
    if values.shape != (len(HAILO_LABEL_MAP),) or not np.isfinite(values).all():
        raise ValueError("Hailo inference must return three finite logits")
    values -= values.max()
    probabilities = np.exp(values)
    probabilities /= probabilities.sum()
    order = np.argsort(probabilities)
    confidence = float(probabilities[order[-1]])
    margin = float(probabilities[order[-1]] - probabilities[order[-2]])
    threshold = (
        metadata.confidence_threshold
        if confidence_threshold is None
        else float(confidence_threshold)
    )
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("confidence_threshold must be between zero and one")
    decision = tuple(HAILO_LABEL_MAP)[int(order[-1])]
    if confidence < threshold or margin < metadata.margin_threshold:
        decision = "halten"
    probabilities_by_label = {
        label: float(probabilities[index])
        for label, index in HAILO_LABEL_MAP.items()
    }
    return {
        "model_id": metadata.model_id,
        "decision": decision,
        "confidence": confidence,
        "proba": probabilities_by_label,
        "threshold_used": threshold,
        "margin": margin,
    }


class HailoRuntime:
    """Loads one fixed-contract trading HEF and performs serialized inference."""

    def __init__(self, hef_path: str, metadata_path: str, device_id: str = ""):
        self.hef_path = hef_path
        self.metadata_path = metadata_path
        self.device_id = device_id.strip()
        self.metadata: HailoModelMetadata | None = None
        self._vdevice = None
        self._activation = None
        self._infer_pipeline = None
        self._input_name = ""
        self._output_name = ""
        self._input_shape: tuple[int, ...] = ()
        self._lock = Lock()

    @classmethod
    def from_environment(cls) -> "HailoRuntime":
        hef_path = os.getenv("HAILO_HEF_PATH", "").strip()
        metadata_path = os.getenv("HAILO_METADATA_PATH", "").strip()
        if not hef_path or not metadata_path:
            raise RuntimeError("HAILO_HEF_PATH and HAILO_METADATA_PATH are required")
        return cls(
            hef_path,
            metadata_path,
            device_id=os.getenv("HAILO_DEVICE_ID", ""),
        )

    def load(self) -> None:
        if not Path(self.hef_path).is_file():
            raise FileNotFoundError(f"Hailo HEF file not found: {self.hef_path}")
        metadata = HailoModelMetadata.load(self.metadata_path)
        try:
            from hailo_platform import (
                HEF,
                ConfigureParams,
                FormatType,
                HailoStreamInterface,
                InferVStreams,
                InputVStreamParams,
                OutputVStreamParams,
                VDevice,
            )
        except ImportError as exc:
            raise RuntimeError(
                "HailoRT Python bindings are required on the inference host"
            ) from exc

        hef = HEF(self.hef_path)
        input_infos = hef.get_input_vstream_infos()
        output_infos = hef.get_output_vstream_infos()
        if len(input_infos) != 1 or len(output_infos) != 1:
            raise ValueError("Trading HEF must expose exactly one input and one output tensor")
        input_shape = tuple(int(dimension) for dimension in input_infos[0].shape)
        output_shape = tuple(int(dimension) for dimension in output_infos[0].shape)
        if math.prod(input_shape) != len(HAILO_FEATURES):
            raise ValueError(
                f"Trading HEF input must contain {len(HAILO_FEATURES)} values; got {input_shape}"
            )
        if math.prod(output_shape) != len(HAILO_LABEL_MAP):
            raise ValueError(
                f"Trading HEF output must contain {len(HAILO_LABEL_MAP)} logits; got {output_shape}"
            )

        self.metadata = metadata
        self._input_name = input_infos[0].name
        self._output_name = output_infos[0].name
        self._input_shape = input_shape
        self._vdevice = (
            VDevice(device_ids=[self.device_id]) if self.device_id else VDevice()
        )
        loaded = False
        try:
            configure_params = ConfigureParams.create_from_hef(
                hef, HailoStreamInterface.PCIe
            )
            network_groups = self._vdevice.configure(hef, configure_params)
            if len(network_groups) != 1:
                raise ValueError("Trading HEF must configure exactly one network group")
            network_group = network_groups[0]
            activation = network_group.activate(network_group.create_params())
            activation.__enter__()
            self._activation = activation
            input_params = InputVStreamParams.make_from_network_group(
                network_group, format_type=FormatType.FLOAT32
            )
            output_params = OutputVStreamParams.make_from_network_group(
                network_group, format_type=FormatType.FLOAT32
            )
            infer_pipeline = InferVStreams(
                network_group, input_params, output_params
            )
            infer_pipeline.__enter__()
            self._infer_pipeline = infer_pipeline
            loaded = True
        finally:
            if not loaded:
                self.close()

    def predict(
        self,
        features: dict[str, float],
        confidence_threshold: float | None = None,
    ) -> dict:
        if self.metadata is None or self._infer_pipeline is None:
            raise RuntimeError("Hailo runtime is not loaded")
        raw_values = np.asarray(
            [float(features.get(name, 0.0)) for name in HAILO_FEATURES],
            dtype=np.float64,
        )
        if not np.isfinite(raw_values).all():
            raise ValueError("Hailo input features must be finite numbers")
        normalized = ((raw_values - self.metadata.means) / self.metadata.scales)
        input_tensor = normalized.astype(np.float32).reshape(self._input_shape)
        with self._lock:
            outputs = self._infer_pipeline.infer({
                self._input_name: input_tensor,
            })
        if self._output_name not in outputs:
            raise RuntimeError("Hailo inference did not return the configured output tensor")
        return prediction_from_logits(
            outputs[self._output_name],
            self.metadata,
            confidence_threshold=confidence_threshold,
        )

    def close(self) -> None:
        try:
            if self._infer_pipeline is not None:
                self._infer_pipeline.__exit__(None, None, None)
        finally:
            self._infer_pipeline = None
            try:
                if self._activation is not None:
                    self._activation.__exit__(None, None, None)
            finally:
                self._activation = None
                try:
                    if self._vdevice is not None:
                        self._vdevice.release()
                finally:
                    self._vdevice = None
