import copy
import contextlib
import io
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_contracts import valid_package
from tools.apply_media import apply_writes, encode, main, prepare_apply, replace_bytes, sha256
from tools.backfill_media import main as backfill_main
from tools.media import PROVENANCE_KEY, media_summary

STAMP = "2026-10-04T01:00:00Z"
REVISION = "media-20261004"


class MediaApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.review = self.root / "review"
        self.backup = self.root / "backup"
        self.relative = "sources/plugin/packages/skin/1.json"
        self.file = self.root / self.relative
        self.file.parent.mkdir(parents=True)
        self.record = valid_package("plugin")
        self.record["_meta"] = {"org.example/keep": {"value": "保留"}}
        self.file.write_bytes(encode(self.record))
        self.source = {"schemaVersion": 2, "sourceId": "test:plugin", "name": "Test source", "agentId": None,
                       "type": "plugin", "baseUrl": "https://example.test/plugin/", "index": "index.json",
                       "revision": "before", "generatedAt": "2026-10-01T00:00:00Z"}
        self.index = {"schemaVersion": 2, "sourceId": "test:plugin", "sourceManifest": "source.json",
                      "agentId": None, "type": "plugin", "revision": "before", "generatedAt": "2026-10-01T00:00:00Z",
                      "packages": {self.record["name"]: {"id": self.record["id"], "latest": self.record["version"],
                                  "versions": [self.record["version"]], "path": "packages/skin/1.json",
                                  "summary": "Keep summary", "recordRevision": "before"}}}
        self.write_catalog()
        self.observations = self.root / "observations.jsonl"
        self.observations.write_text(json.dumps({"recordPath": self.relative,
            "source": {"repository": "test/catalog", "revision": "a" * 40, "path": "plugins.json", "entry": "0"},
            "metadata": {"name": self.record["name"], "preview": "https://images.example/skin.png"}}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            backfill_main(["--root", str(self.root), "--observations", str(self.observations), "--patch-dir", str(self.review)])

    def write_catalog(self):
        (self.root / "sources/plugin/index.json").write_bytes(encode(self.index))
        (self.root / "sources/plugin/source.json").write_bytes(encode(self.source))

    def prepare(self):
        return prepare_apply(self.root, self.review, REVISION, STAMP)

    def catalog_bytes(self):
        return {file.relative_to(self.root).as_posix(): file.read_bytes() for file in (self.root / "sources").rglob("*.json")}

    def rewrite_plan(self, change):
        file = self.review / "plan.json"
        plan = json.loads(file.read_text(encoding="utf-8"))
        change(plan)
        file.write_bytes(encode(plan))

    def test_preflight_is_read_only_and_apply_preserves_identity_and_latest(self):
        before = self.catalog_bytes()
        writes, summary = self.prepare()
        self.assertEqual(before, self.catalog_bytes())
        self.assertEqual(3, summary["filesChanged"])
        self.assertEqual(1, summary["selectedRecordsChanged"])
        result = apply_writes(self.root, self.review, self.backup, writes, summary)
        self.assertEqual("applied", result["mode"])
        record = json.loads(self.file.read_bytes())
        for key in self.record:
            if key not in ("_meta", "updatedAt"):
                self.assertEqual(self.record[key], record[key], key)
        self.assertEqual(self.record["_meta"]["org.example/keep"], record["_meta"]["org.example/keep"])
        self.assertEqual(STAMP, record["updatedAt"])
        index = json.loads((self.root / "sources/plugin/index.json").read_bytes())
        entry = index["packages"][self.record["name"]]
        self.assertEqual(self.index["packages"][self.record["name"]]["latest"], entry["latest"])
        self.assertEqual("Keep summary", entry["summary"])
        self.assertEqual(media_summary(record), entry["media"])
        self.assertEqual(REVISION, entry["recordRevision"])
        source = json.loads((self.root / "sources/plugin/source.json").read_bytes())
        self.assertEqual(source["revision"], index["revision"])
        self.assertEqual(REVISION, source["revision"])
        for relative, content in before.items():
            self.assertEqual(content, (self.backup / relative).read_bytes())
        for item in result["files"]:
            self.assertEqual(item["originalSha256"], sha256(before[item["path"]]))
            self.assertEqual(item["appliedSha256"], sha256((self.root / item["path"]).read_bytes()))
        with self.assertRaisesRegex(ValueError, "Stale original hash"):
            prepare_apply(self.root, self.review, REVISION + "-again", STAMP)

    def test_default_cli_is_preflight_and_apply_needs_backup(self):
        before = self.catalog_bytes()
        self.assertEqual(0, main(["--root", str(self.root), "--review", str(self.review), "--revision", REVISION]))
        self.assertEqual(before, self.catalog_bytes())
        with self.assertRaises(SystemExit):
            main(["--root", str(self.root), "--review", str(self.review), "--revision", REVISION, "--apply"])
        self.assertEqual(before, self.catalog_bytes())

    def test_stale_hash_rejected_without_any_writes(self):
        self.file.write_bytes(self.file.read_bytes() + b" ")
        before = self.catalog_bytes()
        with self.assertRaisesRegex(ValueError, "Stale original hash"):
            self.prepare()
        self.assertEqual(before, self.catalog_bytes())
        self.assertFalse(self.backup.exists())

    def test_proposal_cannot_change_non_media_fields_or_existing_meta(self):
        file = self.review / self.relative
        original = json.loads(file.read_bytes())
        for change in (lambda value: value.update(description="Tampered"),
                       lambda value: value["_meta"].update({"org.example/keep": "Tampered"}),
                       lambda value: value.update(updatedAt=STAMP)):
            proposed = copy.deepcopy(original)
            change(proposed)
            file.write_bytes(encode(proposed))
            with self.assertRaisesRegex(ValueError, "non-media fields"):
                self.prepare()
        self.assertEqual(encode(self.record), self.file.read_bytes())

    def test_media_must_match_reviewed_plan(self):
        file = self.review / self.relative
        proposed = json.loads(file.read_bytes())
        proposed["media"]["previews"][0]["url"] = "https://images.example/unreviewed.png"
        file.write_bytes(encode(proposed))
        with self.assertRaisesRegex(ValueError, "plan identity/media"):
            self.prepare()

    def test_invalid_media_never_reaches_canonical_sources(self):
        file = self.review / self.relative
        proposed = json.loads(file.read_bytes())
        proposed["media"]["previews"][0]["url"] = "javascript:alert(1)"
        file.write_bytes(encode(proposed))
        with self.assertRaisesRegex(ValueError, "package.schema.json"):
            self.prepare()

    def test_duplicate_or_traversal_paths_are_rejected(self):
        plan_bytes = (self.review / "plan.json").read_bytes()
        self.rewrite_plan(lambda plan: (plan["changes"].append(plan["changes"][0]), plan["counts"].update(recordsChanged=2)))
        with self.assertRaisesRegex(ValueError, "duplicate record"):
            self.prepare()
        (self.review / "plan.json").write_bytes(plan_bytes)
        self.rewrite_plan(lambda plan: plan["changes"][0].update(recordPath="sources/plugin/../source.json"))
        with self.assertRaisesRegex(ValueError, "Unsafe relative path"):
            self.prepare()

    def test_plan_issues_wrong_root_or_count_are_rejected(self):
        original = (self.review / "plan.json").read_bytes()
        for change in (lambda plan: plan.update(root=str(self.root / "elsewhere")),
                       lambda plan: plan.update(issues=[{"reason": "conflict"}]),
                       lambda plan: plan["counts"].update(unmatchedCachedSources=1),
                       lambda plan: plan["counts"].update(recordsChanged=2)):
            (self.review / "plan.json").write_bytes(original)
            self.rewrite_plan(change)
            with self.assertRaises(ValueError):
                self.prepare()

    def test_backup_path_must_be_new_and_separate(self):
        writes, summary = self.prepare()
        before = self.catalog_bytes()
        for backup in (self.root, self.root / "sources/plugin/backup", self.review / "backup", self.review):
            with self.assertRaisesRegex(ValueError, "Backup directory"):
                apply_writes(self.root, self.review, backup, writes, summary)
        self.assertEqual(before, self.catalog_bytes())

    def test_concurrent_edit_after_preflight_is_preserved(self):
        writes, summary = self.prepare()
        self.file.write_bytes(self.file.read_bytes() + b" ")
        before = self.catalog_bytes()
        with self.assertRaisesRegex(ValueError, "changed after preflight"):
            apply_writes(self.root, self.review, self.backup, writes, summary)
        self.assertEqual(before, self.catalog_bytes())
        self.assertFalse(self.backup.exists())

    def test_partial_io_failure_rolls_back_original_bytes(self):
        writes, summary = self.prepare()
        before = self.catalog_bytes()
        calls = 0
        def failing_replace(target, content):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated failure")
            replace_bytes(target, content)
        with patch("tools.apply_media.replace_bytes", side_effect=failing_replace):
            with self.assertRaisesRegex(OSError, "simulated failure"):
                apply_writes(self.root, self.review, self.backup, writes, summary)
        self.assertEqual(before, self.catalog_bytes())
        self.assertEqual("rolled-back", json.loads((self.backup / "application.json").read_bytes())["mode"])

    def test_rollback_does_not_overwrite_a_concurrent_edit(self):
        writes, summary = self.prepare()
        calls = 0
        def failing_replace(target, content):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.file.write_bytes(b"concurrent edit")
                raise OSError("simulated failure")
            replace_bytes(target, content)
        with patch("tools.apply_media.replace_bytes", side_effect=failing_replace):
            with self.assertRaises(OSError):
                apply_writes(self.root, self.review, self.backup, writes, summary)
        self.assertEqual(b"concurrent edit", self.file.read_bytes())
        report = json.loads((self.backup / "application.json").read_bytes())
        self.assertEqual("rollback-incomplete", report["mode"])
        self.assertEqual(self.relative, report["rollbackErrors"][0]["path"])

    def test_existing_checksums_and_all_record_revisions_are_refreshed(self):
        for algorithm in ("sha256", "sha512", "blake2b"):
            self.index["packages"][self.record["name"]].setdefault("checksum", {})[algorithm] = hashlib.new(algorithm, self.file.read_bytes()).hexdigest()
        self.index["indexChecksum"] = {"sha256": "a" * 64}
        self.source["indexChecksum"] = {"sha256": "b" * 64}
        self.index["packages"]["other"] = {"latest": "1", "versions": ["1"], "path": "packages/other/1.json", "recordRevision": "before"}
        self.write_catalog()
        writes, _ = self.prepare()
        index = json.loads(writes["sources/plugin/index.json"][1])
        entry = index["packages"][self.record["name"]]
        for algorithm, digest in entry["checksum"].items():
            self.assertEqual(hashlib.new(algorithm, writes[self.relative][1]).hexdigest(), digest)
        self.assertEqual(REVISION, index["packages"]["other"]["recordRevision"])
        own_digest = index.pop("indexChecksum")["sha256"]
        self.assertEqual(sha256(encode(index)), own_digest)
        source = json.loads(writes["sources/plugin/source.json"][1])
        self.assertEqual(sha256(writes["sources/plugin/index.json"][1]), source["indexChecksum"]["sha256"])

    def test_historical_version_does_not_replace_latest_media(self):
        current = copy.deepcopy(self.record)
        current["version"] = "2.0.0"
        current["media"] = {"icon": {"url": "https://images.example/latest.png", "alt": "latest"}}
        latest_file = self.file.parent / "2.json"
        latest_file.write_bytes(encode(current))
        entry = self.index["packages"][self.record["name"]]
        entry.update(latest="2.0.0", versions=[self.record["version"], "2.0.0"], path="packages/skin/2.json", media=media_summary(current))
        self.write_catalog()
        writes, summary = self.prepare()
        index = json.loads(writes["sources/plugin/index.json"][1])
        self.assertEqual(0, summary["selectedRecordsChanged"])
        self.assertEqual(media_summary(current), index["packages"][current["name"]]["media"])
        self.assertNotIn("sources/plugin/packages/skin/2.json", writes)

    def test_existing_media_and_provenance_cannot_be_replaced(self):
        original = copy.deepcopy(self.record)
        original["media"] = {"icon": {"url": "https://images.example/old.png", "alt": "old"}}
        original["_meta"][PROVENANCE_KEY] = {"sources": [{"url": "https://example.test/old"}]}
        self.file.write_bytes(encode(original))
        proposal_file = self.review / self.relative
        proposed = json.loads(proposal_file.read_bytes())
        proposed["media"]["icon"] = {"url": "https://images.example/new.png", "alt": "new"}
        proposed["_meta"][PROVENANCE_KEY] = original["_meta"][PROVENANCE_KEY]
        proposal_file.write_bytes(encode(proposed))
        self.rewrite_plan(lambda plan: plan["changes"][0].update(originalSha256=sha256(self.file.read_bytes()), media=proposed["media"]))
        with self.assertRaisesRegex(ValueError, "existing media"):
            self.prepare()
        proposed["media"]["icon"] = original["media"]["icon"]
        proposed["_meta"][PROVENANCE_KEY] = {"sources": []}
        proposal_file.write_bytes(encode(proposed))
        self.rewrite_plan(lambda plan: plan["changes"][0].update(media=proposed["media"]))
        with self.assertRaisesRegex(ValueError, "existing provenance"):
            self.prepare()

    def test_revision_timestamp_and_index_identity_guards(self):
        for revision, stamp in (("before", STAMP), (REVISION, "2026-09-01T00:00:00Z"), (REVISION, "2026-10-04T00:00:00")):
            with self.assertRaises(ValueError):
                prepare_apply(self.root, self.review, revision, stamp)
        self.index["packages"][self.record["name"]]["latest"] = "other"
        self.write_catalog()
        with self.assertRaisesRegex(ValueError, "Latest identity mismatch"):
            self.prepare()


if __name__ == "__main__":
    unittest.main()
