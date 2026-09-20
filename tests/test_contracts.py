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
    for name in (
        "package.schema.json",
        "index.schema.json",
        "advisory.schema.json",
        "source.schema.json",
    ):
        path = ROOT / name
        if path.exists():
            document = load_json(name)
            resource = Resource.from_contents(document)
            resources.append((document["$id"], resource))
            resources.append((path.resolve().as_uri(), resource))
    return Draft202012Validator(
        schema,
        registry=Registry().with_resources(resources),
        format_checker=FormatChecker(),
    )


class PackageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package_validator = validator("package.schema.json")

    def assert_valid(self, instance):
        errors = list(self.package_validator.iter_errors(instance))
        self.assertEqual([], errors, "\n".join(error.message for error in errors))

    def assert_invalid(self, instance):
        self.assertTrue(list(self.package_validator.iter_errors(instance)))

    def test_mcp_requires_mcp_type_details(self):
        record = {
            "schemaVersion": 1,
            "name": "example-mcp",
            "version": "1.0.0",
            "versionKind": "semver",
            "description": "Example MCP server",
            "license": "MIT",
            "target": {
                "type": "mcp",
                "repository": "npm",
                "package": "@example/mcp",
                "typeRef": {
                    "registryType": "npm",
                    "identifier": "@example/mcp",
                    "transport": "stdio",
                },
            },
        }
        self.assert_valid(record)
        del record["target"]["typeRef"]
        self.assert_invalid(record)

    def test_script_install_requires_integrity(self):
        record = {
            "schemaVersion": 1,
            "name": "scripted-plugin",
            "version": "1",
            "description": "Plugin installed by script",
            "license": "MIT",
            "target": {
                "type": "agent-plugin",
                "repository": "git",
                "typeRef": {
                    "pluginManifestPath": ".claude-plugin/plugin.json",
                    "sourceType": "git",
                },
                "install": {
                    "agent": {
                        "type": "script",
                        "url": "https://example.test/install.sh",
                    }
                },
            },
        }
        self.assert_invalid(record)
        record["target"]["install"]["agent"]["scriptIntegrity"] = "sha256-abc123"
        self.assert_valid(record)

    def test_meta_keys_use_reverse_dns_namespace(self):
        record = {
            "schemaVersion": 1,
            "name": "other-tool",
            "version": "2026.09",
            "description": "An uncategorized agent tool",
            "license": "unknown",
            "target": {
                "type": "generic",
                "repository": "upstream",
                "typeRef": {
                    "kind": "prompt-library",
                    "agentUse": "Provides reusable prompts to agent workflows.",
                },
            },
            "_meta": {"org.example/tool": {"kind": "prompt-library"}},
        }
        self.assert_valid(record)
        record["_meta"] = {"freeform": True}
        self.assert_invalid(record)

    def test_traditional_package_targets_are_out_of_scope(self):
        for target_type in ("pacman", "apt", "rpm", "npm", "pypi", "cargo"):
            with self.subTest(target_type=target_type):
                record = {
                    "schemaVersion": 1,
                    "name": "ordinary-package",
                    "version": "1.0.0",
                    "description": "A non-agent package-manager record",
                    "license": "MIT",
                    "target": {
                        "type": target_type,
                        "repository": "upstream",
                    },
                }
                self.assert_invalid(record)

    def test_generic_requires_agent_scope_evidence(self):
        record = {
            "schemaVersion": 1,
            "name": "generic-tool",
            "version": "1.0.0",
            "description": "An agent-related tool outside the primary categories",
            "license": "MIT",
            "target": {
                "type": "generic",
                "repository": "upstream",
                "typeRef": {
                    "kind": "agent-ui",
                    "agentUse": "Visualizes and controls agent workflow execution.",
                },
            },
        }
        self.assert_valid(record)
        del record["target"]["typeRef"]["agentUse"]
        self.assert_invalid(record)


class SourceContractTests(unittest.TestCase):
    def test_each_category_has_an_independent_source(self):
        source_validator = validator("source.schema.json")
        index_validator = validator("index.schema.json")
        expected = {
            "mcp": "agent-forge[mcp]",
            "plugin": "agent-forge[plugin]",
            "skill": "agent-forge[skill]",
            "other": "agent-forge[other]",
        }
        for category, source_name in expected.items():
            source = load_json(f"sources/{category}/source.json")
            index = load_json(f"sources/{category}/index.json")
            self.assertFalse(list(source_validator.iter_errors(source)))
            self.assertFalse(list(index_validator.iter_errors(index)))
            self.assertEqual(category, source["category"])
            self.assertEqual(source_name, source["name"])
            self.assertEqual(source["name"], index["source"])
            self.assertEqual(source["category"], index["category"])


if __name__ == "__main__":
    unittest.main()
