import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.backfill_media import main as backfill_main, plan_backfill, within
from tools.import_community import Importer, source_ref
from tools.media import MAX_PREVIEWS, PROVENANCE_KEY, media_summary, merge_media, normalize_media, resolve_image_url, safe_image_url
from tools.migrate_v1 import migrate_record
from tools.project import build_projection
from tools.validate import build_validator
from tests.test_contracts import valid_package

ROOT = Path(__file__).resolve().parents[1]
REF = source_ref("community/catalog", "a" * 40, "data/plugins.json", "0")


def sample():
    return valid_package("plugin")


def image(name="preview", **extra):
    return {"url": f"https://images.example/{name}.png", "alt": name, **extra}


class MediaNormalizationTests(unittest.TestCase):
    def test_absolute_claim_is_not_rewritten_to_a_guessed_commit(self):
        url = "https://raw.githubusercontent.com/author/skin/HEAD/preview.png"
        media, issues = normalize_media({"name": "skin", "icon": image("icon"), "preview": url, "screenshots": [url, image("second", theme="light")]})
        self.assertEqual([], issues)
        self.assertEqual([url, image("second")["url"]], [x["url"] for x in media["previews"]])
        self.assertEqual("skin preview", media["previews"][0]["alt"])

    def test_relative_paths_require_explicit_pinned_asset_context(self):
        context = {"repository": "author/skin", "revision": "a" * 40, "path": "assets/plugin.json"}
        self.assertEqual("https://raw.githubusercontent.com/author/skin/" + "a" * 40 + "/assets/preview.png", resolve_image_url("preview.png", context))
        for value in ("preview.png", "../../escape.png", "//evil.example/a", "/absolute.png", "javascript:alert(1)", "http://example.org/a", "preview.png?key=secret", "a%5Cb.png"):
            self.assertIsNone(resolve_image_url(value), value)
        self.assertIsNone(resolve_image_url("../../escape.png", context))
        self.assertIsNone(resolve_image_url("preview.png", {**context, "revision": "HEAD"}))
        self.assertIsNone(resolve_image_url("preview.png", {**context, "revision": "a" * 41}))
        self.assertIsNone(resolve_image_url("preview.png", {**context, "repository": "author/.."}))
        self.assertIsNone(resolve_image_url("preview.png", {**context, "revision": None}))
        self.assertIsNone(resolve_image_url("preview.png", "invalid-context"))
        self.assertIsNone(resolve_image_url("preview.png", {**context, "path": "../plugin.json"}))

    def test_order_conflicts_limits_and_provenance_are_idempotent(self):
        record = sample()
        media, issues = normalize_media({"media": {"icon": image("first")}, "icon": image("other"), "previews": [image(str(i)) for i in range(15)]})
        self.assertEqual(MAX_PREVIEWS, len(media["previews"]))
        self.assertEqual(4, len(issues))
        self.assertEqual([], merge_media(record, media, REF))
        once = copy.deepcopy(record)
        self.assertEqual([], merge_media(record, media, REF))
        self.assertEqual(once, record)
        conflicts = merge_media(record, {"icon": image("last"), "previews": [image("0", alt="changed")]}, {**REF, "entry": "1"})
        self.assertEqual(2, len(conflicts))
        self.assertEqual(once, record)
        self.assertEqual(13, len(record["_meta"][PROVENANCE_KEY]["sources"]))
        summary = media_summary(record)
        self.assertEqual(1, len(summary["previews"]))
        summary["icon"]["alt"] = "mutated"
        self.assertNotEqual(summary["icon"], record["media"]["icon"])

    def test_legacy_migration_keeps_declared_images_and_records_rejections(self):
        legacy = {"name": "skin", "version": "1", "description": "skin", "repository": "https://github.com/author/skin", "preview": image()["url"], "icon": "relative.png", "target": {"typeRef": {"pluginManifestPath": "plugin.json"}}}
        migrated = migrate_record(legacy, "plugin", "plugin/packages/skin.json")
        self.assertEqual(image()["url"], migrated["media"]["previews"][0]["url"])
        self.assertNotIn("icon", migrated["media"])
        self.assertTrue(migrated["_meta"]["org.agentforge/migration"]["mediaIssues"])
        self.assertEqual([], list(build_validator("package.schema.json").iter_errors(migrated)))

    def test_duplicate_import_observation_enriches_existing_package(self):
        with tempfile.TemporaryDirectory() as directory:
            importer = Importer(Path(directory), sources=[])
            importer.ingest_plugin({"repo": "author/skin", "kind": "skin"}, REF, "plugin.json")
            record = next(iter(importer.records.values()))
            identity = (record["id"], record["name"], record["version"])
            importer.ingest_plugin({"repo": "author/skin", "icon": image("first"), "preview": image()}, REF)
            importer.ingest_plugin({"repo": "author/skin", "icon": image("other"), "preview": image("second")}, {**REF, "entry": "1"}, "plugin.json")
            self.assertEqual(identity, (record["id"], record["name"], record["version"]))
            self.assertEqual(image("first"), record["media"]["icon"])
            self.assertEqual(2, len(record["media"]["previews"]))
            self.assertEqual(1, len(importer.unresolved))
            self.assertEqual([], list(build_validator("package.schema.json").iter_errors(record)))

    def test_projection_preserves_gallery_but_only_indexes_first_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); packages = root / "sources/plugin/packages"; packages.mkdir(parents=True)
            value = sample(); value["media"] = {"icon": image("icon"), "previews": [image("one"), image("two")]}
            (packages / "one.json").write_text(json.dumps(value), encoding="utf-8")
            now = datetime(2026, 10, 3, tzinfo=timezone.utc)
            build_projection(root, root / "data", "media-test", now, now, "https://example.org/data", False)
            agent = value["targets"][0]["agentId"]
            index = json.loads((root / f"data/{agent}/plugin/index.json").read_text())
            entry = index["packages"][value["name"]]
            payload = (root / f"data/{agent}/plugin" / entry["path"]).read_bytes()
            self.assertEqual(value["media"], json.loads(payload)["media"])
            self.assertEqual(value["media"]["previews"][:1], entry["media"]["previews"])
            self.assertEqual(hashlib.sha256(payload).hexdigest(), entry["checksum"]["sha256"])
            self.assertTrue(build_validator("index.schema.json").is_valid(index))
            (packages / "one.json").write_text(json.dumps({**value, "version": "0.1"}), encoding="utf-8")
            latest = {**value, "version": "9.0"}; latest.pop("media")
            (packages / "two.json").write_text(json.dumps(latest), encoding="utf-8")
            build_projection(root, root / "data", "media-test-2", now, now, "https://example.org/data", False)
            entry = json.loads((root / f"data/{agent}/plugin/index.json").read_text())["packages"][value["name"]]
            self.assertNotIn("media", entry)


class MediaBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.relative = "sources/plugin/packages/skin.json"
        self.file = self.root / self.relative; self.file.parent.mkdir(parents=True)
        self.record = sample(); self.record["links"]["repository"] = "https://github.com/author/skin"
        self.file.write_text(json.dumps(self.record), encoding="utf-8")
        self.cache = self.root / ".collection-cache"
        self.observations = self.root / "observations.jsonl"

    def observe(self, **extra):
        value = {"recordPath": self.relative, "source": REF, "metadata": {"repo": "author/skin", "icon": image("icon"), "preview": image()}, **extra}
        self.observations.write_text(json.dumps(value) + "\n", encoding="utf-8")

    def test_dry_run_patch_safety_identity_and_repeatability(self):
        self.observe(); original = self.file.read_bytes()
        report, proposed = plan_backfill(self.root, self.cache, self.observations)
        self.assertEqual(original, self.file.read_bytes())
        self.assertEqual(1, report["counts"]["recordsChanged"])
        self.assertEqual(1, report["counts"]["iconsAdded"])
        for key in ("id", "name", "version", "type", "targets", "createdAt", "updatedAt"):
            self.assertEqual(self.record.get(key), proposed[self.relative].get(key))
        patches = self.root / "review"
        self.assertEqual(0, backfill_main(["--root", str(self.root), "--observations", str(self.observations), "--patch-dir", str(patches)]))
        self.assertEqual(original, self.file.read_bytes())
        self.assertEqual(proposed[self.relative], json.loads((patches / self.relative).read_text(encoding="utf-8")))
        self.assertEqual(hashlib.sha256(original).hexdigest(), report["changes"][0]["originalSha256"])
        self.file.write_text(json.dumps(proposed[self.relative]), encoding="utf-8")
        self.assertEqual(0, plan_backfill(self.root, self.cache, self.observations)[0]["counts"]["recordsChanged"])

    def test_cached_revision_and_row_identity_must_match(self):
        self.record.setdefault("_meta", {})["org.agentforge/collection"] = {"sources": [REF]}
        self.file.write_text(json.dumps(self.record), encoding="utf-8")
        cached = self.cache / "catalogs/community/catalog/data/plugins.json"; cached.parent.mkdir(parents=True)
        cached.write_text(json.dumps({"plugins": [{"repo": "author/skin", "preview": image()["url"]}]}), encoding="utf-8")
        manifest = self.cache / "catalog-fetch.json"
        manifest.write_text(json.dumps([{**REF, "revision": "b" * 40, "fetched": [REF["path"]]}]), encoding="utf-8")
        report, proposed = plan_backfill(self.root, self.cache)
        self.assertEqual({}, proposed); self.assertEqual(1, report["counts"]["unmatchedCachedSources"])
        manifest.write_text(json.dumps([{**REF, "fetched": [REF["path"]]}]), encoding="utf-8")
        self.assertEqual(1, plan_backfill(self.root, self.cache)[0]["counts"]["recordsChanged"])
        cached.write_text(json.dumps({"plugins": [{"repo": "another/skin", "preview": image()["url"]}]}), encoding="utf-8")
        report, proposed = plan_backfill(self.root, self.cache)
        self.assertEqual({}, proposed); self.assertEqual(1, len(report["issues"]))

    def test_confirmed_topic_catalog_records_array_is_supported(self):
        self.record.setdefault("_meta", {})["org.agentforge/collection"] = {"sources": [REF]}
        self.file.write_text(json.dumps(self.record), encoding="utf-8")
        cached = self.cache / "catalogs/community/catalog/data/plugins.json"; cached.parent.mkdir(parents=True)
        cached.write_text(json.dumps({"records": [{"repository": "author/skin", "description": "Discovery only"}]}), encoding="utf-8")
        (self.cache / "catalog-fetch.json").write_text(json.dumps([{**REF, "fetched": [REF["path"]]}]), encoding="utf-8")
        report, proposed = plan_backfill(self.root, self.cache)
        self.assertEqual(1, report["counts"]["cachedRowsMatched"])
        self.assertEqual(0, report["counts"]["unmatchedCachedSources"])
        self.assertEqual({}, proposed)

    def test_unsafe_output_or_observation_never_writes_canonical_records(self):
        original = self.file.read_bytes()
        self.observe(recordPath="../outside.json")
        with self.assertRaises(ValueError): plan_backfill(self.root, self.cache, self.observations)
        self.observe()
        for args in (["--report", str(self.file)], ["--patch-dir", str(self.root / "sources/plugin")]):
            with self.assertRaises(SystemExit): backfill_main(["--root", str(self.root), "--observations", str(self.observations), *args])
        self.assertEqual(original, self.file.read_bytes())
        with self.assertRaises(ValueError): within(self.root, "sources/../package.json")


