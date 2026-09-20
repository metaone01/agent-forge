import json
import tempfile
import unittest
from pathlib import Path

from tools.validate import (
    category_for_target,
    serialized_meta_size,
    validate_source_directory,
    validate_source_package,
)


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

    def test_source_manifest_and_index_must_match_directory(self):
        source = {
            "schemaVersion": 1,
            "name": "agent-forge[mcp]",
            "category": "mcp",
            "baseUrl": "https://example.test/sources/mcp/",
            "index": "index.json",
        }
        index = {
            "schemaVersion": 1,
            "source": "agent-forge[plugin]",
            "category": "plugin",
            "updatedAt": "2026-09-20T00:00:00Z",
            "packages": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "mcp"
            source_dir.mkdir()
            (source_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
            (source_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
            errors, warnings = validate_source_directory(source_dir)
        self.assertTrue(any("index source" in error for error in errors))
        self.assertTrue(any("index category" in error for error in errors))
        self.assertEqual([], warnings)

    def test_indexed_package_path_must_exist(self):
        source = {
            "schemaVersion": 1,
            "name": "agent-forge[mcp]",
            "category": "mcp",
            "baseUrl": "https://example.test/sources/mcp/",
            "index": "index.json",
        }
        index = {
            "schemaVersion": 1,
            "source": "agent-forge[mcp]",
            "category": "mcp",
            "updatedAt": "2026-09-20T00:00:00Z",
            "packages": {
                "missing-package": {
                    "latest": "1.0.0",
                    "versions": ["1.0.0"],
                    "path": "packages/missing-package.json",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "mcp"
            source_dir.mkdir()
            (source_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
            (source_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
            errors, warnings = validate_source_directory(source_dir)
        self.assertTrue(any("does not exist" in error for error in errors))
        self.assertEqual([], warnings)

    def test_index_entry_must_match_package_identity_and_versions(self):
        source = {
            "schemaVersion": 1,
            "name": "agent-forge[mcp]",
            "category": "mcp",
            "baseUrl": "https://example.test/sources/mcp/",
            "index": "index.json",
        }
        index = {
            "schemaVersion": 1,
            "source": "agent-forge[mcp]",
            "category": "mcp",
            "updatedAt": "2026-09-20T00:00:00Z",
            "packages": {
                "index-name": {
                    "latest": "2.0.0",
                    "versions": ["1.0.0"],
                    "path": "packages/record.json",
                }
            },
        }
        package = {
            "schemaVersion": 1,
            "name": "document-name",
            "version": "3.0.0",
            "description": "Valid package with mismatched index identity",
            "license": "MIT",
            "target": {
                "type": "mcp",
                "repository": "npm",
                "typeRef": {"registryType": "npm", "identifier": "document-name"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "mcp"
            packages_dir = source_dir / "packages"
            packages_dir.mkdir(parents=True)
            (source_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
            (source_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
            (packages_dir / "record.json").write_text(json.dumps(package), encoding="utf-8")
            errors, warnings = validate_source_directory(source_dir)
        self.assertTrue(any("latest version" in error for error in errors))
        self.assertTrue(any("does not match index key" in error for error in errors))
        self.assertTrue(any("is not listed" in error for error in errors))
        self.assertEqual([], warnings)

    def test_unindexed_package_is_reported_as_warning(self):
        source = {
            "schemaVersion": 1,
            "name": "agent-forge[mcp]",
            "category": "mcp",
            "baseUrl": "https://example.test/sources/mcp/",
            "index": "index.json",
        }
        index = {
            "schemaVersion": 1,
            "source": "agent-forge[mcp]",
            "category": "mcp",
            "updatedAt": "2026-09-20T00:00:00Z",
            "packages": {},
        }
        package = {
            "schemaVersion": 1,
            "name": "unindexed",
            "version": "1.0.0",
            "description": "Valid but not discoverable from the source index",
            "license": "MIT",
            "target": {
                "type": "mcp",
                "repository": "npm",
                "typeRef": {"registryType": "npm", "identifier": "unindexed"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "mcp"
            packages_dir = source_dir / "packages"
            packages_dir.mkdir(parents=True)
            (source_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
            (source_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
            (packages_dir / "record.json").write_text(json.dumps(package), encoding="utf-8")
            errors, warnings = validate_source_directory(source_dir)
        self.assertEqual([], errors)
        self.assertTrue(any("not listed in index.json" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()
