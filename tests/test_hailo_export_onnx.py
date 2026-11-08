import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import hailo_export_onnx


@pytest.fixture
def export_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Mock]:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "model_config.json").write_text("{}")
    output = tmp_path / "model.onnx"
    monkeypatch.setattr(sys, "argv", ["export", str(model_dir), str(output)])
    monkeypatch.setattr(
        hailo_export_onnx, "TimeSeriesTransformerConfig",
        lambda **kwargs: SimpleNamespace(seq_length=60, n_features=9),
    )
    model = Mock()
    monkeypatch.setattr(hailo_export_onnx, "TimeSeriesTransformer", lambda config: model)
    monkeypatch.setattr(hailo_export_onnx.torch, "load", lambda *args, **kwargs: {"weight": 1})
    return output, model


def test_export_publishes_single_model(
    export_setup: tuple[Path, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    output, model = export_setup

    def fake_export(model: Mock, path: str, seq_length: int, n_features: int) -> None:
        assert (seq_length, n_features) == (60, 9)
        Path(path).write_bytes(b"ONNX")

    monkeypatch.setattr(hailo_export_onnx, "export_to_onnx", fake_export)
    assert hailo_export_onnx.main() == 0
    assert output.read_bytes() == b"ONNX"
    model.load_state_dict.assert_called_once_with({"weight": 1}, strict=True)
    assert not list(output.parent.glob(".hailo-export-*"))


def test_export_rejects_external_data(
    export_setup: tuple[Path, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    output, _ = export_setup

    def fake_export(model: Mock, path: str, seq_length: int, n_features: int) -> None:
        Path(path).write_bytes(b"ONNX")
        Path(path + ".data").write_bytes(b"weights")

    monkeypatch.setattr(hailo_export_onnx, "export_to_onnx", fake_export)
    with pytest.raises(RuntimeError, match="external data"):
        hailo_export_onnx.main()
    assert not output.exists()
    assert not list(output.parent.glob(".hailo-export-*"))


def test_export_does_not_overwrite_existing_file(export_setup: tuple[Path, Mock]) -> None:
    output, _ = export_setup
    output.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        hailo_export_onnx.main()
    assert output.read_bytes() == b"original"
