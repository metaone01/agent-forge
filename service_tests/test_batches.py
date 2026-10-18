"""Bounded batch fixtures: real queue/client transitions, no public-network tests."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_forge_service.batches import BatchPolicy, batch_status, load_policy, plan_batch, read_batch_status, run_batch
from agent_forge_service.canonical_mapping import sync_canonical_mappings
from agent_forge_service.collector import enqueue_repository, run_repository_jobs
from agent_forge_service.discovery import DiscoveryPolicy
from agent_forge_service.errors import ServiceError
from agent_forge_service.github import GhTransport
from agent_forge_service.github_client import GitHubClient, RequestPolicy
from agent_forge_service.jobs import JobQueue
from agent_forge_service.scheduling import RankedResource, RefreshPolicy, utc_slot
from agent_forge_service.storage import encode_json, open_state, timestamp
from service_tests.test_collector import BODY
from service_tests.temporary import cleanup_temporary
from service_tests.test_publication_query import record

NOW = datetime(2026,10,11,8,tzinfo=timezone.utc)
POLICY = BatchPolicy('explicit-pilot',8,RefreshPolicy('explicit-pilot',2,168,8,24,0.25),4,0,0,0,(),
    '2026-10-10T00:00:00Z',24,'2026-10-01T00:00:00Z',24,24,DiscoveryPolicy(60,255))


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='forge-batch-test-'); self.addCleanup(cleanup_temporary,self.temp,Path(self.temp.name).resolve())
        self.root=Path(self.temp.name); self.catalog=self.root/'catalog'; self.packages=self.catalog/'sources/plugin/packages'
        self.packages.mkdir(parents=True); self.dbpath=self.root/'runtime/state.db'
        self.db=open_state(self.dbpath); self.addCleanup(self.db.close)
        self.now=NOW; self.calls=[]; self.responses=[]; self.response_for=None; self.advance=timedelta(0)
        def runner(*args):
            self.calls.append(args[0]); status,body=self.response_for(args[0][-1]) if self.response_for else self.responses.pop(0)
            self.now+=self.advance
            text=f'HTTP/2.0 {status} status\r\nETag: "unchanged"\r\n\r\n'+(json.dumps(body) if body is not None else '')
            return (0 if status<400 else 1),text.encode()
        self.client=GitHubClient(self.db,identity='batch-fixture',transport=GhTransport(runner=runner),
            policy=RequestPolicy(jitter_fraction=0),clock=lambda:self.now)

    def write(self,name='alpha',repository='example/repo',agents=('dsh',),version='1.0.0'):
        value=record(name,agents=agents,version=version)
        value['links']={'repository':'https://github.com/'+repository}
        value['distributions']=[{'id':'git','type':'github-repo','url':'https://github.com/'+repository}]
        path=self.packages/(name+'-'+version+'.json');path.write_text(json.dumps(value),encoding='utf-8');return path

    def seed(self,count=1,*,last_hours=25):
        self.now=NOW-timedelta(days=10)
        for i in range(1,count+1): self.write('package'+str(i),'example/repo'+str(i))
        sync_canonical_mappings(self.db,self.catalog,now=self.now)
        for i in range(1,count+1):
            self.now=NOW-timedelta(hours=last_hours,seconds=-i*7)
            with self.db: enqueue_repository(JobQueue(self.db),repository='example/repo'+str(i),slot=timestamp(self.now),bootstrap=True,now=self.now)
            self.responses.append((200,{**BODY,'id':i,'full_name':'example/repo'+str(i)}))
            report=run_repository_jobs(self.db,self.client,owner='seed-worker',max_requests=1,clock=lambda:self.now)
            self.assertEqual(1,report['succeeded'])
        self.now=NOW; self.calls.clear()

    def candidate(self,repo_id,*,age_hours=1,bootstrap=False):
        with self.db:
            self.db.execute('INSERT INTO discovery_candidates VALUES (?,?,?,?,?,?,?)',(repo_id,'example/candidate'+str(repo_id),
                timestamp(NOW-timedelta(hours=age_hours)),timestamp(NOW),int(bootstrap),'discovered',encode_json({})))

    def plan(self,policy=POLICY,**kwargs):
        return plan_batch(self.db,policy,now=self.now,**kwargs)

    def run_batch(self,batch,budget=100):
        identity=batch['batchId'] if isinstance(batch,dict) else batch
        return run_batch(self.db,self.client,identity,owner='batch-worker',max_requests=budget,clock=lambda:self.now)

    def auto(self, *, status=200):
        self.advance=timedelta(seconds=7)
        def respond(endpoint):
            if status!=200: return status,{'message':'temporary failure'}
            if endpoint.startswith('search/'):
                return 200,{'total_count':0,'incomplete_results':False,'items':[]}
            if endpoint.startswith('repositories/'):
                repo_id=int(endpoint.split('/')[1])
                name='example/'+('candidate' if repo_id>=100 else 'repo')+str(repo_id)
            else:
                name=endpoint.removeprefix('repos/')
                suffix=name.removeprefix('example/repo')
                repo_id=int(suffix) if suffix.isdigit() else 900
            return 200,{**BODY,'id':repo_id,'full_name':name}
        self.response_for=respond

    def rank(self,repo_id,metric='github-stars',agent='dsh',category='plugin',n=1,revision='rank-r1',eligible=True):
        return RankedResource('github-repository:'+str(repo_id),agent,category,metric,n,revision,eligible)

    def test_latest_slot_is_idempotent_and_does_not_enqueue_missed_slots(self):
        self.seed(2); self.now+=timedelta(days=3,hours=7)
        batch=self.plan(); self.assertEqual(utc_slot(self.now),batch['slot'])
        again=self.plan(); self.assertEqual(batch['batchId'],again['batchId'])
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM batch_runs').fetchone()[0])
        self.assertEqual(2,self.db.execute('SELECT count(*) FROM batch_jobs').fetchone()[0])
        self.assertEqual('unavailable',batch['summary']['rankingStatus'])
        self.assertFalse(self.calls)

    def test_policy_id_reuse_with_changed_parameters_rolls_back(self):
        self.seed(); batch=self.plan()
        with self.assertRaises(ServiceError) as error: self.plan(replace(POLICY,refresh_budget=3))
        self.assertEqual('batch_policy_conflict',error.exception.code)
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM batch_runs').fetchone()[0])
        self.assertEqual(batch['batchId'],self.plan()['batchId'])

    def test_ordinary_and_fast_selection_reserves_longtail_and_unions_signals(self):
        self.seed(8)
        ranks=tuple(self.rank(i) for i in range(1,7))+tuple(self.rank(i,metric='downloads-source-a') for i in range(1,7))
        batch=self.plan(rankings=ranks,ranking_revision='rank-r1')
        self.assertEqual(1,batch['lanes']['ordinary']['quota']);self.assertEqual(3,batch['lanes']['fast']['quota'])
        self.assertEqual(4,batch['summary']['refreshDeferred'])
        self.assertEqual(4,self.db.execute('SELECT count(DISTINCT job_id) FROM batch_jobs').fetchone()[0])
        self.assertEqual(6,batch['summary']['fastDue'])

    def test_cohort_frozen_within_utc_day_and_recomputed_next_day(self):
        self.seed(2)
        first=self.plan(rankings=(self.rank(1),),ranking_revision='rank-r1')
        self.now+=timedelta(hours=8)
        second=self.plan(rankings=(self.rank(2,revision='rank-r2'),),ranking_revision='rank-r2')
        self.assertEqual(first['cohortId'],second['cohortId']);self.assertEqual('rank-r1',second['summary']['rankingRevision'])
        self.now+=timedelta(hours=8)
        third=self.plan(rankings=(self.rank(2,revision='rank-r2'),),ranking_revision='rank-r2')
        self.assertNotEqual(first['cohortId'],third['cohortId']);self.assertEqual('rank-r2',third['summary']['rankingRevision'])

    def test_shared_repo_multi_agent_membership_does_not_duplicate_requests(self):
        self.seed()
        self.write('another','example/repo1',agents=('claude-code',))
        sync_canonical_mappings(self.db,self.catalog,now=self.now)
        batch=self.plan(rankings=(self.rank(1),self.rank(1,agent='claude-code'),self.rank(1,metric='downloads-other')),ranking_revision='rank-r1')
        self.assertEqual(1,batch['summary']['refreshResources']);self.assertEqual(1,batch['summary']['refreshSelected'])
        row=self.db.execute('SELECT memberships_json,reasons_json FROM refresh_resources').fetchone()
        self.assertEqual([['claude-code','plugin'],['dsh','plugin']],json.loads(row[0]))
        self.assertEqual(2,len(json.loads(row[1])))

    def test_new_candidates_are_protected_without_invented_agent_membership(self):
        self.candidate(100);self.candidate(101,bootstrap=True)
        batch=self.plan()
        self.assertEqual(1,batch['summary']['fastDue'])
        row=self.db.execute('SELECT * FROM refresh_resources WHERE github_id=100').fetchone()
        self.assertEqual([['unclassified','repository']],json.loads(row['memberships_json']))
        self.assertEqual('fast',row['tier'])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM package_units').fetchone()[0])

    def test_demoting_or_changing_policy_does_not_postpone_existing_due_work(self):
        self.seed(last_hours=9)
        first=self.plan(rankings=(self.rank(1),),ranking_revision='rank-r1')
        due=self.db.execute('SELECT next_due_at FROM refresh_resources').fetchone()[0]
        self.now+=timedelta(hours=16)
        second=self.plan()
        self.assertEqual('ordinary',self.db.execute('SELECT tier FROM refresh_resources').fetchone()[0])
        self.assertEqual(due,self.db.execute('SELECT next_due_at FROM refresh_resources').fetchone()[0])
        self.assertEqual(1,second['summary']['ordinaryDue'])
        self.assertEqual(self.db.execute('SELECT job_id FROM batch_jobs WHERE batch_id=?',(first['batchId'],)).fetchone()[0],
                         self.db.execute('SELECT job_id FROM batch_jobs WHERE batch_id=?',(second['batchId'],)).fetchone()[0])

    def test_bootstrap_initial_due_is_stably_spread_across_three_slots(self):
        self.now=NOW.replace(hour=0)
        for i in range(1,40): self.candidate(i,age_hours=8,bootstrap=True)
        self.plan(replace(POLICY,refresh_budget=0))
        dues=[r[0] for r in self.db.execute('SELECT next_due_at FROM refresh_resources')]
        self.assertEqual({0,8,16},{int(v[11:13]) for v in dues})
        self.assertTrue(all(v>=timestamp(NOW-timedelta(hours=8)) for v in dues))
        first=list(dues);self.plan(replace(POLICY,refresh_budget=0));self.assertEqual(first,[r[0] for r in self.db.execute('SELECT next_due_at FROM refresh_resources')])

    def test_nonpositive_or_unsupported_policy_values_fail_closed(self):
        for kwargs in ({'slot_hours':6},{'refresh_budget':True},{'bootstrap_budget':1001},{'topics':('mcp-server','mcp-server')},{'overlap_hours':169}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError): replace(POLICY,**kwargs)

    def test_missing_mapping_membership_requires_explicit_import(self):
        self.seed()
        with self.db: self.db.execute("DELETE FROM metadata WHERE key='canonical_targets_sync'")
        with self.assertRaises(ServiceError) as error:self.plan()
        self.assertEqual('mapping_targets_missing',error.exception.code)
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM batch_runs').fetchone()[0])

    def test_request_quota_persists_across_invocations_and_restart(self):
        self.seed(4);batch=self.plan();self.auto()
        first=self.run_batch(batch,budget=2);self.assertEqual(2,first['requestsThisInvocation'])
        self.assertEqual(2,len(self.calls))
        self.assertEqual(2,self.run_batch(batch,budget=2)['requestsThisInvocation'])
        self.assertEqual(0,self.run_batch(batch,budget=100)['requestsThisInvocation'])
        self.assertEqual(4,len(self.calls));self.assertEqual(4,batch_status(self.db,batch['batchId'])['lanes']['ordinary']['charged'])
        reopened=open_state(self.dbpath)
        try: self.assertEqual(4,batch_status(reopened,batch['batchId'])['lanes']['ordinary']['charged'])
        finally: reopened.close()

    def test_filtered_runner_does_not_consume_unrelated_manual_jobs(self):
        self.seed();batch=self.plan();self.auto()
        with self.db:
            job=enqueue_repository(JobQueue(self.db),repository=999,slot=timestamp(NOW-timedelta(days=1)),bootstrap=True,now=self.now)
        report=self.run_batch(batch)
        self.assertEqual(1,report['requestsThisInvocation'])
        self.assertEqual('pending',self.db.execute('SELECT state FROM jobs WHERE id=?',(job,)).fetchone()[0])
        self.assertNotIn('repositories/999',[c[-1] for c in self.calls])

    def test_actual_execution_respects_ordinary_reserve_under_hot_load(self):
        self.seed(8)
        batch=self.plan(rankings=tuple(self.rank(i) for i in range(1,7)),ranking_revision='rank-r1');self.auto()
        first=self.run_batch(batch,budget=1)
        self.assertEqual(1,first['lanes']['ordinary']['requests'])
        self.assertEqual(0,first['lanes']['fast']['requests'])
        final=self.run_batch(batch)
        self.assertEqual(1,final['lanes']['ordinary']['requests']);self.assertEqual(3,final['lanes']['fast']['requests'])
        self.assertEqual(4,len(self.calls))

    def test_failed_attempt_is_charged_and_keeps_due_and_previous_success(self):
        self.seed();batch=self.plan();before=dict(self.db.execute('SELECT * FROM refresh_resources').fetchone());self.auto(status=500)
        failed=self.run_batch(batch)
        self.assertEqual('budget_exhausted',failed['state']);self.assertEqual(1,failed['lanes']['ordinary']['charged'])
        after=self.db.execute('SELECT * FROM refresh_resources').fetchone()
        self.assertEqual(before['next_due_at'],after['next_due_at']);self.assertEqual(before['last_success_at'],after['last_success_at'])
        self.assertEqual(0,self.run_batch(batch)['requestsThisInvocation'])
        self.now=NOW+timedelta(hours=8);next_batch=self.plan();self.auto();self.assertEqual(1,self.run_batch(next_batch)['requestsThisInvocation'])

    def test_rate_limit_wait_is_refunded_and_not_busy_polled(self):
        self.seed();batch=self.plan()
        with self.db:
            self.db.execute("UPDATE rate_limits SET next_request_at=? WHERE credential_identity='batch-fixture'",(timestamp(NOW+timedelta(minutes=10)),))
        report=self.run_batch(batch)
        self.assertEqual(0,report['requestsThisInvocation']);self.assertEqual(0,report['actionsThisInvocation'])
        self.assertEqual(timestamp(NOW+timedelta(minutes=10)),report['nextNetworkAt']['core'])
        self.assertEqual(0,report['lanes']['ordinary']['charged']);self.assertEqual([],self.calls)
        self.now=NOW+timedelta(minutes=11);self.auto();self.assertEqual(1,self.run_batch(batch)['requestsThisInvocation'])

    def test_uncertain_crash_reservation_is_not_refunded_on_restart(self):
        self.seed();batch=self.plan()
        with patch('agent_forge_service.batches.run_repository_jobs',side_effect=RuntimeError('process interrupted')):
            with self.assertRaises(RuntimeError):self.run_batch(batch)
        report=batch_status(self.db,batch['batchId'])
        self.assertEqual('uncertain',report['state']);self.assertEqual(1,report['lanes']['ordinary']['charged'])
        self.assertEqual(1,report['lanes']['ordinary']['uncertain']);self.assertEqual(0,self.run_batch(batch)['requestsThisInvocation'])
        self.assertEqual([],self.calls)

    def test_new_candidate_first_metadata_preserves_discovery_time(self):
        self.candidate(100,age_hours=2);batch=self.plan();self.auto();self.run_batch(batch)
        repo=self.db.execute('SELECT * FROM repositories').fetchone()
        self.assertEqual(timestamp(NOW-timedelta(hours=2)),repo['first_seen_at']);self.assertFalse(repo['bootstrap'])

    def test_created_and_pushed_streams_share_budget_without_fake_completeness(self):
        policy=replace(POLICY,refresh_budget=0,discovery_budget=1,topics=('mcp-server',))
        batch=self.plan(policy);self.auto();result=self.run_batch(batch)
        self.assertEqual(1,len(self.calls));self.assertEqual(1,result['requestsThisInvocation'])
        self.assertEqual('budget_exhausted',result['state']);self.assertFalse(result['selectedWorkSettled'])
        self.assertEqual(1,sum(s['watermark'] is not None for s in result['streams']))
        self.assertEqual(0,self.run_batch(batch)['requestsThisInvocation'])

    def test_discovery_retry_uses_original_stream_and_does_not_skip_gap(self):
        policy=replace(POLICY,refresh_budget=0,discovery_budget=2,topics=('mcp-server',))
        first=self.plan(policy);streams={s['id'] for s in first['streams']};self.auto(status=500);self.run_batch(first)
        self.now=NOW+timedelta(hours=8);second=self.plan(policy)
        self.assertEqual(streams,{s['id'] for s in second['streams']})
        self.assertTrue(all(s['watermark'] is None for s in second['streams']))
        self.assertEqual({s['active_run_id'] for s in first['streams']},{s['active_run_id'] for s in second['streams']})

    def test_history_pages_and_next_chunks_follow_same_stream_under_one_budget(self):
        policy=replace(POLICY,refresh_budget=0,history_budget=3,topics=('mcp-server',),history_start='2026-10-10T00:00:00Z',history_lag_hours=0,history_chunk_hours=8)
        batch=self.plan(policy);self.auto();report=self.run_batch(batch)
        self.assertEqual(3,report['requestsThisInvocation']);self.assertEqual('budget_exhausted',report['state'])
        stream=self.db.execute("SELECT * FROM discovery_streams WHERE mode='historical'").fetchone()
        self.assertEqual('2026-10-11T00:00:00.000000Z',stream['watermark'])
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM batch_history_cycles').fetchone()[0])
        self.now=NOW+timedelta(hours=8);second=self.plan(policy)
        self.assertEqual(1,self.db.execute('SELECT cycle FROM batch_history_cycles').fetchone()[0])
        self.run_batch(second)
        self.now=NOW+timedelta(hours=16);self.plan(policy)
        self.assertEqual(2,self.db.execute('SELECT cycle FROM batch_history_cycles').fetchone()[0])

    def test_bootstrap_uses_same_queue_and_deduplicates_shared_repository(self):
        self.write();self.write('beta');sync_canonical_mappings(self.db,self.catalog,now=self.now)
        policy=replace(POLICY,refresh_budget=0,bootstrap_budget=2)
        batch=self.plan(policy)
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM batch_jobs').fetchone()[0])
        self.auto();result=self.run_batch(batch)
        self.assertEqual(1,result['requestsThisInvocation']);self.assertEqual(1,self.db.execute('SELECT count(*) FROM repositories').fetchone()[0])
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM metric_observations').fetchone()[0])

    def test_empty_job_filters_never_turn_into_unfiltered_claims(self):
        self.seed();batch=self.plan()
        self.assertIsNone(JobQueue(self.db).claim(owner='test',now=self.now,job_ids=()))
        self.assertIsNone(JobQueue(self.db).claim(owner='test',now=self.now,scopes=()))
        with self.assertRaises(ValueError):JobQueue(self.db).claim(owner='test',now=self.now,job_ids=['not-a-tuple'])
        with self.assertRaises(ValueError):JobQueue(self.db).claim(owner='test',now=self.now,scopes=('x',)*1001)

    def test_read_only_status_and_unknown_batch_errors(self):
        batch=self.plan()
        self.assertEqual(batch['batchId'],read_batch_status(self.dbpath,batch['batchId'])['batchId'])
        with self.assertRaises(ServiceError):read_batch_status(self.dbpath,'missing')
        with self.assertRaises(ValueError):read_batch_status(self.dbpath,limit=-1)

    def test_success_updates_next_due_in_the_same_observation_transaction(self):
        self.seed();batch=self.plan();self.auto();self.run_batch(batch)
        resource=self.db.execute('SELECT * FROM refresh_resources').fetchone()
        repository=self.db.execute('SELECT * FROM repositories').fetchone()
        self.assertEqual(repository['last_success_at'],resource['last_success_at'])
        self.assertEqual(timestamp(datetime.fromisoformat(utc_slot(resource['last_success_at'], POLICY.slot_hours).replace('Z','+00:00'))+timedelta(hours=24)),resource['next_due_at'])

    def test_304_advances_refresh_due_without_a_new_numeric_sample(self):
        self.candidate(100);first=self.plan();self.auto();self.run_batch(first)
        count=self.db.execute('SELECT count(*) FROM metric_observations').fetchone()[0]
        observed=self.db.execute('SELECT observed_at FROM metric_observations').fetchone()[0]
        self.now=NOW+timedelta(hours=8,seconds=8);second=self.plan()
        self.response_for=lambda endpoint:(304,None);result=self.run_batch(second)
        self.assertEqual(1,result['requestsThisInvocation'])
        self.assertEqual(count,self.db.execute('SELECT count(*) FROM metric_observations').fetchone()[0])
        self.assertEqual(observed,self.db.execute('SELECT observed_at FROM metric_observations').fetchone()[0])
        self.assertGreater(self.db.execute('SELECT last_success_at FROM refresh_resources').fetchone()[0],observed)

    def test_observation_from_same_slot_satisfies_a_queued_refresh_without_new_get(self):
        self.seed();batch=self.plan();self.auto()
        with self.db:
            manual=enqueue_repository(JobQueue(self.db),repository='example/repo1',slot=timestamp(NOW),bootstrap=True,now=self.now)
        result=run_repository_jobs(self.db,self.client,owner='manual',max_requests=1,clock=lambda:self.now,job_ids=(manual,))
        self.assertEqual(1,result['requests'])
        samples=self.db.execute('SELECT count(*) FROM metric_observations').fetchone()[0]
        report=self.run_batch(batch)
        self.assertEqual(0,report['requestsThisInvocation']);self.assertTrue(report['selectedWorkSettled'])
        self.assertEqual(1,len(self.calls));self.assertEqual(samples,self.db.execute('SELECT count(*) FROM metric_observations').fetchone()[0])
        audit=json.loads(self.db.execute('SELECT result_json FROM batch_attempts').fetchone()[0])
        self.assertEqual(manual,audit['satisfiedByObservation'])

    def test_planning_failure_rolls_back_cohort_due_and_new_jobs(self):
        self.seed();before=self.db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        with patch('agent_forge_service.batches._discovery',side_effect=RuntimeError('interrupted plan')):
            with self.assertRaises(RuntimeError):self.plan()
        for table in ('batch_runs','refresh_cohorts','refresh_resources','batch_policies'):
            self.assertEqual(0,self.db.execute('SELECT count(*) FROM '+table).fetchone()[0])
        self.assertEqual(before,self.db.execute('SELECT count(*) FROM jobs').fetchone()[0])

    def test_terminal_partial_history_does_not_skip_range_or_start_a_new_cycle(self):
        policy=replace(POLICY,refresh_budget=0,history_budget=2,topics=('mcp-server',),discovery=DiscoveryPolicy(60,1))
        first=self.plan(policy)
        self.advance=timedelta(seconds=7)
        self.response_for=lambda endpoint:(200,{'total_count':1001,'incomplete_results':False,'items':[{**BODY,'id':i,'full_name':'example/repo'+str(i)} for i in range(1,101)]})
        result=self.run_batch(first)
        self.assertEqual('partial',result['state']);self.assertFalse(result['selectedWorkSettled'])
        self.now=NOW+timedelta(hours=8);second=self.plan(policy)
        self.assertEqual(first['streams'][0]['id'],second['streams'][0]['id'])
        self.assertIsNone(second['streams'][0]['watermark']);self.assertEqual(1,self.db.execute('SELECT cycle FROM batch_history_cycles').fetchone()[0])
        self.assertEqual(0,self.run_batch(second)['requestsThisInvocation'])

    def test_inflight_bootstrap_is_resumed_instead_of_duplicate_enqueuing(self):
        self.write();sync_canonical_mappings(self.db,self.catalog,now=self.now)
        policy=replace(POLICY,refresh_budget=0,bootstrap_budget=1)
        first=self.plan(policy);self.auto(status=500);self.run_batch(first)
        self.now=NOW+timedelta(hours=8);second=self.plan(policy)
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM jobs').fetchone()[0])
        first_job=self.db.execute('SELECT job_id FROM batch_jobs WHERE batch_id=?',(first['batchId'],)).fetchone()[0]
        self.assertEqual(first_job,self.db.execute('SELECT job_id FROM batch_jobs WHERE batch_id=?',(second['batchId'],)).fetchone()[0])
        self.auto();self.assertEqual(1,self.run_batch(second)['requestsThisInvocation'])

    def test_retired_mapping_deactivates_watch_and_ignores_old_cohort_entry(self):
        self.seed(2);self.plan(rankings=(self.rank(1),),ranking_revision='rank-r1')
        (self.packages/'package1-1.0.0.json').unlink();sync_canonical_mappings(self.db,self.catalog,now=self.now)
        self.now=NOW+timedelta(hours=8);second=self.plan()
        self.assertEqual(1,second['summary']['ignoredCohortEntries'])
        self.assertEqual(1,second['summary']['refreshResources'])
        self.assertFalse(self.db.execute('SELECT active FROM refresh_resources WHERE github_id=1').fetchone()[0])

    def test_lane_round_robin_position_survives_small_invocations(self):
        self.seed(2);self.write('unknown','example/unknown');sync_canonical_mappings(self.db,self.catalog,now=self.now)
        policy=replace(POLICY,refresh_budget=2,bootstrap_budget=1,discovery_budget=1,history_budget=1,topics=('mcp-server',))
        batch=self.plan(policy,rankings=(self.rank(1),),ranking_revision='rank-r1');self.auto()
        self.assertEqual(0,len(self.calls))  # seed() clears its own transport calls.
        for expected in range(1,6):
            self.assertEqual(1,self.run_batch(batch,budget=1)['requestsThisInvocation'])
            self.assertEqual(expected,len(self.calls))
        result=batch_status(self.db,batch['batchId'])
        self.assertEqual([1,1,1,1,1],[result['lanes'][lane]['requests'] for lane in ('ordinary','fast','bootstrap','incremental','history')])
        self.assertEqual(5,len(self.calls))

    def test_invalid_rank_input_and_runner_limit_have_no_reserved_requests(self):
        self.seed()
        with self.assertRaises(ValueError):self.plan(rankings=(self.rank(999),),ranking_revision='rank-r1')
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM batch_runs').fetchone()[0])
        batch=self.plan()
        for budget in (0,101,True):
            with self.assertRaises(ValueError):self.run_batch(batch,budget=budget)
        with self.assertRaises(ValueError):run_batch(self.db,self.client,batch['batchId'],owner='',max_requests=1,clock=lambda:self.now)
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM batch_attempts').fetchone()[0])

    def test_policy_file_rejects_duplicate_unknown_and_malformed_values(self):
        source=Path('examples/batch-policy.pilot.json');original=json.loads(source.read_text(encoding='utf-8'))
        path=self.root/'policy.json';path.write_text(source.read_text(encoding='utf-8'),encoding='utf-8')
        self.assertEqual(8,load_policy(path).slot_hours)
        for change in ('unknown','topic','start','budget'):
            value=json.loads(json.dumps(original))
            if change=='unknown':value['unexpected']=1
            elif change=='topic':value['discovery']['topics']=[{}]
            elif change=='start':value['discovery']['start']=123
            else:value['budgets']['refresh']=True
            path.write_text(json.dumps(value),encoding='utf-8')
            with self.subTest(change=change),self.assertRaises(ValueError):load_policy(path)
        path.write_text('{"policyId":"a","policyId":"b"}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'duplicate'):load_policy(path)

    def test_old_state_read_status_does_not_migrate_or_write(self):
        path=self.root/'v4.db';db=sqlite3.connect(path);db.execute('PRAGMA user_version=4');db.close()
        self.assertEqual('upgrade_required',read_batch_status(path)['batchSchema'])
        db=sqlite3.connect(path)
        try:self.assertEqual(4,db.execute('PRAGMA user_version').fetchone()[0])
        finally:db.close()

    def test_cli_planning_and_status_are_local_idempotent_commands(self):
        source=Path('examples/batch-policy.pilot.json').resolve()
        def cli(*args):
            call=subprocess.run([sys.executable,'-m','agent_forge_service.worker',*args,'--runtime',str(self.root/'cli-runtime')],capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(0,call.returncode,call.stderr);return json.loads(call.stdout)
        first=cli('batch-plan','--policy',str(source));second=cli('batch-plan','--policy',str(source))
        self.assertEqual(first['batchId'],second['batchId'])
        status=cli('batch-status','--batch-id',first['batchId'])
        self.assertEqual('unavailable',status['summary']['rankingStatus'])
        self.assertEqual(0,sum(l['requests'] for l in status['lanes'].values()))

    def test_batch_writes_preserve_canonical_and_database_integrity(self):
        self.seed(2);before={p.name:p.read_bytes() for p in self.packages.glob('*.json')}
        batch=self.plan();self.auto();self.run_batch(batch)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.packages.glob('*.json')})
        self.assertEqual([],self.db.execute('PRAGMA foreign_key_check').fetchall())
        self.assertEqual('ok',self.db.execute('PRAGMA quick_check').fetchone()[0])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM package_units').fetchone()[0])


    def test_exhausted_quota_does_not_block_free_observation_settlement(self):
        self.seed();batch=self.plan();self.auto(status=500);self.run_batch(batch)
        self.now=NOW+timedelta(minutes=2);self.auto()
        with self.db:manual=enqueue_repository(JobQueue(self.db),repository='example/repo1',slot=timestamp(self.now),bootstrap=True,now=self.now)
        run_repository_jobs(self.db,self.client,owner='manual',max_requests=1,clock=lambda:self.now,job_ids=(manual,))
        result=self.run_batch(batch)
        self.assertEqual(0,result['requestsThisInvocation']);self.assertTrue(result['selectedWorkSettled'])
        self.assertEqual(1,result['lanes']['ordinary']['charged']);self.assertEqual(2,len(self.calls))

    def test_large_cohort_has_complete_status_but_bounded_claim_filters(self):
        with self.db:
            self.db.executemany('INSERT INTO discovery_candidates VALUES (?,?,?,?,?,?,?)',[(i,'example/candidate'+str(i),timestamp(NOW-timedelta(days=10)),timestamp(NOW),0,'discovered','{}') for i in range(100,2100)])
        policy=replace(POLICY,refresh_budget=2000)
        batch=self.plan(policy)
        self.assertEqual(2000,batch['summary']['refreshSelected'])
        self.assertEqual(2000,batch['lanes']['ordinary']['jobs']['pending'])
        self.auto();result=self.run_batch(batch,budget=1)
        self.assertEqual(1,result['requestsThisInvocation'])
        self.assertEqual(1999,result['lanes']['ordinary']['jobs']['pending'])
        with self.assertRaises(ValueError):replace(POLICY,refresh_budget=100001)


    def test_four_hour_slots_match_plus_eight_timezone_and_keep_daily_ordinary(self):
        self.seed()
        policy=replace(POLICY,policy_id='four-hour-pilot',slot_hours=4,refresh=RefreshPolicy('four-hour-pilot',2,168,4,24,0.25))
        first=self.plan(policy)
        self.now=NOW.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(first['batchId'],self.plan(policy)['batchId'])
        self.now=NOW+timedelta(hours=4)
        second=self.plan(policy)
        self.assertEqual(timestamp(self.now),second['slot'])
        self.assertEqual(24,policy.refresh.ordinary_hours)



    def test_budget_shortfall_and_unknown_canonical_coverage_are_explicit(self):
        self.seed(5);self.write('unknown','example/unknown');sync_canonical_mappings(self.db,self.catalog,now=self.now)
        batch=self.plan(replace(POLICY,refresh_budget=1))
        self.assertEqual(['ordinary_daily_target_exceeds_refresh_budget'],batch['summary']['capacityWarnings'])
        self.assertEqual(1,batch['summary']['pendingCanonicalNamesAtPlanning'])
        self.assertEqual('not_guaranteed',batch['catalogCoverage'])


if __name__=='__main__':unittest.main()
