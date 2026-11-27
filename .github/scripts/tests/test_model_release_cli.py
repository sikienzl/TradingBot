"""Exercise the paired model release CLI the way a deployment would.

``scripts/manage_model_release.py`` is the only supported way to change which
model a release profile runs, on both the single-node Raspberry Pi and the CM5
cluster, and its guide ships inside the Pi package. Until now nothing in the
deployment CI ever ran it: the promote/validate/rollback path was only covered by
the pytest suite, which the release tooling job cannot install. This test drives
the real CLI end to end in a temporary release root with synthetic artifacts, so
a packaging or import regression in the release CLI fails the deployment gate.

Only the standard library is available in this job, which is exactly the
constraint the release CLI has to respect: :mod:`src.model_release` and
:mod:`src.scorecard_gate` must stay importable without numpy, pandas or CatBoost
installed.
"""

import contextlib
import io
import json
import pathlib
import re
import sys
import tempfile
import unittest
from datetime import UTC, datetime

ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.manage_model_release import main as release_cli
from src.model_release import promote_release, rollback_release, validate_release

FEATURES = ["rsi", "macd", "atr_14"]
LABEL_MAP = {"verkaufen": 0, "halten": 1, "kaufen": 2}
TRAINING_DATA_SHA256 = "c" * 64
RELEASE_WORKFLOW = ROOT / ".github/workflows/release.yml"
STAGING_STEP = re.compile(
    r"-\s+name:\s+Stage runtime files\n(.*?)\n\s{6}-\s+name:", re.DOTALL
)


def _validation(accuracy: float, macro_f1: float) -> dict[str, float]:
    """Validation block in the shape both trainers emit."""
    return {
        "rows": 300,
        "accuracy": accuracy,
        "macro_f1": macro_f1,
        "threshold_macro_f1": macro_f1 - 0.02,
        "threshold_coverage": 0.58,
    }


