"""Compile a complete ONNX graph to a Hailo HEF using real calibration data."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse, calibrate, and compile a complete ONNX model to HEF."
    )
    parser.add_argument("onnx", type=Path, help="Input ONNX model")
    parser.add_argument("calibration", type=Path, help="Preprocessed NHWC .npy calibration set")
    parser.add_argument("hef", type=Path, help="Output HEF path; must not already exist")
    parser.add_argument("--arch", choices=("hailo8", "hailo8r", "hailo8l"), default="hailo8")
    return parser.parse_args()


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL)


def main() -> int:
    args = parse_args()
    model = args.onnx.resolve(strict=True)
    calibration = args.calibration.resolve(strict=True)
    output = args.hef.absolute()
    if not model.is_file() or not calibration.is_file():
        raise ValueError("ONNX model and calibration set must be files")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    if not output.parent.is_dir():
        raise ValueError(f"Output directory does not exist: {output.parent}")
    if output.suffix.lower() != ".hef":
        raise ValueError("Output filename must end in .hef")

    data = np.load(calibration, mmap_mode="r", allow_pickle=False)
    if not isinstance(data, np.ndarray):
        data.close()
        raise TypeError("Calibration must be a NumPy .npy array, not an archive")
    if data.ndim != 4 or 0 in data.shape:
        raise ValueError("Calibration must be a non-empty four-dimensional NHWC .npy array")
    if data.dtype.kind not in "fiu" or not np.isfinite(data).all():
        raise ValueError("Calibration values must be finite real numbers")
    print(f"Calibration: {data.shape}, {data.dtype}", flush=True)
    del data

    with tempfile.TemporaryDirectory(prefix=".hailo-compile-", dir=output.parent) as temp:
        work = Path(temp)
        parsed = work / "parsed.har"
        optimized = work / "optimized.har"
        run([
            "hailo", "parser", "onnx", str(model), "--net-name", "hailo_model",
            "--hw-arch", args.arch, "--har-path", str(parsed),
        ])
        run([
            "hailo", "optimize", str(parsed), "--hw-arch", args.arch,
            "--calib-set-path", str(calibration), "--output-har-path", str(optimized),
        ])
        run([
            "hailo", "compiler", str(optimized), "--hw-arch", args.arch,
            "--output-dir", str(work),
        ])
        compiled = work / "hailo_model.hef"
        if not compiled.is_file() or compiled.stat().st_size == 0:
            raise RuntimeError(f"Compiler did not produce a non-empty HEF: {compiled}")
        run(["hailortcli", "parse-hef", str(compiled)])
        os.link(compiled, output)
    print(f"HEF created: {output}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
