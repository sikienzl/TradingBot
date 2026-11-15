#!/usr/bin/env python3
"""Reject machine-specific users and addresses in deployment configuration."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

IPV4_PATTERN = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
SERVICE_ACCOUNT_PATTERN = re.compile(r"^\s*(User|Group)\s*=\s*(\S+)\s*$")
SSH_TARGET_PATTERN = re.compile(r"(?<![\w./])([A-Za-z_][\w.-]*)@([A-Za-z0-9_.:-]+)")
ROOT_SERVICE = Path("deploy/node-exporter-textfile.service")


def is_deployment_file(path: Path) -> bool:
    return (
        path.parts[0] in {"deploy", "scripts"}
        or path.suffix == ".service"
        or (path.name.startswith(".env") and path.name.endswith(".example"))
        or path.name == "SERVER_README.md"
    )


def find_violations(path: Path, text: str) -> list[str]:
    violations = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        account_match = SERVICE_ACCOUNT_PATTERN.match(line)
        if account_match:
            directive, account = account_match.groups()
            allowed_account = (
                "root"
                if path == ROOT_SERVICE and directive == "User"
                else "trading"
            )
            if account != allowed_account:
                violations.append(
                    f"{line_number}: {directive} must be {allowed_account}, "
                    f"not {account}"
                )

        if not line.lstrip().startswith("#"):
            for match in SSH_TARGET_PATTERN.finditer(line):
                username, _ = match.groups()
                if username != "trading":
                    violations.append(
                        f"{line_number}: SSH target must use trading, "
                        f"not {username}"
                    )

        for match in IPV4_PATTERN.finditer(line):
            address = match.group()
            octets = [int(octet) for octet in address.split(".")]
            if any(octet > 255 for octet in octets):
                continue
            if octets[0] != 127:
                violations.append(
                    f"{line_number}: hard-coded non-loopback IPv4 address "
                    f"{address}"
                )

    return violations


def staged_files() -> list[tuple[Path, str]]:
    names = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    files = []
    for raw_path in names.split(b"\0"):
        if not raw_path:
            continue
        path = Path(raw_path.decode())
        if not is_deployment_file(path):
            continue
        content = subprocess.run(
            ["git", "show", f":{path.as_posix()}"],
            check=True,
            capture_output=True,
        ).stdout.decode()
        files.append((path, content))
    return files


def tracked_files() -> list[tuple[Path, str]]:
    names = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    files = []
    for raw_path in names.split(b"\0"):
        if not raw_path:
            continue
        path = Path(raw_path.decode())
        if is_deployment_file(path) and path.is_file():
            files.append((path, path.read_text(encoding="utf-8")))
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staged",
        action="store_true",
        help="inspect staged deployment files only (used by the Git hook)",
    )
    args = parser.parse_args()

    try:
        files = staged_files() if args.staged else tracked_files()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Deployment configuration check failed: {exc}", file=sys.stderr)
        return 2

    failures = 0
    for path, content in files:
        for violation in find_violations(path, content):
            print(f"{path}:{violation}", file=sys.stderr)
            failures += 1

    if failures:
        print(
            "Use the trading service account and configure host addresses "
            "outside the repository.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
