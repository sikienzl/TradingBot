import hashlib
import importlib.util
import pathlib
import unittest
import uuid
import zipfile
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "cluster_starter_release", ROOT / ".github/scripts/cluster_starter_release.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTest(unittest.TestCase):
    def test_build_real_starter_and_checksum(self):
        source = ROOT / "cluster-starter"
        paths = sorted(
            path.relative_to(ROOT).as_posix()
            for path in source.rglob("*")
            if path.is_file() and release.allowed_path(path.relative_to(source).as_posix())
        )
        tracked = ("\0".join(paths) + "\0").encode()
        version = f"v1.2.3-test.{uuid.uuid4().hex}"
        archive = ROOT / f"cluster-starter-{version}.zip"
        checksum_path = archive.with_suffix(".zip.sha256")
        try:
            with patch.object(release.subprocess, "check_output", return_value=tracked):
                release.build(version, ROOT)
            checksum = checksum_path.read_text()
            self.assertEqual(checksum, f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
            with zipfile.ZipFile(archive) as zipped:
                self.assertEqual(
                    set(zipped.namelist()),
                    {f"cluster-starter-{version}/{p.removeprefix('cluster-starter/')}" for p in paths},
                )
        finally:
            archive.unlink(missing_ok=True)
            checksum_path.unlink(missing_ok=True)

    def test_rejects_private_paths(self):
        for path in ("inventory/private/hosts.yml", "inventory/example/group_vars/vault.yml",
                     "roles/x/files/.env", "roles/x/../vault.yml", "roles/x/private.pem"):
            with self.subTest(path=path):
                self.assertFalse(release.allowed_path(path))

    def test_rejects_credentials_and_non_placeholder_inventory(self):
        for name, data in (
            ("roles/x/tasks/main.yml", b"password: actual-password\n"),
            ("roles/x/tasks/main.yml", b"-----BEGIN OPENSSH PRIVATE KEY-----\n"),
            ("inventory/vault.example.yml", b"vault_k3s_token: real-token\n"),
            ("inventory/example/hosts.yml", b"ansible_host: 192.0.2.1\n"),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.check_content(name, data)

    def test_rejects_unsafe_versions(self):
        for version in ("../v1.0.0", "v1.0.0\nbad", "v1.0", "v01.0.0", "v1.0.0;echo"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                release.build(version, ROOT)

    def test_build_rejects_tracked_private_inventory(self):
        names = [f"cluster-starter/{name}" for name in release.REQUIRED]
        names.append("cluster-starter/inventory/private/hosts.yml")
        tracked = ("\0".join(names) + "\0").encode()
        with patch.object(release.subprocess, "check_output", return_value=tracked), \
                self.assertRaisesRegex(ValueError, "Unexpected starter path"):
            release.build("v1.2.3", ROOT)


if __name__ == "__main__":
    unittest.main()
