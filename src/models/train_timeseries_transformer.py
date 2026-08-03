"""
Time-Series-Transformer Training for Hailo-8 Edge Inference

Fine-tuned on trading data for fast, accurate anomaly detection.
Optimized for ONNX export and Hailo-8 (26 TOPS, <50MB model).

Architecture:
- Attention-based sequence model
- 4 transformer layers, 8 attention heads
- Input: 60-tick sequences with 9 technical features
- Output: Anomaly score (0-1) + confidence (0-1)
"""

import argparse
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import einops
    import torch
    from torch import nn, optim
    from torch.utils.data import DataLoader, Dataset
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

logger = logging.getLogger(__name__)


@dataclass
class TimeSeriesTransformerConfig:
    """Configuration for Time-Series-Transformer model"""
    seq_length: int = 60
    n_features: int = 9
    hidden_dim: int = 128
    n_layers: int = 4
    n_heads: int = 8
    dropout: float = 0.1
    ff_dim: int = 256
    learning_rate: float = 1e-4
    batch_size: int = 32
    epochs: int = 50
    patience: int = 5


class TimeSeriesDataset(Dataset):
    """PyTorch Dataset for trading tick sequences"""

    def __init__(
        self,
        ticks_df: pd.DataFrame,
        seq_length: int = 60,
        feature_cols: list[str] | None = None,
        label_col: str = "is_anomaly",
    ):
        """
        Initialize dataset.

        Args:
            ticks_df: DataFrame with tick data
            seq_length: Must be pre-split; each row = one sequence
            feature_cols: Columns to use as features
            label_col: Column with anomaly labels (0/1)
        """
        self.seq_length = seq_length
        self.feature_cols = feature_cols or [
            "bid", "ask", "mid", "bid_size", "ask_size",
            "last_trade_price", "last_trade_size", "bid_ask_ratio", "spread"
        ]
        self.label_col = label_col
        prepared = self._prepare_sequences(ticks_df.copy())
        self.sequences = prepared["sequences"]
        self.labels = prepared["labels"]

    def _prepare_sequences(self, ticks_df: pd.DataFrame) -> dict[str, list[np.ndarray]]:
        feature_cols = list(self.feature_cols)
        required_feature_set = set(feature_cols)

        if not required_feature_set.issubset(ticks_df.columns):
            ticks_df = self._build_feature_frame(ticks_df)

        if self.label_col not in ticks_df.columns:
            ticks_df[self.label_col] = self._build_anomaly_labels(ticks_df)

        ticks_df = ticks_df.copy()
        ticks_df[feature_cols] = ticks_df[feature_cols].replace(
            [np.inf, -np.inf], np.nan
        )
        ticks_df[feature_cols] = ticks_df[feature_cols].ffill().bfill().fillna(0.0)
        ticks_df[self.label_col] = pd.to_numeric(
            ticks_df[self.label_col], errors="coerce"
        ).fillna(0.0).clip(0.0, 1.0)

        feature_mean = ticks_df[feature_cols].mean()
        feature_std = ticks_df[feature_cols].std().replace(
            0, np.nan).fillna(1.0)
        normalized = ((ticks_df[feature_cols] -
                      feature_mean) / feature_std).astype(np.float32)

        group_key = "symbol" if "symbol" in ticks_df.columns else "coin" if "coin" in ticks_df.columns else None
        if group_key is not None:
            grouped_frames = [group.copy()
                              for _, group in ticks_df.groupby(group_key, sort=False)]
        else:
            grouped_frames = [ticks_df.copy()]

        sequences: list[np.ndarray] = []
        labels: list[float] = []

        for group in grouped_frames:
            if "timestamp" in group.columns:
                group = group.sort_values("timestamp").reset_index(drop=True)
            group_features = normalized.loc[group.index].reset_index(drop=True)
            group_labels = ticks_df.loc[group.index,
                                        self.label_col].reset_index(drop=True)

            if len(group_features) < self.seq_length:
                continue

            for end_idx in range(self.seq_length - 1, len(group_features)):
                start_idx = end_idx - self.seq_length + 1
                sequences.append(
                    group_features.iloc[start_idx:end_idx +
                                        1].to_numpy(dtype=np.float32)
                )
                labels.append(float(group_labels.iloc[end_idx]))

        if not sequences:
            raise ValueError(
                f"No sequences could be built from the input data for seq_length={self.seq_length}"
            )

        return {"sequences": sequences, "labels": labels}

    def _build_feature_frame(self, ticks_df: pd.DataFrame) -> pd.DataFrame:
        required_ohlcv = {"open", "high", "low", "close", "volume"}
        if not required_ohlcv.issubset(ticks_df.columns):
            missing = sorted(required_ohlcv.difference(set(ticks_df.columns)))
            raise ValueError(
                f"Training data is missing required feature columns: {missing}"
            )

        frame = ticks_df.copy()
        close = pd.to_numeric(frame["close"], errors="coerce").fillna(0.0)
        open_ = pd.to_numeric(frame["open"], errors="coerce").fillna(close)
        high = pd.to_numeric(frame["high"], errors="coerce").fillna(close)
        low = pd.to_numeric(frame["low"], errors="coerce").fillna(close)
        volume = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)
        prev_volume = volume.shift(1).fillna(volume)

        frame["bid"] = np.minimum(open_, close)
        frame["ask"] = np.maximum(open_, close)
        frame["mid"] = (high + low + close) / 3.0
        frame["bid_size"] = prev_volume.clip(lower=0.0)
        frame["ask_size"] = volume.clip(lower=0.0)
        frame["last_trade_price"] = close
        frame["last_trade_size"] = volume.diff(
        ).abs().fillna(volume).clip(lower=0.0)
        frame["bid_ask_ratio"] = frame["bid"] / \
            frame["ask"].replace(0.0, np.nan)
        frame["spread"] = (high - low).clip(lower=0.0)
        return frame

    def _build_anomaly_labels(self, ticks_df: pd.DataFrame) -> pd.Series:
        frame = ticks_df.copy()
        if "timestamp" in frame.columns:
            frame = frame.sort_values("timestamp").reset_index(drop=True)

            close = pd.to_numeric(frame.get("close"),
                                  errors="coerce").ffill().fillna(0.0)
        high = pd.to_numeric(frame.get("high"), errors="coerce").fillna(close)
        low = pd.to_numeric(frame.get("low"), errors="coerce").fillna(close)
        volume = pd.to_numeric(frame.get("volume"),
                               errors="coerce").fillna(0.0)

        abs_return = close.pct_change().abs().fillna(0.0)
        range_pct = ((high - low) / close.replace(0.0, np.nan)
                     ).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        volume_ratio = (volume / volume.rolling(20, min_periods=3).median().replace(
            0.0, np.nan)).replace([np.inf, -np.inf], np.nan).fillna(1.0)

        return_threshold = abs_return.rolling(50, min_periods=10).mean(
        ) + 2.0 * abs_return.rolling(50, min_periods=10).std().fillna(0.0)
        range_threshold = range_pct.rolling(50, min_periods=10).mean(
        ) + 2.0 * range_pct.rolling(50, min_periods=10).std().fillna(0.0)

        anomaly_mask = (
            (abs_return > return_threshold.fillna(abs_return.quantile(0.90)))
            | (range_pct > range_threshold.fillna(range_pct.quantile(0.90)))
            | (volume_ratio > 2.5)
        )
        return anomaly_mask.astype(np.float32)

    def __len__(self):
        return len(self.sequences)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor]:
        """Get one sequence"""
        X = torch.tensor(
            self.sequences[idx],
            dtype=torch.float32
        )

        y = torch.tensor(
            self.labels[idx],
            dtype=torch.float32
        )

        return X, y


