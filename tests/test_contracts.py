import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, RefResolver

ROOT = Path(__file__).resolve().parents[1]


def load_json(relative_path: str):
    with (ROOT / relative_path).open(encoding="utf-8") as handle:
        return json.load(handle)


def validator(schema_name: str):
    schema = load_json(schema_name)
    store = {}
    for name in (
        "package.schema.json",
        "index.schema.json",
        "advisory.schema.json",
        "source.schema.json",
    ):
        path = ROOT / name
        if path.exists():
            document = load_json(name)
            store[document["$id"]] = document
            store[path.resolve().as_uri()] = document
    return Draft202012Validator(
        schema,
        resolver=RefResolver.from_schema(schema, store=store),
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
            "target": {"type": "generic", "repository": "upstream"},
            "_meta": {"org.example/tool": {"kind": "prompt-library"}},
        }
        self.assert_valid(record)
        record["_meta"] = {"freeform": True}
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
