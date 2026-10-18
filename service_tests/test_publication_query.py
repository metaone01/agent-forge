import base64
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import errno
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_forge_service.errors import ServiceError
from agent_forge_service.publishing import SnapshotStore, build_and_publish
from agent_forge_service.query import Query, QueryService
from agent_forge_service.settings import Settings
from agent_forge_service.storage import backup_state, open_state, read_snapshot, writer_lock

NOW = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)


def record(name="alpha", *, identity=None, version="1.0.0", updated="2026-10-10T00:00:00Z", agents=("dsh",)):
    return {
        "schemaVersion": 2, "id": identity or "plugin." + name, "name": name,
        "version": version, "type": "plugin", "description": "Public metadata for terminal tools",
        "license": "MIT", "targets": [{"agentId": agent, "agentVersionRange": None,
            "versionScheme": "unknown", "compatibilityStatus": "unknown", "compatibilityNote": "No compatibility claim was supplied."} for agent in agents],
        "pluginDetails": {"manifestPath": "package.json"},
        "distributions": [{"id": "source", "type": "git", "url": "https://example.test/source.git"}],
        "updatedAt": updated, "keywords": ["terminal"], "facets": {"capabilities": ["gui.observe"]},
        "customFacets": {"other": ["personal-tag"]},
    }


class PublicationQueryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup_temporary)
        self.root = Path(self.temporary.name)
        self.catalog = self.root / "catalog"
        self.packages = self.catalog / "sources/plugin/packages"
        self.packages.mkdir(parents=True)
        self.settings = Settings(self.catalog, self.root / "runtime")

    def cleanup_temporary(self):
        for attempt in range(6):
            try:
                self.temporary.cleanup()
                return
            except OSError as error:
                if (error.errno != errno.ENOTEMPTY and getattr(error, "winerror", None) != 145) or attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def write(self, value, filename=None):
        filename = filename or value["name"].replace("/", "_") + "-" + value["version"] + ".json"
        path = self.packages / filename
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def index(self, packages):
        (self.catalog / "sources/plugin/index.json").write_text(json.dumps({"packages": packages}), encoding="utf-8")

    def build(self, revision="one", now=NOW, **kwargs):
        return build_and_publish(self.settings, now=now, revision=revision, **kwargs)

    def service(self):
        return QueryService(self.settings)

    def assert_code(self, code, function):
        with self.assertRaises(ServiceError) as raised:
            function()
        self.assertEqual(code, raised.exception.code)
        return raised.exception

    def test_schema_import_and_immutable_connection(self):
        self.write(record())
        manifest = self.build()
        self.assertEqual({"packages": 1, "versions": 1}, manifest["counts"])
        connection = read_snapshot(self.settings.snapshots / "one/catalog.db")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM packages")
        finally:
            connection.close()
        page = self.service().packages()
        self.assertEqual("plugin.alpha", page["items"][0]["id"])
        self.assertTrue(page["freshnessSummary"]["stale"])
        self.assertIsNone(page["items"][0]["observedLatest"])
        self.assertEqual("single-version", page["items"][0]["selectionStatus"])

    def test_canonical_index_not_lexical_version_controls_selection(self):
        self.write(record(version="2.0.0"))
        old = record(version="unversioned")
        old["description"] = "obsolete text"
        self.write(old)
        self.index({"alpha": {"latest": "2.0.0", "id": "plugin.alpha"}})
        self.build()
        self.assertEqual("2.0.0", self.service().package("plugin.alpha")["canonicalLatest"])
        self.assertEqual([], self.service().packages(Query(q="obsolete"))["items"])
        self.assertEqual(2, len(self.service().versions("plugin.alpha")["items"]))

    def test_ambiguous_latest_is_unknown_not_invented(self):
        self.write(record(version="1.0.0"))
        self.write(record(version="2.0.0"))
        self.build()
        item = self.service().package("plugin.alpha")
        self.assertIsNone(item["canonicalLatest"])
        self.assertIsNone(item["canonicalRecord"])
        self.assertEqual("unknown", item["selectionStatus"])

    def test_cutoff_and_undated_policy(self):
        self.write(record("old"))
        self.write(record("recent", updated="2026-10-11T07:55:00Z"))
        missing = record("undated")
        missing.pop("updatedAt")
        self.write(missing)
        result = self.build()
        self.assertEqual({"publication_cutoff": 1, "undated": 1}, result["skipped"])
        self.assertEqual(1, result["counts"]["packages"])
        self.build("two", include_undated=True)
        self.assertEqual(2, len(self.service().packages()["items"]))

    def test_index_latest_beyond_cutoff_does_not_fall_back_silently(self):
        self.write(record(version="1.0.0"))
        self.write(record(version="2.0.0", updated="2026-10-11T07:59:00Z"))
        self.index({"alpha": {"latest": "2.0.0"}})
        self.build()
        self.assertIsNone(self.service().package("plugin.alpha")["canonicalLatest"])

    def test_failed_build_does_not_replace_current(self):
        self.write(record())
        self.build()
        bad = record("bad")
        bad.pop("license")
        self.write(bad)
        with self.assertRaises(ValueError):
            self.build("two")
        self.assertEqual("one", SnapshotStore(self.settings.snapshots).current()["revision"])
        self.assertFalse((self.settings.snapshots / ".staging/two").exists())
        state = open_state(self.settings.state_path)
        try:
            self.assertEqual(1, state.execute("SELECT count(*) FROM package_units").fetchone()[0])
        finally:
            state.close()

    def test_duplicate_versions_fail_instead_of_overwrite(self):
        self.write(record(), "a.json")
        self.write(record(), "b.json")
        with self.assertRaises(sqlite3.IntegrityError):
            self.build()
        self.assertFalse((self.settings.snapshots / "current.json").exists())

    def test_identity_collision_fails(self):
        self.write(record("alpha"))
        self.write(record("beta", identity="plugin.alpha"))
        with self.assertRaises(ValueError):
            self.build()

    def test_local_name_collision_fails(self):
        self.write(record("alpha"), "a.json")
        self.write(record("alpha", identity="plugin.second"), "b.json")
        with self.assertRaises(ValueError):
            self.build()

    def test_index_identity_mismatch_fails(self):
        self.write(record())
        self.index({"alpha": {"latest": "1.0.0", "id": "wrong.id"}})
        with self.assertRaises(ValueError):
            self.build()

    def test_keyset_has_no_duplicates_with_equal_timestamps(self):
        for name in ("delta", "alpha", "beta", "gamma"):
            self.write(record(name))
        self.build()
        service, cursor, ids = self.service(), None, []
        while True:
            result = service.packages(Query(limit=1), cursor=cursor)
            ids.extend(item["id"] for item in result["items"])
            cursor = result["nextCursor"]
            if cursor is None:
                break
        self.assertEqual(["plugin.alpha", "plugin.beta", "plugin.delta", "plugin.gamma"], ids)

    def test_cursor_keeps_revision_after_publish(self):
        self.write(record("alpha"))
        self.write(record("beta"))
        self.build()
        service = self.service()
        first = service.packages(Query(limit=1))
        self.write(record("gamma"))
        self.build("two", now=NOW + timedelta(hours=8))
        second = service.packages(Query(limit=1), cursor=first["nextCursor"])
        self.assertEqual("one", second["revision"])
        self.assertEqual(["plugin.beta"], [item["id"] for item in second["items"]])
        self.assertEqual("two", service.packages()["revision"])
        self.assertEqual("one", service.package("plugin.alpha", revision="one")["revision"])

    def test_expired_revision_is_explicit(self):
        self.write(record("alpha"))
        self.write(record("beta"))
        self.build()
        service = self.service()
        cursor = service.packages(Query(limit=1))["nextCursor"]
        (self.settings.snapshots / "one").rename(self.settings.snapshots / "retired")
        self.assert_code("cursor_expired", lambda: service.packages(Query(limit=1), cursor=cursor))

    def test_cursor_rejects_changed_filters_and_malformed_values(self):
        self.write(record("alpha"))
        self.write(record("beta"))
        self.build()
        service = self.service()
        cursor = service.packages(Query(limit=1))["nextCursor"]
        self.assert_code("cursor_mismatch", lambda: service.packages(Query(limit=1, agent="dsh"), cursor=cursor))
        for invalid in ("../one", "!", "null", "a" * 65537):
            self.assert_code("invalid_cursor", lambda value=invalid: service.packages(cursor=value))
        nested = base64.urlsafe_b64encode(("[" * 2000 + "0" + "]" * 2000).encode()).decode().rstrip("=")
        self.assert_code("invalid_cursor", lambda: service.packages(cursor=nested))
        decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        decoded["last"] = [True, "value", "id"]
        forged = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
        self.assert_code("invalid_cursor", lambda: service.packages(Query(limit=1), cursor=forged))

    def test_fts_literal_input_and_relevance_pagination(self):
        for name in ("alpha", "beta", "gamma"):
            self.write(record(name))
        self.build()
        service = self.service()
        first = service.packages(Query(q="terminal", sort="relevance", limit=1))
        second = service.packages(Query(q="terminal", sort="relevance", limit=1), cursor=first["nextCursor"])
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        self.assertEqual([], service.packages(Query(q='" OR 1=1 --'))["items"])
        self.assert_code("invalid_query", lambda: service.packages(Query(q="*")))

    def test_chinese_trigram_and_short_name_tag_only(self):
        value = record("中文工具")
        value["displayName"] = "天气预报工具"
        value["description"] = "隐藏描述：其他语言"
        self.write(value)
        manifest = self.build()
        self.assertTrue(manifest["features"]["trigram"])
        self.assertEqual(1, len(self.service().packages(Query(q="天气预报"))["items"]))
        self.assertEqual(1, len(self.service().packages(Query(q="中文", sort="relevance"))["items"]))
        self.assertEqual([], self.service().packages(Query(q="隐藏"))["items"])

    def test_agent_and_facet_filters_do_not_mix_custom_facets(self):
        self.write(record("alpha", agents=("dsh", "another")))
        self.write(record("beta", agents=("another",)))
        self.build()
        result = self.service().packages(Query(agent="dsh", facets={"capabilities": ["gui.observe"]}, custom_facets={"other": ["personal-tag"]}))
        self.assertEqual(["plugin.alpha"], [item["id"] for item in result["items"]])
        self.assertEqual([], self.service().packages(Query(facets={"capabilities": ["personal-tag"]}))["items"])
        facets = self.service().facets()
        self.assertIn("other", facets["customFacets"])
        self.assertNotIn("other", facets["facets"])

    def test_slash_unicode_and_long_package_ids(self):
        identity = "扩展/" + "甲" * 1400
        self.write(record("unicode", identity=identity))
        self.build()
        self.assertEqual(identity, self.service().package(identity)["id"])

    def test_first_seen_bootstrap_idempotency_rename_and_removal(self):
        path = self.write(record())
        self.build()
        first = self.service().package("plugin.alpha")["firstSeenAt"]
        events = self.service().changes()["items"]
        self.assertEqual("canonical_bootstrap", events[0]["eventType"])
        renamed = record("renamed", identity="plugin.alpha")
        self.write(renamed, path.name)
        self.write(record("new"))
        self.build("two", now=NOW + timedelta(hours=8))
        service = self.service()
        self.assertEqual(first, service.package("plugin.alpha")["firstSeenAt"])
        self.assertFalse(service.package("plugin.new")["bootstrap"])
        self.assertEqual(3, len(service.changes()["items"]))
        self.build("three", now=NOW + timedelta(hours=16))
        self.assertEqual(3, len(service.changes()["items"]))
        path.unlink()
        self.build("four", now=NOW + timedelta(days=1))
        self.assertEqual("canonical_removed", self.service().changes()["items"][-1]["eventType"])
        self.write(renamed, path.name)
        self.build("five", now=NOW + timedelta(days=2))
        self.assertEqual("canonical_restored", self.service().changes()["items"][-1]["eventType"])
        self.assertEqual(first, self.service().package("plugin.alpha")["firstSeenAt"])

    def test_changes_and_versions_are_paginated(self):
        self.write(record(version="1.0.0"))
        self.write(record(version="2.0.0"))
        self.write(record("beta"))
        self.build()
        service = self.service()
        first = service.versions("plugin.alpha", limit=1)
        self.assertEqual("2.0.0", service.versions("plugin.alpha", limit=1, cursor=first["nextCursor"])["items"][0]["version"])
        changes = service.changes(limit=1)
        self.assertEqual(2, service.changes(limit=1, cursor=changes["nextCursor"])["items"][0]["seq"])
        self.assert_code("cursor_mismatch", lambda: service.changes(since=1, limit=1, cursor=changes["nextCursor"]))

    def test_manifest_database_and_pointer_integrity(self):
        self.write(record())
        self.build()
        current_path = self.settings.snapshots / "current.json"
        pointer = json.loads(current_path.read_text())
        pointer["manifestSha256"] = "0" * 64
        current_path.write_text(json.dumps(pointer))
        self.assert_code("snapshot_unavailable", lambda: self.service().packages())
        with (self.settings.snapshots / "one/catalog.db").open("ab") as handle:
            handle.write(b"tampered")
        self.assert_code("snapshot_unavailable", lambda: SnapshotStore(self.settings.snapshots).load("one"))

    def test_invalid_and_existing_revision_are_rejected(self):
        self.write(record())
        self.assert_code("invalid_revision", lambda: self.build("../escape"))
        self.build()
        self.assert_code("revision_exists", lambda: self.build())

    def test_os_lock_cannot_be_bypassed_by_another_worker(self):
        self.write(record())
        with writer_lock(self.settings.runtime_root):
            self.assert_code("worker_busy", lambda: self.build())
        self.build()

    def test_state_backup_is_consistent_and_does_not_overwrite(self):
        self.write(record())
        self.build()
        target = self.root / "state-backup.db"
        backup_state(self.settings.state_path, target)
        connection = sqlite3.connect(target)
        try:
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT count(*) FROM package_units").fetchone()[0])
        finally:
            connection.close()
        with self.assertRaises(ValueError):
            backup_state(self.settings.state_path, target)

    def test_invalid_query_inputs_and_missing_records(self):
        self.write(record())
        self.build()
        service = self.service()
        for query in (Query(limit=0), Query(limit=True), Query(q="x" * 201), Query(type="other"),
                      Query(sort="relevance"), Query(facets={"other": ["x"]}), Query(facets={"capabilities": [None, "x"]})):
            self.assert_code("invalid_query", lambda value=query: service.packages(value))
        self.assert_code("invalid_query", lambda: service.packages(Query(sort="downloads")))
        self.assert_code("sort_unavailable", lambda: service.packages(Query(sort="most_used")))
        self.assert_code("scope_unavailable", lambda: service.packages(Query(scope="unknown")))
        self.assertEqual([], service.packages(Query(scope="discovery"))["items"])
        self.assert_code("not_found", lambda: service.package("missing"))
        self.assert_code("not_found", lambda: service.versions("missing"))

    def test_unknown_timestamps_are_last_and_cursor_crosses_null_boundary(self):
        self.write(record("known"))
        for name in ("unknown-a", "unknown-b"):
            value = record(name)
            value.pop("updatedAt")
            self.write(value)
        self.build(include_undated=True)
        cursor, names = None, []
        while True:
            page = self.service().packages(Query(limit=1), cursor=cursor)
            names.extend(item["name"] for item in page["items"])
            cursor = page["nextCursor"]
            if cursor is None:
                break
        self.assertEqual(["known", "unknown-a", "unknown-b"], names)

    def test_query_budget_interrupts_expensive_sql(self):
        self.write(record())
        self.build()
        service = QueryService(replace(self.settings, query_timeout_seconds=0.000001))
        def expensive():
            with service._connection("one") as connection:
                connection.execute("WITH RECURSIVE counter(x) AS(SELECT 1 UNION ALL SELECT x+1 FROM counter WHERE x<10000000) SELECT sum(x) FROM counter").fetchone()
        self.assert_code("query_timeout", expensive)

    def test_corrupt_pointer_revision_is_not_a_user_input_error(self):
        self.write(record())
        self.build()
        current = self.settings.snapshots / "current.json"
        current.write_text(json.dumps({"revision": "../escape", "manifestSha256": "0" * 64}))
        self.assert_code("snapshot_unavailable", lambda: self.service().packages())

    def test_runtime_cannot_overlap_canonical_sources(self):
        for runtime in (self.catalog, self.catalog.parent, self.catalog / "sources/state"):
            with self.assertRaises(ValueError):
                Settings(self.catalog, runtime)

    def test_lock_is_enforced_across_processes(self):
        self.write(record())
        with writer_lock(self.settings.runtime_root):
            process = subprocess.run([sys.executable, "-m", "agent_forge_service.worker", "build", "--catalog", str(self.catalog),
                                      "--runtime", str(self.settings.runtime_root), "--revision", "locked"],
                                     cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(2, process.returncode)
        self.assertEqual("worker_busy", json.loads(process.stderr)["error"]["code"])

    def test_worker_cli_real_subprocess(self):
        self.write(record())
        process = subprocess.run([sys.executable, "-m", "agent_forge_service.worker", "build", "--catalog", str(self.catalog),
                                  "--runtime", str(self.settings.runtime_root), "--as-of", NOW.isoformat(), "--revision", "cli"],
                                 cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertEqual("cli", json.loads(process.stdout)["revision"])


if __name__ == "__main__":
    unittest.main()