class PositionalEncoding(nn.Module):
    """Positional encoding for transformer"""

    def __init__(self, d_model: int, max_len: int = 5000):
        super().__init__()
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2).float() * (-np.log(10000.0) / d_model)
        )
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        self.register_buffer("pe", pe.unsqueeze(0))

    def forward(self, x):
        return x + self.pe[:, : x.size(1)]


class TimeSeriesTransformer(nn.Module):
    """Time-Series-Transformer for anomaly detection"""

    def __init__(self, config: TimeSeriesTransformerConfig):
        super().__init__()
        self.config = config

        # Embedding
        self.embedding = nn.Linear(config.n_features, config.hidden_dim)
        self.pos_encoding = PositionalEncoding(
            config.hidden_dim, config.seq_length)
        self.dropout = nn.Dropout(config.dropout)

        # Transformer encoder
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=config.hidden_dim,
            nhead=config.n_heads,
            dim_feedforward=config.ff_dim,
            dropout=config.dropout,
            batch_first=True,
        )
        self.transformer_encoder = nn.TransformerEncoder(
            encoder_layer, num_layers=config.n_layers
        )

        # Output heads
        self.anomaly_head = nn.Sequential(
            nn.Linear(config.hidden_dim, 128),
            nn.ReLU(),
            nn.Dropout(config.dropout),
            nn.Linear(128, 1),  # Anomaly score 0-1
            nn.Sigmoid(),
        )

        self.confidence_head = nn.Sequential(
            nn.Linear(config.hidden_dim, 64),
            nn.ReLU(),
            nn.Linear(64, 1),  # Confidence 0-1
            nn.Sigmoid(),
        )

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """
        Forward pass.

        Args:
            x: Input tensor (batch, seq_len, n_features)

        Returns:
            Tuple of (anomaly_scores, confidences)
        """
        # Embedding
        x = self.embedding(x)  # (batch, seq_len, hidden_dim)
        x = self.pos_encoding(x)
        x = self.dropout(x)

        # Transformer
        x = self.transformer_encoder(x)  # (batch, seq_len, hidden_dim)

        # Global average pooling
        x_pool = x.mean(dim=1)  # (batch, hidden_dim)

        # Output heads
        anomaly_score = self.anomaly_head(x_pool)  # (batch, 1)
        confidence = self.confidence_head(x_pool)  # (batch, 1)

        return anomaly_score.squeeze(-1), confidence.squeeze(-1)


