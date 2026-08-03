"""
Hailo-8 ONNX Inference Engine

Optimized inference for Time-Series-Transformer on Hailo-8 (26 TOPS).
Handles ONNX model loading, batch inference, and anomaly score calculation.
"""

import importlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

try:
    import onnxruntime as ort
    ONNX_AVAILABLE = True
except ImportError:
    ONNX_AVAILABLE = False
    logger.warning(
        "onnxruntime not installed. Inference will be unavailable until installed."
    )

hailort = None
HAILO_AVAILABLE = False
HAILO_IMPORT_SOURCE = "unavailable"

try:
    hailort = importlib.import_module("hailort")
    HAILO_AVAILABLE = True
    HAILO_IMPORT_SOURCE = "hailort"
except ImportError:
    logger.warning("hailort not installed. Trying hailo_platform runtime.")

if not HAILO_AVAILABLE:
    try:
        hailort = importlib.import_module("hailo_platform")
        HAILO_AVAILABLE = True
        HAILO_IMPORT_SOURCE = "hailo_platform"
        logger.info("Using hailo_platform compatibility runtime.")
    except ImportError:
        pass


class HailoHEFRunner:
    """Direct Hailo HEF runtime wrapper using pyhailort/hailo_platform."""

    def __init__(self, hef_path: Path):
        if hailort is None:
            raise RuntimeError("Hailo runtime modules are unavailable")
        self.hef_path = Path(hef_path)
        if not self.hef_path.exists():
            raise FileNotFoundError(f"HEF model not found: {self.hef_path}")

        self.vdevice = None
        self.network_group = None
        self.activation = None
        self.infer_pipeline = None
        self.input_name = ""
        self.output_names: list[str] = []
        self.input_shape: tuple[int, ...] = ()
        self.output_shapes: dict[str, tuple[int, ...]] = {}
        self.device_architecture = "unknown"

        self._initialize()

    def _initialize(self) -> None:
        hef = hailort.HEF(str(self.hef_path))
        input_infos = hef.get_input_vstream_infos()
        output_infos = hef.get_output_vstream_infos()
        if not input_infos or not output_infos:
            raise RuntimeError(
                f"HEF {self.hef_path} does not expose input/output vstreams")

        self.input_name = input_infos[0].name
        self.input_shape = tuple(int(dim) for dim in input_infos[0].shape)
        self.output_names = [info.name for info in output_infos]
        self.output_shapes = {
            info.name: tuple(int(dim) for dim in info.shape)
            for info in output_infos
        }

        if hasattr(hailort, "Device"):
            try:
                devices = hailort.Device.scan()
                if devices:
                    self.device_architecture = str(
                        devices[0].device_architecture)
            except Exception:
                pass

        vdevice_params = hailort.VDevice.create_params() if hasattr(
            hailort.VDevice, "create_params") else None
        self.vdevice = hailort.VDevice(
            vdevice_params) if vdevice_params is not None else hailort.VDevice()
        configure_params = hailort.ConfigureParams.create_from_hef(
            hef,
            hailort.HailoStreamInterface.PCIe,
        )
        configured_networks = self.vdevice.configure(hef, configure_params)
        if not configured_networks:
            raise RuntimeError(
                f"Unable to configure Hailo network group for {self.hef_path}")

        self.network_group = configured_networks[0]
        input_params = hailort.InputVStreamParams.make_from_network_group(
            self.network_group,
            format_type=hailort.FormatType.FLOAT32,
        )
        output_params = hailort.OutputVStreamParams.make_from_network_group(
            self.network_group,
            format_type=hailort.FormatType.FLOAT32,
        )
        self.activation = self.network_group.activate()
        self.activation.__enter__()
        self.infer_pipeline = hailort.InferVStreams(
            self.network_group,
            input_params,
            output_params,
        )
        self.infer_pipeline.__enter__()

    def infer(self, model_input: np.ndarray) -> dict[str, np.ndarray]:
        if self.infer_pipeline is None:
            raise RuntimeError("Hailo infer pipeline is not initialized")
        return self.infer_pipeline.infer({self.input_name: model_input.astype(np.float32, copy=False)})

    def close(self) -> None:
        if self.infer_pipeline is not None:
            try:
                self.infer_pipeline.__exit__(None, None, None)
            finally:
                self.infer_pipeline = None
        if self.activation is not None:
            try:
                self.activation.__exit__(None, None, None)
            finally:
                self.activation = None
        if self.vdevice is not None:
            try:
                self.vdevice.release()
            except Exception:
                pass
            self.vdevice = None

    def __del__(self):
        self.close()


