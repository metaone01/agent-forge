"""Exercise checked-in examples through their actual metadata and service contracts."""
import subprocess
import sys
import unittest
from pathlib import Path

from agent_forge_service.batches import load_policy

ROOT = Path(__file__).resolve().parents[1]


class ExampleContractTests(unittest.TestCase):
    def test_checked_in_metadata_passes_validation_cli(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools/validate.py"), "--all"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_service_batch_examples_load_with_the_policy_contract(self):
        paths = sorted((ROOT / "agent_forge_service/examples").glob("batch-policy.*.json"))
        self.assertTrue(paths, "batch policies belong with the service examples")
        for path in paths:
            with self.subTest(path=path.name):
                load_policy(path)


if __name__ == "__main__":
    unittest.main()