class TimeSeriesTransformerTrainer:
    """Training loop for Time-Series-Transformer"""

    def __init__(
        self,
        model: TimeSeriesTransformer,
        config: TimeSeriesTransformerConfig,
        device: str = "cpu",
    ):
        self.model = model.to(device)
        self.config = config
        self.device = device

        self.optimizer = optim.Adam(
            model.parameters(), lr=config.learning_rate)
        self.loss_fn = nn.BCELoss()  # Binary classification

        self.train_losses = []
        self.val_losses = []

    def train_epoch(self, train_loader: DataLoader) -> float:
        """Train for one epoch"""
        self.model.train()
        total_loss = 0.0

        for X_batch, y_batch in train_loader:
            X_batch = X_batch.to(self.device)
            y_batch = y_batch.float().to(self.device)

            # Forward
            anomaly_scores, _ = self.model(X_batch)

            # Loss
            loss = self.loss_fn(anomaly_scores, y_batch)

            # Backward
            self.optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
            self.optimizer.step()

            total_loss += loss.item()

        avg_loss = total_loss / len(train_loader)
        self.train_losses.append(avg_loss)
        return avg_loss

    def validate(self, val_loader: DataLoader) -> float:
        """Evaluate on validation set"""
        self.model.eval()
        total_loss = 0.0

        with torch.no_grad():
            for X_batch, y_batch in val_loader:
                X_batch = X_batch.to(self.device)
                y_batch = y_batch.float().to(self.device)

                anomaly_scores, _ = self.model(X_batch)
                loss = self.loss_fn(anomaly_scores, y_batch)
                total_loss += loss.item()

        avg_loss = total_loss / len(val_loader)
        self.val_losses.append(avg_loss)
        return avg_loss

    def fit(
        self,
        train_loader: DataLoader,
        val_loader: DataLoader | None = None,
    ) -> dict:
        """
        Train model with early stopping.

        Returns:
            Training history dict
        """
        best_val_loss = float("inf")
        patience_counter = 0

        for epoch in range(self.config.epochs):
            train_loss = self.train_epoch(train_loader)

            val_loss = -1
            if val_loader:
                val_loss = self.validate(val_loader)

                if val_loss < best_val_loss:
                    best_val_loss = val_loss
                    patience_counter = 0
                else:
                    patience_counter += 1

                if patience_counter >= self.config.patience:
                    logger.info(f"Early stopping at epoch {epoch}")
                    break

            if (epoch + 1) % 5 == 0:
                logger.info(
                    f"Epoch {epoch+1:3d}: train_loss={train_loss:.4f}, "
                    f"val_loss={val_loss:.4f}"
                )

        return {
            "train_losses": self.train_losses,
            "val_losses": self.val_losses,
            "final_train_loss": self.train_losses[-1],
            "final_val_loss": self.val_losses[-1] if self.val_losses else None,
        }