class TimeSeriesTransformerONNX:
    """
    Time-Series-Transformer ONNX model for anomaly detection.

    Input: Sliding window of recent ticks (e.g., 60 ticks)
    Output: Anomaly score (0-100), confidence (0-1)

    Uses Hailo-8 accelerator if available, falls back to CPU.
    """

    def __init__(
        self,
        model_path: str,
        seq_length: int = 60,
        use_hailo: bool = True,
        device: str = "cpu",
    ):
        """
        Initialize ONNX model.

        Args:
            model_path: Path to ONNX model file
            seq_length: Input sequence length (ticks)
            use_hailo: Try to use Hailo-8 accelerator
            device: Fallback device: "cpu" or "gpu"
        """
        self.model_path = Path(model_path)
        self.model_dir = self.model_path.parent
        self.model_config_path = Path(
            os.getenv(
                "HAILO8_MODEL_CONFIG_PATH",
                str(self.model_dir / "model_config.json"),
            )
        )
        self.model_config = self._load_model_config()
        self.seq_length = int(self.model_config.get("seq_length", seq_length))
        self.device = device
        self.session = None
        self.hailo_runner: HailoHEFRunner | None = None
        self.use_hailo = use_hailo and HAILO_AVAILABLE
        self.last_inference_seconds = 0.0
        self.last_provider = "unknown"
        self.n_features = int(self.model_config.get("n_features", 9))
        self.hailo_runtime_available = HAILO_AVAILABLE
        self.hailo_runtime_source = HAILO_IMPORT_SOURCE
        self.available_providers = []
        self.hailo_execution_provider_available = False
        self.hef_path = Path(
            os.getenv(
                "HAILO8_HEF_PATH",
                str(self.model_dir / "timeseries_transformer.hef"),
            )
        )
        self.hef_runtime_available = False

        if not ONNX_AVAILABLE:
            raise ImportError(
                "onnxruntime required. Install: pip install onnxruntime")

        self._load_model()
        logger.info(
            f"TimeSeriesTransformerONNX loaded: {model_path}, "
            f"seq_length={seq_length}, device={self.device}"
        )

    def _load_model_config(self) -> dict[str, Any]:
        if not self.model_config_path.exists():
            return {}
        try:
            return json.loads(self.model_config_path.read_text())
        except Exception as exc:
            logger.warning("Failed to read model config %s: %s",
                           self.model_config_path, exc)
            return {}

    def _load_model(self):
        """Load ONNX model with appropriate backend"""
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model not found: {self.model_path}")

        force_cpu = os.getenv("HAILO8_FORCE_CPU", "false").lower() == "true"

        if self.use_hailo and not force_cpu and self.hef_path.exists():
            try:
                self.hailo_runner = HailoHEFRunner(self.hef_path)
                self.hef_runtime_available = True
                self.last_provider = "HailoHEF"
                self.device = "hailo"
                logger.info("Using direct Hailo HEF runtime: %s",
                            self.hef_path)
                return
            except Exception as exc:
                logger.warning(
                    "Failed to initialize direct Hailo HEF runtime from %s: %s. Falling back to ONNX runtime.",
                    self.hef_path,
                    exc,
                )

        self.available_providers = ort.get_available_providers()
        self.hailo_execution_provider_available = (
            "HailoExecutionProvider" in self.available_providers
        )

        # Choose execution provider based on available hardware
        providers = []

        if self.use_hailo and not force_cpu and self.hailo_execution_provider_available:
            # Hailo-8 provider (if hailort available)
            providers.append("HailoExecutionProvider")
            logger.info("Using Hailo-8 as execution provider")
        elif self.use_hailo and not self.hailo_execution_provider_available:
            providers.append("CPUExecutionProvider")
            logger.warning(
                "Hailo runtime detected but HailoExecutionProvider is unavailable in onnxruntime. Falling back to CPU."
            )
        elif force_cpu:
            providers.append("CPUExecutionProvider")
            logger.info("HAILO8_FORCE_CPU=true -> using CPUExecutionProvider")
        elif self.device == "gpu":
            # NVIDIA CUDA
            providers.extend(["CUDAExecutionProvider"])
            logger.info("Using CUDA GPU for inference")
        else:
            # CPU fallback
            providers.append("CPUExecutionProvider")
            logger.info("Using CPU for inference")

        self.session = ort.InferenceSession(
            str(self.model_path),
            providers=providers,
        )
        try:
            active_provider = self.session.get_providers()[0]
        except Exception:
            active_provider = providers[0] if providers else "unknown"
        self.last_provider = active_provider

    def get_input_shape(self) -> tuple[int, ...]:
        """Get expected input shape from ONNX model"""
        inputs = self.session.get_inputs()
        if inputs:
            return tuple(inputs[0].shape)
        if self.hailo_runner is not None:
            return (1, *self.hailo_runner.input_shape)
        return (1, self.seq_length, self.n_features)

    def get_output_names(self) -> list[str]:
        """Get output tensor names"""
        if self.hailo_runner is not None:
            return list(self.hailo_runner.output_names)
        return [output.name for output in self.session.get_outputs()]

    def preprocess_ticks(self, ticks: list[dict[str, float]]) -> np.ndarray:
        """
        Preprocess tick data for model input.

        Takes a list of tick dicts: {"bid", "ask", "mid", "bid_size", "ask_size", ...}
        Normalizes features and returns array of shape (1, seq_length, 9)

        Features: [bid, ask, mid, bid_size, ask_size, last_trade, volume, bid_volume_ratio, ask_volume_ratio]
        """
        if len(ticks) < self.seq_length:
            # Pad with first tick
            ticks = [ticks[0]] * (self.seq_length - len(ticks)) + ticks

        ticks = ticks[-self.seq_length:]  # Take last seq_length

        features = []
        for tick in ticks:
            feature_vec = np.array([
                tick.get("bid", 0),
                tick.get("ask", 0),
                tick.get("mid", 0),
                tick.get("bid_size", 0),
                tick.get("ask_size", 0),
                tick.get("last_trade_price", 0),
                tick.get("last_trade_size", 0),
                tick.get("bid_size", 0) /
                max(tick.get("ask_size", 1), 1e-6),  # Volume ratio
                tick.get("spread", tick.get("ask", 0) -
                         tick.get("bid", 0)),     # Spread
            ])
            features.append(feature_vec)

        # Stack and normalize
        X = np.array(features, dtype=np.float32)

        # Simple z-score normalization per feature
        X = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-8)

        # Add batch dimension
        X = np.expand_dims(X, axis=0)  # Shape: (1, seq_len, 9)

        return X

    def infer(self, ticks: list[dict[str, float]]) -> tuple[float, float]:
        """
        Run inference on tick sequence.

        Args:
            ticks: List of tick dicts from KrakenWebSocketV2

        Returns:
            Tuple of (anomaly_score: 0-100, confidence: 0-1)
        """
        if not ticks:
            return 0.0, 0.0

        # Preprocess
        X = self.preprocess_ticks(ticks)

        # Run inference
        try:
            started = time.perf_counter()
            if self.hailo_runner is not None:
                raw_outputs = self.hailo_runner.infer(X)
                outputs = [raw_outputs[name]
                           for name in self.hailo_runner.output_names]
                self.last_provider = "HailoHEF"
            else:
                input_name = self.session.get_inputs()[0].name
                outputs = self.session.run(None, {input_name: X})
            self.last_inference_seconds = time.perf_counter() - started

            anomaly_raw = outputs[0]
            confidence_raw = outputs[1] if len(outputs) > 1 else outputs[0]

            anomaly_score = float(np.ravel(anomaly_raw)[0]) * 100
            confidence = float(np.ravel(confidence_raw)[0])

            # Clamp to valid ranges
            anomaly_score = max(0, min(100, anomaly_score))
            confidence = max(0, min(1, confidence))

            return anomaly_score, confidence

        except Exception as e:
            logger.error(f"Inference failed: {e}")
            self.last_inference_seconds = 0.0
            return 0.0, 0.0

    def get_runtime_stats(self) -> dict[str, Any]:
        return {
            "seq_length": self.seq_length,
            "n_features": self.n_features,
            "provider": self.last_provider,
            "last_inference_seconds": self.last_inference_seconds,
            "model_path": str(self.model_path),
            "hef_path": str(self.hef_path),
            "hailo_runtime_available": self.hailo_runtime_available,
            "hailo_runtime_source": self.hailo_runtime_source,
            "hailo_execution_provider_available": self.hailo_execution_provider_available,
            "hef_runtime_available": self.hef_runtime_available,
        }

    def infer_batch(
        self, tick_batches: list[list[dict[str, float]]]
    ) -> list[tuple[float, float]]:
        """
        Run batch inference on multiple tick sequences.

        Args:
            tick_batches: List of tick sequences

        Returns:
            List of (anomaly_score, confidence) tuples
        """
        results = []

        for ticks in tick_batches:
            score, conf = self.infer(ticks)
            results.append((score, conf))

        return results


