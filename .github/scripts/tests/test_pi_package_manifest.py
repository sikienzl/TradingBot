"""Guard the single-node Pi package against installer drift.

The Pi archive is staged by an explicit ``cp`` list in
``.github/workflows/release.yml``. ``scripts/install_pi.sh`` and
``scripts/install_monitoring_pi.sh`` reference files under ``deploy/`` and
``scripts/`` directly, and the monitoring installer aborts when a systemd unit
is missing. These tests keep the two in sync without third-party dependencies.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
RELEASE_WORKFLOW = ROOT / ".github/workflows/release.yml"
INSTALL_PI = ROOT / "scripts/install_pi.sh"
INSTALL_MONITORING = ROOT / "scripts/install_monitoring_pi.sh"

# Paths the Pi installers may reference inside the extracted package.
PACKAGE_DIRS = ("deploy", "scripts")
# Loop variables used by the installers to iterate over unit names.
LOOP_RE = re.compile(r"^\s*for\s+(\w+)\s+in\s+([^;]+);\s*do\s*$", re.MULTILINE)
# Direct "${INSTALL_DIR}/deploy/name" style references (no variable expansion).
DIRECT_RE = re.compile(r"\$\{INSTALL_DIR\}/(deploy|scripts)/([A-Za-z0-9_.-]+)")
# Prose/doc references such as "deploy/trading-alert-relay.env.example".
PROSE_RE = re.compile(r"\b(deploy|scripts)/([A-Za-z0-9_.-]+)")
# Staged package paths inside the staging step, including "${PKG}/deploy/name".
STAGED_RE = re.compile(r"(?:\$\{PKG\}/|^)(deploy|scripts)/([A-Za-z0-9_.-]+)$")
EXEC_START_RE = re.compile(r"^(ExecStart|ExecStartPre|ExecStartPost|ExecStop)=(.*)$", re.MULTILINE)


def _staging_step() -> str:
    """Return the shell body of the "Stage runtime files" step."""
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r'-\s+name:\s+Stage runtime files\n(.*?)\n\s{6}-\s+name:', text, re.DOTALL)
    if match is None:
        raise AssertionError('release.yml has no "Stage runtime files" step')
    return match.group(1)


def staged_paths() -> set[str]:
    """Every deploy/ or scripts/ path the Pi staging step copies or requires."""
    step = _staging_step()
    found: set[str] = set()
    for line in step.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Drop the cp destination and shell punctuation, keep plain arguments.
        for token in re.split(r"\s+", stripped):
            token = token.strip('\\').strip('"').strip("'")
            match = STAGED_RE.match(token)
            if match:
                found.add(f"{match.group(1)}/{match.group(2)}")
    return found


def installer_loops(script: str) -> set[str]:
    """Unit names the installer iterates over under deploy/ or scripts/."""
    found: set[str] = set()
    for match in LOOP_RE.finditer(script):
        body_start = match.end()
        body = script[body_start : script.find("done", body_start)]
        for directory in PACKAGE_DIRS:
            if f"${{INSTALL_DIR}}/{directory}/${{{match.group(1)}}}" not in body:
                continue
            for name in match.group(2).split():
                found.add(f"{directory}/{name}")
    return found


def referenced_paths(script: str) -> set[str]:
    """Every deploy/ or scripts/ path an installer reads from the package."""
    found = {f"{m.group(1)}/{m.group(2)}" for m in DIRECT_RE.finditer(script)}
    found |= installer_loops(script)
    # Prose mentions (error text pointing an operator at a template) must exist too.
    for match in PROSE_RE.finditer(script):
        candidate = f"{match.group(1)}/{match.group(2)}"
        if (ROOT / candidate).exists():
            found.add(candidate)
    return found


class PiPackageManifestTest(unittest.TestCase):
    def test_staging_step_exists_and_is_non_empty(self) -> None:
        paths = staged_paths()
        self.assertIn("scripts/install_pi.sh", paths)
        self.assertIn("scripts/install_monitoring_pi.sh", paths)
        self.assertIn("deploy/trading-bot.service", paths)

    def test_installer_references_are_staged(self) -> None:
        staged = staged_paths()
        missing: set[str] = set()
        for installer in (INSTALL_PI, INSTALL_MONITORING):
            missing |= referenced_paths(installer.read_text(encoding="utf-8")) - staged
        self.assertEqual(
            missing,
            set(),
            "the Pi installers read package files that release.yml does not stage",
        )

    def test_monitoring_units_are_staged(self) -> None:
        # install_monitoring_pi.sh aborts (not warns) on a missing unit file.
        staged = staged_paths()
        for unit in (
            "pnl-exporter.service",
            "scorecard-status.service",
            "scorecard-status.timer",
            "node-exporter-textfile.service",
            "trading-operational-health.service",
            "trading-operational-health.timer",
            "trading-alert-relay.service",
        ):
            self.assertIn(f"deploy/{unit}", staged)

    def test_staged_paths_exist_in_repository(self) -> None:
        missing = sorted(path for path in staged_paths() if not (ROOT / path).exists())
        self.assertEqual(missing, [], "release.yml stages files that are not in the repository")

    def test_required_files_are_staged(self) -> None:
        step = _staging_step()
        block = re.search(r"required_files=\((.*?)\n\s*\)", step, re.DOTALL)
        self.assertIsNotNone(block, "release.yml has no required_files list")
        staged = staged_paths()
        missing = set()
        for line in block.group(1).splitlines():
            token = line.strip().strip('"')
            if not token:
                continue
            relative = token.replace("${PKG}/", "")
            if re.match(r"^(deploy|scripts)/", relative) and relative not in staged:
                missing.add(relative)
        self.assertEqual(missing, set(), "required_files entries are not staged by cp")

    def test_staged_units_reference_staged_scripts(self) -> None:
        """A unit shipped in the Pi package must not call a script that is not shipped."""
        staged = staged_paths()
        for path in sorted(staged):
            if not path.endswith((".service", ".timer")):
                continue
            text = (ROOT / path).read_text(encoding="utf-8")
            for match in EXEC_START_RE.finditer(text):
                command = match.group(2)
                for script in re.findall(r"/opt/trading_2/scripts/([A-Za-z0-9_.-]+)", command):
                    self.assertIn(
                        f"scripts/{script}",
                        staged,
                        f"{path} runs scripts/{script}, which is not staged",
                    )
                for module in re.findall(r"-m\s+scripts\.([A-Za-z0-9_]+)", command):
                    self.assertIn(
                        f"scripts/{module}.py",
                        staged,
                        f"{path} runs -m scripts.{module}, which is not staged",
                    )


if __name__ == "__main__":
    unittest.main()
