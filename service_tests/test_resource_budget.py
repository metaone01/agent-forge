"""Cache eviction never discards evidence; storage pressure never invents a sample."""
from collections import namedtuple
from dataclasses import replace
from datetime import timedelta
import contextlib
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_forge_service.github_client import CollectionError
from agent_forge_service.resource_budget import CachePolicy,ResourceLimits,DEFAULT_LIMITS,cached_response,cache_status,store_cached_response,storage_status,require_collection_room
from agent_forge_service.scheduler import run_schedule
from agent_forge_service.storage import encode_json,timestamp,open_state
from agent_forge_service.worker import main
from service_tests import test_collector as collector
from service_tests import test_downloads as downloads
from service_tests import test_scheduler as scheduled

Disk=namedtuple('Disk','total used free')


class ResourceBudgetTests(unittest.TestCase):
    def setUp(self):
        self.case=collector.CollectorTests('runTest');self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.db=self.case.db;self.now=self.case.now

    def put(self,key,body='{}',identity='public-fixture',checked=None):
        when=timestamp(checked or self.now)
        values=(key,identity,'"fixture"',None,body,hashlib.sha256(body.encode()).hexdigest(),when,when)
        with self.db:store_cached_response(self.db,values,now=self.now)
        return values

    def facts(self):
        return {t:[tuple(r) for r in self.db.execute('SELECT * FROM '+t+' ORDER BY 1')] for t in
            ['collection_evidence','repositories','repository_observations','metric_observations','checkpoints','jobs','rate_limits']}

    def test_limits_reject_bool_negative_unbounded_and_untyped_config(self):
        for fields in [{'max_entries':True},{'max_entries':0},{'max_entries':10001},{'max_bytes':1023},{'max_age_seconds':59},{'max_age_seconds':90*86400+1}]:
            with self.subTest(fields=fields),self.assertRaises(ValueError):CachePolicy(**fields)
        for fields in [{'max_state_bytes':True},{'max_state_bytes':1024},{'min_free_bytes':-1},{'min_free_bytes':1.1},{'cache':{} }]:
            with self.subTest(fields=fields),self.assertRaises(ValueError):ResourceLimits(**fields)
        with self.assertRaises(ValueError):open_state(self.case.path,resource_limits={})

    def test_total_row_limit_and_utf8_bytes_are_enforced(self):
        self.db.resource_limits=ResourceLimits(cache=CachePolicy(2,1024,3600))
        for i in range(8):
            self.now+=timedelta(seconds=1)
            self.put('key-'+str(i),encode_json({'value':'汉字'*30}))
            status=cache_status(self.db)
            self.assertLessEqual(status['entries'],2);self.assertLessEqual(status['logicalBytes'],1024)
        self.assertEqual(['key-6','key-7'],[r[0] for r in self.db.execute('SELECT request_key FROM http_cache ORDER BY request_key')])
        rows=list(self.db.execute('SELECT * FROM http_cache'))
        self.assertEqual(sum(len(str(v).encode()) for r in rows for v in r if v is not None),status['logicalBytes'])

    def test_oversized_cache_item_is_not_retained(self):
        self.db.resource_limits=ResourceLimits(cache=CachePolicy(3,1024,3600))
        self.put('oversized',encode_json({'large':'汉'*1024}))
        self.assertEqual(0,cache_status(self.db)['entries'])
        self.put('small');self.assertEqual(1,cache_status(self.db)['entries'])

    def test_cache_write_and_eviction_rollback_together(self):
        self.db.resource_limits=ResourceLimits(cache=CachePolicy(1,1024,3600))
        original=self.put('old');self.now+=timedelta(seconds=1)
        with self.assertRaises(RuntimeError):
            with self.db:
                store_cached_response(self.db,('new','public-fixture',None,None,'{}','hash',timestamp(self.now),timestamp(self.now)),now=self.now)
                raise RuntimeError('failed client cache transaction')
        self.assertEqual([original],[tuple(r) for r in self.db.execute('SELECT * FROM http_cache')])

    def test_expired_or_future_cache_never_sends_old_validators(self):
        self.db.resource_limits=ResourceLimits(cache=CachePolicy(3,1024,60));self.put('old')
        before=[tuple(r) for r in self.db.execute('SELECT * FROM http_cache')]
        self.now+=timedelta(seconds=60)
        self.assertIsNone(cached_response(self.db,'old','public-fixture',now=self.now))
        self.assertEqual(before,[tuple(r) for r in self.db.execute('SELECT * FROM http_cache')])
        self.put('new')
        self.assertIsNone(self.db.execute("SELECT 1 FROM http_cache WHERE request_key='old'").fetchone())
        self.assertIsNone(cached_response(self.db,'new','public-fixture',now=self.now-timedelta(seconds=1)))

    def test_cache_lookup_requires_the_matching_identity(self):
        self.put('same-key')
        self.assertIsNone(cached_response(self.db,'same-key','another-profile',now=self.now))
        self.assertIsNotNone(cached_response(self.db,'same-key','public-fixture',now=self.now))

    def test_eviction_preserves_observations_checkpoints_jobs_and_limit_ledger(self):
        self.case.enqueue();self.case.push();self.case.run_jobs();before=self.facts()
        self.db.resource_limits=ResourceLimits(cache=CachePolicy(1,1024,60))
        self.now+=timedelta(seconds=1);self.put('replacement')
        self.assertEqual(before,self.facts())
        self.assertEqual(1,cache_status(self.db)['entries'])
        self.assertEqual('replacement',self.db.execute('SELECT request_key FROM http_cache').fetchone()[0])

    def test_github_eviction_causes_unconditional_get_with_real_new_observation(self):
        self.case.enqueue();self.case.push();self.case.run_jobs()
        observed=self.db.execute('SELECT observed_at FROM repository_observations').fetchone()[0]
        self.db.resource_limits=ResourceLimits(cache=CachePolicy(1,1024,60))
        self.now+=timedelta(seconds=1);self.put('replacement')
        self.case.now+=timedelta(hours=8);self.case.enqueue();self.case.push(body={**collector.BODY,'stargazers_count':11})
        self.assertEqual(1,self.case.run_jobs()['succeeded'])
        self.assertNotIn('If-None-Match:', ' '.join(self.case.calls[-1]))
        rows=list(self.db.execute('SELECT observed_at,response_status FROM repository_observations ORDER BY observed_at'))
        self.assertEqual(2,len(rows));self.assertEqual(observed,rows[0]['observed_at']);self.assertEqual(200,rows[1]['response_status'])

    def test_retained_github_304_does_not_advance_observation_time(self):
        self.case.enqueue();self.case.push();self.case.run_jobs()
        observed=self.db.execute('SELECT observed_at FROM repository_observations').fetchone()[0]
        self.case.now+=timedelta(hours=8);self.case.enqueue();self.case.push(304,None)
        self.assertEqual(1,self.case.run_jobs()['succeeded'])
        rows=list(self.db.execute('SELECT observed_at,checked_at,response_status FROM repository_observations ORDER BY checked_at'))
        self.assertEqual(observed,rows[1]['observed_at']);self.assertEqual(304,rows[1]['response_status']);self.assertNotEqual(rows[0]['checked_at'],rows[1]['checked_at'])

    def test_github_low_disk_wait_is_before_transport_and_has_zero_attempt_charge(self):
        self.case.enqueue()
        with patch('agent_forge_service.resource_budget.shutil.disk_usage',return_value=Disk(1024,1024,0)):
            result=self.case.run_jobs()
        self.assertEqual(0,result['requests']);self.assertEqual(1,result['deferred']);self.assertEqual([],self.case.calls)
        self.assertEqual(0,self.db.execute('SELECT attempts FROM jobs').fetchone()[0])
        self.assertEqual('storage_budget_wait',self.db.execute('SELECT last_error FROM jobs').fetchone()[0])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM metric_observations').fetchone()[0])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM rate_limits').fetchone()[0])

    def test_npm_low_disk_wait_is_before_transport(self):
        case=downloads.DownloadsTests('runTest');case.setUp();self.addCleanup(case.doCleanups)
        with patch('agent_forge_service.resource_budget.shutil.disk_usage',return_value=Disk(1024,1024,0)):
            with self.assertRaises(CollectionError) as caught:case.client.fetch(case.request)
        self.assertFalse(caught.exception.attempted);self.assertEqual('storage_budget_wait',caught.exception.code)
        self.assertEqual([],case.calls);self.assertEqual(0,case.db.execute('SELECT count(*) FROM npm_download_observations').fetchone()[0])

    def test_state_and_wal_bytes_trigger_admission_without_modifying_facts(self):
        self.db.resource_limits=ResourceLimits(max_state_bytes=1024**2,min_free_bytes=0)
        with self.db:
            self.db.execute('INSERT INTO metadata VALUES (?,?)',('large-protected-fact','x'*1024**2))
        before=self.db.execute("SELECT value FROM metadata WHERE key='large-protected-fact'").fetchone()[0]
        status=storage_status(self.case.path.parent,self.db.resource_limits)
        self.assertFalse(status['allowed']);self.assertIn('state_budget_exhausted',status['reasons'])
        self.assertEqual(sum(status['stateFilesBytes'].values()),status['stateTotalBytes'])
        with self.assertRaises(Exception) as caught:require_collection_room(self.db)
        self.assertEqual('storage_budget_wait',caught.exception.code)
        self.assertEqual(before,self.db.execute("SELECT value FROM metadata WHERE key='large-protected-fact'").fetchone()[0])

    def test_status_for_missing_runtime_and_cli_do_not_create_files(self):
        missing=self.case.path.parent/'missing';status=storage_status(missing)
        self.assertTrue(status['allowed']);self.assertEqual(0,status['stateTotalBytes']);self.assertFalse(missing.exists())
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream),patch('agent_forge_service.resource_budget.shutil.disk_usage',return_value=Disk(1024,1024,0)):
            code=main(['storage-status','--runtime',str(missing)])
        self.assertEqual(3,code);self.assertFalse(json.loads(stream.getvalue())['allowed']);self.assertFalse(missing.exists())
        self.assertNotIn('credential_identity',stream.getvalue());self.assertNotIn('body_json',stream.getvalue())

    def test_scheduler_storage_wait_has_no_state_creation_and_recovers(self):
        case=scheduled.SchedulerTests('runTest');case.setUp();self.addCleanup(case.doCleanups);case.write()
        with patch('agent_forge_service.resource_budget.shutil.disk_usage',return_value=Disk(1024,1024,0)):
            result=case.tick()
        self.assertEqual('storage_wait',result['phase']);self.assertEqual(0,result['requestsThisTick'])
        self.assertFalse(case.settings.runtime_root.exists());self.assertEqual([],case.calls)
        result=case.tick();self.assertEqual('published',result['phase']);self.assertTrue(case.settings.state_path.exists())

    def test_scheduler_wait_preserves_pending_batch_and_old_snapshot(self):
        case=scheduled.SchedulerTests('runTest');case.setUp();self.addCleanup(case.doCleanups);case.write()
        case.options=replace(case.options,max_requests=1)
        first=case.tick();state=encode_json(case.state());before=case.settings.state_path.read_bytes()
        with patch('agent_forge_service.resource_budget.shutil.disk_usage',return_value=Disk(1024,1024,0)):
            paused=case.tick()
        self.assertEqual('storage_wait',paused['phase']);self.assertEqual(state,encode_json(case.state()))
        self.assertEqual(before,case.settings.state_path.read_bytes())

    def test_foreground_wait_event_reports_bounded_storage_reasons(self):
        case=scheduled.SchedulerTests('runTest');case.setUp();self.addCleanup(case.doCleanups);events=[];waits=[]
        class Stop:
            def is_set(self):return bool(waits)
            def wait(self,seconds):waits.append(seconds)
        with patch('agent_forge_service.resource_budget.shutil.disk_usage',return_value=Disk(1024,1024,0)):
            run_schedule(case.settings,scheduled.ZERO,case.options,stop=Stop(),clock=lambda:case.now,emit=events.append)
        event=json.loads(events[0]);self.assertEqual('storage_wait',event['phase']);self.assertIn('disk_reserve_low',event['storage']['reasons'])
        self.assertFalse(case.settings.runtime_root.exists());self.assertNotIn('credential',events[0])


if __name__=='__main__':unittest.main()