class AnomalyDetector:
    """
    High-level anomaly detection wrapper.

    Maintains a sliding window of recent ticks and triggers alerts
    when anomaly score exceeds threshold.
    """

    def __init__(
        self,
        model: TimeSeriesTransformerONNX,
        threshold: float = 85.0,
        window_size: int = 100,
    ):
        """
        Initialize detector.

        Args:
            model: TimeSeriesTransformerONNX instance
            threshold: Anomaly score threshold for alerts
            window_size: Size of sliding window to maintain
        """
        self.model = model
        self.threshold = threshold
        self.window_size = window_size
        self.tick_buffer = []  # Sliding window of ticks
        self.alerts = []

        logger.info(
            f"AnomalyDetector initialized: threshold={threshold}, "
            f"window_size={window_size}"
        )

    def update(self, tick: dict[str, float]) -> dict[str, Any] | None:
        """
        Update detector with new tick.

        Args:
            tick: New tick dict from Kraken WebSocket

        Returns:
            Alert dict if anomaly detected, None otherwise
        """
        # Add to buffer
        self.tick_buffer.append(tick)

        # Keep sliding window
        if len(self.tick_buffer) > self.window_size:
            self.tick_buffer.pop(0)

        # Infer only if we have enough data
        if len(self.tick_buffer) < self.model.seq_length:
            return None

        # Run inference
        anomaly_score, confidence = self.model.infer(self.tick_buffer)

        # Check threshold
        if anomaly_score > self.threshold:
            alert = {
                "timestamp": tick.get("timestamp"),
                "symbol": tick.get("symbol"),
                "anomaly_score": anomaly_score,
                "confidence": confidence,
                "window_size": len(self.tick_buffer),
                "tick_count": len(self.tick_buffer),
                "signal_type": self._classify_anomaly(tick),
                "inference_latency_ms": self.model.last_inference_seconds * 1000.0,
                "model_provider": self.model.last_provider,
            }
            self.alerts.append(alert)
            logger.warning(f"🚨 ANOMALY DETECTED: {alert}")
            return alert

        return None

    def _classify_anomaly(self, tick: dict[str, float]) -> str:
        """Classify type of anomaly (breakout, reversal, volatility_spike)"""
        # TODO: Implement multi-class classification
        # For now: return simple heuristic
        spread = tick.get("ask", 0) - tick.get("bid", 0)
        bid_ask_ratio = tick.get("bid_size", 0) / \
            max(tick.get("ask_size", 1e-6), 1)

        if abs(bid_ask_ratio - 1.0) > 0.5:
            return "imbalance"
        elif spread > 100:
            return "wide_spread"
        else:
            return "pattern_anomaly"

    def get_buffer_stats(self) -> dict[str, Any]:
        """Get statistics about current buffer"""
        if not self.tick_buffer:
            return {
                "buffer_size": 0,
                "min_price": None,
                "max_price": None,
                "avg_price": None,
                "volatility": None,
            }

        mids = [t.get("mid", 0) for t in self.tick_buffer]
        return {
            "buffer_size": len(self.tick_buffer),
            "min_price": min(mids),
            "max_price": max(mids),
            "avg_price": sum(mids) / len(mids),
            "volatility": np.std(mids),
        }


def example_inference():
    """Example: Load model and run inference on dummy ticks"""

    # Create dummy ticks (would come from Kraken WebSocket in practice)
    dummy_ticks = [
        {
            "bid": 45000 + i * 10,
            "ask": 45020 + i * 10,
            "mid": 45010 + i * 10,
            "bid_size": 1.5,
            "ask_size": 2.0,
            "last_trade_price": 45010 + i * 10,
            "last_trade_size": 0.5,
            "timestamp": 1720000000 + i,
        }
        for i in range(100)
    ]

    # For demonstration, we'd load a real model. Using placeholder:
    model_path = os.getenv(
        "HAILO8_MODEL_PATH", "/models/hailo/timeseries_transformer.onnx"
    )

    if not Path(model_path).exists():
        logger.warning(f"Model not found at {model_path}. Skipping demo.")
        return

    model = TimeSeriesTransformerONNX(model_path)
    detector = AnomalyDetector(model, threshold=85.0)

    # Process ticks
    for tick in dummy_ticks:
        alert = detector.update(tick)
        if alert:
            logger.info(f"Alert triggered: {alert}")

    logger.info(f"Buffer stats: {detector.get_buffer_stats()}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    example_inference()
