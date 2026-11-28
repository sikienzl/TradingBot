#!/usr/bin/env python3
"""Build and verify a source archive for the CM5 cluster deployment.

The Pi package (``trading-bot-pi-<version>.tar.gz``) carries the systemd units
and the one-shot Pi installer. The cluster deployment is different: the shadow
bot, the Hailo inference service and the dashboard run from Dockerfiles and
user units on two nodes, so it gets its own source archive built from the same
tag. Both archives are published in the same GitHub Release.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import re
import stat
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]

VERSION = re.compile(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?")

# Directories that are packaged wholesale (tracked files only). The ansible/
# directory is the generic paper-dashboard deployment starter referenced by
# SERVER_README.md; only its example templates are tracked, real inventories,
# vault files and TLS material are git-ignored and rejected below.
PACKAGED_DIRS = ("src", "docker", "deploy", "docs", "ansible")
# Real (non-example) ansible inventories, vaults and TLS key material must
# never enter an archive, even if someone force-adds them to git.
ANSIBLE_EXCLUDED = re.compile(
    r"^ansible/(?:files/[^/]+\.(?:crt|key|pem)$|\.vault_pass"
    r"|(?:inventory|group_vars)/.*(?:hosts\.yml|vault\.ya?ml)$)"
)
# The generic two-node CM5/K3s starter is referenced by SERVER_README.md and by
# the docs/ guides, so the cluster archive has to carry it. The allowlist below
# mirrors .github/scripts/cluster_starter_release.py, which publishes the same
# tree as a standalone pre-release: only the tracked example inventories and the
# playbook/role/test trees are packaged, never dot-files or real inventories.
STARTER_DIR = "cluster-starter"
STARTER_TOP_LEVEL = {
    f"{STARTER_DIR}/README.md",
    f"{STARTER_DIR}/ansible.cfg",
    f"{STARTER_DIR}/requirements-controller.txt",
    f"{STARTER_DIR}/requirements.yml",
}
STARTER_INVENTORY = re.compile(
    rf"^{STARTER_DIR}/inventory/(?:example/(?:hosts\.yml|group_vars/all\.yml)"
    r"|vault\.example\.yml)$"
)
STARTER_TREE = re.compile(
    rf"^{STARTER_DIR}/(?:roles|playbooks|tests)/.*\.(?:yml|yaml|j2|py)$"
)
STARTER_VAULT = re.compile(rf"^{STARTER_DIR}/.*vault\.ya?ml$")
# Single top-level files needed to rebuild and operate the cluster deployment.
PACKAGED_FILES = (
    "README.md",
    "SERVER_README.md",
    "SECURITY.md",
    "LICENSE",
    "CHANGELOG.md",
    ".dockerignore",
    ".env.example",
    "requirements.txt",
    "requirements-hailo-shadow.txt",
    "requirements-hailo-server.txt",
    "requirements-dashboard.txt",
)
# Environment templates are included only when they are *.example templates.
ENV_TEMPLATE = re.compile(r"^\.env(?:\.[A-Za-z0-9_.-]+)?\.example$")
# Runtime wrappers live in scripts/, but the Pi-only installers and the release
# builder itself do not belong in a cluster archive.
SCRIPTS_EXCLUDED = {
    "scripts/create_pi_release.sh",
    "scripts/install_monitoring_pi.sh",
    "scripts/install_pi.sh",
    "scripts/run_compare_simulation_regimes_on_pi.sh",
    "scripts/run_compare_simulation_standard_profiles_on_pi.sh",
}
# The CatBoost decision model is small and is referenced by the inference
# service (ReadOnlyPaths=.../model). The 27 MB LLM adapter is not used by the
# cluster shadow deployment and stays out of the archive.
MODEL_DIRS = ("model/catboost_trading_model",)
MODEL_EXCLUDED = re.compile(r"(^|/)fine_tuned_trading_model/|\.hef$")

REQUIRED = (
    "src/trading_bot.py",
    "src/hailo_service.py",
    "src/hailo_contract.py",
    "src/predict_hailo_remote.py",
    "src/public_paper_dashboard.py",
    "src/public_market_recorder.py",
    "docker/hailo-shadow.Dockerfile",
    "docker/public-paper-dashboard.Dockerfile",
    "deploy/hailo-inference.service",
    "deploy/trading-hailo-dryrun.service",
    "deploy/trading-hailo-tunnel.service",
    "deploy/hailo-shadow.env.example",
    "deploy/public-paper-daily-report.service",
    "deploy/postgresql-analytics-backup.service",
    "deploy/postgresql-analytics-backup.timer",
    "deploy/postgresql-analytics-restore-check.service",
    "deploy/postgresql-analytics-restore-check.timer",
    "scripts/postgresql-analytics-backup",
    "scripts/postgresql-analytics-restore-check",
    "scripts/run_public_paper_daily_report.sh",
    "scripts/public_paper_daily_report.py",
    "requirements-hailo-shadow.txt",
    "requirements-hailo-server.txt",
    "requirements-dashboard.txt",
    "SERVER_README.md",
    "ansible/README.md",
    "ansible/playbooks/dashboard.yml",
    "cluster-starter/README.md",
    "cluster-starter/ansible.cfg",
    "cluster-starter/playbooks/site.yml",
    "cluster-starter/inventory/example/hosts.yml",
    "version.txt",
)

SECRET_MARKERS = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----"
    r"|\$ANSIBLE_VAULT;"
    r"|gh[pousr]_[A-Za-z0-9]{36,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}",
)
# In *.env templates every credential value must stay empty or a placeholder.
ENV_ASSIGNMENT = re.compile(
    r"^\s*((?:[A-Z0-9_]*_)?(?:API_KEY|API_SECRET|TOKEN|SECRET|PASSWORD|PASSPHRASE))=(\S*)\s*$"
)
# In ansible YAML files credential-like variables must stay empty, a
# REPLACE_WITH_ placeholder or a vault/template reference.
YAML_ASSIGNMENT = re.compile(
    r"^\s*[\w.-]*(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)[\w.-]*"
    r"\s*:\s*(.*?)\s*$",
    re.IGNORECASE,
)
PLACEHOLDER = ("replace_with_", "replace-with-", "change_me", "change-me", "<", "{{")


def starter_allowed(name: str) -> bool:
    """Return True when a tracked cluster-starter path is a publishable file."""
    if STARTER_VAULT.search(name):
        return False
    if name in STARTER_TOP_LEVEL or STARTER_INVENTORY.fullmatch(name):
        return True
    return bool(STARTER_TREE.fullmatch(name))


def allowed_path(name: str) -> bool:  # noqa: C901
    """Return True when a tracked path belongs in the cluster archive."""
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return False
    if any(part == "__pycache__" for part in path.parts):
        return False
    if MODEL_EXCLUDED.search(name):
        return False
    if ANSIBLE_EXCLUDED.search(name):
        return False
    if path.parts[0] == STARTER_DIR:
        return starter_allowed(name)
    if name in PACKAGED_FILES:
        return True
    if name in SCRIPTS_EXCLUDED:
        return False
    if path.parts[0] in PACKAGED_DIRS:
        return True
    if path.parts[0] == "scripts":
        return True
    if len(path.parts) == 1 and ENV_TEMPLATE.fullmatch(name):
        return True
    return any(name == model or name.startswith(f"{model}/") for model in MODEL_DIRS)


def env_kind(name: str) -> str | None:
    """Classify a packaged path as a real env file, a template, or neither."""
    for part in PurePosixPath(name).parts:
        if ".env" not in part:
            continue
        if part.endswith((".example", ".j2")):
            return "template"
        return "real"
    return None


def check_content(name: str, data: bytes) -> None:  # noqa: C901
    """Reject credential material and filled-in secrets in packaged files."""
    if SECRET_MARKERS.search(data.decode("utf-8", "replace")):
        raise ValueError(f"Possible credential material in {name}")
    kind = env_kind(name)
    if kind == "real":
        raise ValueError(f"Environment file must not be packaged: {name}")
    if kind == "template":
        for line in data.decode("utf-8", "replace").splitlines():
            match = ENV_ASSIGNMENT.match(line)
            if not match:
                continue
            value = match.group(2)
            if value and not value.lower().startswith(PLACEHOLDER):
                raise ValueError(f"Filled-in secret in {name}: {match.group(1)}")
    if name.startswith(("ansible/", f"{STARTER_DIR}/")):
        for line in data.decode("utf-8", "replace").splitlines():
            match = YAML_ASSIGNMENT.match(line)
            if not match:
                continue
            value = match.group(1).strip().strip("'\"")
            if value and not value.lower().startswith(PLACEHOLDER):
                raise ValueError(f"Filled-in credential variable in {name}")


def tracked_files() -> list[str]:
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8")
    return sorted(name for name in raw.split("\0") if name)


def collect() -> dict[str, bytes]:
    names = tracked_files()
    files: dict[str, bytes] = {}
    for name in names:
        if not allowed_path(name):
            continue
        source = ROOT / name
        mode = source.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(f"Non-regular file in package: {name}")
        data = source.read_bytes()
        check_content(name, data)
        files[name] = data
    missing = sorted(set(REQUIRED) - set(files) - {"version.txt"})
    if missing:
        raise ValueError("Missing required cluster files: " + ", ".join(missing))
    return files


def version_file(version: str, commit: str) -> bytes:
    lines = [
        f"version={version}",
        f"git_commit={commit}",
        "target=cm5-cluster",
        "launch=docker build -f docker/hailo-shadow.Dockerfile",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def build(version: str, output_dir: Path, commit: str = "") -> Path:
    if not VERSION.fullmatch(version):
        raise ValueError("Version must be vMAJOR.MINOR.PATCH with an optional prerelease suffix")
    files = collect()
    files["version.txt"] = version_file(version, commit)

    output_dir.mkdir(parents=True, exist_ok=True)
    package = f"trading-cluster-{version}"
    archive = output_dir / f"{package}.tar.gz"
    # Deterministic archive: sorted members, fixed metadata, no timestamps and
    # a fixed gzip mtime, so two builds of one commit produce one checksum.
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=9, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w") as tar:
            directory = tarfile.TarInfo(package)
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            directory.mtime = 0
            tar.addfile(directory)
            for name in sorted(files):
                info = tarfile.TarInfo(f"{package}/{name}")
                info.size = len(files[name])
                # Keep the executable bit of tracked scripts (git mode 100755);
                # everything else is a plain 0644 data file.
                source = ROOT / name
                info.mode = (
                    0o755
                    if source.exists() and source.lstat().st_mode & stat.S_IXUSR
                    else 0o644
                )
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(files[name]))
    archive.write_bytes(buffer.getvalue())

    with tarfile.open(archive, "r:gz") as tar:
        members = [member for member in tar.getmembers() if member.isfile()]
        archived = {member.name for member in members}
        expected = {f"{package}/{name}" for name in files}
        if archived != expected:
            raise ValueError("Archive contents do not match the tracked cluster files")
        for name in REQUIRED:
            if f"{package}/{name}" not in archived:
                raise ValueError(f"Required file missing from archive: {name}")
        for member in members:
            data = tar.extractfile(member).read()
            name = member.name[len(package) + 1:]
            if name != "version.txt" and data != files[name]:
                raise ValueError(f"Archive content mismatch: {name}")
            check_content(name, data)

    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum_path = output_dir / f"{package}.tar.gz.sha256"
    checksum_path.write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    print(f"Verified {len(files)} files in {archive.name} and wrote {checksum_path.name}")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--commit", default="", help="Commit the archive was built from")
    args = parser.parse_args()
    build(args.version, args.output_dir, args.commit)
    sys.exit(0)
