"""Acquisition gates and public control ledgers use synthetic facts, never upstream queries."""
import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from agent_forge_service.source_controls import ACTIONS, POLICY, AUTHORITY, target, target_id, record_control, inspect_control, read_control
from agent_forge_service.storage import open_state, timestamp, encode_json
from agent_forge_service.jobs import JobQueue
from agent_forge_service.collector import enqueue_repository, run_repository_jobs
from agent_forge_service.discovery import enqueue_window
from agent_forge_service.downloads import enqueue_downloads
from agent_forge_service.manifests import enqueue_manifest
from agent_forge_service.releases import enqueue_releases
from agent_forge_service.recommendations import enqueue_recommendation
from agent_forge_service.errors import ServiceError
from agent_forge_service.batches import _due_job, plan_batch, run_batch
from agent_forge_service.bundles import export_bundle, import_bundle
from agent_forge_service.publishing import build_and_publish, digest_file, atomic_json
from agent_forge_service.query import QueryService
from agent_forge_service.http_contract import HttpReadService
from agent_forge_service.settings import Settings
from agent_forge_service.state_recovery import create_state_backup, restore_state_backup
from service_tests.test_publication_query import NOW, record
from service_tests.test_recommendations import SOURCE
from service_tests.temporary import cleanup_temporary


class SourceControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='forge-source-control-')
        self.root=Path(self.temp.name)
        self.addCleanup(cleanup_temporary,self.temp,self.root.resolve())
        self.settings=Settings(self.root/'catalog',self.root/'runtime')
        self.db=open_state(self.settings.state_path);self.addCleanup(self.db.close)
        self.queue=JobQueue(self.db);self.counter=0
        packages=self.settings.catalog_root/'sources/plugin/packages';packages.mkdir(parents=True)
        for name in ['alpha','beta']:
            (packages/(name+'.json')).write_text(json.dumps(record(name)),encoding='utf-8')
        (packages.parent/'index.json').write_text(json.dumps({'packages':{'alpha':{'latest':'1.0.0'},'beta':{'latest':'1.0.0'}}}),encoding='utf-8')
        self.provider={'kind':'provider','provider':'github.com'}

    def control(self,value=None,action='disable',head=None,now=NOW,request=None):
        value=value or self.provider
        if head is None:
            current=inspect_control(self.db,value)['latestEvent'];head=current['id'] if current else 'none'
        self.counter+=1
        return record_control(self.db,value=value,action=action,reason_code=ACTIONS[action],expected_head=head,
            request_key=request or 'request-'+str(self.counter),operator_label='fixture-operator',now=now)

    def code(self,expected,function):
        with self.assertRaises(ServiceError) as caught:function()
        self.assertEqual(expected,caught.exception.code)

    def enqueue(self,repository=123):
        with self.db:return enqueue_repository(self.queue,repository=repository,slot=timestamp(NOW),bootstrap=False,now=NOW)

    def test_provider_pause_preserves_jobs_attempts_and_resumes_same_job(self):
        identity=self.enqueue();before=dict(self.db.execute('SELECT * FROM jobs WHERE id=?',(identity,)).fetchone())
        first=self.control()
        self.assertIsNone(self.queue.claim(owner='worker',now=NOW))
        self.assertEqual(before,dict(self.db.execute('SELECT * FROM jobs WHERE id=?',(identity,)).fetchone()))
        self.assertFalse(_due_job(self.db,NOW,job_ids=(identity,),scopes=None))
        self.control(action='enable',now=NOW+timedelta(seconds=1))
        lease=self.queue.claim(owner='worker',now=NOW+timedelta(seconds=1))
        self.assertEqual(identity,lease.id);self.assertEqual(1,lease.attempts)
        self.assertEqual(first['event']['id'],inspect_control(self.db,self.provider)['history'][0]['id'])

    def test_disabled_first_job_does_not_starve_other_resources(self):
        blocked=self.enqueue(123);allowed=self.enqueue(456)
        self.control({'kind':'github_repository','repositoryId':123})
        lease=self.queue.claim(owner='worker',now=NOW)
        self.assertEqual(allowed,lease.id)
        self.assertEqual(0,self.db.execute('SELECT attempts FROM jobs WHERE id=?',(blocked,)).fetchone()[0])

    def test_new_jobs_under_pause_are_blocked_without_recreating_control(self):
        self.control();identity=self.enqueue()
        self.assertIsNone(self.queue.claim(owner='worker',now=NOW))
        self.assertGreater(self.db.execute('SELECT count(*) FROM collection_job_sources WHERE job_id=?',(identity,)).fetchone()[0],0)

    def test_legacy_missing_bindings_are_backfilled_at_atomic_control_record(self):
        identity=self.enqueue()
        with self.db:self.db.execute('DELETE FROM collection_job_sources')
        self.control();self.assertIsNone(self.queue.claim(owner='worker',now=NOW))
        self.assertEqual(2,self.db.execute('SELECT count(*) FROM collection_job_sources WHERE job_id=?',(identity,)).fetchone()[0])

    def test_already_started_request_can_finish_while_next_claim_is_blocked(self):
        identity=self.enqueue();lease=self.queue.claim(owner='worker',now=NOW)
        self.control(now=NOW+timedelta(seconds=1))
        self.queue.finish(lease,now=NOW+timedelta(seconds=2))
        self.assertEqual('succeeded',self.db.execute('SELECT state FROM jobs WHERE id=?',(identity,)).fetchone()[0])
        self.assertIsNone(self.queue.claim(owner='worker',now=NOW+timedelta(seconds=2)))

    def test_expired_lease_under_pause_does_not_consume_another_attempt(self):
        identity=self.enqueue();self.queue.claim(owner='worker',now=NOW,lease_seconds=1)
        self.control(now=NOW+timedelta(seconds=1))
        self.assertIsNone(self.queue.claim(owner='next-worker',now=NOW+timedelta(seconds=2)))
        self.assertEqual(1,self.db.execute('SELECT attempts FROM jobs WHERE id=?',(identity,)).fetchone()[0])

    def test_cas_idempotency_and_wrong_request_are_explicit(self):
        self.control(request='same')
        replay=record_control(self.db,value=self.provider,action='disable',reason_code=ACTIONS['disable'],expected_head='none',request_key='same',operator_label='fixture-operator',now=NOW)
        self.assertTrue(replay['replayed'])
        self.code('control_head_conflict',lambda:self.control(action='enable',head='none'))
        self.code('control_request_conflict',lambda:self.control(action='enable',request='same'))
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM collection_control_events').fetchone()[0])

    def test_transition_clock_and_secret_labels_are_rejected(self):
        self.code('control_transition_invalid',lambda:self.control(action='enable'))
        self.control();self.code('control_transition_invalid',lambda:self.control())
        self.code('control_clock_wait',lambda:self.control(action='enable',now=NOW-timedelta(seconds=1)))
        with self.assertRaises(ValueError):record_control(self.db,value=self.provider,action='disable',reason_code=ACTIONS['disable'],expected_head='none',request_key='ghp_secret',operator_label='fixture',now=NOW)

    def test_target_fields_are_bounded_factual_and_normalized(self):
        self.assertEqual(target_id({'kind':'github_repository_name','fullName':'Owner/Repo'}),target_id({'kind':'github_repository_name','fullName':'owner/repo'}))
        for value in [{'kind':[]},{'kind':'provider','provider':'unknown'}, {'kind':'github_repository','repositoryId':True}, {'kind':'github_repository','repositoryId':2**53}, {'kind':'provider','provider':'github.com','quality':'unsafe'}, {'kind':'curated_file','repositoryId':123,'path':'../README.md'}]:
            with self.subTest(value=value),self.assertRaises((ValueError,TypeError)):target(value)

    def test_all_current_collection_channels_bind_provider_and_specific_resource(self):
        from service_tests.test_batches import BatchTests
        seed=BatchTests('runTest');seed.setUp();self.addCleanup(seed.doCleanups)
        seed.seed(1)
        db=seed.db;queue=JobQueue(db)
        with db:
            enqueue_repository(queue,repository=1,slot=timestamp(NOW),bootstrap=False,now=NOW)
            enqueue_window(db,topic='mcp',field='created',start=timestamp(NOW-timedelta(hours=1)),end=timestamp(NOW),generation='fixture',now=NOW)
            enqueue_manifest(db,github_id=1,path='.claude-plugin/plugin.json',kind='claude_plugin',agent='claude-code',slot=timestamp(NOW),now=NOW)
            enqueue_releases(db,github_id=1,slot=timestamp(NOW),max_pages=1,now=NOW)
            enqueue_recommendation(db,definition={**SOURCE,'repositoryId':1},slot=timestamp(NOW),now=NOW)
            npm=enqueue_downloads(db,package='@org/fixture',slot=timestamp(NOW),now=NOW)
        record_control(db,value=self.provider,action='disable',reason_code=ACTIONS['disable'],expected_head='none',request_key='pause',operator_label='fixture',now=NOW)
        for kind in ['github_repository','github_discovery','github_manifest','github_releases','github_recommendation']:
            self.assertIsNone(queue.claim(owner='worker',now=NOW,kind=kind),kind)
        self.assertEqual(npm,queue.claim(owner='worker',now=NOW,kind='npm_downloads').id)
        self.assertEqual(0,len(seed.calls))

    def test_topic_and_npm_package_pause_are_selective(self):
        with self.db:
            first=enqueue_window(self.db,topic='mcp',field='created',start=timestamp(NOW-timedelta(hours=1)),end=timestamp(NOW),generation='fixture',now=NOW)
            second=enqueue_window(self.db,topic='skill',field='created',start=timestamp(NOW-timedelta(hours=1)),end=timestamp(NOW),generation='fixture',now=NOW)
            a=enqueue_downloads(self.db,package='alpha',slot=timestamp(NOW),now=NOW)
            b=enqueue_downloads(self.db,package='beta',slot=timestamp(NOW),now=NOW)
        self.control({'kind':'github_topic','topic':'mcp'});self.control({'kind':'npm_package','package':'alpha'})
        self.assertEqual(second,self.queue.claim(owner='worker',now=NOW,kind='github_discovery').payload['runId'])
        self.assertEqual(b,self.queue.claim(owner='worker',now=NOW,kind='npm_downloads').id)

    def test_curated_file_pause_preserves_observations_and_other_files(self):
        with self.db:
            blocked=enqueue_recommendation(self.db,definition=SOURCE,slot=timestamp(NOW),now=NOW)
            allowed=enqueue_recommendation(self.db,definition={**SOURCE,'path':'OTHER.md'},slot=timestamp(NOW),now=NOW)
        self.control({'kind':'curated_file','repositoryId':123,'path':'README.md'})
        self.assertEqual(allowed,self.queue.claim(owner='worker',now=NOW,kind='github_recommendation').payload['scanId'])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM recommendation_observations').fetchone()[0])

    def test_batch_pause_does_not_reserve_get_or_claim_daily_coverage(self):
        from service_tests.test_batches import BatchTests,POLICY as BATCH_POLICY
        seed=BatchTests('runTest');seed.setUp();self.addCleanup(seed.doCleanups);seed.seed(1)
        report=plan_batch(seed.db,BATCH_POLICY,now=NOW)
        record_control(seed.db,value=self.provider,action='disable',reason_code=ACTIONS['disable'],expected_head='none',request_key='pause',operator_label='fixture',now=NOW)
        result=run_batch(seed.db,seed.client,report['batchId'],owner='worker',max_requests=5,clock=lambda:NOW)
        self.assertEqual(0,result['requestsThisInvocation']);self.assertEqual(0,result['actionsThisInvocation'])
        self.assertEqual(0,seed.db.execute('SELECT count(*) FROM batch_attempts').fetchone()[0])
        self.assertFalse(result['selectedWorkSettled'])
        self.assertGreater(sum(lane.get('collectionPausedJobs',0) for lane in result['lanes'].values()),0)

    def build(self,revision,now=NOW):
        return build_and_publish(self.settings,revision=revision,now=now)

    def test_no_event_and_cutoff_before_event_keep_old_public_contract(self):
        self.build('old');self.code('source_controls_unavailable',lambda:QueryService(self.settings).source_controls())
        self.control(now=NOW+timedelta(seconds=1));self.build('before-event')
        self.assertNotIn('sourceControls',QueryService(self.settings).store.current()['features'])
        self.assertEqual(503,HttpReadService(self.settings).handle('GET','/api/v1/source-controls').status)

    def test_projection_is_separate_from_counts_versions_and_metric_facts(self):
        self.build('before');before=QueryService(self.settings).packages()
        self.control();self.build('paused');after=QueryService(self.settings).packages()
        self.assertEqual(before['items'],after['items'])
        controls=QueryService(self.settings).source_controls();self.assertEqual('disabled',controls['items'][0]['collectionState'])
        self.assertNotIn('fixture-operator',json.dumps(controls));self.assertNotIn('request-1',json.dumps(controls))
        self.assertEqual(2,len(after['items']))

    def test_cursor_and_http_parameter_contract_stay_pinned(self):
        self.control();self.control({'kind':'provider','provider':'npmjs.org'});self.build('one')
        query=QueryService(self.settings);page=query.source_controls(limit=1)
        self.control(action='enable',now=NOW+timedelta(seconds=1));self.build('two',NOW+timedelta(seconds=1))
        next_page=query.source_controls(limit=1,cursor=page['nextCursor'])
        self.assertEqual('one',next_page['revision']);self.assertNotEqual(page['items'][0]['id'],next_page['items'][0]['id'])
        self.code('cursor_mismatch',lambda:query.source_controls(limit=2,cursor=page['nextCursor']))
        reader=HttpReadService(self.settings)
        for pairs in [[('limit','0')],[('limit','1'),('limit','2')],[('source','invented')]]:
            self.assertEqual(400,reader.handle('GET','/api/v1/source-controls',pairs).status)
        self.assertEqual(405,reader.handle('POST','/api/v1/source-controls').status)

    def test_public_bundle_receiver_needs_no_private_state(self):
        self.control();self.build('one')
        directory=self.root/'bundle';receipt=export_bundle(self.settings,directory)
        receiver=Settings(self.root/'absent-canonical',self.root/'receiver')
        import_bundle(receiver,directory,expected_bundle_sha256=receipt['bundleSha256'])
        self.assertEqual('disabled',QueryService(receiver).source_controls(revision='one')['items'][0]['collectionState'])
        self.assertFalse(receiver.state_path.exists())

    def test_public_full_ledger_and_recent_history_are_distinct(self):
        for number in range(22):self.control(action='disable' if number%2==0 else 'enable',now=NOW+timedelta(seconds=number))
        self.build('history',NOW+timedelta(seconds=22))
        item=QueryService(self.settings).source_controls()['items'][0]
        self.assertEqual(22,item['historyCount']);self.assertEqual(20,len(item['history']));self.assertTrue(item['historyTruncated'])
        self.assertEqual('enabled',item['collectionState'])

    def test_backup_restore_preserves_event_heads_and_queued_gates(self):
        identity=self.enqueue();self.control()
        receipt=create_state_backup(self.settings,self.root/'backup')
        restored=Settings(self.root/'absent',self.root/'restored')
        restore_state_backup(restored,self.root/'backup',expected_backup_sha256=receipt['backupSha256'])
        with open_state(restored.state_path) as db:
            self.assertEqual(1,db.execute('PRAGMA user_version').fetchone()[0]);self.assertIsNone(JobQueue(db).claim(owner='worker',now=NOW))
            self.assertEqual(inspect_control(self.db,self.provider)['latestEvent'],inspect_control(db,self.provider)['latestEvent'])

    def test_read_only_v11_inspect_does_not_migrate(self):
        path=self.root/'old.db'
        db=sqlite3.connect(path);db.execute('PRAGMA user_version=11');db.close()
        before=path.read_bytes();result=read_control(path,self.provider)
        self.assertEqual(11,result['stateVersion']);self.assertEqual('enabled',result['collectionState']);self.assertEqual(before,path.read_bytes())

    def test_repository_id_controls_apply_to_last_observed_named_alias(self):
        from service_tests.test_batches import BatchTests
        seed=BatchTests('runTest');seed.setUp();self.addCleanup(seed.doCleanups);seed.seed(1)
        with seed.db:identity=enqueue_repository(JobQueue(seed.db),repository='example/repo1',slot=timestamp(NOW),bootstrap=False,now=NOW)
        record_control(seed.db,value={'kind':'github_repository','repositoryId':1},action='disable',reason_code=ACTIONS['disable'],expected_head='none',request_key='pause',operator_label='fixture',now=NOW)
        self.assertIsNone(JobQueue(seed.db).claim(owner='worker',now=NOW,job_ids=(identity,)))
        self.assertEqual(0,len(seed.calls))

    def test_name_control_applies_to_known_id_without_pretending_name_is_stable_identity(self):
        from service_tests.test_batches import BatchTests
        seed=BatchTests('runTest');seed.setUp();self.addCleanup(seed.doCleanups);seed.seed(1)
        with seed.db:identity=enqueue_repository(JobQueue(seed.db),repository=1,slot=timestamp(NOW),bootstrap=False,now=NOW)
        record_control(seed.db,value={'kind':'github_repository_name','fullName':'example/repo1'},action='disable',reason_code=ACTIONS['disable'],expected_head='none',request_key='pause',operator_label='fixture',now=NOW)
        self.assertIsNone(JobQueue(seed.db).claim(owner='worker',now=NOW,job_ids=(identity,)))

    def test_resuming_one_scope_does_not_override_provider_pause(self):
        identity=self.enqueue();self.control();repository={'kind':'github_repository','repositoryId':123}
        self.control(repository);self.control(repository,action='enable')
        self.assertIsNone(self.queue.claim(owner='worker',now=NOW))
        self.control(action='enable');self.assertEqual(identity,self.queue.claim(owner='worker',now=NOW).id)

    def test_duplicate_private_json_and_broken_ledger_do_not_publish(self):
        self.control();self.build('safe')
        duplicate='{"kind":"provider","provider":"github.com","provider":"github.com"}'
        with self.db:self.db.execute('UPDATE collection_control_events SET target_json=?',(duplicate,))
        with self.assertRaises(ValueError):self.build('broken')
        self.assertEqual('safe',QueryService(self.settings).store.current()['revision'])

    def test_private_sequence_gap_is_rejected(self):
        from agent_forge_service.source_controls import _hash
        event=self.control()['event'];event.pop('id');event['sequence']=2
        with self.db:self.db.execute('UPDATE collection_control_events SET seq=2,id=?',(_hash(event),))
        with self.assertRaisesRegex(ValueError,'missing sequence'):self.build('gap')

    def test_cli_inspect_and_record_are_local_and_require_existing_state(self):
        repo=Path(__file__).resolve().parents[1]
        base=[sys.executable,'-m','agent_forge_service.worker']
        inspect=base+['source-control-inspect','--runtime',str(self.settings.runtime_root),'--target-json',json.dumps(self.provider)]
        before=self.settings.state_path.read_bytes();result=subprocess.run(inspect,cwd=repo,capture_output=True)
        self.assertEqual(0,result.returncode,result.stderr);self.assertEqual('enabled',json.loads(result.stdout)['collectionState']);self.assertEqual(before,self.settings.state_path.read_bytes())
        record_args=base+['source-control-record','--runtime',str(self.settings.runtime_root),'--target-json',json.dumps(self.provider),'--expected-head','none','--action','disable','--reason-code',ACTIONS['disable'],'--request-key','cli-fixture','--operator-label','private-fixture-operator']
        result=subprocess.run(record_args,cwd=repo,capture_output=True);self.assertEqual(0,result.returncode,result.stderr)
        self.assertNotIn('private-fixture-operator',result.stdout.decode());self.assertNotIn('cli-fixture',result.stdout.decode())
        missing=list(record_args);missing[missing.index('--runtime')+1]=str(self.root/'absent')
        result=subprocess.run(missing,cwd=repo,capture_output=True);self.assertNotEqual(0,result.returncode);self.assertFalse((self.root/'absent').exists())

    def tamper_bundle(self,directory,update):
        db=sqlite3.connect(directory/'catalog.db');db.row_factory=sqlite3.Row
        update(db);db.commit();db.close()
        manifest=json.loads((directory/'manifest.json').read_text())
        manifest['database']['sha256']=digest_file(directory/'catalog.db');manifest['database']['bytes']=(directory/'catalog.db').stat().st_size
        atomic_json(directory/'manifest.json',manifest)
        descriptor=json.loads((directory/'bundle.json').read_text())
        descriptor['database']['sha256']=manifest['database']['sha256'];descriptor['database']['bytes']=manifest['database']['bytes']
        descriptor['manifest']['sha256']=digest_file(directory/'manifest.json');descriptor['manifest']['bytes']=(directory/'manifest.json').stat().st_size
        atomic_json(directory/'bundle.json',descriptor)
        return digest_file(directory/'bundle.json')

    def test_rehashed_public_private_field_and_row_change_are_rejected(self):
        self.control();self.build('one');directory=self.root/'bundle';export_bundle(self.settings,directory)
        def update(db):
            row=db.execute('SELECT * FROM source_control_events').fetchone();value=json.loads(row['data_json']);value['operatorLabel']='secret'
            db.execute('UPDATE source_control_events SET data_json=?',(encode_json(value),))
        checksum=self.tamper_bundle(directory,update)
        with self.assertRaises(ValueError):import_bundle(Settings(self.root/'absent',self.root/'rejected'),directory,expected_bundle_sha256=checksum)
        self.assertFalse((self.root/'rejected/snapshots/current.json').exists())

    def test_public_controls_not_declared_and_extra_private_column_are_rejected(self):
        self.control();self.build('one')
        for mode in ['extra-column','missing-feature']:
            directory=self.root/mode;export_bundle(self.settings,directory)
            if mode=='extra-column':
                checksum=self.tamper_bundle(directory,lambda db:db.execute('ALTER TABLE source_control_events ADD COLUMN operator_label TEXT'))
            else:
                manifest=json.loads((directory/'manifest.json').read_text());manifest['features'].pop('sourceControls');manifest.pop('sourceControlCoverage')
                db=sqlite3.connect(directory/'catalog.db');db.execute('UPDATE metadata SET value=? WHERE key=?',(encode_json(manifest['features']),'features'));db.execute("DELETE FROM metadata WHERE key='sourceControlCoverage'");db.commit();db.close()
                # Regenerate manifest/descriptor checksums after mirroring the deliberate feature removal.
                atomic_json(directory/'manifest.json',manifest)
                checksum=self.tamper_bundle(directory,lambda db:None)
            with self.assertRaises(ValueError):import_bundle(Settings(self.root/'absent',self.root/('rejected-'+mode)),directory,expected_bundle_sha256=checksum)


    def test_modified_public_sql_and_sequence_are_rejected_after_rehash(self):
        from agent_forge_service.source_controls import _hash
        self.control();self.control(action='enable');self.build('one')
        for mode in ['sql','sequence']:
            directory=self.root/('tamper-'+mode);export_bundle(self.settings,directory)
            def update(db):
                if mode=='sql':
                    rows=db.execute('SELECT * FROM source_control_events').fetchall()
                    db.execute('DROP TABLE source_control_events')
                    db.execute('CREATE TABLE source_control_events(seq INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,target_id TEXT NOT NULL,recorded_at TEXT NOT NULL,data_json TEXT NOT NULL, CHECK(seq>0))')
                    db.execute('CREATE INDEX source_control_history ON source_control_events(target_id,seq)')
                    db.executemany('INSERT INTO source_control_events VALUES (?,?,?,?,?)',[tuple(row) for row in rows])
                else:
                    row=db.execute('SELECT * FROM source_control_events WHERE seq=2').fetchone();event=json.loads(row['data_json']);event['sequence']=3;event.pop('id');event['id']=_hash(event)
                    db.execute('UPDATE source_control_events SET seq=3,id=?,data_json=? WHERE seq=2',(event['id'],encode_json(event)))
            checksum=self.tamper_bundle(directory,update)
            with self.assertRaisesRegex(ValueError,'SQL schema|missing sequence'):
                import_bundle(Settings(self.root/'absent',self.root/('rejected-'+mode)),directory,expected_bundle_sha256=checksum)

    def test_disabled_jobs_do_not_call_network_clients(self):
        from agent_forge_service.github_client import GitHubClient
        from agent_forge_service.github import GhTransport
        self.enqueue();self.control()
        def forbidden(*args):raise AssertionError('paused collection tried gh')
        client=GitHubClient(self.db,identity='fixture',transport=GhTransport(runner=forbidden),clock=lambda:NOW)
        result=run_repository_jobs(self.db,client,owner='worker',max_requests=3,clock=lambda:NOW)
        self.assertEqual(0,result['requests']);self.assertEqual(0,result['claimed']);self.assertEqual(1,result['pending'])

    def test_cli_public_control_query_uses_exact_revision_without_private_state(self):
        self.control();self.build('one');directory=self.root/'bundle';receipt=export_bundle(self.settings,directory)
        receiver=Settings(self.root/'absent',self.root/'receiver')
        import_bundle(receiver,directory,expected_bundle_sha256=receipt['bundleSha256'])
        command=[sys.executable,'-m','agent_forge_service.worker','source-controls-query','--runtime',str(receiver.runtime_root),'--revision','one','--limit','1']
        result=subprocess.run(command,cwd=Path(__file__).resolve().parents[1],capture_output=True)
        self.assertEqual(0,result.returncode,result.stderr);body=json.loads(result.stdout)
        self.assertEqual('one',body['revision']);self.assertEqual('disabled',body['items'][0]['collectionState'])
        self.assertFalse(receiver.state_path.exists());self.assertNotIn('fixture-operator',result.stdout.decode())

if __name__=='__main__':unittest.main()
