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


REVISION = "dsh-plugin-20261001T000000Z"


def package_record(*, package_type="plugin", package_id="example.package", name="example.package", version="1.0.0", agents=("dsh",)):
    record = {
        "schemaVersion": 2,
        "id": package_id,
        "name": name,
        "version": version,
        "type": package_type,
        "description": "A valid metadata-only package record.",
        "license": "MIT",
        "targets": [
            {"agentId": agent, "agentVersionRange": "^0.2.0", "versionScheme": "semver", "compatibilityStatus": "known"}
            for agent in agents
        ],
    }
    if package_type == "plugin":
        record["pluginDetails"] = {"manifestPath": "plugin.json"}
        record["distributions"] = [{"id": "source", "type": "git", "url": "https://example.test/plugin.git"}]
    elif package_type == "mcp":
        record["mcpDetails"] = {"registryType": "npm", "identifier": package_id}
        record["distributions"] = [{"id": "source", "type": "registry", "url": "https://registry.npmjs.org/"}]
    elif package_type == "skill":
        record["skillDetails"] = {"skillPath": "skill.md"}
        record["distributions"] = [{"id": "source", "type": "git", "url": "https://example.test/skill.git"}]
    elif package_type == "general":
        record["generalDetails"] = {"toolType": "prompt-library", "agentUse": "Testing"}
        record["distributions"] = [{"id": "source", "type": "archive", "url": "https://example.test/tool.zip"}]
    elif package_type == "bundle":
        record["bundleDetails"] = {"members": [{"memberType": "package", "memberId": "example.member"}]}
    return record


def source_manifest(*, source_type="plugin", source_id="agent-forge:dsh:plugin", agent_id="dsh", revision=REVISION):
    return {
        "schemaVersion": 2,
        "sourceId": source_id,
        "name": "Agent Forge DSH Plugins",
        "agentId": agent_id,
        "type": source_type,
        "baseUrl": "https://example.test/sources/dsh/plugin/",
        "index": "index.json",
        "revision": revision,
        "generatedAt": "2026-10-01T00:00:00Z",
        "updatedAt": "2026-10-01T00:00:00Z",
    }


def source_index(source, *, revision=None):
    return {
        "schemaVersion": 2,
        "sourceId": source["sourceId"],
        "sourceManifest": "source.json",
        "agentId": source["agentId"],
        "type": source["type"],
        "revision": revision or source["revision"],
        "generatedAt": source["generatedAt"],
        "updatedAt": source["updatedAt"],
        "packages": {},
    }


class ValidationToolTests(unittest.TestCase):
    def test_package_types_map_to_physical_source_categories(self):
        for package_type in ("mcp", "plugin", "skill", "general", "bundle"):
            self.assertEqual(package_type, category_for_target(package_type))
        with self.assertRaises(KeyError):
            category_for_target("pacman")

    def test_meta_size_is_utf8_serialized_bytes(self):
        instance = {"_meta": {"org.example/data": "汉" * 1400}}
        self.assertGreater(serialized_meta_size(instance), 4096)

    def test_package_in_wrong_source_category_is_rejected(self):
        instance = package_record(package_type="mcp")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            path.write_text(json.dumps(instance), encoding="utf-8")
            errors, warnings = validate_source_package(path, "skill")
        self.assertTrue(any("belongs to source category 'mcp'" in error for error in errors))
        self.assertEqual([], warnings)

    def test_package_must_target_the_source_agent(self):
        instance = package_record(agents=("hermes",))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            path.write_text(json.dumps(instance), encoding="utf-8")
            errors, warnings = validate_source_package(path, "plugin", "dsh")
        self.assertTrue(any("do not include source Agent 'dsh'" in error for error in errors))
        self.assertEqual([], warnings)

    def test_source_manifest_and_index_projection_fields_must_match(self):
        source = source_manifest()
        index = source_index(source, revision="dsh-plugin-20261001T080000Z")
        index["sourceId"] = "agent-forge:other:plugin"
        index["agentId"] = "hermes"
        index["type"] = "mcp"
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "plugin"
            source_dir.mkdir()
            (source_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
            (source_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
            errors, warnings = validate_source_directory(source_dir)
        self.assertTrue(any("index sourceId" in error for error in errors))
        self.assertTrue(any("index agentId" in error for error in errors))
        self.assertTrue(any("index type" in error for error in errors))
        self.assertTrue(any("index revision" in error for error in errors))
        self.assertEqual([], warnings)

    def test_indexed_package_path_must_exist(self):
        source = source_manifest()
        index = source_index(source)
        index["packages"] = {"missing-package": {"latest": "1.0.0", "versions": ["1.0.0"], "path": "packages/missing-package.json"}}
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "plugin"
            source_dir.mkdir()
            (source_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
            (source_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
            errors, warnings = validate_source_directory(source_dir)
        self.assertTrue(any("does not exist" in error for error in errors))
        self.assertEqual([], warnings)

    def test_index_entry_must_match_package_identity_versions_and_revision(self):
        source = source_manifest()
        index = source_index(source)
        index["packages"] = {"index-name": {"latest": "2.0.0", "versions": ["1.0.0"], "path": "packages/record.json", "recordRevision": "dsh-plugin-20261001T080000Z"}}
        package = package_record(name="document-name", version="3.0.0")
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "plugin"
            packages_dir = source_dir / "packages"
            packages_dir.mkdir(parents=True)
            (source_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
            (source_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
            (packages_dir / "record.json").write_text(json.dumps(package), encoding="utf-8")
            errors, warnings = validate_source_directory(source_dir)
        self.assertTrue(any("latest version" in error for error in errors))
        self.assertTrue(any("does not match index key" in error for error in errors))
        self.assertTrue(any("is not listed" in error for error in errors))
        self.assertTrue(any("recordRevision" in error for error in errors))
        self.assertEqual([], warnings)

    def test_mirror_revision_must_match_manifest(self):
        source = source_manifest()
        source["sourceMirrors"] = [{"sourceId": "agent-forge-cn:dsh:plugin", "url": "https://mirror.example.test/agent-forge/", "mirrorOf": source["sourceId"], "revision": "dsh-plugin-20261001T080000Z", "priority": 20}]
        index = source_index(source)
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "plugin"
            source_dir.mkdir()
            (source_dir / "source.json").write_text(json.dumps(source), encoding="utf-8")
            (source_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
            errors, warnings = validate_source_directory(source_dir)
        self.assertTrue(any("source mirror" in error and "revision" in error for error in errors))
        self.assertEqual([], warnings)

    def test_unindexed_package_is_reported_as_warning(self):
        source = source_manifest()
        index = source_index(source)
        package = package_record()
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "plugin"
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
