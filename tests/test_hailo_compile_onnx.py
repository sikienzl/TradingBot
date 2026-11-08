import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from scripts import hailo_compile_onnx


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    model = tmp_path / "model.onnx"
    model.write_bytes(b"model")
    calibration = tmp_path / "calibration.npy"
    np.save(calibration, np.zeros((2, 4, 4, 3), dtype=np.float32))
    output = tmp_path / "output.hef"
    monkeypatch.setattr(sys, "argv", ["compile", str(model), str(calibration), str(output)])
    return model, calibration, output


def test_compiles_complete_graph_without_automatic_recommendations(
    inputs: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    model, calibration, output = inputs
    commands: list[list[str]] = []

    def fake_run(command: list[str]) -> None:
        commands.append(command)
        if command[1] == "compiler":
            Path(command[-1], "hailo_model.hef").write_bytes(b"HEF")

    monkeypatch.setattr(hailo_compile_onnx, "run", fake_run)

    assert hailo_compile_onnx.main() == 0
    assert output.read_bytes() == b"HEF"
    assert [command[1] for command in commands] == [
        "parser", "optimize", "compiler", "parse-hef"
    ]
    assert "-y" not in commands[0]
    assert str(model) in commands[0]
    assert str(calibration) in commands[1]
    assert list(output.parent.glob(".hailo-compile-*")) == []


def test_parser_failure_never_publishes_a_hef(
    inputs: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    output = inputs[2]

    def fail(command: list[str]) -> None:
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(hailo_compile_onnx, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        hailo_compile_onnx.main()
    assert not output.exists()
    assert list(output.parent.glob(".hailo-compile-*")) == []


@pytest.mark.parametrize(
    "values",
    [np.zeros((2, 4, 4)), np.full((2, 4, 4, 3), np.nan)],
)
def test_invalid_calibration_is_rejected(
    inputs: tuple[Path, Path, Path], values: np.ndarray
) -> None:
    np.save(inputs[1], values)
    with pytest.raises(ValueError, match="Calibration"):
        hailo_compile_onnx.main()
    assert not inputs[2].exists()


def test_existing_output_is_not_overwritten(inputs: tuple[Path, Path, Path]) -> None:
    output = inputs[2]
    output.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        hailo_compile_onnx.main()
    assert output.read_bytes() == b"existing"
