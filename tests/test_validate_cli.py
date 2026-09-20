import json
import tempfile
import unittest
from pathlib import Path

from tools.validate import category_for_target, serialized_meta_size, validate_source_package


class ValidationToolTests(unittest.TestCase):
    def test_target_types_map_to_physical_source_categories(self):
        self.assertEqual("mcp", category_for_target("mcp"))
        self.assertEqual("plugin", category_for_target("agent-plugin"))
        self.assertEqual("skill", category_for_target("skill"))
        self.assertEqual("other", category_for_target("generic"))
        self.assertEqual("other", category_for_target("pacman"))

    def test_meta_size_is_utf8_serialized_bytes(self):
        instance = {"_meta": {"org.example/data": "汉" * 1400}}
        self.assertGreater(serialized_meta_size(instance), 4096)

    def test_package_in_wrong_source_category_is_rejected(self):
        instance = {
            "schemaVersion": 1,
            "name": "wrong-place",
            "version": "1.0.0",
            "description": "MCP record placed in the skill source",
            "license": "MIT",
            "target": {
                "type": "mcp",
                "repository": "npm",
                "typeRef": {"registryType": "npm", "identifier": "wrong-place"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            path.write_text(json.dumps(instance), encoding="utf-8")
            errors, warnings = validate_source_package(path, "skill")
        self.assertTrue(any("belongs to source category 'mcp'" in error for error in errors))
        self.assertEqual([], warnings)


if __name__ == "__main__":
    unittest.main()