def export_to_onnx(
    model: TimeSeriesTransformer,
    output_path: str,
    seq_length: int = 60,
    n_features: int = 9,
):
    """
    Export PyTorch model to ONNX format.

    Compatible with Hailo-8 & CPUs.
    """
    model.eval()

    # Dummy input
    model_device = next(model.parameters()).device
    dummy_input = torch.randn(1, seq_length, n_features, device=model_device)

    # Export
    try:
        torch.onnx.export(
            model,
            dummy_input,
            output_path,
            input_names=["input"],
            output_names=["anomaly_score", "confidence"],
            opset_version=14,
            do_constant_folding=True,
        )
        logger.info(f"✅ Model exported to {output_path}")
    except Exception as e:
        logger.error(f"❌ ONNX export failed: {e}")


def export_hailo_assets(output_model_dir: str) -> dict[str, str]:
    output_dir = Path(output_model_dir)
    onnx_path = output_dir / "timeseries_transformer.onnx"
    config_path = output_dir / "model_config.json"
    hef_placeholder_path = output_dir / "hailo_compile_instructions.json"

    instructions = {
        "onnx_model_path": str(onnx_path),
        "model_config_path": str(config_path),
        "recommended_hailo_parser": "hailomz parse",
        "recommended_hailo_compile": "hailomz compile",
        "target_artifact": str(output_dir / "timeseries_transformer.hef"),
        "notes": [
            "Run quantization/calibration on the RTX 3090 host before compiling for Hailo-8.",
            "Use representative market windows matching the exported seq_length and feature order.",
        ],
    }
    with open(hef_placeholder_path, "w") as file_obj:
        json.dump(instructions, file_obj, indent=2)
    logger.info("✅ Hailo compile instructions saved: %s", hef_placeholder_path)
    return {
        "onnx_model_path": str(onnx_path),
        "model_config_path": str(config_path),
        "hailo_compile_instructions_path": str(hef_placeholder_path),
    }


