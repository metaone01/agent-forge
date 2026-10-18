"""Public bundle transfer, immutable collisions, publication guards and recovery."""
import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_forge_service.bundles import activate_snapshot, export_bundle, import_bundle, snapshot_status
from agent_forge_service.errors import ServiceError
from agent_forge_service.publishing import SnapshotStore, atomic_json, build_and_publish, digest_file
from agent_forge_service.query import Query, QueryService
from agent_forge_service.settings import Settings
from agent_forge_service.storage import encode_json, writer_lock
from agent_forge_service.worker import main
from service_tests import test_publication_query as fixtures
from service_tests.temporary import cleanup_temporary


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.addCleanup(cleanup_temporary,self.temp,self.root)
        self.catalog=self.root/'catalog';self.packages=self.catalog/'sources/plugin/packages';self.packages.mkdir(parents=True)
        self.source=Settings(self.catalog,self.root/'source-runtime')
        self.target=Settings(self.catalog,self.root/'target-runtime')
        self.write('alpha');self.write('beta')
        self.manifest=build_and_publish(self.source,now=fixtures.NOW,revision='one')
        self.bundle=self.root/'bundle'
        self.export=export_bundle(self.source,self.bundle)

    def write(self,name,version='1.0.0'):
        value=fixtures.record(name,version=version)
        (self.packages/(name+'-'+version+'.json')).write_text(json.dumps(value),encoding='utf-8')
        return value

    def import_it(self,settings=None):
        return import_bundle(settings or self.target,self.bundle,expected_bundle_sha256=self.export['bundleSha256'])

    def activate(self,revision='one',settings=None,manifest_hash=None,pointer=None):
        settings=settings or self.target
        if manifest_hash is None:manifest_hash=digest_file(settings.snapshots/revision/'manifest.json')
        if pointer is None:pointer=snapshot_status(settings)['pointerSha256']
        return activate_snapshot(settings,revision,expected_manifest_sha256=manifest_hash,expected_pointer_sha256=pointer)

    def resign(self):
        descriptor=json.loads((self.bundle/'bundle.json').read_text())
        for key,name in [('manifest','manifest.json'),('database','catalog.db')]:
            descriptor[key].update(sha256=digest_file(self.bundle/name),bytes=(self.bundle/name).stat().st_size)
        manifest=json.loads((self.bundle/'manifest.json').read_text());manifest['database']=dict(descriptor['database'])
        atomic_json(self.bundle/'manifest.json',manifest)
        descriptor['manifest'].update(sha256=digest_file(self.bundle/'manifest.json'),bytes=(self.bundle/'manifest.json').stat().st_size)
        atomic_json(self.bundle/'bundle.json',descriptor)
        self.export['bundleSha256']=digest_file(self.bundle/'bundle.json')

    def edit_manifest_and_database(self,edit):
        manifest=json.loads((self.bundle/'manifest.json').read_text())
        edit(manifest)
        db=sqlite3.connect(self.bundle/'catalog.db')
        try:
            for key,value in manifest.items():
                if key!='database':db.execute('UPDATE metadata SET value=? WHERE key=?',(encode_json(value),key))
            db.commit()
        finally:db.close()
        atomic_json(self.bundle/'manifest.json',manifest);self.resign()

    def assert_staging_empty(self):
        staging=self.target.snapshots/'.staging'
        self.assertEqual([],list(staging.iterdir()) if staging.exists() else [])

    def test_export_contains_only_three_public_files_no_state_or_credentials(self):
        self.assertEqual({'bundle.json','manifest.json','catalog.db'},{p.name for p in self.bundle.iterdir()})
        self.assertTrue(self.source.state_path.exists())
        descriptor=json.loads((self.bundle/'bundle.json').read_text())
        self.assertEqual('agent-forge-query-snapshot',descriptor['kind'])
        self.assertEqual(self.export['bundleSha256'],digest_file(self.bundle/'bundle.json'))
        self.assertNotIn('credential',json.dumps(descriptor))

    def test_import_is_validated_but_does_not_implicitly_publish_or_create_state(self):
        result=self.import_it();self.assertTrue(result['installed']);self.assertFalse(result['activated'])
        self.assertFalse(self.target.state_path.exists());self.assertFalse((self.target.snapshots/'current.json').exists())
        self.assertEqual(self.manifest,SnapshotStore(self.target.snapshots).load('one'))
        self.assert_staging_empty()

    def test_activate_bootstrap_makes_same_full_query_result_and_preserves_all_versions(self):
        self.import_it();result=self.activate(pointer='absent')
        self.assertTrue(result['changed']);self.assertFalse(result['previousPointerHealthy'])
        with patch('agent_forge_service.query.utc_now',return_value=fixtures.NOW):
            self.assertEqual(QueryService(self.source).packages(),QueryService(self.target).packages())
        self.assertFalse(self.target.state_path.exists())

    def test_multiple_canonical_versions_are_transferred_without_latest_fallback(self):
        self.write('alpha',version='2.0.0')
        build_and_publish(self.source,now=fixtures.NOW,revision='multi')
        self.bundle=self.root/'multi-bundle';self.export=export_bundle(self.source,self.bundle)
        self.import_it();self.activate('multi')
        from agent_forge_service.storage import read_snapshot
        db=read_snapshot(self.target.snapshots/'multi/catalog.db')
        try:
            self.assertEqual(['1.0.0','2.0.0'],[r[0] for r in db.execute("SELECT version FROM package_versions WHERE package_id='plugin.alpha' ORDER BY version")])
            self.assertIsNone(db.execute("SELECT canonical_latest FROM packages WHERE id='plugin.alpha'").fetchone()[0])
        finally:db.close()

    def test_identical_import_and_activation_are_idempotent(self):
        self.import_it();self.activate();pointer=(self.target.snapshots/'current.json').read_bytes()
        second=self.import_it();self.assertFalse(second['installed'])
        result=self.activate();self.assertFalse(result['changed'])
        self.assertEqual(pointer,(self.target.snapshots/'current.json').read_bytes())
        self.assert_staging_empty()

    def test_existing_pointer_and_state_are_not_modified_by_import(self):
        self.import_it();self.activate();before=(self.target.snapshots/'current.json').read_bytes()
        self.target.state_path.write_bytes(b'private state remains untouched')
        self.import_it()
        self.assertEqual(before,(self.target.snapshots/'current.json').read_bytes())
        self.assertEqual(b'private state remains untouched',self.target.state_path.read_bytes())

    def test_expected_bundle_checksum_is_required_and_rejects_corruption(self):
        for checksum in ['0'*64,'invalid','A'*64,None]:
            with self.subTest(checksum=checksum),self.assertRaises(ValueError):
                import_bundle(self.target,self.bundle,expected_bundle_sha256=checksum)
        self.assertFalse((self.target.snapshots/'one').exists())

    def test_missing_extra_nested_private_and_journal_files_are_rejected(self):
        for name in ['state.db','catalog.db-wal','unexpected','directory']:
            extra=self.bundle/name
            if name=='directory':extra.mkdir()
            else:extra.write_text('private')
            with self.assertRaises(ValueError):self.import_it()
            if extra.is_dir():extra.rmdir()
            else:extra.unlink()
        (self.bundle/'manifest.json').unlink()
        with self.assertRaises(ValueError):self.import_it()

    def test_descriptor_path_traversal_future_schema_bool_sizes_and_revisions_rejected(self):
        original=(self.bundle/'bundle.json').read_bytes()
        cases=[('schemaVersion',2),('schemaVersion',True),('kind','other'),('revision','../escape')]
        for key,value in cases:
            body=json.loads(original);body[key]=value
            atomic_json(self.bundle/'bundle.json',body);self.export['bundleSha256']=digest_file(self.bundle/'bundle.json')
            with self.subTest(key=key,value=value),self.assertRaises((ValueError,ServiceError)):self.import_it()
        for key,value in [('path','../catalog.db'),('bytes',True),('sha256','bad')]:
            body=json.loads(original);body['database'][key]=value
            atomic_json(self.bundle/'bundle.json',body);self.export['bundleSha256']=digest_file(self.bundle/'bundle.json')
            with self.subTest(key=key),self.assertRaises(ValueError):self.import_it()
        self.assertFalse((self.target.snapshots/'one').exists())

    def test_duplicate_and_nonfinite_metadata_keys_fail_closed(self):
        for raw in [b'{"schemaVersion":1,"schemaVersion":1}',b'{"schemaVersion":NaN}',b'['*2000+b'0'+b']'*2000]:
            (self.bundle/'bundle.json').write_bytes(raw);self.export['bundleSha256']=digest_file(self.bundle/'bundle.json')
            with self.assertRaises(ValueError):self.import_it()

    def test_metadata_and_database_copy_byte_budgets_are_enforced(self):
        with self.assertRaises(ValueError):import_bundle(self.target,self.bundle,expected_bundle_sha256=self.export['bundleSha256'],max_database_bytes=4096)
        for budget in [True,0,32*1024**3+1]:
            with self.assertRaises(ValueError):import_bundle(self.target,self.bundle,expected_bundle_sha256=self.export['bundleSha256'],max_database_bytes=budget)
        (self.bundle/'bundle.json').write_bytes(b' '*8193);self.export['bundleSha256']=digest_file(self.bundle/'bundle.json')
        with self.assertRaises(ValueError):self.import_it()

    def test_corrupt_database_hash_retains_current_and_cleans_owned_staging(self):
        self.import_it();self.activate();pointer=(self.target.snapshots/'current.json').read_bytes()
        with (self.bundle/'catalog.db').open('r+b') as stream:stream.write(b'NOT SQLITE HEADER')
        with self.assertRaises(ValueError):self.import_it()
        self.assertEqual(pointer,(self.target.snapshots/'current.json').read_bytes());self.assert_staging_empty()

    def test_checksum_recomputed_non_sqlite_still_fails_semantic_validation(self):
        (self.bundle/'catalog.db').write_bytes(b'invalid sqlite');self.resign()
        with self.assertRaises(ServiceError):self.import_it()
        self.assertFalse((self.target.snapshots/'one').exists());self.assert_staging_empty()

    def test_manifest_database_revision_and_counts_must_agree(self):
        self.edit_manifest_and_database(lambda m:m['counts'].update(packages=99))
        with self.assertRaises(ValueError):self.import_it()
        self.assert_staging_empty()

    def test_manifest_cannot_disagree_with_embedded_metadata_even_with_valid_hash(self):
        manifest=json.loads((self.bundle/'manifest.json').read_text());manifest['generatedAt']='2026-10-10T00:00:00Z'
        atomic_json(self.bundle/'manifest.json',manifest);self.resign()
        with self.assertRaises(ValueError):self.import_it()

    def test_foreign_key_corruption_is_rejected_even_with_valid_checksums(self):
        db=sqlite3.connect(self.bundle/'catalog.db')
        db.execute("INSERT INTO package_agents VALUES ('missing-package','dsh')");db.commit();db.close();self.resign()
        with self.assertRaises(ValueError):self.import_it()

    def test_private_tables_views_triggers_and_incomplete_query_contract_are_rejected(self):
        original=(self.bundle/'catalog.db').read_bytes()
        statements=["CREATE TABLE collection_evidence(secret TEXT)","CREATE VIEW secret_view AS SELECT * FROM packages",
                    "CREATE TRIGGER unwanted AFTER INSERT ON packages BEGIN SELECT 1; END", "ALTER TABLE package_agents RENAME TO unavailable"]
        for sql in statements:
            (self.bundle/'catalog.db').write_bytes(original)
            db=sqlite3.connect(self.bundle/'catalog.db');db.execute(sql);db.commit();db.close();self.resign()
            with self.subTest(sql=sql),self.assertRaises(ValueError):self.import_it()
        self.assert_staging_empty()

    def test_schema_is_rejected_before_reading_an_executable_metadata_view(self):
        db=sqlite3.connect(self.bundle/'catalog.db')
        db.execute('DROP TABLE metadata')
        db.execute("CREATE VIEW metadata AS SELECT 'revision' AS key, metadata_view_was_executed() AS value")
        db.commit();db.close();self.resign()
        # The unregistered function would raise SQLiteError if the view were evaluated first.
        with self.assertRaisesRegex(ValueError,'private/unsupported tables or executable schema objects'):
            self.import_it()
        self.assert_staging_empty()

    def test_future_query_schema_or_sqlite_version_mismatch_are_rejected(self):
        self.edit_manifest_and_database(lambda m:m.update(schemaVersion=4))
        with self.assertRaises(ValueError):self.import_it()


    def test_same_revision_different_content_never_overwrites_installed_revision(self):
        self.import_it();before=digest_file(self.target.snapshots/'one/catalog.db')
        self.edit_manifest_and_database(lambda m:m.update(includeUndated=True))
        with self.assertRaises(ServiceError) as raised:self.import_it()
        self.assertEqual('revision_conflict',raised.exception.code)
        self.assertEqual(before,digest_file(self.target.snapshots/'one/catalog.db'));self.assert_staging_empty()

    def test_copy_interruption_never_installs_partial_revision(self):
        with patch('agent_forge_service.bundles._copy',side_effect=OSError('injected copy failure')):
            with self.assertRaises(OSError):self.import_it()
        self.assertFalse((self.target.snapshots/'one').exists());self.assert_staging_empty()

    def test_copy_source_change_is_detected_before_installation(self):
        from agent_forge_service import bundles
        original=bundles._copy
        def mutate(source,target,checksum,size):
            if source.name=='catalog.db':
                with source.open('ab') as stream:stream.write(b'changed')
            return original(source,target,checksum,size)
        with patch('agent_forge_service.bundles._copy',side_effect=mutate):
            with self.assertRaises(ValueError):self.import_it()
        self.assertFalse((self.target.snapshots/'one').exists());self.assert_staging_empty()

    def test_failed_install_rename_keeps_old_current_and_retries_cleanly(self):
        with patch('agent_forge_service.bundles.os.rename',side_effect=OSError('injected rename failure')):
            with self.assertRaises(OSError):self.import_it()
        self.assert_staging_empty();self.assertTrue(self.import_it()['installed'])

    def test_post_install_sync_failure_leaves_valid_unpublished_revision_for_retry(self):
        with patch('agent_forge_service.bundles.sync_directory',side_effect=OSError('injected sync failure')):
            with self.assertRaises(OSError):self.import_it()
        self.assertFalse((self.target.snapshots/'current.json').exists())
        self.assertEqual(self.manifest,SnapshotStore(self.target.snapshots).load('one'))
        self.assertFalse(self.import_it()['installed']);self.assert_staging_empty()

    def test_activation_requires_exact_current_pointer_and_target_manifest_hash(self):
        self.import_it();self.activate();before=(self.target.snapshots/'current.json').read_bytes()
        with self.assertRaises(ServiceError) as raised:self.activate(pointer='absent')
        self.assertEqual('publication_conflict',raised.exception.code)
        with self.assertRaises(ServiceError) as raised:self.activate(manifest_hash='0'*64)
        self.assertEqual('snapshot_checksum',raised.exception.code)
        self.assertEqual(before,(self.target.snapshots/'current.json').read_bytes())

    def test_pointer_replacement_failure_keeps_previous_readable_revision(self):
        self.import_it();self.activate();before=(self.target.snapshots/'current.json').read_bytes()
        self.write('gamma');build_and_publish(self.source,now=fixtures.NOW,revision='two')
        other=self.root/'bundle-two';export=export_bundle(self.source,other)
        import_bundle(self.target,other,expected_bundle_sha256=export['bundleSha256'])
        with patch('agent_forge_service.publishing.os.replace',side_effect=OSError('injected pointer failure')):
            with self.assertRaises(OSError):self.activate('two')
        self.assertEqual(before,(self.target.snapshots/'current.json').read_bytes())
        self.assertEqual('one',SnapshotStore(self.target.snapshots).current()['revision'])
        self.assertFalse(list(self.target.snapshots.glob('current.json.*.tmp')))

    def test_post_pointer_sync_failure_is_reconciled_from_actual_pointer_not_blind_retry(self):
        self.import_it()
        from agent_forge_service import publishing
        sync = publishing.sync_directory
        def fail_after_pointer(path):
            if (self.target.snapshots / 'current.json').exists():
                raise OSError('injected post-pointer sync failure')
            return sync(path)
        with patch('agent_forge_service.publishing.sync_directory', side_effect=fail_after_pointer):
            with self.assertRaises(OSError):self.activate(pointer='absent')
        status=snapshot_status(self.target)
        self.assertTrue(status['currentHealthy']);self.assertEqual('one',status['currentRevision'])
        with self.assertRaises(ServiceError):self.activate(pointer='absent')
        self.assertFalse(self.activate(pointer=status['pointerSha256'])['changed'])

    def test_pointer_change_during_validation_is_not_overwritten(self):
        self.import_it();self.activate()
        from agent_forge_service import bundles
        original=bundles._verify_snapshot
        def altered(*args):
            result=original(*args)
            atomic_json(self.target.snapshots/'current.json',{'revision':'one','manifestSha256':self.export['manifestSha256'],'publishedAt':'external-change'})
            return result
        before=snapshot_status(self.target)['pointerSha256']
        with patch('agent_forge_service.bundles._verify_snapshot',side_effect=altered):
            with self.assertRaises(ServiceError) as raised:self.activate(pointer=before)
        self.assertEqual('publication_conflict',raised.exception.code)
        self.assertEqual('external-change',json.loads(self.target.snapshots.joinpath('current.json').read_text())['publishedAt'])

    def test_target_change_after_validation_is_not_published(self):
        self.import_it()
        from agent_forge_service import bundles
        original=bundles._verify_snapshot
        def altered(*args):
            result=original(*args)
            with self.target.snapshots.joinpath('one/catalog.db').open('ab') as stream:stream.write(b'external-change')
            return result
        with patch('agent_forge_service.bundles._verify_snapshot',side_effect=altered):
            with self.assertRaises(ServiceError) as raised:self.activate(pointer='absent')
        self.assertEqual('snapshot_checksum',raised.exception.code)
        self.assertFalse(self.target.snapshots.joinpath('current.json').exists())

    def test_switch_and_rollback_preserve_old_cursor_and_reader(self):
        self.import_it();self.activate();service=QueryService(self.target);query=Query(sort='name',limit=1)
        first=service.packages(query);self.assertTrue(first['nextCursor'])
        self.write('gamma');build_and_publish(self.source,now=fixtures.NOW,revision='two')
        other=self.root/'bundle-two';export=export_bundle(self.source,other)
        import_bundle(self.target,other,expected_bundle_sha256=export['bundleSha256']);self.activate('two')
        second=service.packages(query,cursor=first['nextCursor'])
        self.assertEqual('one',second['revision']);self.assertEqual('beta',second['items'][0]['name'])
        self.assertEqual('two',service.packages(query)['revision'])
        result=self.activate('one');self.assertEqual('two',result['previousRevision'])
        self.assertEqual('one',service.packages(query)['revision'])
        self.assertTrue((self.target.snapshots/'two').exists())

    def test_bad_pointer_recovery_requires_hash_and_does_not_reset_source_generation(self):
        self.import_it();self.activate();path=self.target.snapshots/'current.json';path.write_bytes(b'{broken')
        status=snapshot_status(self.target);self.assertFalse(status['currentHealthy'])
        result=self.activate(pointer=status['pointerSha256'])
        self.assertFalse(result['previousPointerHealthy']);self.assertTrue(result['changed'])
        self.assertEqual(self.manifest['generatedAt'],result['generatedAt'])
        self.assertEqual('one',SnapshotStore(self.target.snapshots).current()['revision'])

    def test_oversized_but_bounded_corrupt_pointer_can_be_recovered(self):
        self.import_it();self.target.snapshots.joinpath('current.json').write_bytes(b'x'*5000)
        status=snapshot_status(self.target);self.assertFalse(status['currentHealthy'])
        self.assertTrue(self.activate(pointer=status['pointerSha256'])['changed'])

    def test_corrupt_target_does_not_replace_a_healthy_pointer(self):
        self.import_it();self.activate();before=(self.target.snapshots/'current.json').read_bytes()
        target=self.target.snapshots/'one/catalog.db'
        with target.open('r+b') as stream:stream.write(b'broken')
        with self.assertRaises(ValueError):self.activate()
        self.assertEqual(before,(self.target.snapshots/'current.json').read_bytes())

    def test_writer_lock_rejects_concurrent_export_import_and_activation(self):
        with writer_lock(self.target.runtime_root):
            with self.assertRaises(ServiceError):self.import_it()
        self.import_it()
        with writer_lock(self.target.runtime_root):
            with self.assertRaises(ServiceError):self.activate()
        with writer_lock(self.source.runtime_root):
            with self.assertRaises(ServiceError):export_bundle(self.source,self.root/'locked-export')

    def test_output_existing_or_canonical_or_snapshot_overlap_is_rejected(self):
        for path in [self.bundle,self.source.snapshots/'unsafe',self.catalog/'sources'/'bundle']:
            with self.subTest(path=path),self.assertRaises(ValueError):export_bundle(self.source,path)
        self.assertFalse((self.catalog/'sources'/'bundle').exists())

    def test_export_failure_cleans_only_its_owned_staging_and_keeps_source(self):
        current=(self.source.snapshots/'current.json').read_bytes()
        with patch('agent_forge_service.bundles._copy',side_effect=OSError('injected')):
            with self.assertRaises(OSError):export_bundle(self.source,self.root/'failed-export')
        self.assertFalse((self.root/'failed-export').exists());self.assertFalse(list(self.root.glob('.bundle-staging-*')))
        self.assertEqual(current,(self.source.snapshots/'current.json').read_bytes())

    def test_hardlinked_bundle_input_is_rejected(self):
        os.link(self.bundle/'catalog.db',self.root/'alias.db')
        with self.assertRaises(ValueError):self.import_it()

    def test_symlinked_bundle_file_or_snapshot_root_is_rejected(self):
        alias=self.root/'aliased-source.db';shutil.copyfile(self.bundle/'catalog.db',alias)
        (self.bundle/'catalog.db').unlink()
        try:(self.bundle/'catalog.db').symlink_to(alias)
        except OSError as error:
            if getattr(error,'winerror',None) in (5,1314):self.skipTest('Windows account cannot create symbolic links')
            raise
        with self.assertRaises(ValueError):self.import_it()
        self.target.runtime_root.mkdir(exist_ok=True);outside=self.root/'outside';outside.mkdir()
        self.target.snapshots.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):snapshot_status(self.target)

    def test_status_does_not_create_files_or_claim_unverified_directory_is_healthy(self):
        self.assertEqual('absent',snapshot_status(self.target)['pointerSha256'])
        self.assertFalse(self.target.runtime_root.exists())
        (self.target.snapshots/'broken-revision').mkdir(parents=True)
        status=snapshot_status(self.target);self.assertFalse(status['currentHealthy'])
        self.assertEqual(['broken-revision'],status['revisions']);self.assertFalse(self.target.state_path.exists())

    def test_cli_export_import_inspect_and_explicit_activate(self):
        output=io.StringIO();path=self.root/'cli-bundle'
        with contextlib.redirect_stdout(output):code=main(['snapshot-export','--runtime',str(self.source.runtime_root),'--output',str(path)])
        self.assertEqual(0,code);export=json.loads(output.getvalue())
        output=io.StringIO()
        with contextlib.redirect_stdout(output):code=main(['snapshot-import','--runtime',str(self.target.runtime_root),'--bundle',str(path),'--expected-bundle-sha256',export['bundleSha256']])
        self.assertEqual(0,code);self.assertFalse(json.loads(output.getvalue())['activated'])
        output=io.StringIO()
        with contextlib.redirect_stdout(output):code=main(['snapshot-activate','--runtime',str(self.target.runtime_root),'--revision','one','--expected-manifest-sha256',export['manifestSha256'],'--expected-pointer-sha256','absent'])
        self.assertEqual(0,code);self.assertEqual('one',json.loads(output.getvalue())['revision'])
        output=io.StringIO()
        with contextlib.redirect_stdout(output):code=main(['snapshot-status','--runtime',str(self.target.runtime_root)])
        self.assertEqual(0,code);self.assertTrue(json.loads(output.getvalue())['currentHealthy'])


    def test_dashboard_summary_survives_public_transfer_without_private_state(self):
        expected = QueryService(self.source).dashboard()
        self.import_it();self.activate(pointer='absent')
        actual = QueryService(self.target).dashboard()
        self.assertEqual(expected['totals'],actual['totals'])
        self.assertEqual(expected['compatibility'],actual['compatibility'])
        self.assertEqual('precomputed',actual['projectionMode'])
        self.assertFalse(self.target.state_path.exists())

    def test_rehashed_misleading_dashboard_counts_are_rejected(self):
        db=sqlite3.connect(self.bundle/'catalog.db')
        try:
            value=json.loads(db.execute("SELECT value FROM dashboard_summaries WHERE scope='dashboard:global'").fetchone()[0])
            value['totals']['packages']+=1
            db.execute("UPDATE dashboard_summaries SET value=? WHERE scope='dashboard:global'",(encode_json(value),));db.commit()
        finally:db.close()
        self.resign()
        with self.assertRaisesRegex(ValueError,'dashboard summary differs'):
            self.import_it()
        self.assertFalse((self.target.snapshots/'one').exists());self.assert_staging_empty()

    def test_missing_dashboard_scope_is_rejected_after_rehash(self):
        db=sqlite3.connect(self.bundle/'catalog.db')
        try:
            db.execute("DELETE FROM dashboard_summaries WHERE scope='dashboard:agent:dsh'");db.commit()
        finally:db.close()
        self.resign()
        with self.assertRaisesRegex(ValueError,'dashboard scopes'):
            self.import_it()
        self.assert_staging_empty()

    def test_undeclared_dashboard_table_is_rejected(self):
        self.edit_manifest_and_database(lambda manifest:manifest['features'].pop('dashboard'))
        with self.assertRaisesRegex(ValueError,'dashboard table is not declared'):
            self.import_it()
        self.assert_staging_empty()


if __name__=='__main__':unittest.main()
