"""Only the initial release contract is supported; development DBs are untouched."""
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from agent_forge_service.collection_schema import initialize_state
from agent_forge_service.errors import ServiceError
from agent_forge_service.publishing import atomic_json, digest_file, SnapshotStore
from agent_forge_service.schema_contracts import STATE_SCHEMA_VERSION, STATE_CONTRACT, QUERY_SCHEMA_VERSION, QUERY_CONTRACT
from agent_forge_service.storage import open_state
from service_tests import test_publication_query as fixture


class SchemaContractTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.PublicationQueryTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_initial_state_has_complete_atomic_schema_and_reopens_without_backfill(self):
        path = self.case.root / 'initial.db'
        db = open_state(path)
        self.assertEqual(1, STATE_SCHEMA_VERSION)
        self.assertEqual(1, db.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual(STATE_CONTRACT, json.loads(db.execute("SELECT value FROM metadata WHERE key='storageContract'").fetchone()[0]))
        for table in ['jobs','repositories','repository_aliases','canonical_mapping_records','batch_runs','manifest_scans','npm_download_observations','download_refresh_cohorts','release_scans','recommendation_sources','candidate_review_decisions','collection_control_events']:
            self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone(), table)
        db.close()
        before = path.read_bytes()
        db = open_state(path)
        self.assertEqual(0, db.execute('SELECT count(*) FROM repository_aliases').fetchone()[0])
        db.close()
        self.assertEqual(before, path.read_bytes())

    def test_unsupported_or_unmarked_state_is_rejected_without_touching_bytes(self):
        for version in (0, 1, 2, 99):
            with self.subTest(version=version):
                path = self.case.root / ('unsupported-' + str(version) + '.db')
                db = sqlite3.connect(path)
                db.executescript("CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL); INSERT INTO metadata VALUES('preserve','evidence'); PRAGMA user_version=" + str(version))
                db.close()
                before = path.read_bytes()
                with self.assertRaises(ServiceError) as raised:
                    open_state(path)
                self.assertEqual('state_version', raised.exception.code)
                self.assertEqual(before, path.read_bytes())
                self.assertFalse(path.with_name(path.name + '-wal').exists())

    def test_same_number_without_current_contract_does_not_trigger_repair(self):
        path = self.case.root / 'unmarked.db'
        db = sqlite3.connect(path)
        db.execute('PRAGMA user_version=1'); db.close()
        before = path.read_bytes()
        with self.assertRaises(ServiceError): open_state(path)
        self.assertEqual(before, path.read_bytes())

    def test_initialization_error_rolls_back_all_tables_marker_and_version(self):
        class FailedInitialization(sqlite3.Connection):
            def executescript(self, text):
                return super().executescript(text.replace('PRAGMA user_version=1;', 'SELECT missing_column FROM jobs; PRAGMA user_version=1;'))
        db = sqlite3.connect(self.case.root / 'failed.db', factory=FailedInitialization)
        try:
            with self.assertRaises(sqlite3.Error): initialize_state(db)
            self.assertEqual(0, db.execute('PRAGMA user_version').fetchone()[0])
            self.assertEqual([], db.execute('SELECT name FROM sqlite_master').fetchall())
        finally: db.close()

    def test_query_and_canonical_versions_are_independent(self):
        self.case.write(fixture.record())
        manifest = self.case.build()
        self.assertEqual(1, QUERY_SCHEMA_VERSION)
        self.assertEqual(1, manifest['schemaVersion'])
        self.assertEqual(QUERY_CONTRACT, manifest['storageContract'])
        self.assertEqual(2, self.case.service().package('plugin.alpha')['canonicalRecord']['schemaVersion'])

    def test_query_without_current_marker_or_with_other_format_is_rejected(self):
        self.case.write(fixture.record()); manifest = self.case.build()
        directory = self.case.settings.snapshots / 'one'
        original = json.loads(json.dumps(manifest))
        for field, value in [('schemaVersion', 2), ('storageContract', None)]:
            changed = {**original, field: value}
            atomic_json(directory / 'manifest.json', changed)
            with self.assertRaises(ServiceError): SnapshotStore(self.case.settings.snapshots).load('one')
        atomic_json(directory / 'manifest.json', original)
        db = sqlite3.connect(directory / 'catalog.db')
        db.execute("DELETE FROM metadata WHERE key='storageContract'"); db.commit(); db.close()
        original['database'].update(bytes=(directory / 'catalog.db').stat().st_size, sha256=digest_file(directory / 'catalog.db'))
        atomic_json(directory / 'manifest.json', original)
        with self.assertRaises(ServiceError): SnapshotStore(self.case.settings.snapshots).load('one')


if __name__ == '__main__': unittest.main()
