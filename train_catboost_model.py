import json
import os
from typing import List, Tuple

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier
from sklearn.metrics import accuracy_score, classification_report, f1_score

from research_signal import (
    RESEARCH_FEATURE_COLUMNS,
    apply_research_features,
    load_latest_research_signal,
)


BASE_FEATURE_COLUMNS: List[str] = [
    "rsi",
    "macd",
    "macd_signal",
    "macd_hist",
    "sma_50",
    "sma_200",
    "ema_20",
    "ema_50",
    "ema_200",
    "atr_14",
    "stoch_k",
    "stoch_d",
    "cci_20",
    "obv",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "volume",
]

LABEL_MAP = {"verkaufen": 0, "halten": 1, "kaufen": 2}
INV_LABEL_MAP = {v: k for k, v in LABEL_MAP.items()}


def load_data(path: str = "training_data.csv") -> pd.DataFrame:
    df = pd.read_csv(path)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "coin" in out.columns and "timestamp" in out.columns:
        out = out.sort_values(["coin", "timestamp"]).reset_index(drop=True)
    elif "timestamp" in out.columns:
        out = out.sort_values(["timestamp"]).reset_index(drop=True)

    # Additional robust features
    if "close" in out.columns:
        out["ret_1"] = out.groupby("coin")["close"].pct_change(
            1) if "coin" in out.columns else out["close"].pct_change(1)
        out["ret_3"] = out.groupby("coin")["close"].pct_change(
            3) if "coin" in out.columns else out["close"].pct_change(3)
        out["ret_6"] = out.groupby("coin")["close"].pct_change(
            6) if "coin" in out.columns else out["close"].pct_change(6)
        out["vol_6"] = out.groupby("coin")["close"].pct_change().rolling(6).std().reset_index(
            level=0, drop=True) if "coin" in out.columns else out["close"].pct_change().rolling(6).std()

    return out


def create_profit_labels(
    df: pd.DataFrame,
    horizon: int = 6,
    buy_threshold: float = 0.012,
    sell_threshold: float = -0.012,
    fee_per_trade: float = 0.002,
    slippage: float = 0.001,
) -> pd.DataFrame:
    out = df.copy()

    if "coin" in out.columns:
        future_close = out.groupby("coin")["close"].shift(-horizon)
    else:
        future_close = out["close"].shift(-horizon)

    gross_return = (future_close / out["close"]) - 1.0

    # Roundtrip-Kosten: Entry+Exit
    total_cost = 2.0 * (fee_per_trade + slippage)
    net_return = gross_return - total_cost
    out["future_net_return"] = net_return

    out["label"] = np.where(
        out["future_net_return"] >= buy_threshold,
        "kaufen",
        np.where(out["future_net_return"] <=
                 sell_threshold, "verkaufen", "halten"),
    )

    return out


def train_val_split_time(df: pd.DataFrame, train_ratio: float = 0.8) -> Tuple[pd.DataFrame, pd.DataFrame]:
    split_idx = int(len(df) * train_ratio)
    return df.iloc[:split_idx].copy(), df.iloc[split_idx:].copy()


