#!/usr/bin/env python3
"""Build and verify a release archive from tracked cluster-starter files."""

import argparse
import hashlib
import re
import stat
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
STARTER = "cluster-starter/"
REQUIRED = {
    "README.md",
    "ansible.cfg",
    "requirements-controller.txt",
    "requirements.yml",
    "inventory/example/hosts.yml",
    "inventory/example/group_vars/all.yml",
    "inventory/vault.example.yml",
    "playbooks/site.yml",
    "roles/storage/files/inspect_disk.py",
    "tests/test_disk_guard.py",
}
INVENTORY = {
    "inventory/example/hosts.yml",
    "inventory/example/group_vars/all.yml",
    "inventory/vault.example.yml",
}
EXAMPLE_PLACEHOLDERS = {
    "service_account_authorized_key", "nas_share", "nas_mount_path",
    "storage_device", "storage_expected_serial", "storage_mount_path", "storage_format_confirm_serial",
    "k3s_version", "k3s_installer_sha256", "k3s_server_address", "hailo_deb_url",
    "hailo_deb_sha256", "postgresql_db", "postgresql_user", "postgresql_client_cidr",
    "postgresql_k8s_namespace", "postgresql_k8s_service", "postgresql_k8s_endpoint_ip",
}
TOP_LEVEL = {"README.md", "ansible.cfg", "requirements-controller.txt", "requirements.yml", ".gitignore"}
SECRET_MARKERS = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----"
    r"|\$ANSIBLE_VAULT;"
    r"|gh[pousr]_[A-Za-z0-9]{36,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}",
    re.IGNORECASE,
)
ASSIGNMENT = re.compile(
    r"^\s*(?:[\w.-]*(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)[\w.-]*)"
    r"\s*:\s*(.*?)\s*$",
    re.IGNORECASE,
)
VERSION = re.compile(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?")


def allowed_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") or part.startswith(".") for part in path.parts):
        return name == ".gitignore"
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in path.parts):
        return False
    if name in TOP_LEVEL or name in INVENTORY:
        return True
    return (
        len(path.parts) >= 2
        and path.parts[0] in {"roles", "playbooks", "tests"}
        and path.suffix in {".yml", ".yaml", ".j2", ".py"}
        and not any(part in {"__pycache__", "vault.yml", "vault.yaml"} for part in path.parts)
    )


def check_content(name, data):  # noqa: C901
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Non-text file in starter: {name}") from exc
    if SECRET_MARKERS.search(text):
        raise ValueError(f"Possible credential material in {name}")
    for line in text.splitlines():
        match = ASSIGNMENT.match(line)
        if match:
            value = match.group(1).strip().strip("'\"")
            if value and not value.startswith(("REPLACE_WITH_", "{{")):
                raise ValueError(f"Possible literal credential in {name}")
    if name == "inventory/vault.example.yml":
        keys = re.findall(r"^(vault_[a-z0-9_]+):\s*(\S+)\s*$", text, re.MULTILINE)
        if len(keys) != 4 or any(not value.startswith("REPLACE_WITH_") for _, value in keys):
            raise ValueError("Vault example must contain four placeholder values")
    if name == "inventory/example/hosts.yml":
        for key in ("ansible_host", "k3s_node_name", "ansible_user"):
            values = re.findall(rf"^\s*{key}:\s*(\S+)\s*$", text, re.MULTILINE)
            if len(values) != (1 if key == "ansible_user" else 2) or any(
                not value.startswith("REPLACE_WITH_") for value in values
            ):
                raise ValueError(f"Example inventory {key} must use placeholders")
    if name == "inventory/example/group_vars/all.yml":
        for key in EXAMPLE_PLACEHOLDERS:
            values = re.findall(rf"^{key}:\s*(\S+)\s*$", text, re.MULTILINE)
            if len(values) != 1 or "REPLACE_WITH_" not in values[0]:
                raise ValueError(f"Example variable {key} must use a placeholder")
        targets = re.findall(r"^  - (\S+:9100)\s*$", text, re.MULTILINE)
        if len(targets) != 2 or any(not value.startswith("REPLACE_WITH_") for value in targets):
            raise ValueError("Example Prometheus targets must use placeholders")


def build(version, output_dir):  # noqa: C901
    if not VERSION.fullmatch(version):
        raise ValueError("Version must be vMAJOR.MINOR.PATCH with an optional prerelease suffix")
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--", STARTER], cwd=ROOT
    ).decode("utf-8").split("\0")
    names = sorted(name[len(STARTER):] for name in tracked if name)
    if not REQUIRED.issubset(names):
        raise ValueError("Missing required starter files: " + ", ".join(sorted(REQUIRED - set(names))))
    if len(names) != len(set(names)):
        raise ValueError("Duplicate tracked paths")

    files = {}
    for name in names:
        if not allowed_path(name):
            raise ValueError(f"Unexpected starter path: {name}")
        source = ROOT / STARTER / name
        if not stat.S_ISREG(source.lstat().st_mode) or any(
            parent.is_symlink() for parent in source.parents if parent != ROOT and ROOT in parent.parents
        ):
            raise ValueError(f"Non-regular file in starter: {name}")
        data = source.read_bytes()
        check_content(name, data)
        files[name] = data

    output_dir.mkdir(parents=True, exist_ok=True)
    package = f"cluster-starter-{version}"
    archive = output_dir / f"{package}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for name, data in files.items():
            zipped.writestr(f"{package}/{name}", data)

    with zipfile.ZipFile(archive) as zipped:
        expected = {f"{package}/{name}" for name in files}
        if len(zipped.namelist()) != len(expected) or set(zipped.namelist()) != expected:
            raise ValueError("Archive contents do not match the tracked starter files")
        if zipped.testzip() is not None:
            raise ValueError("Archive integrity check failed")
        for name, data in files.items():
            archived = zipped.read(f"{package}/{name}")
            if archived != data:
                raise ValueError(f"Archive content mismatch: {name}")
            check_content(name, archived)

    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum_path = output_dir / f"{package}.zip.sha256"
    checksum_path.write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    print(f"Verified {len(files)} files in {archive.name} and wrote {checksum_path.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    build(args.version, args.output_dir)
