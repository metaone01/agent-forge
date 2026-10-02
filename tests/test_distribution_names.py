import json
import tempfile
import unittest
from pathlib import Path

from tools.distribution_names import SOURCE_LABELS, distribution_name, distribution_type, normalize_distribution, normalize_plugin_source, rename
from tools.validate import build_validator


class DistributionNamesTests(unittest.TestCase):
    def test_platform_labels_do_not_mutate_input_or_mislabel_other_sources(self):
        for value, expected in (
            ({"type": "git", "url": "https://github.com/author/tool"}, "github repository"),
            ({"type": "registry", "url": "https://www.npmjs.com/package/tool"}, "npm package"),
            ({"type": "registry", "registry": "npm", "url": "https://registry.npmjs.org/tool"}, "npm package"),
            ({"type": "git", "url": "https://github.com.example.test/author/tool"}, None),
            ({"type": "release", "url": "https://github.com/author/tool/releases/tag/v1"}, None),
            ({"type": "registry", "registry": "pypi", "url": "https://pypi.org/project/tool/"}, "pypi package"),
        ):
            with self.subTest(value=value):
                before = dict(value)
                self.assertEqual(expected, distribution_name(value))
                self.assertEqual(before, value)

    def test_all_collected_platforms_and_archive_hosts_have_precise_types(self):
        cases = (
            ("pypi", "https://pypi.org/project/tool", "pypi-pkg"),
            ("nuget", "https://www.nuget.org/packages/Tool", "nuget-pkg"),
            ("cargo", "https://crates.io/crates/tool", "crates-pkg"),
            ("oci", "https://ghcr.io/author/tool", "ghcr-image"),
            ("oci", "https://docker.io/author/tool", "dockerhub-image"),
            ("oci", "https://hub.docker.com/r/author/tool", "dockerhub-image"),
            ("oci", "https://quay.io/author/tool", "quay-image"),
            ("oci", "https://us-central1-docker.pkg.dev/project/tool", "gcp-artifact-image"),
            ("oci", "https://author/tool", "oci-image"),
            ("oci", "https://ghcr.io.example.test/author/tool", "oci-image"),
            ("mcpb", "https://github.com/author/tool/releases/download/v1/tool.mcpb", "github-mcpb"),
            ("mcpb", "https://release-assets.githubusercontent.com/file.mcpb", "github-mcpb"),
            ("mcpb", "https://gitlab.com/author/tool/-/releases/v1/tool.mcpb", "gitlab-mcpb"),
            ("mcpb", "https://example.test/file.mcpb", "mcpb-pkg"),
        )
        validator = build_validator("package.schema.json")
        for registry, url, expected in cases:
            with self.subTest(url=url):
                distribution = {"id": "stable-source", "type": "registry", "registry": registry, "url": url}
                self.assertTrue(normalize_distribution(distribution))
                self.assertEqual(expected, distribution["type"])
                self.assertEqual(SOURCE_LABELS[expected], distribution["name"])
                self.assertEqual("stable-source", distribution["id"])
                self.assertFalse(normalize_distribution(distribution))
                self.assertEqual([], list(validator.evolve(schema=validator.schema["$defs"]["distribution"]).iter_errors(distribution)))

    def test_api_installation_does_not_make_a_general_tool_an_mcp_endpoint(self):
        distribution = {"type": "other", "url": "https://example.test/api", "install": {"type": "api"}}
        self.assertIsNone(distribution_type(distribution, "general"))
        self.assertEqual("mcp-endpoint", distribution_type(distribution, "mcp"))

    def test_migration_preserves_identity_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package_path = root / "sources/plugin/packages/tool.json"
            package_path.parent.mkdir(parents=True)
            record = {"id": "plugin.tool", "type": "plugin", "name": "tool", "version": "1", "distributions": [
                {"id": "source", "type": "git", "url": "https://github.com/author/tool", "ref": "v1"},
            ]}
            package_path.write_text(json.dumps(record), encoding="utf-8")
            report = rename(root, False)
            self.assertEqual(1, report["changedRecords"])
            self.assertEqual(record, json.loads(package_path.read_text()))
            rename(root, True)
            updated = json.loads(package_path.read_text())
            self.assertEqual("github repository", updated["distributions"][0].pop("name"))
            self.assertEqual("github-repo", updated["distributions"][0]["type"])
            updated["distributions"][0]["type"] = "git"
            del updated["updatedAt"]
            self.assertEqual(record, updated)
            self.assertEqual(0, rename(root, True)["changedRecords"])

    def test_source_types_and_display_names_are_supported_by_schema(self):
        validator = build_validator("package.schema.json")
        for kind, name, url in (("github-repo", "github repository", "https://github.com/author/tool"), ("npm-pkg", "npm package", "https://www.npmjs.com/package/tool")):
            distribution = {"id": "source", "name": name, "type": kind, "url": url}
            self.assertEqual([], list(validator.evolve(schema=validator.schema["$defs"]["distribution"]).iter_errors(distribution)))

    def test_plugin_source_type_only_changes_github_git_sources(self):
        for host, source_type, expected in (("github.com", "git", "github-repo"), ("gitlab.com", "git", "git"), ("github.com", "archive", "archive")):
            record = {"links": {"repository": "https://" + host + "/author/tool"}, "pluginDetails": {"sourceType": source_type}}
            normalize_plugin_source(record)
            self.assertEqual(expected, record["pluginDetails"]["sourceType"])
