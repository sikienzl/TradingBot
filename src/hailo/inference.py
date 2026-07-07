"""
Hailo-8 ONNX Inference Engine

Optimized inference for Time-Series-Transformer on Hailo-8 (26 TOPS).
Handles ONNX model loading, batch inference, and anomaly score calculation.
"""

import os
import logging
from typing import List, Tuple, Optional, Dict, Any
from pathlib import Path
import numpy as np

try:
    import onnxruntime as ort
    ONNX_AVAILABLE = True
except ImportError:
    ONNX_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning(
        "onnxruntime not installed. Hailo inference will be unavailable.")

try:
    import hailort
    HAILO_AVAILABLE = True
except ImportError:
    HAILO_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("hailort not installed. Using ONNX CPU fallback.")


logger = logging.getLogger(__name__)


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
        self.seq_length = seq_length
        self.device = device
        self.session = None
        self.use_hailo = use_hailo and HAILO_AVAILABLE

        if not ONNX_AVAILABLE:
            raise ImportError(
                "onnxruntime required. Install: pip install onnxruntime")

        self._load_model()
        logger.info(
            f"TimeSeriesTransformerONNX loaded: {model_path}, "
            f"seq_length={seq_length}, device={self.device}"
        )

    def _load_model(self):
        """Load ONNX model with appropriate backend"""
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model not found: {self.model_path}")

        # Choose execution provider based on available hardware
        providers = []

        if self.use_hailo:
            # Hailo-8 provider (if hailort available)
            providers.append("HailoExecutionProvider")
            logger.info("Using Hailo-8 as execution provider")
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

    def get_input_shape(self) -> Tuple[int, ...]:
        """Get expected input shape from ONNX model"""
        inputs = self.session.get_inputs()
        if inputs:
            return tuple(inputs[0].shape)
        return (1, self.seq_length, 9)  # Default: (batch, seq_len, features)

    def get_output_names(self) -> List[str]:
        """Get output tensor names"""
        return [output.name for output in self.session.get_outputs()]

    def preprocess_ticks(self, ticks: List[Dict[str, float]]) -> np.ndarray:
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
        X = np.array(features, dtype=np.float32)  # Shape: (seq_len, 9)

        # Simple z-score normalization per feature
        X = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-8)

        # Add batch dimension
        X = np.expand_dims(X, axis=0)  # Shape: (1, seq_len, 9)

        return X

    def infer(self, ticks: List[Dict[str, float]]) -> Tuple[float, float]:
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
            input_name = self.session.get_inputs()[0].name
            outputs = self.session.run(None, {input_name: X})

            # Outputs: [anomaly_score (1,), confidence (1,)]
            anomaly_score = float(outputs[0][0]) * 100  # Scale to 0-100
            confidence = float(outputs[1][0])  # Already 0-1

            # Clamp to valid ranges
            anomaly_score = max(0, min(100, anomaly_score))
            confidence = max(0, min(1, confidence))

            return anomaly_score, confidence

        except Exception as e:
            logger.error(f"Inference failed: {e}")
            return 0.0, 0.0

    def infer_batch(
        self, tick_batches: List[List[Dict[str, float]]]
    ) -> List[Tuple[float, float]]:
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

    def update(self, tick: Dict[str, float]) -> Optional[Dict[str, Any]]:
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
            }
            self.alerts.append(alert)
            logger.warning(f"🚨 ANOMALY DETECTED: {alert}")
            return alert

        return None

    def _classify_anomaly(self, tick: Dict[str, float]) -> str:
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

    def get_buffer_stats(self) -> Dict[str, Any]:
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