class ModelReleaseCliTest(unittest.TestCase):
    """promote -> validate -> rollback against a temporary release root."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="model-release-cli-")
        self.root = pathlib.Path(self._directory.name)
        self.release_root = self.root / "opt-trading-models"
        self.artifacts = self._write_candidate(self.root / "candidate")
        self.marker = self._write_go_marker()

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _write_candidate(self, directory: pathlib.Path) -> list[pathlib.Path]:
        directory.mkdir(parents=True, exist_ok=True)
        cpu_model = directory / "catboost_model.cbm"
        cpu_model.write_bytes(b"cpu model bytes")
        hef = directory / "model.hef"
        hef.write_bytes(b"hailo hef bytes")
        cpu_metadata = directory / "cpu_metadata.json"
        cpu_metadata.write_text(
            json.dumps(
                {
                    "feature_contract": "hailo",
                    "features": FEATURES,
                    "label_map": LABEL_MAP,
                    "training_data_sha256": TRAINING_DATA_SHA256,
                    "label_horizon": 6,
                    "train_rows": 1200,
                    "validation_rows": 300,
                    "validation": _validation(accuracy=0.81, macro_f1=0.72),
                    "recommended_confidence_threshold": 0.52,
                    "recommended_confidence_threshold_stats": {
                        "macro_f1": 0.7,
                        "coverage": 0.58,
                    },
                    "walk_forward": [
                        {
                            "fold": 1,
                            "train_size": 500,
                            "val_size": 150,
                            "accuracy": 0.75,
                            "macro_f1": 0.66,
                            "weighted_f1": 0.69,
                        },
                        {
                            "fold": 2,
                            "train_size": 650,
                            "val_size": 150,
                            "accuracy": 0.79,
                            "macro_f1": 0.7,
                            "weighted_f1": 0.73,
                        },
                    ],
                    "walk_forward_summary": {
                        "folds": 2.0,
                        "accuracy": 0.77,
                        "macro_f1": 0.68,
                        "weighted_f1": 0.71,
                    },
                }
            ),
            encoding="utf-8",
        )
        hailo_metadata = directory / "hailo_metadata.json"
        hailo_metadata.write_text(
            json.dumps(
                {
                    "model_id": "trading-mlp-h8-v1",
                    "feature_contract": "hailo",
                    "features": FEATURES,
                    "label_map": LABEL_MAP,
                    "confidence_threshold": 0.5,
                    "data_provenance": {
                        "training_data_sha256": TRAINING_DATA_SHA256,
                        "label_map": LABEL_MAP,
                        "label_horizon": 6,
                        "train_fraction": 0.8,
                        "train_rows": 1200,
                        "validation_rows": 300,
                    },
                    "validation": _validation(accuracy=0.8, macro_f1=0.71),
                }
            ),
            encoding="utf-8",
        )
        return [cpu_model, cpu_metadata, hef, hailo_metadata]

    def _write_go_marker(self) -> pathlib.Path:
        marker = self.root / "latest_status.env"
        self._write_marker(marker, "GO")
        return marker

    @staticmethod
    def _write_marker(marker: pathlib.Path, verdict: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        marker.write_text(
            f"TIMESTAMP_UTC={stamp}\nRUN_MODE=standard\nVERDICT={verdict}\n",
            encoding="utf-8",
        )

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        """Run the release CLI with ``argv`` and return its exit code and output."""
        saved = sys.argv
        sys.argv = ["manage_model_release.py", *argv]
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                exit_code = release_cli()
        finally:
            sys.argv = saved
        return exit_code, buffer.getvalue()

    def promote_argv(self, release_id: str) -> list[str]:
        return [
            "promote",
            "--release-root", str(self.release_root),
            "--release-id", release_id,
            "--cpu-model", str(self.artifacts[0]),
            "--cpu-metadata", str(self.artifacts[1]),
            "--hailo-hef", str(self.artifacts[2]),
            "--hailo-metadata", str(self.artifacts[3]),
            "--scorecard-status-json", str(self.root / "absent.json"),
            "--scorecard-status-env", str(self.marker),
        ]

    def validate_argv(self, release_id: str) -> list[str]:
        return [
            "validate",
            "--release-root", str(self.release_root),
            "--release-id", release_id,
        ]

    def promote(self, release_id: str) -> str:
        exit_code, output = self.run_cli(self.promote_argv(release_id))
        self.assertEqual(exit_code, 0, f"promotion failed: {output}")
        return output

    def validate(self, release_id: str) -> str:
        exit_code, output = self.run_cli(self.validate_argv(release_id))
        self.assertEqual(exit_code, 0, f"validation failed: {output}")
        return output

    def active_target(self) -> str:
        return (self.release_root / "active").resolve().name

    def test_promote_validate_rollback_cycle(self) -> None:
        first = self.promote("trading-2026-01")
        self.assertIn("Scorecard gate accepted: verdict=GO", first)
        self.assertIn("Active model release:", first)
        self.assertEqual(self.active_target(), "trading-2026-01")

        # The staged release is exactly the four paired artifacts plus one
        # manifest, and the candidate workspace is left untouched.
        release = self.release_root / "releases" / "trading-2026-01"
        self.assertEqual(
            sorted(
                path.relative_to(release).as_posix()
                for path in release.rglob("*")
                if path.is_file()
            ),
            [
                "cpu/catboost_model.cbm",
                "cpu/metadata.json",
                "hailo/metadata.json",
                "hailo/model.hef",
                "manifest.json",
            ],
        )
        self.assertEqual(self.artifacts[0].read_bytes(), b"cpu model bytes")

        report = self.validate("trading-2026-01")
        self.assertIn("Release: trading-2026-01", report)
        self.assertIn("Feature contract: hailo (3 features)", report)
        self.assertIn(f"Training data sha256: {TRAINING_DATA_SHA256}", report)
        self.assertIn("cpu: rows=300 accuracy=0.8100 macro_f1=0.7200", report)
        self.assertIn("cpu walk-forward: folds=2 accuracy=0.7700", report)
        self.assertIn("hailo walk-forward: not recorded", report)
        self.assertIn("artifact hailo_hef: hailo/model.hef sha256=", report)
        self.assertIn("Model release validated:", report)

        # A second candidate becomes the active release; the first stays intact
        # so it remains a rollback target.
        self.artifacts = self._write_candidate(self.root / "candidate-2")
        self.promote("trading-2026-02")
        self.assertEqual(self.active_target(), "trading-2026-02")
        self.validate("trading-2026-01")

        exit_code, output = self.run_cli(
            ["rollback", "--release-root", str(self.release_root),
             "--release-id", "trading-2026-01"]
        )
        self.assertEqual(exit_code, 0, f"rollback failed: {output}")
        self.assertIn("Active model release:", output)
        self.assertEqual(self.active_target(), "trading-2026-01")
        self.validate("trading-2026-01")

    def test_promotion_refuses_without_fresh_go_verdict(self) -> None:
        self._write_marker(self.marker, "NO-GO")
        with self.assertRaises(SystemExit) as refused:
            self.run_cli(self.promote_argv("trading-2026-01"))
        self.assertIn("scorecard verdict is NO-GO", str(refused.exception))
        self.assertFalse((self.release_root / "active").exists())

    def test_validate_reports_tampered_artifact(self) -> None:
        self.promote("trading-2026-01")
        stored = self.release_root / "releases" / "trading-2026-01" / "hailo" / "model.hef"
        stored.write_bytes(b"swapped hef")

        exit_code, output = self.run_cli(self.validate_argv("trading-2026-01"))
        self.assertEqual(exit_code, 1)
        self.assertIn("checksum mismatch", output)
        # A failed inspection must not move the active release.
        self.assertEqual(self.active_target(), "trading-2026-01")

    def test_validate_reports_missing_release(self) -> None:
        exit_code, output = self.run_cli(self.validate_argv("never-promoted"))
        self.assertEqual(exit_code, 1)
        self.assertIn("never-promoted", output)

    def test_library_entry_points_match_the_cli(self) -> None:
        """The library API a cluster playbook would call stays usable."""
        release = promote_release(
            self.release_root,
            "library-release",
            self.artifacts[0],
            self.artifacts[1],
            self.artifacts[2],
            self.artifacts[3],
        )
        manifest = validate_release(release)
        self.assertEqual(manifest["release_id"], "library-release")
        self.assertEqual(
            manifest["validation_reports"]["cpu"]["provenance"]["training_data_sha256"],
            TRAINING_DATA_SHA256,
        )
        rollback_release(self.release_root, "library-release")
        self.assertEqual(self.active_target(), "library-release")

    def test_release_cli_is_staged_in_the_pi_package(self) -> None:
        """The documented release CLI has to ship in the Pi archive."""
        match = STAGING_STEP.search(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "release.yml has no staging step")
        tokens = {
            token.strip("\\").strip('"').strip("'")
            for line in match.group(1).splitlines()
            for token in re.split(r"\s+", line.strip())
        }
        self.assertIn("scripts/manage_model_release.py", tokens)
        self.assertIn("${PKG}/scripts/manage_model_release.py", tokens)

    def test_release_cli_stays_importable_without_ml_dependencies(self) -> None:
        """No numpy/pandas/catboost import may appear on the release CLI path."""
        for name in ("numpy", "pandas", "catboost", "sklearn", "torch"):
            self.assertNotIn(name, sys.modules)


if __name__ == "__main__":
    unittest.main()