def train_transformer(
    training_data_path: str,
    output_model_dir: str = "/models/hailo",
    config: TimeSeriesTransformerConfig | None = None,
):
    """
    Full training pipeline: load data → train → export to ONNX

    Args:
        training_data_path: Path to CSV with tick sequences
        output_model_dir: Directory to save model
        config: Training config (uses defaults if None)
    """
    if not TORCH_AVAILABLE:
        raise ImportError("PyTorch required for training")

    config = config or TimeSeriesTransformerConfig()
    Path(output_model_dir).mkdir(parents=True, exist_ok=True)

    # Load data
    logger.info(f"Loading training data from {training_data_path}")
    df = pd.read_csv(training_data_path)

    # Split train/val
    n_train = int(0.8 * len(df))
    train_df = df[:n_train].reset_index(drop=True)
    val_df = df[n_train:].reset_index(drop=True)

    train_dataset = TimeSeriesDataset(train_df, seq_length=config.seq_length)
    val_dataset = TimeSeriesDataset(val_df, seq_length=config.seq_length)

    train_loader = DataLoader(
        train_dataset, batch_size=config.batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=config.batch_size)

    # Initialize model
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = TimeSeriesTransformer(config).to(device)
    trainer = TimeSeriesTransformerTrainer(model, config, device=device)

    # Train
    logger.info(f"Training on {device}, {len(train_loader)} batches")
    history = trainer.fit(train_loader, val_loader)

    # Save PyTorch model
    pt_path = Path(output_model_dir) / "timeseries_transformer_state.pt"
    torch.save(model.state_dict(), pt_path)
    logger.info(f"✅ Model state saved: {pt_path}")

    # Save config
    config_path = Path(output_model_dir) / "model_config.json"
    with open(config_path, "w") as f:
        json.dump(config.__dict__, f, indent=2)
    logger.info(f"✅ Config saved: {config_path}")

    # Save history
    history_path = Path(output_model_dir) / "training_history.json"
    with open(history_path, "w") as f:
        json.dump(history, f, indent=2)
    logger.info(f"✅ History saved: {history_path}")

    # Export to ONNX
    onnx_path = Path(output_model_dir) / "timeseries_transformer.onnx"
    export_to_onnx(model, str(onnx_path), seq_length=config.seq_length)
    export_hailo_assets(output_model_dir)

    logger.info("🎉 Training complete! Ready for Hailo-8 deployment.")
    return model, history


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Train and export Time-Series-Transformer for Hailo-8 prefiltering"
    )
    parser.add_argument(
        "--train-data",
        default=os.getenv("TIMESERIES_TRAIN_DATA", "training_data.csv"),
        help="Path to CSV training data",
    )
    parser.add_argument(
        "--output-dir",
        default=os.getenv("TIMESERIES_OUTPUT_DIR", "/models/hailo"),
        help="Output directory for model artifacts",
    )
    parser.add_argument(
        "--seq-length",
        type=int,
        default=int(os.getenv("TIMESERIES_SEQ_LENGTH", "60")),
        help="Sequence length for transformer input",
    )
    parser.add_argument(
        "--hidden-dim",
        type=int,
        default=int(os.getenv("TIMESERIES_HIDDEN_DIM", "128")),
        help="Transformer hidden dimension",
    )
    parser.add_argument(
        "--n-layers",
        type=int,
        default=int(os.getenv("TIMESERIES_N_LAYERS", "4")),
        help="Number of transformer encoder layers",
    )
    parser.add_argument(
        "--n-heads",
        type=int,
        default=int(os.getenv("TIMESERIES_N_HEADS", "8")),
        help="Number of attention heads",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.getenv("TIMESERIES_BATCH_SIZE", "32")),
        help="Training batch size",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=int(os.getenv("TIMESERIES_EPOCHS", "50")),
        help="Maximum training epochs",
    )
    parser.add_argument(
        "--learning-rate",
        type=float,
        default=float(os.getenv("TIMESERIES_LEARNING_RATE", "0.0001")),
        help="Optimizer learning rate",
    )
    parser.add_argument(
        "--patience",
        type=int,
        default=int(os.getenv("TIMESERIES_PATIENCE", "5")),
        help="Early stopping patience (epochs)",
    )

    args = parser.parse_args()

    config = TimeSeriesTransformerConfig(
        seq_length=max(1, args.seq_length),
        hidden_dim=max(8, args.hidden_dim),
        n_layers=max(1, args.n_layers),
        n_heads=max(1, args.n_heads),
        batch_size=max(1, args.batch_size),
        epochs=max(1, args.epochs),
        learning_rate=max(1e-7, args.learning_rate),
        patience=max(1, args.patience),
    )

    try:
        train_transformer(
            training_data_path=args.train_data,
            output_model_dir=args.output_dir,
            config=config,
        )
    except Exception as exc:
        logger.error("Training failed: %s", exc)
        raise
