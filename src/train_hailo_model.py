"""Train a small tabular classifier and export the fixed Hailo ONNX contract."""

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np
import pandas as pd

from src.hailo_contract import (
    HAILO_FEATURES,
    HAILO_INPUT_NAME,
    HAILO_LABEL_MAP,
    HAILO_OUTPUT_NAME,
)


def prepare_hailo_data(data_path: str) -> tuple[np.ndarray, ...]:
    from src.train_catboost_model import (
        LABEL_HORIZON,
        _purge_training_boundary,
        add_features,
        compute_class_weights,
        create_profit_labels,
    )

    df = pd.read_csv(data_path)
    required = {"timestamp", "coin", "close"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Training data is missing columns: {', '.join(sorted(missing))}")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    if df["coin"].isna().any():
        raise ValueError("Training data contains empty coin symbols")
    df["coin"] = df["coin"].astype(str).str.strip().str.upper()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["timestamp", "close"])
    if df["coin"].eq("").any() or (df["close"] <= 0).any():
        raise ValueError("Training data contains empty coins or non-positive close prices")

    df = add_features(df)
    df = create_profit_labels(df, horizon=LABEL_HORIZON)
    df = df.dropna(subset=["future_net_return", "label"])
    df = df.sort_values(["timestamp", "coin"], kind="stable").reset_index(drop=True)
    if len(df) < max(200, 2 * LABEL_HORIZON):
        raise ValueError("At least 200 chronologically labelled rows are required")

    timestamps = df["timestamp"].drop_duplicates().sort_values().to_list()
    split_index = int(len(timestamps) * 0.8)
    if split_index <= 0 or split_index >= len(timestamps):
        raise ValueError("Insufficient unique timestamps for a chronological split")
    validation_start = timestamps[split_index]
    train_df = df.loc[df["timestamp"] < validation_start].copy()
    validation_df = df.loc[df["timestamp"] >= validation_start].copy()
    train_df = _purge_training_boundary(
        train_df, validation_df, horizon=LABEL_HORIZON
    )
    if train_df.empty or validation_df.empty:
        raise ValueError("Insufficient data after the purged chronological split")

    for feature in HAILO_FEATURES:
        if feature not in train_df:
            train_df[feature] = 0.0
        if feature not in validation_df:
            validation_df[feature] = 0.0
    x_train = (
        train_df.reindex(columns=HAILO_FEATURES)
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0.0)
        .to_numpy(dtype=np.float32)
    )
    x_validation = (
        validation_df.reindex(columns=HAILO_FEATURES)
        .replace([np.inf, -np.inf], np.nan)
        .fillna(0.0)
        .to_numpy(dtype=np.float32)
    )
    y_train = train_df["label"].map(HAILO_LABEL_MAP).to_numpy(dtype=np.int64)
    y_validation = validation_df["label"].map(HAILO_LABEL_MAP).to_numpy(dtype=np.int64)
    if pd.isna(y_train).any() or pd.isna(y_validation).any():
        raise ValueError("Training data contains labels outside the Hailo label map")
    class_weights = compute_class_weights(y_train, n_classes=len(HAILO_LABEL_MAP))
    return x_train, y_train, x_validation, y_validation, np.asarray(class_weights)


