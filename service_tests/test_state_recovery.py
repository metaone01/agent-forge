"""Private state recovery preserves facts, fencing, retries and charged budgets."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from agent_forge_service.errors import ServiceError
from agent_forge_service.jobs import JobQueue, save_checkpoint
from agent_forge_service.publishing import atomic_json,digest_file
from agent_forge_service.settings import Settings
from agent_forge_service.state_recovery import (create_state_backup,inspect_state_backup,restore_state_backup,recovery_status,verify_state)
from agent_forge_service.storage import encode_json,open_state,remember_package,timestamp,writer_lock
from agent_forge_service.worker import main
from service_tests.temporary import cleanup_temporary
from service_tests.test_collector import NOW


class StateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.addCleanup(cleanup_temporary,self.tmp,self.root)
        self.source=Settings(self.root/'catalog',self.root/'source');self.target=Settings(self.root/'catalog',self.root/'receiver')
        self.db=open_state(self.source.state_path);self.addCleanup(self.db.close)
        self.queue=JobQueue(self.db)
        with self.db:
            remember_package(self.db,'plugin.alpha','plugin','alpha','canonical-hash',timestamp(NOW),True)
            self.db.execute("INSERT INTO rate_limits(credential_identity,resource,remaining,quota_limit,blocked_until,next_request_at) VALUES ('local-profile','core',0,60,?,?)",(timestamp(NOW+timedelta(hours=1)),timestamp(NOW+timedelta(seconds=1))))
            save_checkpoint(self.db,source_id='fixture',shard_key='repo',cursor=None,watermark=timestamp(NOW),complete=True,now=NOW,details={'evidence':'retained'})
        self.backup=self.root/'private-backup';self.result=self.create()

    def create(self,path=None):return create_state_backup(self.source,path or self.backup)
    def restore(self,settings=None):return restore_state_backup(settings or self.target,self.backup,expected_backup_sha256=self.result['backupSha256'])
    def inspect(self):return inspect_state_backup(self.backup,expected_backup_sha256=self.result['backupSha256'])
    def replace_backup(self):
        self.backup=self.root/('backup-'+str(len(list(self.root.iterdir()))));self.result=self.create()
    def job(self,key='job',max_attempts=5):
        with self.db:return self.queue.enqueue(key=key,kind='fixture',scope='repo',payload={'id':123},now=NOW,due_at=NOW,max_attempts=max_attempts)
    def resigned(self):
        descriptor=json.loads(self.backup.joinpath('backup.json').read_text())
        descriptor['database'].update(sha256=digest_file(self.backup/'state.db'),bytes=self.backup.joinpath('state.db').stat().st_size)
        atomic_json(self.backup/'backup.json',descriptor);self.result['backupSha256']=digest_file(self.backup/'backup.json')
    def restored_connection(self):
        self.restore();db=open_state(self.target.state_path);self.addCleanup(db.close);return db

    def test_wal_committed_facts_are_included_and_uncommitted_changes_are_not(self):
        # Source connection stays open with committed data in WAL throughout the backup.
        self.assertTrue(self.source.state_path.with_name('state.db-wal').exists())
        db=sqlite3.connect(self.backup/'state.db')
        try:self.assertEqual('canonical-hash',db.execute('SELECT canonical_hash FROM package_units').fetchone()[0])
        finally:db.close()
        self.db.execute("UPDATE package_units SET canonical_hash='uncommitted'")
        try:
            output=self.root/'while-uncommitted';result=self.create(output)
            db=sqlite3.connect(output/'state.db')
            try:self.assertEqual('canonical-hash',db.execute('SELECT canonical_hash FROM package_units').fetchone()[0])
            finally:db.close()
            self.assertTrue(inspect_state_backup(output,expected_backup_sha256=result['backupSha256'])['verified'])
        finally:self.db.rollback()

    def test_private_two_file_format_is_separate_from_public_snapshot_bundle(self):
        self.assertEqual({'backup.json','state.db'},{p.name for p in self.backup.iterdir()})
        info=self.inspect();self.assertTrue(info['private']);self.assertTrue(info['verified'])
        self.assertEqual(1,info['descriptor']['stateSchemaVersion'])
        self.assertEqual(1,info['descriptor']['tableCounts']['package_units'])
        self.assertFalse((self.backup/'catalog.db').exists())

    def test_restore_fresh_runtime_preserves_bytes_first_seen_checkpoints_and_limits(self):
        result=self.restore();self.assertTrue(result['restored']);self.assertEqual(0,result['networkRequests'])
        self.assertEqual(digest_file(self.backup/'state.db'),digest_file(self.target.state_path))
        self.assertEqual(1,self.target.state_path.stat().st_nlink)
        db=sqlite3.connect(self.target.state_path)
        try:
            self.assertEqual(timestamp(NOW),db.execute('SELECT first_seen_at FROM package_units').fetchone()[0])
            self.assertEqual(timestamp(NOW),db.execute('SELECT watermark FROM checkpoints').fetchone()[0])
            self.assertEqual(timestamp(NOW+timedelta(hours=1)),db.execute('SELECT blocked_until FROM rate_limits').fetchone()[0])
        finally:db.close()
        self.assertFalse(self.target.snapshots.exists())
        self.assertEqual({'state.db','worker.lock','state-recovery.json'},{p.name for p in self.target.runtime_root.iterdir()})

    def test_worker_reopen_retains_initial_state_and_all_first_seen_events(self):
        before=self.db.execute('SELECT count(*) FROM change_events').fetchone()[0]
        db=self.restored_connection()
        with db:remember_package(db,'plugin.alpha','plugin','alpha','canonical-hash',timestamp(NOW+timedelta(days=3)),True)
        self.assertEqual(before,db.execute('SELECT count(*) FROM change_events').fetchone()[0])
        self.assertEqual(timestamp(NOW),db.execute('SELECT first_seen_at FROM package_units').fetchone()[0])

    def test_lease_is_not_reset_and_expired_reclaim_fences_old_completion(self):
        job=self.job();lease=self.queue.claim(owner='original',now=NOW,lease_seconds=60)
        self.replace_backup();db=self.restored_connection();queue=JobQueue(db)
        self.assertIsNone(queue.claim(owner='recovered',now=NOW+timedelta(seconds=10)))
        reclaimed=queue.claim(owner='recovered',now=NOW+timedelta(seconds=61))
        self.assertEqual(job,reclaimed.id);self.assertEqual(2,reclaimed.attempts);self.assertNotEqual(lease.token,reclaimed.token)
        with self.assertRaises(ServiceError) as caught:queue.finish(lease,now=NOW+timedelta(seconds=62))
        self.assertEqual('lease_lost',caught.exception.code)
        queue.finish(reclaimed,now=NOW+timedelta(seconds=62))
        self.assertEqual('succeeded',db.execute('SELECT state FROM jobs WHERE id=?',(job,)).fetchone()[0])

    def test_expired_final_attempt_becomes_dead_not_new_retry_budget(self):
        job=self.job(max_attempts=1);self.queue.claim(owner='original',now=NOW,lease_seconds=60)
        self.replace_backup();db=self.restored_connection()
        self.assertIsNone(JobQueue(db).claim(owner='recovered',now=NOW+timedelta(seconds=61)))
        self.assertEqual(('dead',1),tuple(db.execute('SELECT state,attempts FROM jobs WHERE id=?',(job,)).fetchone()))

    def test_retry_wait_stays_due_at_source_time_no_restore_timestamp_rewrite(self):
        job=self.job();lease=self.queue.claim(owner='original',now=NOW)
        self.queue.fail(lease,now=NOW,code='github_primary_limit',retry_at=NOW+timedelta(hours=1))
        self.replace_backup();db=self.restored_connection();queue=JobQueue(db)
        self.assertIsNone(queue.claim(owner='recovered',now=NOW+timedelta(minutes=59)))
        self.assertEqual(2,queue.claim(owner='recovered',now=NOW+timedelta(hours=1)).attempts)

    def test_uncertain_batch_request_charge_and_counters_are_not_refunded(self):
        from agent_forge_service.batches import plan_batch
        from service_tests.test_batches import POLICY
        batch=plan_batch(self.db,POLICY,now=NOW)
        with self.db:self.db.execute('INSERT INTO batch_attempts VALUES (?,?,?,?,?,?)',('uncertain',batch['batchId'],'ordinary',timestamp(NOW),None,None))
        self.replace_backup();db=self.restored_connection()
        row=db.execute("SELECT * FROM batch_attempts WHERE id='uncertain'").fetchone()
        self.assertIsNone(row['requests']);self.assertIsNone(row['result_json'])
        self.assertEqual(1,db.execute('SELECT count(*) FROM batch_attempts').fetchone()[0])
        self.assertEqual(self.db.execute('SELECT quotas_json FROM batch_runs').fetchone()[0],db.execute('SELECT quotas_json FROM batch_runs').fetchone()[0])

    def test_backup_publication_retries_only_bounded_windows_sharing_errors(self):
        from agent_forge_service.state_recovery import _publish_backup_directory
        stage=self.root/'.state-backup-test';stage.mkdir();output=self.root/'new-backup'
        busy=PermissionError(13,'sharing violation');busy.winerror=5
        with patch('agent_forge_service.state_recovery.os.rename',side_effect=[busy,None]) as rename,patch('agent_forge_service.state_recovery.time.sleep'):
            _publish_backup_directory(stage,output)
        self.assertEqual(2,rename.call_count)

    def test_backup_publication_persistent_or_other_errors_still_fail(self):
        from agent_forge_service.state_recovery import _publish_backup_directory
        stage=self.root/'.state-backup-test';stage.mkdir();output=self.root/'new-backup'
        for error,attempts in [(PermissionError(13,'permanent access error'),1),(OSError('disk error'),1)]:
            with patch('agent_forge_service.state_recovery.os.rename',side_effect=error) as rename,patch('agent_forge_service.state_recovery.time.sleep'),self.assertRaises(OSError):
                _publish_backup_directory(stage,output)
            self.assertEqual(attempts,rename.call_count)
        error=PermissionError(13,'Windows busy');error.winerror=32
        with patch('agent_forge_service.state_recovery.os.rename',side_effect=error) as rename,patch('agent_forge_service.state_recovery.time.sleep'),self.assertRaises(PermissionError):
            _publish_backup_directory(stage,output)
        self.assertEqual(6,rename.call_count)

    def test_backup_publication_does_not_overwrite_target_created_during_retry(self):
        from agent_forge_service.state_recovery import _publish_backup_directory
        stage=self.root/'.state-backup-test';stage.mkdir();output=self.root/'new-backup'
        def compete(*args):
            output.mkdir();(output/'keep').write_text('other writer')
            error=PermissionError(13,'Windows busy');error.winerror=5
            raise error
        with patch('agent_forge_service.state_recovery.os.rename',side_effect=compete) as rename,patch('agent_forge_service.state_recovery.time.sleep'),self.assertRaises(FileExistsError):
            _publish_backup_directory(stage,output)
        self.assertEqual(1,rename.call_count);self.assertEqual('other writer',(output/'keep').read_text())

    def test_existing_target_never_overwritten_even_if_state_matches(self):
        self.restore();before=digest_file(self.target.state_path)
        with self.assertRaises(ServiceError) as caught:self.restore()
        self.assertEqual('recovery_target_not_empty',caught.exception.code)
        self.assertEqual(before,digest_file(self.target.state_path))

    def test_preexisting_wal_snapshot_config_or_empty_directory_files_reject_recovery(self):
        for name in ['state.db-wal','state.db-shm','config.json','snapshots']:
            target=replace(self.target,runtime_root=self.root/('target-'+name.replace('.','-')))
            target.runtime_root.mkdir();(target.runtime_root/name).write_text('preserve')
            with self.assertRaises(ServiceError):self.restore(target)
            self.assertEqual('preserve',(target.runtime_root/name).read_text())
            self.assertFalse(target.state_path.exists())

    def test_failed_copy_preserves_source_and_leaves_only_lock_for_retry(self):
        source_hash=digest_file(self.backup/'state.db')
        with patch('agent_forge_service.state_recovery._copy',side_effect=OSError('injected copy failure')):
            with self.assertRaises(OSError):self.restore()
        self.assertEqual({'worker.lock'},{p.name for p in self.target.runtime_root.iterdir()})
        self.assertEqual(source_hash,digest_file(self.backup/'state.db'));self.assertTrue(self.restore()['restored'])

    def test_failed_link_never_overwrites_concurrently_created_state(self):
        def competing(*args):
            self.target.state_path.write_bytes(b'external writer')
            raise FileExistsError('already exists')
        with patch('agent_forge_service.state_recovery.os.link',side_effect=competing):
            with self.assertRaises(FileExistsError):self.restore()
        self.assertEqual(b'external writer',self.target.state_path.read_bytes())
        self.assertFalse(list(self.target.runtime_root.glob('.state-recovery-*')))

    def test_target_changed_during_validation_is_rejected(self):
        from agent_forge_service import state_recovery
        original=state_recovery.inspect_state_backup
        def competing(*args,**kwargs):
            result=original(*args,**kwargs);(self.target.runtime_root/'unexpected').write_text('external')
            return result
        with patch('agent_forge_service.state_recovery.inspect_state_backup',side_effect=competing):
            with self.assertRaises(ServiceError):self.restore()
        self.assertFalse(self.target.state_path.exists());self.assertTrue((self.target.runtime_root/'unexpected').exists())

    def test_post_install_sync_failure_retains_valid_state_and_forbids_blind_restore_retry(self):
        with patch('agent_forge_service.state_recovery.sync_directory',side_effect=OSError('injected sync failure')):
            with self.assertRaises(OSError):self.restore()
        self.assertEqual(digest_file(self.backup/'state.db'),digest_file(self.target.state_path))
        self.assertEqual(1,self.target.state_path.stat().st_nlink)
        self.assertTrue(recovery_status(self.target)['statePresent'])
        self.assertFalse(recovery_status(self.target)['recoveryReceiptPresent'])
        with self.assertRaises(ServiceError):self.restore()

    def test_receipt_failure_retains_installed_state_and_no_reset(self):
        with patch('agent_forge_service.state_recovery.atomic_json',side_effect=OSError('injected receipt failure')):
            with self.assertRaises(OSError):self.restore()
        self.assertTrue(recovery_status(self.target)['statePresent'])
        self.assertEqual(digest_file(self.backup/'state.db'),digest_file(self.target.state_path))

    def test_backup_output_existing_overlap_and_concurrent_worker_rejected(self):
        for output in [self.backup,self.source.runtime_root/'backup',self.source.catalog_root/'sources'/'backup']:
            with self.assertRaises(ValueError):self.create(output)
        with writer_lock(self.source.runtime_root):
            with self.assertRaises(ServiceError):self.create(self.root/'locked-backup')
        with writer_lock(self.target.runtime_root):
            with self.assertRaises(ServiceError):self.restore()

    def test_bad_checksum_missing_private_extra_or_database_corruption_rejected(self):
        with self.assertRaises(ValueError):inspect_state_backup(self.backup,expected_backup_sha256='0'*64)
        extra=self.backup/'GH_TOKEN';extra.write_text('never public')
        with self.assertRaises(ValueError):self.inspect()
        extra.unlink();(self.backup/'state.db').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):self.restore()
        self.assertFalse(self.target.state_path.exists())

    def test_recomputed_hash_does_not_accept_invalid_sqlite(self):
        (self.backup/'state.db').write_bytes(b'not SQLite');self.resigned()
        with self.assertRaises(ServiceError):self.restore()
        self.assertFalse(self.target.state_path.exists())

    def test_descriptor_statistics_must_agree_with_verified_sqlite(self):
        body=json.loads((self.backup/'backup.json').read_text());body['tableCounts']['jobs']=999
        atomic_json(self.backup/'backup.json',body);self.result['backupSha256']=digest_file(self.backup/'backup.json')
        with self.assertRaises(ValueError):self.restore()

    def test_future_or_old_schema_missing_tables_views_and_extra_extensions_rejected(self):
        original=(self.backup/'state.db').read_bytes()
        for sql in ['PRAGMA user_version=13','PRAGMA user_version=8','DROP TABLE http_cache','CREATE TABLE extra_private(secret TEXT)',
                    "CREATE VIEW side_effect AS SELECT undefined_function()"]:
            (self.backup/'state.db').write_bytes(original)
            db=sqlite3.connect(self.backup/'state.db');db.execute(sql);db.commit();db.close();self.resigned()
            with self.subTest(sql=sql),self.assertRaises(ValueError):self.restore()
            self.assertFalse(self.target.state_path.exists())

    def test_foreign_key_integrity_and_descriptor_path_traversal_rejected(self):
        db=sqlite3.connect(self.backup/'state.db')
        db.execute("INSERT INTO repository_aliases VALUES ('owner/repo',123,'time','time','unknown-observation')");db.commit();db.close();self.resigned()
        with self.assertRaises(ValueError):self.restore()
        body=json.loads(self.backup.joinpath('backup.json').read_text());body['database']['path']='../state.db'
        atomic_json(self.backup/'backup.json',body);self.result['backupSha256']=digest_file(self.backup/'backup.json')
        with self.assertRaises(ValueError):self.restore()

    def test_private_backup_sidecars_and_hardlinks_rejected(self):
        for suffix in ['-wal','-shm','-journal']:
            sidecar=self.backup/('state.db'+suffix);sidecar.write_bytes(b'leftover')
            with self.assertRaises(ValueError):self.inspect()
            sidecar.unlink()
        os.link(self.backup/'state.db',self.root/'hardlink.db')
        with self.assertRaises(ValueError):self.restore()

    def test_budget_refuses_large_state_and_copy_failure_does_not_publish_backup(self):
        with self.assertRaises(ValueError):create_state_backup(self.source,self.root/'too-small',max_state_bytes=4096)
        self.assertFalse((self.root/'too-small').exists());self.assertFalse(list(self.root.glob('.state-backup-*')))
        with patch('agent_forge_service.state_recovery._standalone_backup',side_effect=OSError('injected backup failure')):
            with self.assertRaises(OSError):self.create(self.root/'failed')
        self.assertFalse((self.root/'failed').exists());self.assertFalse(list(self.root.glob('.state-backup-*')))

    def test_no_source_state_or_unsupported_live_schema_does_not_create_empty_backup(self):
        source=replace(self.source,runtime_root=self.root/'missing')
        with self.assertRaises(ValueError):create_state_backup(source,self.root/'empty-backup')
        self.assertFalse(source.state_path.exists())
        self.db.execute('PRAGMA user_version=8');self.db.commit()
        with self.assertRaises(ValueError):self.create(self.root/'old-state')
        self.assertEqual(8,self.db.execute('PRAGMA user_version').fetchone()[0])

    def test_schema_comparison_preserves_literal_case_in_constraints(self):
        db=sqlite3.connect(self.backup/'state.db')
        db.execute('PRAGMA writable_schema=ON')
        db.execute("UPDATE sqlite_master SET sql=replace(sql,'''pending''','''PENDING''') WHERE name='jobs'")
        db.commit();db.close();self.resigned()
        with self.assertRaisesRegex(ValueError,'differs from the installed Worker contract'):self.restore()


    def test_invalid_timestamp_and_nonfinite_or_duplicate_descriptor_are_rejected(self):
        body=json.loads((self.backup/'backup.json').read_text());body['createdAt']=None
        atomic_json(self.backup/'backup.json',body);self.result['backupSha256']=digest_file(self.backup/'backup.json')
        with self.assertRaises(ValueError):self.restore()
        for raw in [b'{"schemaVersion":1,"schemaVersion":1}',b'{"schemaVersion":NaN}']:
            self.backup.joinpath('backup.json').write_bytes(raw);self.result['backupSha256']=digest_file(self.backup/'backup.json')
            with self.assertRaises(ValueError):self.restore()

    def test_recovery_status_is_offline_and_does_not_create_an_absent_runtime(self):
        self.assertFalse(recovery_status(self.target)['statePresent']);self.assertFalse(self.target.runtime_root.exists())
        self.restore();status=recovery_status(self.target)
        self.assertTrue(status['statePresent']);self.assertTrue(status['recoveryReceiptPresent'])
        self.assertFalse(status['automaticResumption'])

    def test_cli_managed_backup_verify_restore_and_status(self):
        def call(args):
            out=io.StringIO()
            with contextlib.redirect_stdout(out):code=main(args)
            self.assertEqual(0,code);return json.loads(out.getvalue())
        backup=self.root/'cli-backup'
        result=call(['state-backup','--runtime',str(self.source.runtime_root),'--output',str(backup)])
        verified=call(['state-backup-verify','--runtime',str(self.target.runtime_root),'--backup',str(backup),'--expected-backup-sha256',result['backupSha256']])
        self.assertTrue(verified['verified']);self.assertFalse(self.target.runtime_root.exists())
        result=call(['state-restore','--runtime',str(self.target.runtime_root),'--backup',str(backup),'--expected-backup-sha256',result['backupSha256']])
        self.assertTrue(result['restored'])
        self.assertTrue(call(['state-recovery-status','--runtime',str(self.target.runtime_root)])['statePresent'])


if __name__=='__main__':unittest.main()
