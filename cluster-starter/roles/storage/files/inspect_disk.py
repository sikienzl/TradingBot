#!/usr/bin/python3
"""Fail closed before using a whole disk; emit its current Btrfs state."""
import json
import os
import stat
import subprocess
import sys


def inspect(device, expected_serial, mode):  # noqa: C901
    resolved = os.path.realpath(device)
    if not stat.S_ISBLK(os.stat(resolved).st_mode):
        raise ValueError("device is not a block device")
    result = subprocess.run(
        ["lsblk", "--json", "--paths", "--output",
         "PATH,TYPE,FSTYPE,MOUNTPOINTS,SERIAL", resolved],
        check=True, capture_output=True, text=True,
    )
    devices = json.loads(result.stdout)["blockdevices"]
    if len(devices) != 1:
        raise ValueError("expected exactly one disk")
    disk = devices[0]
    if disk["type"] != "disk" or disk["path"] != resolved:
        raise ValueError("target must be a whole disk")
    if not disk.get("serial") or disk["serial"].strip() != expected_serial:
        raise ValueError("disk serial does not match the confirmed serial")
    if disk.get("children"):
        raise ValueError("disk has partitions or dependent block devices")
    fs = disk.get("fstype")
    if fs == "btrfs":
        return {"format_needed": False}
    if mode != "format":
        raise ValueError("existing mode requires an existing Btrfs filesystem")
    if fs or any(disk.get("mountpoints") or []):
        raise ValueError("disk has a filesystem or is mounted")
    active_swap = subprocess.run(
        ["swapon", "--noheadings", "--raw", "--show=NAME"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    if any(
        os.path.realpath(path.strip()) == resolved
        for path in active_swap
        if path.strip().startswith("/")
    ):
        raise ValueError("disk is active swap")
    signatures = subprocess.run(
        ["wipefs", "--no-act", "--noheadings", "--output", "TYPE", resolved],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if signatures:
        raise ValueError("disk contains existing signatures")
    return {"format_needed": True}


if __name__ == "__main__":
    try:
        print(json.dumps(inspect(*sys.argv[1:])))
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        print(f"Refusing disk operation: {exc}", file=sys.stderr)
        sys.exit(1)
