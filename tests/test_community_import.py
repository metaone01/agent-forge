import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.identity import identity_errors
from tools.import_community import Importer, source_ref
from tools.project import projected_record_path
from tools.validate import build_validator


REF = source_ref("community/catalog", "a" * 40, "data/plugins.json", "0")


class CommunityImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.importer = Importer(Path(self.directory.name), sources=[])

    def test_plugin_directory_without_manifest_stays_pending(self):
        self.importer.ingest_plugin({"repo": "author/project", "name": "project", "category": "plugin"}, REF)
        self.assertEqual({}, self.importer.records)
        self.assertEqual(1, len(self.importer.unresolved))

    def test_plugin_with_dsh_bundle_manifest_is_not_metadata_bundle(self):
        self.importer.ingest_plugin({"repo": "author/project", "category": "bundle", "evidence": "package.json#dsh.bundle"}, REF, "package.json")
        record = next(iter(self.importer.records.values()))
        self.assertEqual("plugin", record["type"])
        self.assertNotIn("bundleDetails", record)
        self.assertEqual([], list(build_validator("package.schema.json").iter_errors(record)))

    def test_same_short_name_in_different_repositories_does_not_merge(self):
        for owner in ("alice", "bob"):
            self.importer.ingest_plugin({"repo": f"{owner}/memory", "name": "memory"}, REF, "package.json")
        self.assertEqual(2, len(self.importer.records))
        self.assertEqual(2, len({record["id"] for record in self.importer.records.values()}))

    def test_distinct_plugins_in_monorepo_keep_independent_identities(self):
        for subpath in ("packages/ui", "packages/memory"):
            self.importer.ingest_plugin({"repo": "author/monorepo"}, REF, subpath + "/package.json", subpath)
        self.assertEqual(2, len(self.importer.records))

    def test_duplicate_sources_merge_provenance_not_compatible_ranges(self):
        row = {"repo": "author/project"}
        self.importer.ingest_plugin(row, REF, "package.json")
        another = source_ref("another/catalog", "b" * 40, "plugins.json")
        self.importer.ingest_plugin(row, another)
        self.assertEqual(1, len(self.importer.records))
        record = next(iter(self.importer.records.values()))
        self.assertEqual(2, len(record["_meta"]["org.agentforge/collection"]["sources"]))
        self.assertEqual("unknown", record["targets"][0]["compatibilityStatus"])

    def test_skill_identity_uses_repository_and_file_path_not_frontmatter_name(self):
        for repo, path in (("alice/skills", "skills/memory/SKILL.md"), ("bob/skills", "skills/memory/SKILL.md"), ("alice/skills", "skills/review/SKILL.md")):
            self.importer.ingest_skill(repo, path, {"name": "memory"}, REF, "a" * 40)
        self.assertEqual(3, len(self.importer.records))
        self.assertEqual(3, len({record["id"] for record in self.importer.records.values()}))

    def test_skill_path_traversal_does_not_publish(self):
        self.importer.ingest_skill("author/skills", "../SKILL.md", {}, REF)
        self.assertEqual({}, self.importer.records)

    def test_registry_duplicate_remotes_are_deduplicated(self):
        server = {"name": "org.example/memory", "version": "1", "remotes": [
            {"url": "https://example.test/mcp", "type": "streamable-http"},
            {"url": "https://example.test/mcp", "type": "streamable-http"},
        ]}
        with patch("tools.import_community.load", side_effect=[
            {"errors": [], "pages": [{"page": 1}]}, {"servers": [{"server": server}]},
        ]):
            self.importer.registry()
        record = next(iter(self.importer.records.values()))
        self.assertEqual(1, len(record["mcpDetails"]["remotes"]))
        self.assertEqual(1, len(record["distributions"]))
        self.assertEqual([], list(build_validator("package.schema.json").iter_errors(record)))

    def test_projected_paths_preserve_case_sensitive_name_and_version(self):
        self.assertNotEqual(projected_record_path("author/Tool", "V1").lower(), projected_record_path("author/tool", "V1").lower())
        self.assertNotEqual(projected_record_path("tool", "V1").lower(), projected_record_path("tool", "v1").lower())


class IdentityTests(unittest.TestCase):
    def test_equal_names_across_types_are_valid_with_distinct_ids(self):
        records = [("plugin.json", {"id": "plugin.memory", "type": "plugin", "name": "memory", "version": "1"}), ("skill.json", {"id": "skill.memory", "type": "skill", "name": "memory", "version": "1"})]
        self.assertEqual([], identity_errors(records))

    def test_global_id_cannot_describe_different_types(self):
        records = [("plugin.json", {"id": "shared.memory", "type": "plugin", "name": "memory", "version": "1"}), ("skill.json", {"id": "shared.memory", "type": "skill", "name": "memory", "version": "1"})]
        self.assertTrue(any("global package id" in error for error in identity_errors(records)))

    def test_source_local_name_cannot_describe_different_ids(self):
        records = [("a.json", {"id": "alice.tool", "type": "plugin", "name": "tool", "version": "1"}), ("b.json", {"id": "bob.tool", "type": "plugin", "name": "tool", "version": "2"})]
        self.assertTrue(any("source-local name" in error for error in identity_errors(records)))

    def test_versions_share_identity_but_duplicate_version_is_rejected(self):
        first = {"id": "plugin.tool", "type": "plugin", "name": "tool", "version": "1"}
        second = {**first, "version": "2"}
        self.assertEqual([], identity_errors([("a.json", first), ("b.json", second)]))
        self.assertTrue(identity_errors([("a.json", first), ("b.json", dict(first))]))
