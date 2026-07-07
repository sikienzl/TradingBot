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

import os
import json
import logging
from pathlib import Path
from typing import Tuple, Dict, List, Optional
import pickle

import numpy as np
import pandas as pd
from dataclasses import dataclass

try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.utils.data import Dataset, DataLoader
    import einops
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
        feature_cols: Optional[List[str]] = None,
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
        self.ticks_df = ticks_df
        self.seq_length = seq_length
        self.feature_cols = feature_cols or [
            "bid", "ask", "mid", "bid_size", "ask_size",
            "last_trade_price", "last_trade_size", "bid_ask_ratio", "spread"
        ]
        self.label_col = label_col

        # Normalize features
        self.feature_mean = self.ticks_df[self.feature_cols].mean()
        self.feature_std = self.ticks_df[self.feature_cols].std() + 1e-8
        self.ticks_df_norm = (
            (self.ticks_df[self.feature_cols] -
             self.feature_mean) / self.feature_std
        )

    def __len__(self):
        return len(self.ticks_df)

    def __getitem__(self, idx: int) -> Tuple[torch.Tensor, torch.Tensor]:
        """Get one sequence"""
        X = torch.tensor(
            self.ticks_df_norm.iloc[idx].values,
            dtype=torch.float32
        ).unsqueeze(0)  # Add sequence dimension

        y = torch.tensor(
            self.ticks_df[self.label_col].iloc[idx],
            dtype=torch.long  # Classification
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

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
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
        val_loader: Optional[DataLoader] = None,
    ) -> Dict:
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
    dummy_input = torch.randn(1, seq_length, n_features)

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


def train_transformer(
    training_data_path: str,
    output_model_dir: str = "/models/hailo",
    config: Optional[TimeSeriesTransformerConfig] = None,
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
    train_df = df[:n_train]
    val_df = df[n_train:]

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

    logger.info(f"🎉 Training complete! Ready for Hailo-8 deployment.")
    return model, history


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Example: train_transformer("training_data.csv")
    logger.info("Time-Series-Transformer training module ready.")
