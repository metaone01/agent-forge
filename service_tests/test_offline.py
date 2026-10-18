"""Offline Agent/type membership, revision fidelity and transport validation."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch

from agent_forge_service.offline import export_offline, verify_offline
from agent_forge_service.publishing import atomic_json, digest_file
from agent_forge_service.settings import Settings
from agent_forge_service.storage import read_snapshot
from agent_forge_service.worker import main
from service_tests import test_publication_query as fixture
from tools.snapshot import main as snapshot_main, build_snapshot


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.PublicationQueryTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.write(fixture.record("alpha", version="1.0.0", agents=("dsh", "claude-code")))
        self.case.write(fixture.record("alpha", version="2.0.0", agents=("claude-code",)))
        self.case.write(fixture.record("unknown", version="1.0.0"))
        self.case.write(fixture.record("unknown", version="2.0.0"))
        self.case.index({"alpha": {"latest": "2.0.0"}})
        self.case.build("one")
        self.output = self.case.root / "offline"

    def export(self, **kwargs):
        return export_offline(self.case.settings, self.output, agent="dsh", package_type="plugin", **kwargs)

    def verify(self, result):
        return verify_offline(self.output, expected_export_sha256=result["exportSha256"])

    def edit(self, result, sql):
        db = sqlite3.connect(self.output / "catalog.db")
        try:
            db.executescript(sql)
            db.commit()
        finally:
            db.close()
        descriptor = json.loads((self.output / "offline.json").read_text())
        descriptor["database"].update(bytes=(self.output / "catalog.db").stat().st_size,
                                      sha256=digest_file(self.output / "catalog.db"))
        atomic_json(self.output / "offline.json", descriptor)
        result["exportSha256"] = digest_file(self.output / "offline.json")

    def test_version_scope_keeps_original_records_and_does_not_invent_latest(self):
        before = {p: digest_file(p) for p in [self.case.settings.state_path,
                  self.case.settings.snapshots / "current.json", self.case.settings.snapshots / "one/catalog.db"]}
        result = self.export()
        self.assertEqual({"packages": 2, "versions": 3}, result["counts"])
        self.assertTrue(self.verify(result)["valid"])
        db = read_snapshot(self.output / "catalog.db")
        source = read_snapshot(self.case.settings.snapshots / "one/catalog.db")
        try:
            alpha = db.execute("SELECT * FROM packages WHERE id='plugin.alpha'").fetchone()
            self.assertEqual(("2.0.0", None, "not_in_scope"), (alpha["canonical_latest"], alpha["selected_version"], alpha["selected_status"]))
            unknown = db.execute("SELECT * FROM packages WHERE id='plugin.unknown'").fetchone()
            self.assertEqual((None, None, "unknown"), (unknown["canonical_latest"], unknown["selected_version"], unknown["selected_status"]))
            row = db.execute("SELECT * FROM package_versions WHERE package_id='plugin.alpha'").fetchone()
            original = source.execute("SELECT * FROM package_versions WHERE package_id='plugin.alpha' AND version='1.0.0'").fetchone()
            self.assertEqual(tuple(original), tuple(row))
            self.assertEqual(2, len(json.loads(row["canonical_json"])["targets"]))
            for package in db.execute("SELECT id,first_seen_at,bootstrap FROM packages"):
                expected = source.execute("SELECT id,first_seen_at,bootstrap FROM packages WHERE id=?", (package["id"],)).fetchone()
                self.assertEqual(tuple(expected), tuple(package))
            self.assertEqual(1, db.execute("SELECT count(*) FROM packages_fts WHERE packages_fts MATCH ?", ('"alpha"',)).fetchone()[0])
        finally:
            db.close(); source.close()
        self.assertEqual(before, {p: digest_file(p) for p in before})

    def test_selected_latest_and_empty_scope_are_valid(self):
        result = export_offline(self.case.settings, self.output, agent="claude-code", package_type="plugin")
        self.assertTrue(self.verify(result)["valid"])
        db = read_snapshot(self.output / "catalog.db")
        try:
            self.assertEqual(("2.0.0", "available"), tuple(db.execute("SELECT selected_version,selected_status FROM packages").fetchone()))
        finally:
            db.close()
        empty = self.case.root / "empty"
        result = export_offline(self.case.settings, empty, agent="absent-agent", package_type="skill")
        self.assertEqual({"packages": 0, "versions": 0}, result["counts"])
        self.assertTrue(verify_offline(empty, expected_export_sha256=result["exportSha256"])["valid"])

    def test_explicit_old_revision_survives_current_change_and_needs_no_state(self):
        self.case.write(fixture.record("later"))
        self.case.build("two")
        public = self.case.root / "public"
        shutil.copytree(self.case.settings.snapshots, public / "snapshots")
        settings = Settings(self.case.root / "absent-canonical", public)
        result = export_offline(settings, self.output, agent="dsh", package_type="plugin", revision="one")
        self.assertEqual("one", result["source"]["revision"])
        self.assertEqual({"packages": 2, "versions": 3}, result["counts"])
        self.assertFalse(settings.state_path.exists())
        self.assertFalse(settings.catalog_root.exists())
        self.assertTrue(self.verify(result)["valid"])

    def test_output_collision_and_protected_paths_are_rejected(self):
        self.export()
        with self.assertRaises(ValueError): self.export()
        for path in [self.case.catalog / "export", self.case.settings.runtime_root / "export", self.case.root]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                export_offline(self.case.settings, path, agent="dsh", package_type="plugin")

    def test_scope_values_are_data_and_do_not_become_paths_or_sql(self):
        result = export_offline(self.case.settings, self.output, agent="../x' OR 1=1", package_type="plugin")
        self.assertEqual({"packages": 0, "versions": 0}, result["counts"])
        self.assertTrue(self.verify(result)["valid"])
        with self.assertRaises(ValueError):
            export_offline(self.case.settings, self.case.root / "bad", agent="dsh", package_type="../plugin")

    def test_wrong_anchor_and_changed_database_are_rejected(self):
        result = self.export()
        with self.assertRaises(ValueError): verify_offline(self.output, expected_export_sha256="0" * 64)
        with (self.output / "catalog.db").open("ab") as f: f.write(b"tamper")
        with self.assertRaises(ValueError): self.verify(result)

    def test_resigned_unknown_table_is_rejected(self):
        result = self.export()
        self.edit(result, "CREATE TABLE private_jobs(secret TEXT);")
        with self.assertRaises(ValueError): self.verify(result)

    def test_resigned_false_latest_is_rejected(self):
        result = self.export()
        self.edit(result, "UPDATE packages SET selected_version='1.0.0',selected_status='available' WHERE id='plugin.alpha';")
        with self.assertRaises(ValueError): self.verify(result)

    def test_resigned_bad_payload_and_search_content_are_rejected(self):
        result = self.export()
        self.edit(result, "UPDATE packages_fts SET description='invented quality';")
        with self.assertRaises(ValueError): self.verify(result)
        self.edit(result, "UPDATE package_versions SET payload_hash='bad';")
        with self.assertRaises(ValueError): self.verify(result)

    def test_extra_files_and_duplicate_descriptor_keys_are_rejected(self):
        result = self.export()
        extra = self.output / "state.db"
        extra.write_bytes(b"private")
        with self.assertRaises(ValueError): self.verify(result)
        extra.unlink()
        path = self.output / "offline.json"
        text = path.read_text(); path.write_text(text.replace('{', '{"kind":"duplicate",', 1))
        with self.assertRaises(ValueError): verify_offline(self.output, expected_export_sha256=digest_file(path))

    def test_interrupted_export_is_not_accepted(self):
        with patch("agent_forge_service.offline.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.export()
        self.assertFalse((self.output / "offline.json").exists())
        with self.assertRaises(ValueError): verify_offline(self.output, expected_export_sha256="0" * 64)

    def test_worker_and_existing_tool_export_share_the_format(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = main(["offline-export", "--runtime", str(self.case.settings.runtime_root), "--output", str(self.output), "--agent", "dsh", "--type", "plugin", "--revision", "one"])
        self.assertEqual(0, status)
        result = json.loads(stream.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, main(["offline-verify", "--runtime", str(self.case.root / "unused"), "--directory", str(self.output), "--expected-export-sha256", result["exportSha256"]]))
        self.assertFalse((self.case.root / "unused").exists())
        other = self.case.root / "via-tool"
        with contextlib.redirect_stdout(stream := io.StringIO()):
            self.assertEqual(0, snapshot_main(["--query-runtime", str(self.case.settings.runtime_root), "--output", str(other), "--agent", "dsh", "--type", "plugin", "--revision", "one"]))
        self.assertEqual(result["exportSha256"], json.loads(stream.getvalue())["exportSha256"])

    def test_resigned_bad_descriptor_shapes_return_validation_errors(self):
        result = self.export()
        original = json.loads((self.output / "offline.json").read_text())
        for field in ("scope", "source", "counts", "database"):
            for value in (None, [], "invalid"):
                with self.subTest(field=field, value=value):
                    descriptor = {**original, field: value}
                    atomic_json(self.output / "offline.json", descriptor)
                    with self.assertRaises(ValueError):
                        verify_offline(self.output, expected_export_sha256=digest_file(self.output / "offline.json"))

    def test_offline_export_cannot_be_imported_as_online_bundle(self):
        from agent_forge_service.bundles import import_bundle
        result = self.export()
        with self.assertRaises((ValueError, OSError)):
            import_bundle(self.case.settings, self.output, expected_bundle_sha256=result["exportSha256"])

    def test_symbolic_link_output_and_database_are_rejected(self):
        link = self.case.root / "linked-output"
        try:
            link.symlink_to(self.case.settings.runtime_root, target_is_directory=True)
        except OSError:
            self.skipTest("Windows symlink privilege is unavailable")
        with self.assertRaises(ValueError):
            export_offline(self.case.settings, link, agent="dsh", package_type="plugin")
        result = self.export()
        database = self.output / "catalog.db"
        saved = self.case.root / "saved-offline.db"
        database.rename(saved)
        database.symlink_to(saved)
        with self.assertRaises(ValueError): self.verify(result)

    def test_legacy_snapshot_tool_keeps_its_original_contract(self):
        data = self.case.root / "data/dsh/plugin"
        data.mkdir(parents=True)
        (data / "index.json").write_text(json.dumps({"revision": "legacy", "packages": {"alpha": {"latest": "1", "versions": ["1"], "path": "alpha.json"}}}))
        target = self.case.root / "legacy-db"
        self.assertEqual(1, build_snapshot(self.case.root / "data", target))
        db = sqlite3.connect(target / "dsh/plugin.db")
        try: self.assertEqual(("alpha", "1", "legacy"), db.execute("SELECT name,latest,revision FROM packages").fetchone())
        finally: db.close()


if __name__ == "__main__":
    unittest.main()
