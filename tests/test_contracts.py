import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]


def load_json(relative_path: str):
    with (ROOT / relative_path).open(encoding="utf-8") as handle:
        return json.load(handle)


def validator(schema_name: str):
    schema = load_json(schema_name)
    resources = []
    for name in ("package.schema.json", "index.schema.json", "advisory.schema.json", "source.schema.json"):
        path = ROOT / name
        if path.exists():
            document = load_json(name)
            resource = Resource.from_contents(document)
            resources.extend(((document["$id"], resource), (path.resolve().as_uri(), resource)))
    return Draft202012Validator(schema, registry=Registry().with_resources(resources), format_checker=FormatChecker())


def valid_package(package_type="plugin"):
    details = {
        "mcp": {"registryType": "npm", "identifier": "@example/server", "transport": "stdio", "environmentVariables": ["API_KEY"], "remotes": [{"url": "https://example.test/mcp", "transport": "sse"}]},
        "plugin": {"manifestPath": ".agent/plugin.json", "sourceType": "git", "marketplaceUrl": "https://example.test/marketplace", "entrypoint": "src/index.js", "permissions": ["filesystem.read"]},
        "skill": {"skillPath": "SKILL.md", "allowedTools": ["read_file"], "compatibilityNotes": "Works with the documented Agent API.", "metadata": {"authoring": "example"}},
        "general": {"toolType": "prompt-library", "agentUse": "Provides reusable prompts to agent workflows.", "inputFormats": ["text"], "outputFormats": ["text"]},
        "bundle": {"members": [{"memberType": "package", "memberId": "example.base", "versionRange": "^1.0.0", "versionScheme": "semver", "agentId": "dsh"}, {"memberType": "bundle", "memberId": "example.nested", "override": {"agentVersionRange": "^0.2.0", "versionScheme": "semver", "reason": "Upstream declaration is conservative.", "createdAt": "2026-09-30T16:00:00Z", "createdBy": "admin"}}], "effectiveTargets": [{"agentId": "dsh", "agentVersionRange": ">=0.2.0 <0.3.0", "versionScheme": "semver", "compatibilityStatus": "known", "rangeSource": "override"}], "facetUnion": {"capabilities": ["gui.modify"]}, "calculationRevision": "2026-09-30T16:00:00Z"},
    }[package_type]
    return {
        "schemaVersion": 2, "id": "example.gui-skin" if package_type != "bundle" else "example.bundle", "name": "example-gui-skin" if package_type != "bundle" else "example-bundle", "displayName": "Example GUI Skin", "version": "1.2.3", "versionScheme": "semver", "description": "Metadata-only example record.", "releaseNotes": "Initial release.", "license": "MIT",
        "links": {"repository": "https://github.com/example/project", "homepage": "https://example.test", "readme": "https://github.com/example/project/blob/main/README.md", "license": "https://github.com/example/project/blob/main/LICENSE", "documentation": "https://example.test/docs", "changelog": "https://example.test/changelog", "issues": "https://github.com/example/project/issues"},
        "keywords": ["gui", "agent"], "maintainers": [{"name": "Example Maintainer", "role": "maintainer"}], "platform": {"os": ["linux"], "arch": ["x86_64"], "libc": ["glibc"], "runtime": ["python"]}, "type": package_type, "subtype": "skin" if package_type == "plugin" else None, f"{package_type}Details": details,
        "targets": [{"agentId": "dsh", "agentVersionRange": "^0.2.0", "versionScheme": "semver", "compatibilityStatus": "known", "targetMetadata": {"entrypoint": "src/index.js"}, "installMetadata": {"method": "copy"}, "status": "active"}, {"agentId": "hermes", "agentVersionRange": None, "compatibilityStatus": "unknown", "compatibilityNote": "Upstream did not publish a parseable Agent range.", "status": "experimental"}],
        "distributions": [{"id": "github-release", "type": "release", "url": "https://github.com/example/project/releases/tag/v1.2.3", "agentIds": ["dsh", "hermes"], "version": "1.2.3", "ref": "v1.2.3", "priority": 10, "regions": ["global"], "checksum": {"sha256": "a" * 64}, "signature": {"url": "https://example.test/project.sig", "type": "sigstore"}, "install": {"type": "url", "url": "https://example.test/install"}, "notes": "Installer selects this candidate."}, {"id": "git-source", "type": "git", "url": "https://github.com/example/project.git", "ref": "v1.2.3", "priority": 20}],
        "dependencies": [{"id": "example.base", "versionRange": ">=1.0.0", "versionScheme": "semver", "agentId": "dsh"}], "conflicts": [{"id": "example.legacy", "reason": "Mutually exclusive UI hooks."}], "provides": [{"id": "example.gui-theme"}], "replaces": [{"id": "example.old-skin", "versionRange": "<1.0.0", "versionScheme": "semver"}],
        "facets": {"capabilities": ["gui.modify"], "effects": ["workspace.modify"], "dataPractices": ["none"], "permissions": ["filesystem.read"], "runtime": ["desktop"], "integrations": ["dsh"]}, "customFacets": {"capabilities": ["user.example.preview"], "other": ["user.example.note"]}, "lifecycle": {"status": "active", "since": "2026-09-30T16:00:00Z"}, "createdAt": "2026-09-30T15:00:00Z", "updatedAt": "2026-09-30T16:00:00Z", "publishedAt": "2026-09-30T16:00:00Z", "_meta": {"org.example/record": {"imported": True}},
    }


class PackageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package_validator = validator("package.schema.json")

    def assert_valid(self, instance):
        errors = list(self.package_validator.iter_errors(instance))
        self.assertEqual([], errors, "\n".join(error.message for error in errors))

    def assert_invalid(self, instance):
        self.assertTrue(list(self.package_validator.iter_errors(instance)))

    def test_all_five_types_require_details(self):
        for package_type in ("mcp", "plugin", "skill", "general", "bundle"):
            with self.subTest(package_type=package_type):
                self.assert_valid(valid_package(package_type))
                record = valid_package(package_type)
                del record[f"{package_type}Details"]
                self.assert_invalid(record)

    def test_general_uses_tool_type_and_never_kind(self):
        record = valid_package("general")
        self.assert_valid(record)
        record["generalDetails"]["kind"] = record["generalDetails"].pop("toolType")
        self.assert_invalid(record)

    def test_subtype_custom_facets_links_and_version_scheme(self):
        record = valid_package()
        self.assertEqual("skin", record["subtype"])
        self.assertIn("readme", record["links"])
        self.assertIn("license", record["links"])
        self.assertIn("capabilities", record["customFacets"])
        self.assert_valid(record)
        record["versionKind"] = "semver"
        self.assert_invalid(record)

    def test_unknown_agent_compatibility_is_explicit(self):
        record = valid_package()
        self.assert_valid(record)
        record["targets"][1]["compatibilityNote"] = None
        self.assert_invalid(record)
        record["targets"][1]["compatibilityNote"] = "No parseable range."
        record["targets"][1]["agentVersionRange"] = "^0.2.0"
        self.assert_invalid(record)

    def test_distributions_accept_multiple_install_candidates(self):
        record = valid_package()
        self.assert_valid(record)
        record["distributions"][0]["type"] = "not-a-distribution"
        self.assert_invalid(record)

    def test_bundle_members_support_nested_bundle_and_override(self):
        record = valid_package("bundle")
        self.assert_valid(record)
        record["bundleDetails"]["members"][0]["memberType"] = "invalid"
        self.assert_invalid(record)


class CatalogContractTests(unittest.TestCase):
    def test_catalog_manifest_supports_mirrors_separately_from_distributions(self):
        source_validator = validator("source.schema.json")
        source = {"schemaVersion": 2, "sourceId": "agent-forge:dsh:plugin", "name": "Agent Forge DSH Plugins", "agentId": "dsh", "type": "plugin", "baseUrl": "https://metaone01.github.io/agent-forge/sources/dsh/plugin/", "index": "index.json", "revision": "dsh-plugin-20261001T000000Z", "generatedAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-01T00:00:00Z", "mirrorOf": None, "priority": 10, "official": True, "description": "Canonical metadata catalog.", "indexChecksum": {"sha256": "b" * 64}, "sourceMirrors": [{"sourceId": "agent-forge-cn:dsh:plugin", "url": "https://mirror.example.cn/agent-forge/dsh/plugin/", "mirrorOf": "agent-forge:dsh:plugin", "revision": "dsh-plugin-20261001T000000Z", "priority": 20, "region": "CN", "official": False}], "relatedSources": [{"sourceId": "agent-forge-cn:dsh:plugin", "url": "https://mirror.example.cn/agent-forge/dsh/plugin/", "relation": "mirror", "revision": "dsh-plugin-20261001T000000Z", "priority": 20, "region": "CN"}]}
        self.assertFalse(list(source_validator.iter_errors(source)))
        source["sourceMirrors"][0]["priority"] = "not-an-integer"
        self.assertTrue(list(source_validator.iter_errors(source)))

    def test_index_records_revision_and_projection_fields(self):
        index_validator = validator("index.schema.json")
        index = {"schemaVersion": 2, "sourceId": "agent-forge:dsh:plugin", "sourceManifest": "source.json", "sourceUrl": "https://metaone01.github.io/agent-forge/sources/dsh/plugin/source.json", "agentId": "dsh", "type": "plugin", "revision": "dsh-plugin-20261001T000000Z", "generatedAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-01T00:00:00Z", "ttl": 28800, "indexChecksum": {"sha256": "c" * 64}, "packages": {"example.gui-skin": {"latest": "1.2.3", "versions": ["1.2.3"], "path": "packages/example.gui-skin/1.2.3.json", "checksum": {"sha256": "d" * 64}, "recordRevision": "dsh-plugin-20261001T000000Z", "subtype": "skin"}}}
        self.assertFalse(list(index_validator.iter_errors(index)))
        index["revision"] = "../escape"
        self.assertTrue(list(index_validator.iter_errors(index)))


class AdvisoryContractTests(unittest.TestCase):
    def test_advisory_v2_references_agent_aware_package(self):
        advisory_validator = validator("advisory.schema.json")
        record = {
            "schemaVersion": 2,
            "id": "GHSA-example",
            "aliases": ["CVE-2026-0001"],
            "source": "example-advisory-feed",
            "sourceUrl": "https://example.test/advisories/GHSA-example",
            "sourceRevision": "2026-10-01",
            "asOf": "2026-10-01T00:00:00Z",
            "status": "published",
            "disclaimer": "Third-party data; not a detection result or safety guarantee.",
            "affectedPackages": [{"name": "example-gui-skin", "sourceId": "agent-forge:dsh:plugin", "agentId": "dsh", "type": "plugin", "range": "<1.2.3", "versionScheme": "semver", "fixed": "1.2.3"}],
        }
        self.assertFalse(list(advisory_validator.iter_errors(record)))
        record["affectedPackages"][0]["type"] = "other"
        self.assertTrue(list(advisory_validator.iter_errors(record)))


if __name__ == "__main__":
    unittest.main()
