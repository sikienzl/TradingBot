import importlib.util
import json
import pathlib
import stat
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch


path = pathlib.Path(__file__).resolve().parents[1] / "roles/storage/files/inspect_disk.py"
spec = importlib.util.spec_from_file_location("inspect_disk", path)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class DiskGuardTest(unittest.TestCase):
    def setUp(self):
        self.disk = {
            "path": "/dev/disk1", "type": "disk", "fstype": None,
            "mountpoints": [None], "serial": "serial-1",
        }

    def run_guard(self, mode="format", signatures="", swap=""):
        def run(argv, **kwargs):
            if argv[0] == "lsblk":
                output = json.dumps({"blockdevices": [self.disk]})
            elif argv[0] == "swapon":
                output = swap
            else:
                output = signatures
            return subprocess.CompletedProcess(argv, 0, stdout=output)

        with patch.object(guard.os.path, "realpath", return_value="/dev/disk1"), \
             patch.object(guard.os, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
             patch.object(guard.subprocess, "run", side_effect=run):
            return guard.inspect("/dev/disk/by-id/id", "serial-1", mode)

    def test_blank_disk_requires_format_mode(self):
        self.assertEqual(self.run_guard(), {"format_needed": True})
        with self.assertRaises(ValueError):
            self.run_guard("existing")

    def test_existing_btrfs_is_not_formatted(self):
        self.disk["fstype"] = "btrfs"
        self.assertEqual(self.run_guard("existing"), {"format_needed": False})
        self.assertEqual(self.run_guard("format"), {"format_needed": False})

    def test_rejects_partitions_mounts_wrong_serial_and_other_filesystems(self):
        for field, value in [
            ("children", [{"path": "/dev/disk1p1"}]),
            ("mountpoints", ["/"]),
            ("serial", "different"),
            ("fstype", "ext4"),
            ("type", "part"),
        ]:
            with self.subTest(field=field), patch.dict(self.disk, {field: value}):
                with self.assertRaises(ValueError):
                    self.run_guard()

    def test_rejects_hidden_signatures(self):
        with self.assertRaises(ValueError):
            self.run_guard(signatures="gpt")

    def test_rejects_active_swap_disk(self):
        with self.assertRaisesRegex(ValueError, "active swap"):
            self.run_guard(swap="/dev/disk1\n")


if __name__ == "__main__":
    unittest.main()
