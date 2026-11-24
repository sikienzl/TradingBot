import hashlib
import importlib.util
import pathlib
import tarfile
import unittest
import uuid
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "cluster_release", ROOT / ".github/scripts/cluster_release.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ClusterReleaseTest(unittest.TestCase):
    def test_build_real_archive_and_checksum(self):
        version = f"v1.2.3-test.{uuid.uuid4().hex}"
        output = ROOT / "dist-cluster-test"
        try:
            archive = release.build(version, output, commit="0" * 40)
            checksum_path = pathlib.Path(f"{archive}.sha256")
            self.assertEqual(
                checksum_path.read_text(),
                f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n",
            )
            with tarfile.open(archive, "r:gz") as tar:
                names = {
                    member.name.removeprefix(f"trading-cluster-{version}/")
                    for member in tar.getmembers()
                    if member.isfile()
                }
            self.assertTrue(set(release.REQUIRED).issubset(names))
            self.assertIn("version.txt", names)
        finally:
            for path in sorted(output.glob(f"trading-cluster-{version}*")):
                path.unlink()
            output.rmdir()

    def test_archive_is_deterministic(self):
        output = ROOT / "dist-cluster-test"
        try:
            first = release.build("v1.2.3-det", output, commit="0" * 40)
            second = release.build("v1.2.3-det", ROOT / "dist-cluster-test-2", commit="0" * 40)
            self.assertEqual(
                hashlib.sha256(first.read_bytes()).hexdigest(),
                hashlib.sha256(second.read_bytes()).hexdigest(),
            )
        finally:
            for path in (ROOT / "dist-cluster-test", ROOT / "dist-cluster-test-2"):
                for member in sorted(path.glob("*")):
                    member.unlink()
                path.rmdir()

    def test_excludes_pi_only_and_oversized_paths(self):
        for name in (
            "scripts/install_pi.sh",
            "scripts/create_pi_release.sh",
            "model/fine_tuned_trading_model/adapter_model.safetensors",
            "model/hailo_model.hef",
            "tests/test_trading_bot_risk.py",
            "src/__pycache__/trading_bot.cpython-314.pyc",
            "trade_journal.csv",
            ".env",
            "ansible/inventory/hosts.yml",
            "ansible/group_vars/paper_dashboard/vault.yml",
            "ansible/.vault_pass.txt",
            "ansible/files/dashboard.key",
        ):
            with self.subTest(name=name):
                self.assertFalse(release.allowed_path(name))

    def test_includes_cluster_paths(self):
        for name in (
            "docker/hailo-shadow.Dockerfile",
            "deploy/hailo-shadow.env.example",
            "src/hailo_service.py",
            "scripts/run_public_paper_daily_report.sh",
            "requirements-hailo-shadow.txt",
            "requirements-hailo-server.txt",
            "model/catboost_trading_model/catboost_model.cbm",
            ".env.paper_kraken.example",
            "ansible/README.md",
            "ansible/playbooks/dashboard.yml",
            "ansible/inventory/hosts.yml.example",
            "ansible/group_vars/paper_dashboard/vault.yml.example",
            "ansible/roles/public_paper_dashboard/templates/dashboard.env.j2",
        ):
            with self.subTest(name=name):
                self.assertTrue(release.allowed_path(name))

    def test_rejects_unsafe_paths(self):
        for name in (
            "/etc/passwd",
            "../outside.py",
            "src/../README.md",
            ".git/config",
            ".env.local",
        ):
            with self.subTest(name=name):
                self.assertFalse(release.allowed_path(name))

    def test_rejects_credentials_and_real_env_files(self):
        for name, data in (
            ("deploy/hailo-shadow.env.example", b"HAILO_API_TOKEN=super-secret-token\n"),
            ("deploy/x.env.example", b"ANALYTICS_DB_PASSWORD=hunter2\n"),
            ("deploy/private.env", b"KRAKEN_API_KEY=abc\n"),
            ("src/trading_bot.py", b"-----BEGIN OPENSSH PRIVATE KEY-----\n"),
            ("ansible/group_vars/paper_dashboard/vault.yml.example", b"$ANSIBLE_VAULT;1.1;AES256\n"),
            ("ansible/group_vars/paper_dashboard/vars.yml.example", b"vault_public_paper_dashboard_password: hunter2\n"),
            ("ansible/roles/public_paper_dashboard/defaults/main.yml", b"public_paper_dashboard_api_key: abc123\n"),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.check_content(name, data)

    def test_accepts_placeholder_env_templates(self):
        for name, data in (
            ("deploy/hailo-shadow.env.example", b"HAILO_API_TOKEN=\n"),
            ("deploy/x.env.example", b"DASHBOARD_PASSWORD=replace-with-a-long-random-password\n"),
            ("deploy/x.env.example", b"KRAKEN_API_KEY=REPLACE_WITH_KEY\n"),
            ("ansible/group_vars/paper_dashboard/vault.yml.example", b"vault_public_paper_dashboard_password: REPLACE_WITH_LONG_RANDOM_PASSWORD\n"),
            ("ansible/group_vars/paper_dashboard/vars.yml.example", b'public_paper_dashboard_password: "{{ vault_public_paper_dashboard_password }}"\n'),
            ("ansible/roles/public_paper_dashboard/defaults/main.yml", b'public_paper_dashboard_password: ""\n'),
            ("ansible/roles/public_paper_dashboard/templates/dashboard.env.j2", b"DASHBOARD_PASSWORD={{ public_paper_dashboard_password }}\n"),
        ):
            with self.subTest(name=name):
                release.check_content(name, data)

    def test_rejects_unsafe_versions(self):
        for version in ("../v1.0.0", "v1.0.0\nbad", "v1.0", "v01.0.0", "v1.0.0;echo", "1.0.0"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                release.build(version, ROOT / "dist-cluster-test")

    def test_missing_required_file_fails(self):
        remaining = [name for name in release.REQUIRED if name != "docker/hailo-shadow.Dockerfile"]
        required = tuple(remaining) + ("docker/does-not-exist.Dockerfile",)
        with patch.object(release, "REQUIRED", required), self.assertRaises(ValueError) as caught:
            release.build("v1.2.3", ROOT / "dist-cluster-test")
        self.assertIn("does-not-exist.Dockerfile", str(caught.exception))

    def test_archive_content_matches_tracked_files(self):
        output = ROOT / "dist-cluster-test"
        version = f"v1.2.3-cmp.{uuid.uuid4().hex[:8]}"
        try:
            archive = release.build(version, output)
            files = release.collect()
            with tarfile.open(archive, "r:gz") as tar:
                for name, data in files.items():
                    if name == "version.txt":
                        continue
                    self.assertEqual(tar.extractfile(f"trading-cluster-{version}/{name}").read(), data)
        finally:
            for path in sorted(output.glob("*")):
                path.unlink()
            output.rmdir()


if __name__ == "__main__":
    unittest.main()
