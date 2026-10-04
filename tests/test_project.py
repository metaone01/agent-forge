import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.project import build_projection
from tools.snapshot import build_snapshot


def record(name: str, package_type: str, updated: str):
    details = {
        "mcp": {"registryType": "git", "identifier": name},
        "plugin": {"manifestPath": "plugin.json"},
        "skill": {"skillPath": "SKILL.md"},
        "general": {"toolType": "prompt-library", "agentUse": "test"},
        "bundle": {"members": [{"memberType": "package", "memberId": "example.member"}]},
    }[package_type]
    result = {
        "schemaVersion": 2,
        "id": f"dsh.{name}",
        "name": name,
        "version": "1.0.0",
        "type": package_type,
        "description": "test record",
        "license": "MIT",
        "targets": [{"agentId": "dsh", "agentVersionRange": "^0.2.0", "versionScheme": "semver", "compatibilityStatus": "known"}],
        "facets": {"capabilities": ["gui.observe"]},
        f"{package_type}Details": details,
        "updatedAt": updated,
    }
    if package_type != "bundle":
        result["distributions"] = [{"id": "source", "type": "git", "url": "https://example.test/source.git"}]
    return result


class ProjectionTests(unittest.TestCase):
    def test_canonical_latest_controls_path_checksum_and_search_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sources/plugin"
            (source / "packages").mkdir(parents=True)
            for version, description in (("2.0.0", "current description"), ("unversioned", "old snapshot")):
                value = record("tool", "plugin", "2026-10-01T00:00:00Z")
                value.update(version=version, description=description)
                (source / "packages" / (version + ".json")).write_text(json.dumps(value), encoding="utf-8")
            (source / "index.json").write_text(json.dumps({"packages": {"tool": {"latest": "2.0.0"}}}), encoding="utf-8")
            output = root / "data"
            now = datetime(2026, 10, 2, tzinfo=timezone.utc)
            build_projection(root, output, "test", now, now, "https://example.test/data", False)
            entry = json.loads((output / "dsh/plugin/index.json").read_text())["packages"]["tool"]
            payload = (output / "dsh/plugin" / entry["path"]).read_bytes()
            import hashlib
            self.assertEqual("2.0.0", entry["latest"])
            self.assertEqual("2.0.0", json.loads(payload)["version"])
            self.assertEqual(hashlib.sha256(payload).hexdigest(), entry["checksum"]["sha256"])
            self.assertIn("current description", entry["searchText"])
            self.assertEqual("dsh.tool", entry["id"])

    def test_display_name_comes_only_from_selected_version(self):
        from tools.validate import build_validator

        for title in ("当前 <显示名>", None):
            with self.subTest(title=title), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "sources/plugin"
                (source / "packages").mkdir(parents=True)
                for version in ("1.0.0", "2.0.0"):
                    value = record("tool", "plugin", "2026-10-01T00:00:00Z")
                    value["version"] = version
                    if version == "1.0.0":
                        value["displayName"] = "旧显示名"
                    elif title is not None:
                        value["displayName"] = title
                    (source / "packages" / (version + ".json")).write_text(json.dumps(value), encoding="utf-8")
                (source / "index.json").write_text(json.dumps({"packages": {"tool": {"latest": "2.0.0"}}}), encoding="utf-8")
                now = datetime(2026, 10, 2, tzinfo=timezone.utc)
                build_projection(root, root / "data", "test", now, now, "https://example.test/data", False)
                index = json.loads((root / "data/dsh/plugin/index.json").read_text())
                entry = index["packages"]["tool"]
                self.assertEqual("dsh.tool", entry["id"])
                self.assertEqual("tool", json.loads((root / "data/dsh/plugin" / entry["path"]).read_text())["name"])
                if title is None:
                    self.assertNotIn("displayName", entry)
                else:
                    self.assertEqual(title, entry["displayName"])
                self.assertEqual([], list(build_validator("index.schema.json").iter_errors(index)))

    def test_cutoff_projection_and_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packages = root / "sources" / "plugin" / "packages"
            packages.mkdir(parents=True)
            (packages / "old.json").write_text(json.dumps(record("old", "plugin", "2026-10-01T00:00:00Z")), encoding="utf-8")
            (packages / "new.json").write_text(json.dumps(record("new", "plugin", "2026-10-01T00:25:00Z")), encoding="utf-8")
            source = {
                "schemaVersion": 2, "sourceId": "agent-forge:dsh:plugin", "name": "test", "agentId": "dsh", "type": "plugin",
                "baseUrl": "https://example.test/data/dsh/plugin/", "index": "index.json", "revision": "old", "generatedAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-01T00:00:00Z"
            }
            source_path = root / "sources" / "plugin" / "source.json"
            source_path.write_text(json.dumps(source), encoding="utf-8")
            (source_path.parent / "index.json").write_text(json.dumps({"schemaVersion": 2, "sourceId": source["sourceId"], "sourceManifest": "source.json", "agentId": "dsh", "type": "plugin", "revision": "old", "generatedAt": source["generatedAt"], "updatedAt": source["updatedAt"], "packages": {}}), encoding="utf-8")
            output = root / "data"
            result = build_projection(root, output, "20261001T003000Z", datetime(2026, 10, 1, 0, 30, tzinfo=timezone.utc), datetime(2026, 10, 1, 0, 20, tzinfo=timezone.utc), "https://example.test/data", False)
            self.assertEqual(1, result["packageCount"])
            index = json.loads((output / "dsh" / "plugin" / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(["old"], list(index["packages"]))
            snapshots = root / "snapshots"
            self.assertEqual(5, build_snapshot(output, snapshots))
            self.assertTrue((snapshots / "dsh" / "plugin.db").exists())


if __name__ == "__main__":
    unittest.main()
