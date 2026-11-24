import unittest
from pathlib import Path

from scripts.check_deployment_config import find_violations


class DeploymentConfigCheckTests(unittest.TestCase):
    def test_allows_generic_service_account(self):
        self.assertEqual(
            find_violations(
                Path("deploy/example.service"),
                "[Service]\nUser=trading\nGroup=trading\n",
            ),
            [],
        )

    def test_rejects_non_generic_service_account_and_private_address(self):
        violations = find_violations(
            Path("deploy/example.service"),
            "[Service]\nUser=siegfried\nGroup=trading\n"
            "ExecStart=ssh siegfried@192.168.1.15\n",
        )

        self.assertEqual(len(violations), 3)
        self.assertTrue(any("User must be trading" in item for item in violations))
        self.assertTrue(any("SSH target must use trading" in item for item in violations))
        self.assertTrue(any("192.168.1.15" in item for item in violations))

    def test_allows_loopback_and_required_node_exporter_root(self):
        self.assertEqual(
            find_violations(
                Path("deploy/node-exporter-textfile.service"),
                "[Service]\nUser=root\nExecStart=/bin/ln -s 127.0.0.1\n",
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
