"""Export an existing trained Transformer checkpoint for the Hailo compiler."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import torch

from src.models.train_timeseries_transformer import (
    TimeSeriesTransformer,
    TimeSeriesTransformerConfig,
    export_to_onnx,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export existing Transformer weights as Hailo-compatible ONNX opset 14."
    )
    parser.add_argument("model_dir", type=Path, help="Directory with model_config.json and state dict")
    parser.add_argument("onnx", type=Path, help="Output ONNX file; must not already exist")
    args = parser.parse_args()

    output = args.onnx.absolute()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    if output.suffix.lower() != ".onnx" or not output.parent.is_dir():
        raise ValueError("Output must be an .onnx file in an existing directory")

    config_path = args.model_dir / "model_config.json"
    weights_path = args.model_dir / "timeseries_transformer_state.pt"
    with config_path.open(encoding="utf-8") as handle:
        config = TimeSeriesTransformerConfig(**json.load(handle))
    model = TimeSeriesTransformer(config)
    weights = torch.load(weights_path, map_location="cpu", weights_only=True)
    model.load_state_dict(weights, strict=True)
    model.eval()

    with tempfile.TemporaryDirectory(prefix=".hailo-export-", dir=output.parent) as temp:
        exported = Path(temp) / "model.onnx"
        export_to_onnx(model, str(exported), config.seq_length, config.n_features)
        if not exported.is_file() or exported.stat().st_size == 0:
            raise RuntimeError(f"Exporter did not create an ONNX file: {exported}")
        if any(path != exported for path in exported.parent.iterdir()):
            raise RuntimeError("Export produced external data files; refusing to publish an incomplete model")
        os.link(exported, output)
    print(f"ONNX model created: {output}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