def generate_walk_forward_splits(
    n_rows: int,
    n_splits: int = 3,
    min_train_size: int = 300,
    min_val_size: int = 100,
) -> List[Tuple[int, int, int, int]]:
    """Erzeugt expanding-window Splits: Train [0:train_end), Val [val_start:val_end)."""
    if n_rows <= 0 or n_splits <= 0:
        return []

    max_splits_by_val = max(1, n_rows // max(min_val_size, 1))
    effective_splits = min(n_splits, max_splits_by_val)
    val_size = max(min_val_size, n_rows // (effective_splits + 1))

    splits: List[Tuple[int, int, int, int]] = []
    train_end = max(min_train_size, val_size)
    for _ in range(effective_splits):
        val_start = train_end
        val_end = min(val_start + val_size, n_rows)
        if val_start >= n_rows or val_end - val_start < min_val_size:
            break
        if train_end < min_train_size:
            break
        splits.append((0, train_end, val_start, val_end))
        train_end = val_end

    return splits


def run_walk_forward_evaluation(
    df: pd.DataFrame,
    n_splits: int = 3,
    min_train_size: int = 300,
    min_val_size: int = 100,
) -> pd.DataFrame:
    """Runs walk-forward evaluation and returns metrics per fold."""
    splits = generate_walk_forward_splits(
        n_rows=len(df),
        n_splits=n_splits,
        min_train_size=min_train_size,
        min_val_size=min_val_size,
    )
    if not splits:
        return pd.DataFrame()

    rows = []
    for fold_idx, (train_start, train_end, val_start, val_end) in enumerate(splits, start=1):
        train_df = df.iloc[train_start:train_end].copy()
        val_df = df.iloc[val_start:val_end].copy()

        x_train, y_train, _ = prepare_xy(train_df)
        x_val, y_val, _ = prepare_xy(val_df)

        model = CatBoostClassifier(
            iterations=400,
            learning_rate=0.03,
            depth=6,
            loss_function="MultiClass",
            eval_metric="TotalF1",
            random_seed=42,
            verbose=False,
        )
        model.fit(x_train, y_train, eval_set=(
            x_val, y_val), use_best_model=True)

        y_pred = model.predict(x_val).reshape(-1).astype(int)
        rows.append({
            "fold": fold_idx,
            "train_size": int(len(x_train)),
            "val_size": int(len(x_val)),
            "accuracy": float(accuracy_score(y_val, y_pred)),
            "macro_f1": float(f1_score(y_val, y_pred, average="macro", zero_division=0)),
            "weighted_f1": float(f1_score(y_val, y_pred, average="weighted", zero_division=0)),
        })

    return pd.DataFrame(rows)


def prepare_xy(df: pd.DataFrame) -> Tuple[pd.DataFrame, np.ndarray, List[str]]:
    all_features = BASE_FEATURE_COLUMNS + [
        "ret_1", "ret_3", "ret_6", "vol_6"] + RESEARCH_FEATURE_COLUMNS
    available_features = [c for c in all_features if c in df.columns]
    x = df[available_features].copy()
    y = df["label"].map(LABEL_MAP).values
    x = x.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return x, y, available_features


def train_model(
    data_path: str = "training_data.csv",
    output_dir: str = "./model/catboost_trading_model",
    walk_forward_splits: int = 3,
    research_signal_path: str = "",
) -> None:
    df = load_data(data_path)
    df = add_features(df)
    research_features = load_latest_research_signal(
        research_signal_path or os.getenv("RESEARCH_SIGNAL_PATH", ""))
    df = apply_research_features(df, research_features)
    df = create_profit_labels(df)

    required = ["close", "label"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Fehlende Spalten: {missing}")

    df = df.dropna(subset=["close", "future_net_return", "label"])

    if "timestamp" in df.columns:
        df = df.sort_values(["timestamp"]).reset_index(drop=True)

    train_df, val_df = train_val_split_time(df, train_ratio=0.8)
    x_train, y_train, features = prepare_xy(train_df)
    x_val, y_val, _ = prepare_xy(val_df)

    model = CatBoostClassifier(
        iterations=600,
        learning_rate=0.03,
        depth=6,
        loss_function="MultiClass",
        eval_metric="TotalF1",
        random_seed=42,
        verbose=100,
    )

    model.fit(x_train, y_train, eval_set=(x_val, y_val), use_best_model=True)

    os.makedirs(output_dir, exist_ok=True)
    model_path = os.path.join(output_dir, "catboost_model.cbm")
    meta_path = os.path.join(output_dir, "metadata.json")

    model.save_model(model_path)

    metadata = {
        "features": features,
        "label_map": LABEL_MAP,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=True, indent=2)

    y_pred = model.predict(x_val).reshape(-1).astype(int)
    y_pred_labels = [INV_LABEL_MAP[v] for v in y_pred]
    y_true_labels = [INV_LABEL_MAP[v] for v in y_val]

    print("\n=== Validierungsreport (CatBoost) ===")
    print(classification_report(y_true_labels, y_pred_labels, digits=3))

    walk_forward_df = run_walk_forward_evaluation(
        df,
        n_splits=walk_forward_splits,
        min_train_size=300,
        min_val_size=100,
    )
    if walk_forward_df.empty:
        print("\n=== Walk-Forward Evaluation ===")
        print("Insufficient data for walk-forward splits.")
    else:
        print("\n=== Walk-Forward Evaluation ===")
        print(walk_forward_df.to_string(
            index=False, float_format=lambda v: f"{v:.4f}"))
        print(
            "Mittelwerte: "
            f"accuracy={walk_forward_df['accuracy'].mean():.4f}, "
            f"macro_f1={walk_forward_df['macro_f1'].mean():.4f}, "
            f"weighted_f1={walk_forward_df['weighted_f1'].mean():.4f}"
        )
        walk_forward_path = os.path.join(output_dir, "walk_forward_report.csv")
        walk_forward_df.to_csv(walk_forward_path, index=False)
        print(f"Walk-Forward Report gespeichert: {walk_forward_path}")

    print(f"\nModell gespeichert: {model_path}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Train CatBoost trading model with optional AutoResearch features."
    )
    parser.add_argument("--data-path", default="training_data.csv")
    parser.add_argument(
        "--output-dir", default="./model/catboost_trading_model")
    parser.add_argument("--walk-forward-splits", type=int, default=3)
    parser.add_argument(
        "--research-signal-path",
        default="",
        help="Path to latest AutoResearch JSON signal. Can also be set via RESEARCH_SIGNAL_PATH.",
    )
    args = parser.parse_args()

    train_model(
        data_path=args.data_path,
        output_dir=args.output_dir,
        walk_forward_splits=args.walk_forward_splits,
        research_signal_path=args.research_signal_path,
    )


if __name__ == "__main__":
    main()
