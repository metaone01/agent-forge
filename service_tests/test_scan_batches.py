"""Opt-in automatic scans: real SQLite/queue/anonymous-client state machines, no network."""
import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict,replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from agent_forge_service.batches import BatchPolicy,plan_batch,run_batch,batch_status,load_policy
from agent_forge_service.scan_batches import ManifestProfile,ManifestBatchPolicy,ReleaseBatchPolicy,PREFIX
from agent_forge_service.scheduler import scheduled_tick,ScheduleOptions
from agent_forge_service.scheduling import RefreshPolicy
from agent_forge_service.discovery import DiscoveryPolicy
from agent_forge_service.settings import Settings
from agent_forge_service.storage import open_state,timestamp,encode_json
from agent_forge_service.public_git import AnonymousGitClient,AnonymousGitTransport,ANONYMOUS_IDENTITY
from agent_forge_service.github_client import RequestPolicy,GitHubClient
from agent_forge_service.github import GhTransport
from agent_forge_service.errors import ServiceError
from agent_forge_service.source_controls import record_control,ACTIONS
from agent_forge_service.manifests import enqueue_manifest
from agent_forge_service.releases import enqueue_releases
from agent_forge_service.state_recovery import create_state_backup,restore_state_backup
from agent_forge_service.publishing import build_and_publish
from service_tests.test_publication_query import NOW,record
from service_tests.test_manifests import blob,tree,entry,COMMIT,ROOT,DIRECTORY,FIELDS,Opener,Response
from service_tests.test_releases import item
from service_tests.temporary import cleanup_temporary

PROFILE=ManifestProfile('.claude-plugin/plugin.json','claude_plugin','claude-code')
BASE=BatchPolicy('scan-pilot',8,RefreshPolicy('scan-pilot',2,168,8,24,.25),0,0,0,0,(),
    '2026-10-01T00:00:00Z',24,'2026-10-01T00:00:00Z',24,24,DiscoveryPolicy())
MANIFEST=ManifestBatchPolicy(6,1,24,8,(PROFILE,))
RELEASE=ReleaseBatchPolicy(3,1,24,8,3)


class ScanBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='forge-scan-batch-');self.root=Path(self.temp.name)
        self.addCleanup(cleanup_temporary,self.temp,self.root.resolve())
        self.settings=Settings(self.root/'catalog',self.root/'runtime');self.db=open_state(self.settings.state_path);self.addCleanup(self.db.close)
        self.now=NOW;self.raw=json.dumps(FIELDS).encode();self.object=blob(self.raw)
        self.root_tree=tree(ROOT,[entry('.claude-plugin',DIRECTORY,'040000','tree')])
        self.directory=tree(DIRECTORY,[entry('plugin.json',self.object['sha'],size=len(self.raw))])
        self.release_items=[item()];self.fail_status=None
        self.opener=Opener(self.respond)
        self.client=self.make_client(self.db)
        self.seed(123)
        packages=self.settings.catalog_root/'sources/plugin/packages';packages.mkdir(parents=True)
        value=record();value['updatedAt']=timestamp(NOW-timedelta(hours=1))
        (packages/'alpha.json').write_text(json.dumps(value),encoding='utf-8');(packages.parent/'index.json').write_text(json.dumps({'packages':{'alpha':{'latest':'1.0.0'}}}),encoding='utf-8')

    def seed(self,identity,discovered=True,observed=True):
        with self.db:
            if discovered:self.db.execute('INSERT INTO discovery_candidates VALUES (?,?,?,?,?,?,?)',(identity,'owner/repo'+str(identity),timestamp(NOW-timedelta(days=1)),timestamp(NOW),0,'discovered','{}'))
            if observed:self.db.execute('INSERT INTO repositories VALUES (?,?,?,?,?,?,?)',(identity,'owner/repo'+str(identity),timestamp(NOW-timedelta(days=1)),1,timestamp(NOW),timestamp(NOW),'{}'))

    def make_client(self,db):return AnonymousGitClient(db,transport=AnonymousGitTransport(opener=self.opener),clock=lambda:self.now,policy=RequestPolicy(jitter_fraction=0))

    def respond(self,request):
        self.now+=timedelta(seconds=2)
        route=request.full_url.removeprefix('https://api.github.com/')
        if self.fail_status:return Response({'message':'secondary rate limit' if self.fail_status==403 else 'fixture failure'},request.full_url,self.fail_status,{'Retry-After':'120'} if self.fail_status==403 else {})
        if route.startswith('repositories/') and '/releases?' not in route:return {'id':int(route.split('/')[1]),'full_name':'owner/repo','private':False,'default_branch':'main'}
        if '/releases?' in route:return self.release_items
        if route.endswith('/commits/main'):return {'sha':COMMIT,'commit':{'tree':{'sha':ROOT}}}
        if route.endswith('/git/trees/'+ROOT):return self.root_tree
        if route.endswith('/git/trees/'+DIRECTORY):return self.directory
        if route.endswith('/git/blobs/'+self.object['sha']):return self.object
        self.fail('unexpected anonymous request '+route)

    def policy(self,manifests=MANIFEST,releases=RELEASE,**kwargs):return replace(BASE,manifests=manifests,releases=releases,**kwargs)

    def plan(self,policy=None):return plan_batch(self.db,policy or self.policy(),now=self.now)

    def run_batch_step(self,result):
        self.now+=timedelta(seconds=2)
        return run_batch(self.db,None,result['batchId'],owner='scan-worker',max_requests=20,clock=lambda:self.now,anonymous_client=self.client)

    def finish(self,batch):
        for _ in range(30):
            result=self.run_batch_step(batch)
            if result['state'] in ('settled','partial','budget_exhausted'):return result
        self.fail('bounded scan batch did not settle')

    def code(self,code,function):
        with self.assertRaises(ServiceError) as caught:function()
        self.assertEqual(code,caught.exception.code)

    def test_dynamic_manifest_steps_and_release_metadata_share_anonymous_client(self):
        batch=self.plan();result=self.finish(batch)
        self.assertTrue(result['selectedWorkSettled']);self.assertEqual(7,len(self.opener.requests))
        self.assertEqual(6,result['lanes']['manifests']['charged']);self.assertEqual(1,result['lanes']['releases']['charged'])
        self.assertEqual({'complete':1},result['lanes']['manifests']['scanStates'])
        self.assertEqual({'traversal_complete':1},result['lanes']['releases']['scanStates'])
        self.assertEqual('review_pending',self.db.execute('SELECT state FROM manifest_candidates').fetchone()[0])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM package_units').fetchone()[0])
        for request in self.opener.requests:
            self.assertEqual('GET',request.method);self.assertNotIn('Authorization',request.headers);self.assertNotIn('Cookie',request.headers)
        self.assertEqual({ANONYMOUS_IDENTITY},{row[0] for row in self.db.execute('SELECT DISTINCT credential_identity FROM rate_limits')})

    def test_disabled_scan_lanes_have_explicit_null_config_and_no_side_effects(self):
        batch=self.plan(BASE)
        self.assertNotIn('manifests',batch['lanes']);self.assertNotIn('releases',batch['lanes'])
        config=json.loads(self.db.execute('SELECT config_json FROM batch_policies').fetchone()[0])
        self.assertIsNone(config['manifests']);self.assertIsNone(config['releases'])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM manifest_scans').fetchone()[0])
        self.assertEqual(0,self.db.execute("SELECT count(*) FROM metadata WHERE key LIKE 'public-scan-rotation:%'").fetchone()[0])
        self.assertEqual(0,len(self.opener.requests))

    def test_budget_exhaustion_preserves_scan_and_next_slot_resumes_remaining_steps(self):
        policy=self.policy(manifests=replace(MANIFEST,budget=3),releases=None)
        first=self.plan(policy);result=self.finish(first);self.assertEqual('budget_exhausted',result['state'])
        scan=first['summary']['manifests']['selectedScanIds'][0];self.assertEqual(3,self.db.execute('SELECT step FROM manifest_scans').fetchone()[0])
        self.now=NOW+timedelta(hours=8);second=self.plan(policy)
        self.assertEqual([scan],second['summary']['manifests']['selectedScanIds']);self.assertEqual(1,second['summary']['manifests']['resumedScans'])
        self.assertEqual(3,second['summary']['manifests']['remainingPathRequestsWithoutRetries'][scan])
        result=self.finish(second);self.assertTrue(result['selectedWorkSettled']);self.assertEqual(6,len(self.opener.requests))
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM manifest_scans').fetchone()[0])

    def test_check_intervals_do_not_repeat_completed_paths_every_slot(self):
        policy=self.policy();self.finish(self.plan(policy))
        self.now=NOW+timedelta(hours=8);batch=self.plan(policy)
        self.assertEqual(0,batch['summary']['manifests']['selected']);self.assertEqual(0,batch['summary']['releases']['selected'])
        self.now=NOW+timedelta(hours=24);batch=self.plan(policy)
        self.assertEqual(1,batch['summary']['manifests']['selected']);self.assertEqual(1,batch['summary']['releases']['selected'])

    def test_round_robin_deferred_targets_are_not_starved_by_long_scans(self):
        self.seed(124);self.seed(125);policy=self.policy(manifests=replace(MANIFEST,budget=1),releases=None)
        ids=[]
        for hours in [0,8,16]:
            self.now=NOW+timedelta(hours=hours);batch=self.plan(policy);ids+=batch['summary']['manifests']['selectedScanIds']
        self.assertEqual(3,len(set(ids)));self.assertEqual(3,self.db.execute('SELECT count(DISTINCT github_id) FROM manifest_scans').fetchone()[0])
        self.now=NOW+timedelta(hours=24);batch=self.plan(policy);self.assertEqual([ids[0]],batch['summary']['manifests']['selectedScanIds'])

    def test_new_repository_and_discovery_only_scope_appear_on_next_plan(self):
        policy=self.policy();self.finish(self.plan(policy));self.seed(124,observed=False)
        self.now=NOW+timedelta(hours=8);batch=self.plan(policy)
        self.assertEqual(1,batch['summary']['manifests']['selected']);self.assertEqual(0,batch['summary']['releases']['selected'])
        self.assertEqual(2,batch['summary']['manifests']['targets']);self.assertEqual(1,batch['summary']['releases']['targets'])

    def test_unrelated_manual_scan_tasks_are_not_run_by_selected_batch(self):
        self.seed(124)
        with self.db:
            manual=enqueue_manifest(self.db,github_id=124,path='other/.claude-plugin/plugin.json',kind='claude_plugin',agent='claude-code',slot=timestamp(NOW),now=NOW)
        batch=self.plan(self.policy(releases=None));self.finish(batch)
        self.assertEqual('queued',self.db.execute('SELECT state FROM manifest_scans WHERE id=?',(manual,)).fetchone()[0])
        self.assertEqual(0,self.db.execute('SELECT sum(j.attempts) FROM manifest_tasks t JOIN jobs j ON j.id=t.job_id WHERE t.scan_id=?',(manual,)).fetchone()[0])

    def test_manual_same_profile_active_scan_is_reused(self):
        with self.db:manual=enqueue_manifest(self.db,github_id=123,path=PROFILE.path,kind=PROFILE.kind,agent=PROFILE.agent,slot=timestamp(NOW-timedelta(hours=8)),now=NOW-timedelta(hours=8))
        batch=self.plan(self.policy(releases=None));self.assertEqual([manual],batch['summary']['manifests']['selectedScanIds'])
        self.assertEqual(1,batch['summary']['manifests']['resumedScans'])

    def test_paused_sources_have_no_get_reservation_and_keep_unfinished_work(self):
        record_control(self.db,value={'kind':'provider','provider':'github.com'},action='disable',reason_code=ACTIONS['disable'],expected_head='none',request_key='pause',operator_label='fixture',now=NOW)
        batch=self.plan();result=self.run_batch_step(batch)
        self.assertEqual(0,result['requestsThisInvocation']);self.assertEqual(0,result['actionsThisInvocation']);self.assertEqual(0,len(self.opener.requests))
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM batch_attempts').fetchone()[0]);self.assertFalse(result['selectedWorkSettled'])
        self.assertEqual(2,sum(lane.get('collectionPausedJobs',0) for lane in result['lanes'].values()))

    def test_partial_release_page_cap_is_never_success_or_daily_coverage(self):
        self.release_items=[item(identity=number) for number in range(1,101)]
        batch=self.plan(self.policy(manifests=None,releases=replace(RELEASE,max_pages=1)));result=self.finish(batch)
        self.assertEqual('partial',result['state']);self.assertFalse(result['selectedWorkSettled']);self.assertEqual({'partial':1},result['lanes']['releases']['scanStates'])
        self.now=NOW+timedelta(hours=8);next_batch=self.plan(self.policy(manifests=None,releases=replace(RELEASE,max_pages=1)))
        self.assertEqual(1,next_batch['summary']['releases']['selected'])

    def test_partial_release_manual_page_cap_conflict_is_explicit(self):
        with self.db:enqueue_releases(self.db,github_id=123,slot=timestamp(NOW),max_pages=5,now=NOW)
        self.code('scan_policy_conflict',lambda:self.plan(self.policy(manifests=None)))
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM batch_runs').fetchone()[0])

    def test_missing_path_is_completed_check_without_automatic_classification(self):
        self.root_tree=tree(ROOT,[]);batch=self.plan(self.policy(releases=None));result=self.finish(batch)
        self.assertTrue(result['selectedWorkSettled']);self.assertEqual({'not_found':1},result['lanes']['manifests']['scanStates'])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM manifest_candidates').fetchone()[0])
        self.now=NOW+timedelta(hours=8);self.assertEqual(0,self.plan(self.policy(releases=None))['summary']['manifests']['selected'])

    def test_scan_rotation_clock_and_corrupt_cursor_are_fail_closed(self):
        policy=self.policy();self.now=NOW+timedelta(hours=8);self.plan(policy)
        self.now=NOW;self.code('scan_rotation_clock_wait',lambda:self.plan(policy))
        self.now=NOW+timedelta(hours=16)
        with self.db:self.db.execute('UPDATE metadata SET value=? WHERE key=?',(json.dumps({'bad':'state'}),PREFIX+policy.policy_id+':manifests'))
        self.code('scan_rotation_state_invalid',lambda:self.plan(policy))
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM batch_runs').fetchone()[0])

    def test_immediate_stop_does_not_reserve_or_request(self):
        batch=self.plan();result=run_batch(self.db,None,batch['batchId'],owner='worker',max_requests=10,clock=lambda:self.now,anonymous_client=self.client,should_stop=lambda:True)
        self.assertEqual(0,result['requestsThisInvocation']);self.assertEqual(0,self.db.execute('SELECT count(*) FROM batch_attempts').fetchone()[0])

    def test_authenticated_client_cannot_be_injected_for_git_object_lanes(self):
        batch=self.plan();client=GitHubClient(self.db,identity='fixture',transport=GhTransport(runner=lambda *args:self.fail('must not use gh')))
        with self.assertRaises(ValueError):run_batch(self.db,None,batch['batchId'],owner='worker',max_requests=1,anonymous_client=client)

    def test_restart_and_private_backup_restore_keep_cursor_and_scan_chain(self):
        policy=self.policy(manifests=replace(MANIFEST,budget=3),releases=None);first=self.plan(policy);self.finish(first)
        receipt=create_state_backup(self.settings,self.root/'backup');restored=Settings(self.root/'absent',self.root/'restored')
        restore_state_backup(restored,self.root/'backup',expected_backup_sha256=receipt['backupSha256'])
        db=open_state(restored.state_path);self.addCleanup(db.close);self.now=NOW+timedelta(hours=8)
        second=plan_batch(db,policy,now=self.now);self.assertEqual(first['summary']['manifests']['selectedScanIds'],second['summary']['manifests']['selectedScanIds'])
        for _ in range(8):
            self.now+=timedelta(seconds=2);result=run_batch(db,None,second['batchId'],owner='restored-worker',max_requests=10,clock=lambda:self.now,anonymous_client=self.make_client(db))
            if result['selectedWorkSettled']:break
        self.assertTrue(result['selectedWorkSettled']);self.assertEqual(1,db.execute('SELECT count(*) FROM manifest_candidates').fetchone()[0])

    def test_anonymous_only_scheduler_does_not_require_gh_identity_and_publishes(self):
        policy=self.policy();options=ScheduleOptions(max_requests=10,max_batch_seconds=300,poll_seconds=1)
        for _ in range(16):
            self.now+=timedelta(seconds=2)
            result=scheduled_tick(self.settings,policy,options,clock=lambda:self.now,anonymous_factory=self.make_client,
                client_factory=lambda db:self.fail('anonymous-only schedule must not create gh client'))
            if result['phase']=='published':break
        self.assertEqual('published',result['phase']);self.assertEqual('selected_work_settled',result['publication']['completionReason'])
        self.assertEqual(7,len(self.opener.requests))
        manifest=json.loads((self.settings.snapshots/result['publication']['revision']/'manifest.json').read_text())
        self.assertEqual(7,manifest['collectionBatch']['confirmedRequests']);self.assertEqual(1,manifest['counts']['packages'])

    def test_initial_policy_loads_explicit_optional_scan_channels(self):
        example=Path(__file__).resolve().parents[1]/'agent_forge_service/examples/scheduler-curated-pilot.json'
        old=load_policy(example);self.assertIsNone(old.manifests);self.assertIsNone(old.releases)
        data=json.loads(example.read_text());data.update(schemaVersion=1,recommendations=None,manifests={'budget':6,'maxScans':1,'intervalHours':24,'retryHours':8,'profiles':[asdict(PROFILE)]},releases={'budget':3,'maxScans':1,'intervalHours':24,'retryHours':8,'maxPages':3})
        path=self.root/'v4.json';path.write_text(json.dumps(data));policy=load_policy(path)
        self.assertEqual(MANIFEST,policy.manifests);self.assertEqual(RELEASE,policy.releases);self.assertIsNone(policy.recommendations)
        for edit in [lambda d:d.update(schemaVersion=2),lambda d:d['manifests'].update(intervalHours=7),lambda d:d['releases'].update(maxPages=True),lambda d:d['manifests']['profiles'].append(asdict(PROFILE))]:
            wrong=json.loads(json.dumps(data));edit(wrong);path.write_text(json.dumps(wrong))
            with self.assertRaises(ValueError):load_policy(path)

    def test_crash_after_anonymous_get_keeps_uncertain_budget_charge(self):
        from agent_forge_service import batches
        policy=self.policy(manifests=replace(MANIFEST,budget=1),releases=None);batch=self.plan(policy)
        real=batches.run_manifest_jobs
        def crash(*args,**kwargs):
            real(*args,**kwargs)
            raise RuntimeError('simulated crash after persisted GET before batch settlement')
        self.now+=timedelta(seconds=2)
        with patch.object(batches,'run_manifest_jobs',side_effect=crash),self.assertRaises(RuntimeError):
            run_batch(self.db,None,batch['batchId'],owner='worker',max_requests=5,clock=lambda:self.now,anonymous_client=self.client)
        status=batch_status(self.db,batch['batchId'])
        self.assertEqual(1,status['lanes']['manifests']['charged']);self.assertEqual(1,status['lanes']['manifests']['uncertain']);self.assertEqual('uncertain',status['state'])
        result=self.run_batch_step(batch);self.assertEqual(0,result['requestsThisInvocation']);self.assertEqual(1,len(self.opener.requests))
        self.now=NOW+timedelta(hours=8);resumed=self.plan(policy)
        self.assertEqual(batch['summary']['manifests']['selectedScanIds'],resumed['summary']['manifests']['selectedScanIds'])
        self.assertEqual(5,next(iter(resumed['summary']['manifests']['remainingPathRequestsWithoutRetries'].values())))

    def test_terminal_release_failure_is_reconciled_and_retried_next_interval(self):
        policy=self.policy(manifests=None);self.fail_status=401;batch=self.plan(policy);result=self.finish(batch)
        self.assertEqual({'failed':1},result['lanes']['releases']['scanStates']);self.assertEqual('partial',result['state'])
        self.now=NOW+timedelta(hours=4);self.assertEqual(batch['batchId'],self.plan(policy)['batchId']);self.assertEqual(1,self.db.execute('SELECT count(*) FROM release_scans').fetchone()[0])
        self.now=NOW+timedelta(hours=8);self.fail_status=None;resumed=self.plan(policy)
        self.assertEqual(1,resumed['summary']['releases']['selected']);self.assertNotEqual(batch['summary']['releases']['selectedScanIds'],resumed['summary']['releases']['selectedScanIds'])

    def test_anonymous_rate_limit_blocks_both_scan_lanes_without_authenticated_fallback(self):
        self.fail_status=403;batch=self.plan();self.now+=timedelta(seconds=2)
        first=self.run_batch_step(batch);self.assertEqual(1,first['requestsThisInvocation']);self.assertIsNotNone(first['nextNetworkAt']['anonymousCore'])
        second=self.run_batch_step(batch);self.assertEqual(0,second['requestsThisInvocation']);self.assertEqual(1,len(self.opener.requests))
        self.assertEqual({ANONYMOUS_IDENTITY},{row[0] for row in self.db.execute('SELECT DISTINCT credential_identity FROM rate_limits')})

    def test_resume_scope_keeps_old_scan_instead_of_recreating_jobs(self):
        policy=self.policy(releases=None);batch=self.plan(policy)
        paused=record_control(self.db,value={'kind':'github_repository','repositoryId':123},action='disable',reason_code=ACTIONS['disable'],expected_head='none',request_key='pause',operator_label='fixture',now=self.now)
        result=self.run_batch_step(batch);self.assertEqual(0,result['requestsThisInvocation'])
        record_control(self.db,value={'kind':'github_repository','repositoryId':123},action='enable',reason_code=ACTIONS['enable'],expected_head=paused['event']['id'],request_key='resume',operator_label='fixture',now=self.now)
        result=self.finish(batch);self.assertTrue(result['selectedWorkSettled']);self.assertEqual(1,self.db.execute('SELECT count(*) FROM manifest_scans').fetchone()[0])

    def test_daily_facts_from_manual_completed_scans_avoid_duplicate_get(self):
        self.finish(self.plan());self.now=NOW+timedelta(hours=8)
        policy=self.policy(policy_id='new-policy',refresh=replace(BASE.refresh,policy_id='new-policy'))
        batch=self.plan(policy)
        self.assertEqual(0,batch['summary']['manifests']['selected']);self.assertEqual(0,batch['summary']['releases']['selected'])
        self.assertEqual(7,len(self.opener.requests))

    def test_multi_profile_same_repository_does_not_guess_compatibility(self):
        profiles=(PROFILE,ManifestProfile('SKILL.md','agent_skill','codex'))
        policy=self.policy(manifests=replace(MANIFEST,max_scans=2,profiles=profiles),releases=None)
        batch=self.plan(policy);self.assertEqual(2,batch['summary']['manifests']['selected'])
        self.assertEqual({'claude-code','codex'},{row[0] for row in self.db.execute('SELECT agent FROM manifest_scans')})
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM manifest_candidates').fetchone()[0])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM package_units').fetchone()[0])

    def test_sample_policy_is_valid_and_nothing_is_automatically_enabled(self):
        path=Path(__file__).resolve().parents[1]/'agent_forge_service/examples/scheduler-scan-pilot.json'
        policy=load_policy(path);self.assertEqual(12,policy.manifests.budget);self.assertEqual(3,policy.releases.max_pages)
        self.assertEqual(0,len(self.opener.requests));self.assertEqual(0,self.db.execute('SELECT count(*) FROM batch_policies').fetchone()[0])

    def test_planning_reads_each_scan_history_once_for_many_targets(self):
        for identity in range(200,230):self.seed(identity)
        statements=[];self.db.set_trace_callback(statements.append)
        try:batch=self.plan()
        finally:self.db.set_trace_callback(None)
        history=[sql for sql in statements if sql.startswith('SELECT * FROM manifest_scans WHERE slot<=') or sql.startswith('SELECT * FROM release_scans WHERE slot<=')]
        self.assertEqual(2,len(history));self.assertEqual(31,batch['summary']['manifests']['targets']);self.assertEqual(31,batch['summary']['releases']['targets'])

if __name__=='__main__':unittest.main()
