"""Public observation projection, scoped metrics, immutable ranking and batch inputs."""
from __future__ import annotations

import errno
import contextlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from agent_forge_service.batches import plan_batch
from agent_forge_service.canonical_mapping import sync_canonical_mappings
from agent_forge_service.collector import enqueue_repository, run_repository_jobs
from agent_forge_service.errors import ServiceError
from agent_forge_service.github import GhTransport
from agent_forge_service.github_client import GitHubClient, RequestPolicy
from agent_forge_service.jobs import JobQueue
from agent_forge_service.metrics import WindowPolicy
from agent_forge_service.observations import published_star_rankings
from agent_forge_service.publishing import SnapshotStore, build_and_publish, digest_file, atomic_json
from agent_forge_service.query import Query, QueryService, _decode_cursor, _encode_cursor
from agent_forge_service.settings import Settings
from agent_forge_service.storage import encode_json, open_state, read_snapshot, timestamp
from service_tests.test_batches import POLICY
from service_tests.test_collector import BODY
from service_tests.test_publication_query import NOW, record


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = self.root / 'catalog'
        self.packages = self.catalog / 'sources/plugin/packages'; self.packages.mkdir(parents=True)
        self.settings = Settings(self.catalog, self.root / 'runtime')
        self.db = open_state(self.settings.state_path); self.addCleanup(self.db.close)
        self.now = NOW - timedelta(days=2)
        self.status, self.body = 200, BODY
        self.calls = []
        def runner(*args):
            self.calls.append(args[0])
            return (0 if self.status < 400 else 1), (f'HTTP/2.0 {self.status} status\r\nETag: "fixture"\r\n\r\n' + (json.dumps(self.body) if self.body is not None else '')).encode()
        self.client = GitHubClient(self.db, identity='private-fixture-label', transport=GhTransport(runner=runner), policy=RequestPolicy(jitter_fraction=0), clock=lambda: self.now)

    def cleanup(self):
        for attempt in range(6):
            try: self.temp.cleanup(); return
            except OSError as error:
                if (error.errno != errno.ENOTEMPTY and getattr(error, 'winerror', None) not in (5,32,145)) or attempt == 5: raise
                time.sleep(.05 * (attempt + 1))

    def write(self, name='alpha', repo=1, *, version='1.0.0', agents=('dsh',), extra_repo=None):
        value = record(name, version=version, agents=agents)
        value['links'] = {'repository': 'https://github.com/example/repo' + str(repo)}
        if extra_repo:
            value['distributions'] = [{'id': 'git', 'type': 'github-repo', 'url': 'https://github.com/example/repo' + str(extra_repo)}]
        path = self.packages / (name + '-' + version + '.json'); path.write_text(json.dumps(value), encoding='utf8')
        return path

    def mapping(self):
        return sync_canonical_mappings(self.db, self.catalog, now=NOW - timedelta(days=2))

    def observe(self, repo, stars, when, *, status=200):
        self.now = when
        self.status = status
        self.body = None if status == 304 else {**BODY, 'id': repo, 'full_name': 'example/repo' + str(repo), 'stargazers_count': stars}
        with self.db:
            job = enqueue_repository(JobQueue(self.db), repository=repo, slot=when, bootstrap=True, now=when)
        result = run_repository_jobs(self.db, self.client, owner='fixture', max_requests=1, clock=lambda: self.now, job_ids=(job,))
        self.assertEqual(1, result['succeeded'])

    def history(self, repos=((1, 100, 148),), *, hours=24, age=0):
        for repo, previous, current in sorted(repos, reverse=True):
            self.observe(repo, previous, NOW - timedelta(hours=hours + age, seconds=repo))
        for repo, previous, current in sorted(repos, reverse=True):
            self.observe(repo, current, NOW - timedelta(hours=age, seconds=repo))

    def build(self, revision='one', *, now=NOW, settings=None):
        return build_and_publish(settings or self.settings, now=now, revision=revision)

    def service(self):
        return QueryService(self.settings)

    def test_real_collector_history_projects_growth_with_evidence(self):
        self.write(); self.mapping(); self.history(); manifest = self.build()
        result = self.service().package('plugin.alpha')
        self.assertEqual(148, result['repositoryMetrics']['stars']['value'])
        growth = result['repositoryMetrics']['starGrowth']
        self.assertEqual(2, growth['valuePerHour'])
        self.assertTrue(growth['qualification']['eligible'])
        self.assertEqual('observation_estimate', growth['time_basis'])
        self.assertEqual('repository', result['observations'][0]['subjectKind'])
        self.assertNotIn('private-fixture-label', json.dumps(result))
        self.assertEqual(1, manifest['observationCoverage']['eligibleStarGrowthPackages'])
        self.assertEqual(2, len(self.service().metrics('plugin.alpha')['items']))
        self.assertTrue(manifest['features']['observations'])

    def test_one_sample_has_no_growth_and_is_not_zero(self):
        self.write(); self.mapping(); self.observe(1, 100, NOW - timedelta(minutes=1)); self.build()
        growth = self.service().package('plugin.alpha')['repositoryMetrics']['starGrowth']
        self.assertIsNone(growth['valuePerHour'])
        self.assertEqual('insufficient_samples', growth['reason'])
        self.assertFalse(growth['qualification']['eligible'])

    def test_global_sort_precedes_pagination_and_null_tail_is_stable(self):
        self.write('alpha',1); self.write('beta',2); self.write('unknown',3); self.mapping()
        self.history(((1,100,148),(2,100,172)))
        self.build(); service = self.service()
        query = Query(sort='star_growth',limit=1)
        first = service.packages(query); second = service.packages(query,cursor=first['nextCursor']); third = service.packages(query,cursor=second['nextCursor'])
        self.assertEqual(['plugin.beta','plugin.alpha','plugin.unknown'], [r['items'][0]['id'] for r in (first,second,third)])
        self.assertIsNone(third['nextCursor'])
        self.assertEqual('shared_repository_signal', first['sortScope'])

    def test_growth_sort_with_search_and_agent_filters_keeps_the_full_query_semantics(self):
        self.write('alpha',1);self.write('beta',2,agents=('claude-code',));self.mapping();self.history(((1,0,48),(2,0,72)));self.build()
        result=self.service().packages(Query(q='terminal',sort='star_growth',type='plugin'))
        self.assertEqual(['plugin.beta','plugin.alpha'],[item['id'] for item in result['items']])
        result=self.service().packages(Query(q='terminal',sort='star_growth',agent='dsh'))
        self.assertEqual(['plugin.alpha'],[item['id'] for item in result['items']])

    def test_star_total_is_not_star_growth(self):
        self.write('huge',1); self.write('fast',2); self.mapping(); self.history(((1,10000,10024),(2,0,48))); self.build()
        self.assertEqual('plugin.huge', self.service().packages(Query(sort='stars'))['items'][0]['id'])
        self.assertEqual('plugin.fast', self.service().packages(Query(sort='star_growth'))['items'][0]['id'])

    def test_same_repo_has_one_fact_history_not_one_counter_per_plugin(self):
        self.write('alpha',1); self.write('beta',1); self.mapping(); self.history(); self.build()
        conn = read_snapshot(self.settings.snapshots/'one/catalog.db')
        try:
            self.assertEqual(1, conn.execute('SELECT count(*) FROM repository_observations').fetchone()[0])
            self.assertEqual(2, conn.execute('SELECT count(*) FROM repository_metric_samples').fetchone()[0])
            self.assertEqual(2, conn.execute('SELECT count(*) FROM package_repository_links').fetchone()[0])
        finally: conn.close()
        for name in ('alpha','beta'):
            self.assertTrue(self.service().package('plugin.'+name)['repositoryMetrics']['sharedRepositorySignal'])

    def test_ambiguous_multiple_repos_are_visible_but_never_summed_or_selected(self):
        self.write(extra_repo=2); self.mapping(); self.history(((1,100,148),(2,1000,2000))); self.build()
        result = self.service().package('plugin.alpha')
        self.assertEqual('ambiguous_repositories',result['repositoryMappingStatus'])
        self.assertNotIn('repositoryMetrics', result)
        self.assertEqual(2,len(result['observations']))
        self.assertIsNone(result['repositoryId'])
        self.assertEqual((), published_star_rankings(SnapshotStore(self.settings.snapshots),now=NOW)[0])

    def test_mapping_must_match_exact_selected_canonical_hash(self):
        path = self.write(); self.mapping(); self.history()
        value=json.loads(path.read_text(encoding='utf8')); value['links']['repository']='https://github.com/example/repo2'; path.write_text(json.dumps(value),encoding='utf8')
        self.build(); result=self.service().package('plugin.alpha')
        self.assertEqual('mapping_stale', result['repositoryMappingStatus'])
        self.assertEqual([],result['observations'])
        self.assertNotIn('repositoryMetrics',result)

    def test_selected_version_does_not_inherit_another_versions_repo(self):
        self.write(version='1.0.0',repo=1); self.write(version='2.0.0',repo=2)
        (self.catalog/'sources/plugin/index.json').write_text(json.dumps({'packages':{'alpha':{'latest':'2.0.0','id':'plugin.alpha'}}}),encoding='utf8')
        self.mapping(); self.history(); self.build()
        result=self.service().package('plugin.alpha')
        self.assertEqual('unmapped',result['repositoryMappingStatus'])
        self.assertEqual([],result['observations'])
        self.assertNotIn('repositoryMetrics',result)

    def test_unknown_version_selection_does_not_pick_a_star_repo(self):
        self.write(version='1.0.0'); self.write(version='2.0.0'); self.mapping(); self.history(); self.build()
        result=self.service().package('plugin.alpha')
        self.assertEqual('selection_unknown',result['repositoryMappingStatus'])
        self.assertEqual([],result['observations'])

    def test_short_window_has_rate_but_is_not_eligible_for_24h_board(self):
        self.write(); self.mapping(); self.history(hours=1); self.build()
        growth=self.service().package('plugin.alpha')['repositoryMetrics']['starGrowth']
        self.assertEqual(48,growth['valuePerHour'])
        self.assertEqual('interval_too_short',growth['qualification']['reason'])
        self.assertEqual((),published_star_rankings(SnapshotStore(self.settings.snapshots),now=NOW)[0])

    def test_negative_star_net_growth_is_kept(self):
        self.write(); self.mapping(); self.history(((1,100,76),)); self.build()
        growth=self.service().packages(Query(sort='star_growth'))['items'][0]['repositoryMetrics']['starGrowth']
        self.assertEqual(-1,growth['valuePerHour'])
        self.assertTrue(growth['qualification']['eligible'])

    def test_stale_numeric_sample_cannot_become_fresh_growth_after_304(self):
        self.write(); self.mapping(); self.history(age=48)
        self.observe(1,None,NOW-timedelta(minutes=1),status=304); self.build()
        result=self.service().package('plugin.alpha')
        self.assertFalse(result['freshness']['stale'])
        self.assertEqual('stale',result['repositoryMetrics']['starGrowth']['qualification']['reason'])
        self.assertEqual(2,len(self.service().metrics('plugin.alpha')['items']))
        self.assertEqual((),published_star_rankings(SnapshotStore(self.settings.snapshots),now=NOW)[0])

    def test_stale_metadata_does_not_rank_as_current_stars(self):
        self.write('stale',1); self.write('fresh',2); self.mapping()
        self.observe(1,10000,NOW-timedelta(hours=48)); self.observe(2,1,NOW-timedelta(minutes=1)); self.build()
        result=self.service().packages(Query(sort='stars'))
        self.assertEqual('plugin.fresh',result['items'][0]['id'])
        self.assertEqual(10000,result['items'][1]['repositoryMetrics']['stars']['value'])
        self.assertTrue(result['items'][1]['freshness']['stale'])

    def test_future_observation_is_not_published(self):
        self.write(); self.mapping(); self.observe(1,100,NOW+timedelta(hours=1)); self.build()
        result=self.service().package('plugin.alpha')
        self.assertNotIn('repositoryMetrics',result)
        self.assertEqual([],self.service().metrics('plugin.alpha')['items'])

    def test_global_freshness_is_partial_not_falsely_all_updated(self):
        self.write(); self.write('unknown',2); self.mapping(); self.history(); self.build()
        summary=self.service().packages()['freshnessSummary']
        self.assertTrue(summary['hasUpstreamObservations']); self.assertTrue(summary['stale'])
        self.assertEqual(1,summary['coverage']['freshPackages'])
        self.assertEqual(2,summary['coverage']['packages'])

    def test_credential_view_fields_never_enter_public_projection(self):
        self.write(); self.mapping(); self.history()
        with self.db:
            row=self.db.execute('SELECT id,metadata_json FROM repository_observations ORDER BY checked_at DESC LIMIT 1').fetchone()
            metadata=json.loads(row['metadata_json']); metadata['permissions']={'admin':True}; metadata['credentialIdentity']='private-fixture-label'
            self.db.execute('UPDATE repository_observations SET metadata_json=? WHERE id=?',(encode_json(metadata),row['id']))
        self.build(); result=json.dumps(self.service().package('plugin.alpha'))
        self.assertNotIn('permissions',result); self.assertNotIn('private-fixture-label',result)
        source=json.dumps(self.service().sources())
        self.assertNotIn('private-fixture-label',source)

    def test_metric_history_cursor_remains_on_old_revision_after_new_publication(self):
        self.write(); self.mapping(); self.history(); self.build()
        service=self.service(); first=service.metrics('plugin.alpha',limit=1)
        self.observe(1,200,NOW+timedelta(hours=8)); self.build('two',now=NOW+timedelta(hours=8))
        second=service.metrics('plugin.alpha',limit=1,cursor=first['nextCursor'])
        self.assertEqual('one',second['revision'])
        self.assertEqual(148,second['items'][0]['sample']['value'])
        self.assertIsNone(second['nextCursor'])

    def test_metric_cursor_cannot_change_package_or_limit(self):
        self.write(); self.write('other'); self.mapping(); self.history(); self.build()
        service=self.service(); cursor=service.metrics('plugin.alpha',limit=1)['nextCursor']
        for name,limit in [('plugin.other',1),('plugin.alpha',2)]:
            with self.assertRaises(ServiceError) as error: service.metrics(name,limit=limit,cursor=cursor)
            self.assertEqual('cursor_mismatch',error.exception.code)

    def test_repo_pushed_time_not_manifest_version_or_canonical_updated(self):
        self.write(); self.mapping(); self.history(); self.build()
        result=self.service().packages(Query(sort='repo_pushed'))
        self.assertEqual('shared_repository_signal',result['sortScope'])
        self.assertEqual('1.0.0',result['items'][0]['canonicalLatest'])
        self.assertIsNone(result['items'][0]['observedLatest'])

    def test_published_rankings_deduplicate_repository_per_agent_type(self):
        self.write('alpha',1,agents=('dsh','claude-code')); self.write('same',1); self.write('fast',2)
        self.mapping(); self.history(((1,100,148),(2,100,172))); self.build()
        entries,revision,status=published_star_rankings(SnapshotStore(self.settings.snapshots),now=NOW)
        self.assertEqual('published_snapshot',status); self.assertEqual('one',revision)
        dsh=[entry for entry in entries if entry.agent=='dsh']
        self.assertEqual(['github-repository:2','github-repository:1'],[entry.identity for entry in dsh])
        self.assertEqual([1,2],[entry.rank for entry in dsh])
        self.assertEqual(3,len(entries))

    def test_package_top_n_is_applied_before_repository_request_deduplication(self):
        self.write('alpha',1);self.write('beta',1);self.write('gamma',2)
        self.mapping();self.history(((1,0,72),(2,0,48)));self.build()
        entries,_,_=published_star_rankings(SnapshotStore(self.settings.snapshots),now=NOW)
        self.assertEqual({'github-repository:1':1,'github-repository:2':3},{entry.identity:entry.rank for entry in entries})
        # A Top-2 package board contains two packages from repo 1, not two repositories.
        self.assertEqual({'github-repository:1'},{entry.identity for entry in entries if entry.rank<=2})

    def test_published_rankings_requalify_age_at_plan_time(self):
        self.write(); self.mapping(); self.history(); self.build()
        entries,_,status=published_star_rankings(SnapshotStore(self.settings.snapshots),now=NOW+timedelta(hours=27))
        self.assertEqual((),entries)
        self.assertEqual('published_snapshot_no_eligible_samples',status)

    def test_batch_cohort_uses_verified_published_growth_and_freezes_daily(self):
        self.write(); self.mapping(); self.history(); self.build()
        policy=replace(POLICY,policy_id='published-cohort',refresh=replace(POLICY.refresh,policy_id='published-cohort'))
        store=SnapshotStore(self.settings.snapshots)
        batch=plan_batch(self.db,policy,now=NOW,ranking_store=store)
        self.assertEqual('published_snapshot',batch['summary']['rankingStatus'])
        self.assertEqual('one',batch['summary']['rankingRevision'])
        self.observe(1,200,NOW+timedelta(hours=8)); self.build('two',now=NOW+timedelta(hours=8))
        later=plan_batch(self.db,policy,now=NOW+timedelta(hours=8),ranking_store=store)
        self.assertEqual('one',later['summary']['rankingRevision'])
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM refresh_cohorts').fetchone()[0])

    def test_unpublished_or_corrupt_rankings_do_not_silently_use_live_stars(self):
        self.write(); self.mapping(); self.history()
        store=SnapshotStore(self.settings.snapshots)
        self.assertIsNone(published_star_rankings(store,now=NOW))
        self.build()
        (self.settings.snapshots/'one/catalog.db').write_bytes(b'corrupted')
        with self.assertRaises(ServiceError): published_star_rankings(SnapshotStore(self.settings.snapshots),now=NOW)

    def test_nearest_baseline_uses_actual_time_not_planned_slot(self):
        self.write(); self.mapping()
        self.observe(1,0,NOW-timedelta(hours=28)); self.observe(1,10,NOW-timedelta(hours=24)); self.observe(1,20,NOW-timedelta(hours=17)); self.observe(1,58,NOW-timedelta(minutes=1)); self.build()
        growth=self.service().package('plugin.alpha')['repositoryMetrics']['starGrowth']
        self.assertAlmostEqual(48/(24-1/60),growth['valuePerHour'])
        self.assertEqual(timestamp(NOW-timedelta(hours=24)),growth['window_start'])

    def test_malformed_scope_causes_publication_failure_and_keeps_old_revision(self):
        self.write(); self.mapping(); self.history(); self.build()
        with self.db:
            row=self.db.execute('SELECT id,sample_json FROM metric_observations LIMIT 1').fetchone()
            sample=json.loads(row['sample_json']); sample['subject_id']='999'
            self.db.execute('UPDATE metric_observations SET sample_json=? WHERE id=?',(encode_json(sample),row['id']))
        with self.assertRaises(ValueError):self.build('two')
        self.assertEqual('one',SnapshotStore(self.settings.snapshots).current()['revision'])

    def test_precomputed_star_sort_uses_full_dataset_index(self):
        self.write(); self.mapping(); self.history(); self.build()
        statements=[]
        def traced(path,timeout):
            connection=read_snapshot(path,timeout);connection.set_trace_callback(statements.append);return connection
        with patch('agent_forge_service.query.read_snapshot',side_effect=traced):
            self.service().packages(Query(sort='star_growth'))
        statement=next(statement for statement in statements if statement.startswith('SELECT p.*'))
        connection=read_snapshot(self.settings.snapshots/'one/catalog.db')
        try:
            plan=[row[3] for row in connection.execute("EXPLAIN QUERY PLAN "+statement)]
            self.assertTrue(any('package_star_growth_rank' in entry for entry in plan),plan)
            self.assertFalse(any('TEMP B-TREE' in entry for entry in plan),plan)
        finally:connection.close()

    def test_published_rankings_no_longer_current_are_counted_not_silently_discarded(self):
        self.write();self.mapping();self.history();self.build()
        with self.db:self.db.execute("UPDATE canonical_repository_links SET active=0")
        policy=replace(POLICY,policy_id='retired-published-cohort',refresh=replace(POLICY.refresh,policy_id='retired-published-cohort'))
        result=plan_batch(self.db,policy,now=NOW,ranking_store=SnapshotStore(self.settings.snapshots))
        self.assertEqual(1,result['summary']['ignoredCohortEntries'])
        self.assertEqual(0,result['summary']['effectiveRankingEntries'])
        self.assertEqual('published_snapshot',result['summary']['rankingStatus'])


    def test_star_count_cursor_rejects_noninteger_or_nonfinite_numeric_keys(self):
        self.write('alpha',1);self.write('beta',2);self.mapping();self.history(((1,0,24),(2,0,48)));self.build()
        service=self.service();query=Query(sort='stars',limit=1);first=service.packages(query)
        cursor=_decode_cursor(first['nextCursor'])
        for bad in (True,1.5,-1,'24'):
            forged=_encode_cursor(cursor['r'],cursor['q'],[0,bad,cursor['last'][2]])
            with self.assertRaises(ServiceError) as error:service.packages(query,cursor=forged)
            self.assertEqual('invalid_cursor',error.exception.code)

    def test_freshness_summary_expires_without_new_publication(self):
        self.write();self.mapping();self.history();self.build()
        with patch('agent_forge_service.query.utc_now',return_value=NOW+timedelta(hours=27)):
            result=self.service().package('plugin.alpha')
        self.assertTrue(result['freshnessSummary']['stale'])
        self.assertEqual('snapshot_stale',result['freshnessSummary']['reason'])
        self.assertGreater(result['snapshotAgeSeconds'],26*3600)
        self.assertEqual(timestamp(NOW),result['freshnessSummary']['asOf'])

    def test_explicit_discontinuity_blocks_cross_segment_baseline(self):
        self.write();self.mapping()
        self.observe(1,0,NOW-timedelta(hours=24));self.observe(1,100,NOW-timedelta(hours=12));self.observe(1,124,NOW-timedelta(seconds=1))
        with self.db:
            row=self.db.execute('SELECT id,sample_json FROM metric_observations ORDER BY observed_at LIMIT 1 OFFSET 1').fetchone()
            sample=json.loads(row['sample_json']);sample['discontinuity']='explicit_reset'
            self.db.execute('UPDATE metric_observations SET sample_json=? WHERE id=?',(encode_json(sample),row['id']))
        self.build();growth=self.service().package('plugin.alpha')['repositoryMetrics']['starGrowth']
        self.assertEqual(timestamp(NOW-timedelta(hours=12)),growth['window_start'])
        self.assertFalse(growth['qualification']['eligible'])

    def test_cli_metrics_queries_only_published_history(self):
        from agent_forge_service.worker import main
        self.write();self.mapping();self.history();self.build()
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            result=main(['metrics','--runtime',str(self.settings.runtime_root),'plugin.alpha','--limit','1'])
        self.assertEqual(0,result)
        payload=json.loads(output.getvalue());self.assertEqual(1,len(payload['items']));self.assertIsNotNone(payload['nextCursor'])

    def test_unscoped_download_or_user_sort_not_disguised_as_stars(self):
        self.write(); self.build()
        for sort in ('downloads','download_growth','most_used'):
            with self.assertRaises(ServiceError) as error:self.service().packages(Query(sort=sort))
            self.assertEqual('sort_unavailable' if sort == 'most_used' else 'invalid_query',error.exception.code)


if __name__=='__main__':unittest.main()