if __name__ == "__main__":
    unittest.main()

class MediaPreservationTests(unittest.TestCase):

    def test_importer_only_resolves_relative_assets_from_the_actual_manifest_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            importer = Importer(Path(directory), sources=[])
            ref = source_ref("author/skin", "a" * 40, "plugin.json")
            importer.ingest_plugin({"repo": "author/skin", "icon": "assets/icon.png", "preview": "assets/preview.png"}, ref, "plugin.json")
            record = next(iter(importer.records.values()))
            self.assertEqual("https://raw.githubusercontent.com/author/skin/" + "a" * 40 + "/assets/icon.png", record["media"]["icon"]["url"])
            self.assertEqual(1, len(record["media"]["previews"]))
        with tempfile.TemporaryDirectory() as directory:
            importer = Importer(Path(directory), sources=[])
            importer.ingest_plugin({"repo": "author/skin", "icon": "assets/icon.png"}, REF, "plugin.json")
            self.assertNotIn("media", next(iter(importer.records.values())))

    def test_opaque_provenance_extension_is_not_overwritten(self):
        record = sample(); record.setdefault("_meta", {})[PROVENANCE_KEY] = "opaque upstream extension"
        before = copy.deepcopy(record)
        self.assertTrue(merge_media(record, {"icon": image()}, REF))
        self.assertEqual(before, record)

    def test_importer_metadata_refresh_does_not_drop_existing_media(self):
        with tempfile.TemporaryDirectory() as directory:
            importer = Importer(Path(directory), sources=[])
            importer.ingest_plugin({"repo": "author/skin", "icon": image()}, REF, "plugin.json")
            record = next(iter(importer.records.values()))
            refreshed = copy.deepcopy(record); refreshed.pop("media"); refreshed["description"] = "New upstream description"
            key = importer.put(refreshed, ("plugin", "author/skin", ""), REF, overwrite=True)
            self.assertEqual(record["media"], importer.records[key]["media"])
            self.assertEqual("New upstream description", importer.records[key]["description"])


if __name__ == "__main__":
    unittest.main()
