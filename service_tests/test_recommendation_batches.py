"""Recommendation batch budgets, source rotation, recovery and scheduled publication."""
from __future__ import annotations
import json,tempfile,unittest,sqlite3
from pathlib import Path
from dataclasses import replace,asdict
from datetime import timedelta
from unittest.mock import patch

from agent_forge_service.batches import plan_batch,run_batch,batch_status,load_policy
from agent_forge_service.recommendation_batches import RecommendationsBatchPolicy,PREFIX,selected_scans
from agent_forge_service.recommendations import RecommendationClient,GhRecommendationTransport,enqueue_recommendation,run_recommendation_jobs
from agent_forge_service.recommendation_projection import public_source_identity
from agent_forge_service.github_client import GitHubClient,RequestPolicy
from agent_forge_service.github import GhTransport
from agent_forge_service.storage import open_state,encode_json,timestamp
from agent_forge_service.settings import Settings
from agent_forge_service.scheduler import scheduled_tick,ScheduleOptions,scheduler_status
from agent_forge_service.query import QueryService
from agent_forge_service.publishing import build_and_publish,digest_file
from agent_forge_service.state_recovery import create_state_backup,restore_state_backup
from agent_forge_service.errors import ServiceError
from service_tests.test_recommendations import SOURCE,NOW,COMMIT,TREE,content,blob,wire,Clock
from service_tests.test_batches import POLICY
from service_tests.test_publication_query import record
from service_tests.temporary import cleanup_temporary


def configuration(*sources,budget=5,maximum=1,interval=24,retry=8):
    return RecommendationsBatchPolicy(budget,maximum,interval,retry,tuple(encode_json(value) for value in (sources or (SOURCE,))))


class RecommendationBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='forge-curated-batch-');self.root=Path(self.temp.name).resolve()
        self.addCleanup(cleanup_temporary,self.temp,self.root)
        self.catalog=self.root/'canonical';(self.catalog/'sources').mkdir(parents=True)
        self.settings=Settings(self.catalog,self.root/'runtime',publication_delay_seconds=0)
        self.db=open_state(self.settings.state_path);self.addCleanup(self.db.close)
        self.clock=Clock();self.raw=content('https://github.com/o/alpha','https://github.com/o/beta')
        self.calls=[];self.responses={};self.on_request=None;self.commit=COMMIT
        self.policy=replace(POLICY,policy_id='curated-pilot',refresh=replace(POLICY.refresh,policy_id='curated-pilot'),refresh_budget=0,
            recommendations=configuration())
        self.client=self.client_for(self.db)

    def runner(self,argv,*unused):
        endpoint=argv[-1];self.calls.append(endpoint)
        if self.on_request:self.on_request(endpoint)
        if endpoint in self.responses:return self.responses[endpoint]
        b=blob(self.raw)
        if endpoint.startswith('repositories/'):
            identity=int(endpoint.split('/')[-1]);body={'id':identity,'full_name':'source/list-'+str(identity),'private':False,'default_branch':'main'}
        elif '/commits/' in endpoint:body={'sha':self.commit,'commit':{'tree':{'sha':TREE}}}
        elif '/git/trees/' in endpoint:body={'sha':TREE,'truncated':False,'tree':[{'path':'README.md','mode':'100644','type':'blob','sha':b['sha'],'size':len(self.raw)}]}
        elif '/git/blobs/' in endpoint:body=b
        else:raise AssertionError(endpoint)
        return 0,wire(body)

    def client_for(self,connection,identity='fixture-profile'):
        return RecommendationClient(connection,identity=identity,transport=GhRecommendationTransport(runner=self.runner),clock=self.clock,
            policy=RequestPolicy(core_spacing_seconds=.1,jitter_fraction=0),random_value=lambda:0)
    def plan(self,policy=None):return plan_batch(self.db,policy or self.policy,now=self.clock.now)
    def execute(self,batch,maximum=100):return run_batch(self.db,None,batch['batchId'],owner='fixture-worker',max_requests=maximum,clock=self.clock,recommendation_client=self.client)
    def state(self):return json.loads(self.db.execute('SELECT value FROM metadata WHERE key=?',(PREFIX+self.policy.policy_id,)).fetchone()[0])
    def next_slot(self):self.clock.now=self.clock.now.replace(minute=0,second=0,microsecond=0)+timedelta(hours=8)

    def test_root_scan_charges_five_dynamic_steps_and_updates_only_private_due(self):
        batch=self.plan();result=self.execute(batch)
        self.assertEqual(5,result['requestsThisInvocation']);self.assertEqual(5,result['lanes']['recommendations']['charged'])
        self.assertTrue(result['selectedWorkSettled']);self.assertEqual(1,self.db.execute('SELECT count(*) FROM recommendation_snapshots').fetchone()[0])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM package_units').fetchone()[0])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM change_events').fetchone()[0])
        row=self.state()['resources'][public_source_identity(SOURCE)]
        self.assertEqual('2026-10-13T00:00:00.000000Z',row['nextDueAt']);self.assertIsNotNone(row['lastSuccessAt'])
        self.assertEqual(1,self.db.execute('PRAGMA user_version').fetchone()[0])

    def test_same_slot_is_idempotent_and_does_not_requeue_completed_scan(self):
        first=self.plan();self.execute(first);again=self.plan()
        self.assertEqual(first['batchId'],again['batchId']);self.assertEqual(1,self.db.execute('SELECT count(*) FROM recommendation_scans').fetchone()[0])
        self.assertEqual(0,self.execute(again)['requestsThisInvocation'])

    def test_daily_sources_are_skipped_in_intermediate_slots(self):
        self.execute(self.plan());self.next_slot();batch=self.plan()
        self.assertEqual(0,batch['summary']['recommendations']['selected']);self.assertTrue(batch['selectedWorkSettled'])
        self.next_slot();self.assertEqual(0,self.plan()['summary']['recommendations']['selected'])
        self.next_slot();self.assertEqual(1,self.plan()['summary']['recommendations']['selected'])

    def test_explicit_eight_hour_interval_can_run_at_each_utc_slot(self):
        self.policy=replace(self.policy,recommendations=configuration(interval=8));self.execute(self.plan());self.next_slot()
        self.assertEqual(1,self.plan()['summary']['recommendations']['selected'])

    def test_budget_exhaustion_preserves_scan_for_later_slot(self):
        self.policy=replace(self.policy,recommendations=configuration(budget=3))
        batch=self.plan();scan=selected_scans(self.db,batch['batchId'])[0];result=self.execute(batch)
        self.assertEqual('budget_exhausted',result['state']);self.assertEqual(3,result['requestsThisInvocation'])
        self.assertIsNone(self.state()['resources'][public_source_identity(SOURCE)]['lastSuccessAt'])
        self.assertEqual(0,self.execute(batch)['requestsThisInvocation'])
        self.next_slot();later=self.plan();self.assertEqual((scan,),selected_scans(self.db,later['batchId']))
        result=self.execute(later);self.assertEqual(2,result['requestsThisInvocation']);self.assertTrue(result['selectedWorkSettled'])
        self.assertEqual(1,later['summary']['recommendations']['resumedScans'])
        self.assertEqual([],later['summary']['recommendations']['capacityWarnings'])
        self.assertEqual([2],list(later['summary']['recommendations']['remainingRequestsAtPlanning'].values()))

    def test_source_aliases_are_one_refresh_scope_not_two_votes_or_gets(self):
        alias={**SOURCE,'label':'Another config label','maxEntries':99}
        self.policy=replace(self.policy,recommendations=configuration(SOURCE,alias,maximum=2,budget=10))
        batch=self.plan();summary=batch['summary']['recommendations'];self.assertEqual((2,1,1),(summary['configuredAliases'],summary['sources'],summary['selected']))
        self.assertEqual(5,self.execute(batch)['requestsThisInvocation'])

    def test_round_rotation_gives_unfinished_and_new_sources_turns(self):
        other={**SOURCE,'repositoryId':124}
        self.policy=replace(self.policy,recommendations=configuration(SOURCE,other,budget=1,maximum=1))
        first=self.plan();scan1=selected_scans(self.db,first['batchId'])[0];self.execute(first);self.next_slot()
        second=self.plan();scan2=selected_scans(self.db,second['batchId'])[0];self.assertNotEqual(scan1,scan2);self.execute(second);self.next_slot()
        third=self.plan();self.assertEqual((scan1,),selected_scans(self.db,third['batchId']))
        self.assertEqual(1,third['summary']['recommendations']['deferred'])

    def test_manual_complete_scan_can_satisfy_due_without_new_gets(self):
        with self.db:enqueue_recommendation(self.db,definition=SOURCE,slot=self.clock.now,now=self.clock.now)
        run_recommendation_jobs(self.db,self.client,owner='manual',max_requests=5,clock=self.clock)
        batch=self.plan();self.assertEqual(0,batch['summary']['recommendations']['selected']);self.assertTrue(batch['selectedWorkSettled'])
        self.assertEqual(0,self.execute(batch)['requestsThisInvocation'])

    def test_manual_partial_scan_is_resumed_with_its_original_slot(self):
        with self.db:scan=enqueue_recommendation(self.db,definition=SOURCE,slot=self.clock.now,now=self.clock.now)
        run_recommendation_jobs(self.db,self.client,owner='manual',max_requests=2,clock=self.clock)
        self.next_slot();batch=self.plan();self.assertEqual((scan,),selected_scans(self.db,batch['batchId']))
        self.assertEqual(3,self.execute(batch)['requestsThisInvocation'])

    def test_unrelated_manual_scan_is_outside_batch_request_selection(self):
        with self.db:scan=enqueue_recommendation(self.db,definition={**SOURCE,'repositoryId':999},slot=self.clock.now,now=self.clock.now)
        result=self.execute(self.plan());self.assertEqual(5,result['requestsThisInvocation'])
        self.assertEqual(0,self.db.execute('SELECT step FROM recommendation_scans WHERE id=?',(scan,)).fetchone()[0])
        self.assertNotIn('repositories/999',self.calls)

    def test_terminal_failure_retries_later_without_retracting_old_members(self):
        self.execute(self.plan());self.clock.now+=timedelta(days=1);batch=self.plan()
        self.responses['repos/source/list-123/git/trees/'+TREE]=(0,wire({'sha':TREE,'truncated':False,'tree':[]}));result=self.execute(batch)
        self.assertEqual('partial',result['state']);self.assertFalse(result['selectedWorkSettled'])
        self.assertEqual(3,result['requestsThisInvocation']);self.assertEqual(2,self.db.execute("SELECT count(*) FROM recommendation_observations WHERE status='active'").fetchone()[0])
        self.assertEqual('2026-10-13T08:00:00.000000Z',self.state()['resources'][public_source_identity(SOURCE)]['nextDueAt'])
        self.clock.now+=timedelta(hours=4)
        # The same slot's immutable plan is not silently replaced.
        self.assertEqual(batch['batchId'],self.plan()['batchId'])
        self.clock.now+=timedelta(hours=4);self.responses.clear();self.assertEqual(1,self.plan()['summary']['recommendations']['selected'])

    def test_uncertain_reserved_attempt_is_not_refunded(self):
        self.policy=replace(self.policy,recommendations=configuration(budget=1));batch=self.plan()
        with patch('agent_forge_service.batches.run_recommendation_jobs',side_effect=RuntimeError('process stopped after reservation')):
            with self.assertRaises(RuntimeError):self.execute(batch)
        status=batch_status(self.db,batch['batchId']);self.assertEqual('uncertain',status['state'])
        self.assertEqual(1,status['lanes']['recommendations']['charged']);self.assertEqual(0,len(self.calls))
        self.assertEqual(0,self.execute(batch)['requestsThisInvocation'])

    def test_shared_core_limit_blocks_both_clients_and_different_identity_rejected(self):
        batch=self.plan();github=GitHubClient(self.db,identity='fixture-profile',clock=self.clock)
        wrong=self.client_for(self.db,'different')
        with self.assertRaises(ValueError):run_batch(self.db,github,batch['batchId'],owner='worker',max_requests=1,clock=self.clock,recommendation_client=wrong)
        self.client.fetch(__import__('agent_forge_service.recommendations',fromlist=['RecommendationGitRequest']).RecommendationGitRequest('repository',123))
        self.assertEqual(github.next_allowed_at('core',now=self.clock.now),self.client.next_allowed_at('core',now=self.clock.now))

    def test_stop_before_collection_does_not_reserve_or_fetch(self):
        batch=self.plan();status=run_batch(self.db,None,batch['batchId'],owner='worker',max_requests=5,clock=self.clock,recommendation_client=self.client,should_stop=lambda:True)
        self.assertEqual(0,status['requestsThisInvocation']);self.assertEqual(0,status['lanes']['recommendations']['charged']);self.assertEqual([],self.calls)

    def test_corrupt_due_state_fails_before_creating_a_new_batch(self):
        self.plan();self.next_slot()
        with self.db:self.db.execute('UPDATE metadata SET value=? WHERE key=?',('null',PREFIX+self.policy.policy_id))
        with self.assertRaises(ServiceError) as error:self.plan()
        self.assertEqual('recommendation_refresh_state_invalid',error.exception.code)
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM batch_runs').fetchone()[0])

    def test_backward_planning_clock_fails_without_rewriting_due(self):
        self.plan();raw=encode_json(self.state());self.clock.now-=timedelta(hours=8)
        with self.assertRaises(ServiceError):self.plan()
        self.assertEqual(raw,encode_json(self.state()))

    def test_policy_mutation_requires_a_new_policy_id(self):
        self.plan()
        with self.assertRaises(ServiceError) as error:self.plan(replace(self.policy,recommendations=configuration(interval=8)))
        self.assertEqual('batch_policy_conflict',error.exception.code)

    def test_disabled_configuration_creates_no_state_or_lane(self):
        policy=replace(self.policy,recommendations=configuration(budget=0,maximum=0))
        batch=self.plan(policy);self.assertNotIn('recommendations',batch['lanes']);self.assertEqual('disabled',batch['summary']['recommendations']['status'])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM recommendation_scans').fetchone()[0])

    def test_scheduler_collects_and_publishes_only_complete_scope(self):
        package=record(identity='synthetic/curated/包');package['distributions'][0]['url']='https://github.com/o/alpha'
        path=self.catalog/'sources/plugin/packages';path.mkdir(parents=True,exist_ok=True);(path/'alpha.json').write_text(json.dumps(package),encoding='utf8')
        options=ScheduleOptions(1,3600)
        for iteration in range(8):
            result=scheduled_tick(self.settings,self.policy,options,identity='fixture-profile',clock=self.clock,recommendations_factory=self.client_for)
            if result['phase']=='published':break
        self.assertEqual('published',result['phase']);self.assertEqual(5,result['batch']['lanes']['recommendations']['requests'])
        query=QueryService(self.settings);value=query.recommendations(package['id'])
        self.assertEqual(1,len(value['items']));self.assertIsNone(value['items'][0]['sourceRank'])
        self.assertEqual(2,query.recommendation_sources()['recommendationCoverage']['members'])
        calls=len(self.calls);self.assertEqual('idle',scheduled_tick(self.settings,self.policy,options,identity='fixture-profile',clock=self.clock,recommendations_factory=self.client_for)['phase']);self.assertEqual(calls,len(self.calls))

    def test_scheduler_budget_deadline_publishes_partial_without_partial_members(self):
        path=self.catalog/'sources/plugin/packages';path.mkdir(parents=True);(path/'alpha.json').write_text(json.dumps(record()),encoding='utf8')
        self.policy=replace(self.policy,recommendations=configuration(budget=3))
        result=scheduled_tick(self.settings,self.policy,ScheduleOptions(5,3600),identity='fixture-profile',clock=self.clock,recommendations_factory=self.client_for)
        self.assertEqual('published',result['phase']);self.assertEqual('request_budgets_exhausted',result['publication']['completionReason'])
        self.assertEqual([],QueryService(self.settings).recommendation_sources()['items'])
        self.next_slot();result=scheduled_tick(self.settings,self.policy,ScheduleOptions(5,3600),identity='fixture-profile',clock=self.clock,recommendations_factory=self.client_for)
        self.assertEqual('published',result['phase']);self.assertEqual(2,len(self.calls)-3);self.assertEqual(1,len(QueryService(self.settings).recommendation_sources()['items']))

    def test_scheduler_requires_identity_for_recommendation_only_policy(self):
        with self.assertRaises(ValueError):scheduled_tick(self.settings,self.policy,ScheduleOptions(5,3600),clock=self.clock)
        self.assertEqual([],self.calls)

    def test_initial_policy_enables_curated_channel_explicitly(self):
        pilot=json.loads(Path('agent_forge_service/examples/scheduler-pilot.json').read_text())
        pilot.update(schemaVersion=1,recommendations={'budget':5,'maxSources':1,'intervalHours':24,'retryHours':8,'sources':[SOURCE]})
        path=self.root/'policy.json';path.write_text(json.dumps(pilot),encoding='utf8')
        loaded=load_policy(path);self.assertEqual(5,loaded.recommendations.budget)
        self.assertIsNone(load_policy(Path('agent_forge_service/examples/scheduler-pilot.json')).recommendations)
        for field,value in [('intervalHours',5),('maxSources',True),('sources',[{**SOURCE,'label':None}]),('budget',-1)]:
            bad=json.loads(json.dumps(pilot));bad['recommendations'][field]=value;path.write_text(json.dumps(bad),encoding='utf8')
            with self.subTest(field=field),self.assertRaises((ValueError,TypeError)):load_policy(path)

    def test_disabled_curated_channel_is_null_in_config_and_absent_from_lanes(self):
        policy=replace(self.policy,recommendations=None);batch=self.plan(policy)
        raw=json.loads(self.db.execute('SELECT config_json FROM batch_policies').fetchone()[0]);self.assertIsNone(raw['recommendations'])
        self.assertNotIn('recommendations',batch['lanes'])


    def test_backup_restore_resumes_original_scan_and_preserves_due_and_budget(self):
        batch=self.plan();self.execute(batch,maximum=2)
        scan=selected_scans(self.db,batch['batchId']);before=encode_json(self.state())
        receipt=create_state_backup(self.settings,self.root/'backup')
        target=Settings(self.catalog,self.root/'restored',publication_delay_seconds=0)
        restore_state_backup(target,self.root/'backup',expected_backup_sha256=receipt['backupSha256'])
        restored=open_state(target.state_path);self.addCleanup(restored.close)
        self.assertEqual(before,restored.execute('SELECT value FROM metadata WHERE key=?',(PREFIX+self.policy.policy_id,)).fetchone()[0])
        self.assertEqual(scan,selected_scans(restored,batch['batchId']))
        self.assertEqual(2,batch_status(restored,batch['batchId'])['lanes']['recommendations']['charged'])
        result=run_batch(restored,None,batch['batchId'],owner='restored-worker',max_requests=5,clock=self.clock,recommendation_client=self.client_for(restored))
        self.assertEqual(3,result['requestsThisInvocation']);self.assertTrue(result['selectedWorkSettled'])
        self.assertFalse(target.snapshots.exists())

    def test_scheduler_interrupt_after_get_recovers_same_batch_before_publish(self):
        path=self.catalog/'sources/plugin/packages';path.mkdir(parents=True);(path/'alpha.json').write_text(json.dumps(record()),encoding='utf8')
        stopped=[False];self.on_request=lambda _:stopped.__setitem__(0,True)
        options=ScheduleOptions(5,3600)
        result=scheduled_tick(self.settings,self.policy,options,identity='fixture-profile',clock=self.clock,recommendations_factory=self.client_for,should_stop=lambda:stopped[0])
        self.assertEqual('interrupted',result['phase']);self.assertEqual(1,len(self.calls));self.assertFalse(self.settings.snapshots.exists())
        batch_id=scheduler_status(self.settings.state_path,self.policy.policy_id)['active']['batchId']
        self.on_request=None;result=scheduled_tick(self.settings,self.policy,options,identity='fixture-profile',clock=self.clock,recommendations_factory=self.client_for)
        self.assertEqual('published',result['phase']);self.assertEqual(batch_id,result['publication']['batchId']);self.assertEqual(5,len(self.calls))

    def test_secondary_limit_is_shared_and_keeps_old_facts(self):
        batch=self.plan();self.responses['repositories/123']=(1,wire({'message':'secondary rate limit'},403,{'Retry-After':'60'}))
        result=self.execute(batch);self.assertEqual(1,result['requestsThisInvocation'])
        self.assertTrue(result['nextNetworkAt']['core']);self.assertEqual(1,result['lanes']['recommendations']['charged'])
        github=GitHubClient(self.db,identity='fixture-profile',clock=self.clock)
        self.assertEqual(result['nextNetworkAt']['core'],github.next_allowed_at('core',now=self.clock.now))
        self.assertEqual(0,self.execute(batch)['requestsThisInvocation']);self.assertEqual(0,self.db.execute('SELECT count(*) FROM recommendation_snapshots').fetchone()[0])

    def test_future_manual_scan_is_not_selected_before_its_slot(self):
        with self.db:future=enqueue_recommendation(self.db,definition=SOURCE,slot=self.clock.now+timedelta(days=1),now=self.clock.now)
        batch=self.plan();self.assertNotIn(future,selected_scans(self.db,batch['batchId']))
        self.assertEqual(5,self.execute(batch)['requestsThisInvocation'])
        self.assertEqual('queued',self.db.execute('SELECT state FROM recommendation_scans WHERE id=?',(future,)).fetchone()[0])

    def test_capacity_warning_discloses_full_git_chain_cost(self):
        self.policy=replace(self.policy,recommendations=configuration(SOURCE,{**SOURCE,'repositoryId':124},budget=5,maximum=2))
        summary=self.plan()['summary']['recommendations']
        self.assertEqual([5,5],sorted(summary['minimumRequestsPerCompleteScan'].values()))
        self.assertEqual(15,summary['dailyRequestBudget'])
        self.assertEqual(['request_budget_cannot_complete_selected_scans_in_one_slot'],summary['capacityWarnings'])

    def test_four_hour_slots_require_aligned_source_intervals(self):
        four=replace(self.policy,slot_hours=4,refresh=replace(self.policy.refresh,fast_hours=4),recommendations=configuration(interval=8,retry=4))
        self.assertEqual(4,four.slot_hours);self.assertEqual(1,self.plan(four)['summary']['recommendations']['selected'])
        with self.assertRaises(ValueError):replace(self.policy,recommendations=configuration(retry=4))

    def test_configured_sources_do_not_depend_on_canonical_rankings(self):
        first=self.plan();self.assertEqual('unavailable',first['summary']['rankingStatus'])
        self.assertEqual(1,first['summary']['recommendations']['selected'])
        self.execute(first);self.assertEqual(0,self.db.execute('SELECT count(*) FROM change_events').fetchone()[0])
        self.assertTrue(all(row[0] is None for row in self.db.execute('SELECT source_rank FROM recommendation_members')))

    def test_malformed_refresh_fields_and_duplicate_json_keys_are_rejected(self):
        batch=self.plan();original=encode_json(self.state())
        for raw in ['[]','{"schemaVersion":1,"schemaVersion":1}',original.replace('"nextDueAt":"2026-10-12T00:00:00.000000Z"','"nextDueAt":null')]:
            with self.db:self.db.execute('UPDATE metadata SET value=? WHERE key=?',(raw,PREFIX+self.policy.policy_id))
            self.next_slot()
            with self.subTest(raw=raw),self.assertRaises(ServiceError):self.plan()
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM batch_runs').fetchone()[0])

    def test_new_policy_reuses_inflight_scan_without_duplicate_get_chain(self):
        batch=self.plan();self.execute(batch,maximum=1)
        new=replace(self.policy,policy_id='curated-second',refresh=replace(self.policy.refresh,policy_id='curated-second'))
        second=self.plan(new);self.assertEqual(selected_scans(self.db,batch['batchId']),selected_scans(self.db,second['batchId']))
        self.assertEqual(4,self.execute(second)['requestsThisInvocation']);self.assertEqual(1,self.db.execute('SELECT count(*) FROM recommendation_snapshots').fetchone()[0])

    def test_regular_github_and_recommendation_lanes_share_durable_rotation(self):
        from agent_forge_service.canonical_mapping import sync_canonical_mappings
        package=record();package['links']={'repository':'https://github.com/o/alpha'}
        path=self.catalog/'sources/plugin/packages';path.mkdir(parents=True);(path/'alpha.json').write_text(json.dumps(package),encoding='utf8')
        sync_canonical_mappings(self.db,self.catalog,now=self.clock.now)
        self.policy=replace(self.policy,bootstrap_budget=1)
        calls=[]
        def transport(argv,*_):
            calls.append(argv[-1]);return 0,wire({'id':77,'full_name':'o/alpha','private':False,'default_branch':'main','stargazers_count':1})
        github=GitHubClient(self.db,identity='fixture-profile',transport=GhTransport(runner=transport),policy=self.client.policy,clock=self.clock,random_value=lambda:0)
        batch=self.plan();result=run_batch(self.db,github,batch['batchId'],owner='fair-worker',max_requests=2,clock=self.clock,recommendation_client=self.client)
        self.assertEqual(1,result['lanes']['bootstrap']['charged']);self.assertEqual(1,result['lanes']['recommendations']['charged'])
        self.assertEqual(1,len(calls));self.assertEqual(1,len(self.calls))



    def test_newer_complete_manual_check_satisfies_older_incomplete_scope(self):
        self.policy=replace(self.policy,recommendations=configuration(retry=48))
        with self.db:old=enqueue_recommendation(self.db,definition=SOURCE,slot=self.clock.now,now=self.clock.now)
        run_recommendation_jobs(self.db,self.client,owner='manual',max_requests=1,clock=self.clock,scan_ids=(old,))
        self.next_slot()
        with self.db:new=enqueue_recommendation(self.db,definition=SOURCE,slot=self.clock.now,now=self.clock.now)
        run_recommendation_jobs(self.db,self.client,owner='manual',max_requests=5,clock=self.clock,scan_ids=(new,))
        batch=self.plan();self.assertEqual(0,batch['summary']['recommendations']['selected'])
        self.assertEqual(0,self.execute(batch)['requestsThisInvocation'])
        self.assertEqual('running',self.db.execute('SELECT state FROM recommendation_scans WHERE id=?',(old,)).fetchone()[0])
        self.next_slot()
        with self.db:self.db.execute("UPDATE recommendation_scans SET state='failed',finished_at=? WHERE id=?",(timestamp(self.clock.now),old))
        later=self.plan();self.assertEqual(0,later['summary']['recommendations']['selected'])
        self.assertEqual('2026-10-13T08:00:00.000000Z',self.state()['resources'][public_source_identity(SOURCE)]['nextDueAt'])



    def test_late_older_selection_does_not_extend_newer_source_success_clock(self):
        self.execute(self.plan())
        first=self.db.execute('SELECT id,scan_id FROM recommendation_snapshots').fetchone()
        self.next_slot()
        with self.db:new=enqueue_recommendation(self.db,definition=SOURCE,slot=self.clock.now,now=self.clock.now)
        self.commit='c'*40
        run_recommendation_jobs(self.db,self.client,owner='manual',max_requests=5,clock=self.clock,scan_ids=(new,))
        latest=self.db.execute('SELECT completed_at FROM recommendation_snapshots WHERE scan_id=?',(new,)).fetchone()[0]
        self.clock.now+=timedelta(hours=8);late=timestamp(self.clock.now)
        with self.db:
            self.db.execute('UPDATE recommendation_snapshots SET completed_at=? WHERE id=?',(late,first['id']))
            self.db.execute('UPDATE recommendation_scans SET finished_at=? WHERE id=?',(late,first['scan_id']))
        self.plan();resource=self.state()['resources'][public_source_identity(SOURCE)]
        self.assertEqual(latest,resource['lastSuccessAt'])
        self.assertEqual('2026-10-13T08:00:00.000000Z',resource['nextDueAt'])


if __name__=='__main__':unittest.main()