def train_and_export(
    data_path: str,
    output_dir: str,
    epochs: int = 150,
    patience: int = 20,
    learning_rate: float = 0.001,
    model_id: str = "trading-mlp-h8-v1",
) -> dict:
    if epochs < 1 or patience < 1 or learning_rate <= 0:
        raise ValueError("epochs, patience, and learning_rate must be positive")
    if not model_id.strip():
        raise ValueError("model_id must not be empty")
    try:
        import torch
        from torch import nn
    except ImportError as exc:
        raise RuntimeError(
            "PyTorch and ONNX are required for Hailo model export; "
            "install the hailo-export extra"
        ) from exc
    if importlib.util.find_spec("onnx") is None:
        raise RuntimeError(
            "ONNX is required for Hailo model export; install the hailo-export extra"
        )

    x_train, y_train, x_validation, y_validation, class_weights = prepare_hailo_data(
        data_path
    )
    if len(np.unique(y_train)) < 2:
        raise ValueError("Training split must contain at least two label classes")
    if len(np.unique(y_validation)) < 2:
        raise ValueError("Validation split must contain at least two label classes")

    means = x_train.mean(axis=0, dtype=np.float64)
    scales = x_train.std(axis=0, dtype=np.float64)
    scales[scales < 1e-8] = 1.0
    x_train = ((x_train - means) / scales).astype(np.float32)
    x_validation = ((x_validation - means) / scales).astype(np.float32)

    class TradingMLP(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.layers = nn.Sequential(
                nn.Linear(len(HAILO_FEATURES), 32),
                nn.ReLU(),
                nn.Linear(32, 16),
                nn.ReLU(),
                nn.Linear(16, len(HAILO_LABEL_MAP)),
            )

        def forward(self, features):
            return self.layers(features)

    torch.manual_seed(42)
    torch.set_num_threads(1)
    model = TradingMLP()
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    loss_function = nn.CrossEntropyLoss(
        weight=torch.tensor(class_weights, dtype=torch.float32)
    )
    train_features = torch.from_numpy(x_train)
    train_labels = torch.from_numpy(y_train)
    validation_features = torch.from_numpy(x_validation)
    validation_labels = torch.from_numpy(y_validation)

    best_loss = float("inf")
    best_weights = copy.deepcopy(model.state_dict())
    epochs_without_improvement = 0
    for _ in range(epochs):
        model.train()
        optimizer.zero_grad()
        train_loss = loss_function(model(train_features), train_labels)
        train_loss.backward()
        optimizer.step()

        model.eval()
        with torch.no_grad():
            validation_loss = float(
                loss_function(model(validation_features), validation_labels).item()
            )
        if validation_loss < best_loss:
            best_loss = validation_loss
            best_weights = copy.deepcopy(model.state_dict())
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1
            if epochs_without_improvement >= patience:
                break

    model.load_state_dict(best_weights)
    model.eval()
    with torch.no_grad():
        validation_logits = model(validation_features).cpu().numpy()

    from sklearn.metrics import accuracy_score, f1_score

    from src.train_catboost_model import tune_confidence_threshold

    validation_logits = validation_logits - validation_logits.max(axis=1, keepdims=True)
    validation_probabilities = np.exp(validation_logits)
    validation_probabilities /= validation_probabilities.sum(axis=1, keepdims=True)
    confidence_threshold, threshold_stats = tune_confidence_threshold(
        y_validation,
        validation_probabilities,
        hold_label=HAILO_LABEL_MAP["halten"],
    )

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    onnx_path = output_path / "trading_mlp.onnx"
    torch.onnx.export(
        model,
        torch.zeros((1, len(HAILO_FEATURES)), dtype=torch.float32),
        onnx_path,
        input_names=[HAILO_INPUT_NAME],
        output_names=[HAILO_OUTPUT_NAME],
        opset_version=17,
        dynamo=False,
    )

    metadata = {
        "model_id": model_id,
        "features": list(HAILO_FEATURES),
        "means": means.tolist(),
        "scales": scales.tolist(),
        "label_map": HAILO_LABEL_MAP,
        "input_name": HAILO_INPUT_NAME,
        "input_shape": [1, len(HAILO_FEATURES)],
        "output_name": HAILO_OUTPUT_NAME,
        "output_shape": [1, len(HAILO_LABEL_MAP)],
        "output_kind": "logits",
        "confidence_threshold": confidence_threshold,
        "margin_threshold": 0.03,
        "validation": {
            "rows": len(y_validation),
            "accuracy": float(accuracy_score(y_validation, validation_probabilities.argmax(axis=1))),
            "macro_f1": float(f1_score(
                y_validation,
                validation_probabilities.argmax(axis=1),
                average="macro",
                zero_division=0,
            )),
            "threshold_macro_f1": float(threshold_stats["macro_f1"]),
            "threshold_coverage": float(threshold_stats["coverage"]),
        },
    }
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=output_path, delete=False
    ) as metadata_file:
        json.dump(metadata, metadata_file, ensure_ascii=True, indent=2)
        metadata_file.write("\n")
        temporary_path = Path(metadata_file.name)
    os.replace(temporary_path, output_path / "metadata.json")
    print(f"ONNX model: {onnx_path}")
    print(f"Metadata:   {output_path / 'metadata.json'}")
    print(
        "Validation only (not a deployment recommendation): "
        f"rows={len(y_validation)} accuracy={metadata['validation']['accuracy']:.3f} "
        f"macro_f1={metadata['validation']['macro_f1']:.3f} "
        f"threshold={confidence_threshold:.2f}"
    )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Train and export a trading MLP for later Hailo-8 compilation."
    )
    parser.add_argument("--data-path", default="training_data.csv")
    parser.add_argument("--output-dir", default="./model/hailo_trading_model")
    parser.add_argument("--epochs", type=int, default=150)
    parser.add_argument("--patience", type=int, default=20)
    parser.add_argument("--learning-rate", type=float, default=0.001)
    parser.add_argument("--model-id", default="trading-mlp-h8-v1")
    args = parser.parse_args()
    try:
        train_and_export(
            data_path=args.data_path,
            output_dir=args.output_dir,
            epochs=args.epochs,
            patience=args.patience,
            learning_rate=args.learning_rate,
            model_id=args.model_id,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
