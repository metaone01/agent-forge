"""Discovery projection through real private collectors, public snapshots and contracts."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from agent_forge_service.bundles import export_bundle, import_bundle, activate_snapshot
from agent_forge_service.discovery import enqueue_window, run_discovery_jobs
from agent_forge_service.errors import ServiceError
from agent_forge_service.github import GhTransport
from agent_forge_service.github_client import GitHubClient, RequestPolicy
from agent_forge_service.http_contract import HttpReadService
from agent_forge_service.manifests import enqueue_manifest, run_manifest_jobs
from agent_forge_service.public_git import AnonymousGitClient, AnonymousGitTransport
from agent_forge_service.publishing import build_and_publish, atomic_json, digest_file, SnapshotStore
from agent_forge_service.query import Query, QueryService, _decode_cursor, _encode_cursor
from agent_forge_service.settings import Settings
from agent_forge_service.storage import open_state, encode_json, timestamp
from service_tests.temporary import cleanup_temporary
from service_tests.test_publication_query import NOW, record
from service_tests.test_manifests import Opener, blob, tree, entry

COMMIT, ROOT, DIRECTORY = 'a' * 40, 'b' * 40, 'c' * 40
SECRET = 'private-credential-label-sentinel'


def repository(identity, **fields):
    return {'id': identity, 'full_name': f'owner/repo-{identity}', 'private': False,
            'visibility': 'public', 'topics': ['mcp-server'], **fields}


class DiscoveryProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='forge-public-discovery-')
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(cleanup_temporary, self.temp, self.root)
        self.catalog = self.root / 'catalog'
        self.canonical = self.catalog / 'sources/plugin/packages'
        self.canonical.mkdir(parents=True)
        (self.canonical / 'canonical.json').write_text(json.dumps(record()), encoding='utf8')
        self.settings = Settings(self.catalog, self.root / 'runtime')
        self.db = open_state(self.settings.state_path)
        self.addCleanup(self.db.close)
        self.now = NOW
        self.calls = []
        self.body = None
        def runner(args, *unused):
            self.calls.append(args)
            return 0, ('HTTP/2.0 200 OK\r\nETag: "discovery"\r\n\r\n' + json.dumps(self.body)).encode()
        self.client = GitHubClient(self.db, identity=SECRET, transport=GhTransport(runner=runner),
            policy=RequestPolicy(jitter_fraction=0), clock=lambda: self.now)
        self.generation = 0

    def collect(self, items, topic='mcp-server', total=None, incomplete=False):
        self.generation += 1
        self.now += timedelta(seconds=7)
        with self.db:
            run = enqueue_window(self.db, topic=topic, field='created',
                start=timestamp(self.now.replace(microsecond=0) - timedelta(hours=8)),
                end=timestamp(self.now.replace(microsecond=0)), generation=f'run-{self.generation}', now=self.now)
        self.body = {'total_count': len(items) if total is None else total,
            'incomplete_results': incomplete, 'items': items}
        result = run_discovery_jobs(self.db, self.client, owner='public-test-worker', max_requests=1, clock=lambda: self.now)
        self.assertEqual(1, result['succeeded'])
        self.now += timedelta(seconds=7)
        return run

    def manifest(self, identity=1, fields=None, agent='claude-code', kind='claude_plugin', path='.claude-plugin/plugin.json'):
        fields = fields if fields is not None else {'name': 'hello', 'description': 'A public declaration 数据库', 'version': '1.2.3', 'license': 'MIT'}
        raw = json.dumps(fields).encode() if kind == 'claude_plugin' else ('---\n' + '\n'.join(k + ': ' + json.dumps(v) for k, v in fields.items()) + '\n---\nignored private body instruction').encode()
        obj = blob(raw)
        segments = path.split('/')
        root = tree(ROOT, [entry(segments[0], DIRECTORY, '040000', 'tree')])
        directory = tree(DIRECTORY, [entry(segments[1], obj['sha'], size=len(raw))])
        def respond(request):
            endpoint = request.full_url.removeprefix('https://api.github.com/')
            if endpoint == f'repositories/{identity}':
                return repository(identity, default_branch='main')
            if endpoint.endswith('/commits/main'):
                return {'sha': COMMIT, 'commit': {'tree': {'sha': ROOT}}}
            if endpoint.endswith('/git/trees/' + ROOT):
                return root
            if endpoint.endswith('/git/trees/' + DIRECTORY):
                return directory
            if endpoint.endswith('/git/blobs/' + obj['sha']):
                return obj
            self.fail('unexpected anonymous GET: ' + endpoint)
        opener = Opener(respond)
        client = AnonymousGitClient(self.db, transport=AnonymousGitTransport(opener=opener),
            policy=RequestPolicy(jitter_fraction=0), clock=lambda: self.now)
        with self.db:
            enqueue_manifest(self.db, github_id=identity, path=path, kind=kind, agent=agent, slot=timestamp(self.now), now=self.now)
        for _ in range(8):
            self.now += timedelta(seconds=2)
            run_manifest_jobs(self.db, client, owner='declaration-worker', max_requests=1, clock=lambda: self.now)
            row = self.db.execute('SELECT * FROM manifest_candidates WHERE github_id=?', (identity,)).fetchone()
            if row:
                self.now += timedelta(seconds=2)
                return row
        self.fail('manifest pipeline did not settle')

    def publish(self, revision='one', now=None):
        return build_and_publish(self.settings, now=now or self.now, revision=revision)

    def query(self, **parameters):
        return QueryService(self.settings).packages(Query(scope='discovery', **parameters))

    def code(self, expected, callable):
        with self.assertRaises(ServiceError) as raised:
            callable()
        self.assertEqual(expected, raised.exception.code)

    def test_repository_candidates_are_evidence_not_canonical_or_compatibility(self):
        self.collect([repository(1, stargazers_count=23), repository(2, topics=['claude-code', 'plugin'])])
        manifest = self.publish()
        self.assertEqual({'packages': 1, 'versions': 1}, manifest['counts'])
        self.assertEqual(2, manifest['discoveryCoverage']['repositories'])
        result = self.query()
        self.assertEqual('first_seen', result['sort'])
        self.assertEqual(2, len(result['items']))
        candidate = result['items'][0]
        self.assertEqual('repository', candidate['kind'])
        self.assertIsNone(candidate['type'])
        self.assertEqual([], candidate['agents'])
        self.assertEqual('unreviewed', candidate['reviewStatus'])
        self.assertEqual(23, candidate['repository']['stars'])
        self.assertEqual('github_search', candidate['sourceEvidence'][0]['kind'])
        self.assertIn('evidenceHash', candidate['sourceEvidence'][0])
        self.assertFalse(result['freshnessSummary']['complete'])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual([], self.query(agent='claude-code')['items'])
        self.assertEqual([], self.query(type='plugin')['items'])
        self.assertEqual([], self.query(facets={'capabilities': ['gui.observe']})['items'])
        self.assertEqual(1, len(QueryService(self.settings).packages()['items']))

    def test_manifest_declarations_keep_missing_fields_and_explicit_profile(self):
        self.collect([repository(1)])
        row = self.manifest(fields={'name': 'hello'})
        self.publish()
        result = self.query(type='plugin', agent='claude-code')
        self.assertEqual(1, len(result['items']))
        item = result['items'][0]
        self.assertEqual('manifest:' + row['id'], item['id'])
        self.assertEqual('needs_metadata', item['reviewStatus'])
        self.assertIsNone(item['declaredMetadata']['version'])
        self.assertIsNone(item['declaredMetadata']['license'])
        self.assertIsNone(item['declaredMetadata']['description'])
        self.assertNotIn('canonicalRecord', item)
        self.assertEqual('explicit-profile-not-compatibility-verification', item['manifestEvidence']['association'])
        self.assertTrue(item['issues'])
        self.assertNotIn('schema_valid', [event['state'] for event in item['lifecycle']])
        self.assertNotIn(SECRET, json.dumps(item))
        self.assertEqual(item, QueryService(self.settings).discovery(item['id'])['item'])

    def test_complete_manifest_is_review_pending_not_adopted(self):
        self.collect([repository(1)])
        self.manifest()
        manifest = self.publish()
        item = self.query(type='plugin')['items'][0]
        self.assertEqual('review_pending', item['reviewStatus'])
        self.assertIn('schema_valid', [event['state'] for event in item['lifecycle']])
        self.assertEqual({'packages': 1, 'versions': 1}, manifest['counts'])
        self.assertEqual(COMMIT, item['manifestEvidence']['commitSha'])
        self.assertEqual('1.2.3', item['declaredMetadata']['version'])
        self.code('not_found', lambda: QueryService(self.settings).package(item['id']))

    def test_agent_skill_body_is_never_published(self):
        self.collect([repository(1)])
        self.manifest(kind='agent_skill', agent='codex', path='hello/SKILL.md')
        self.publish()
        item = self.query(type='skill', agent='codex')['items'][0]
        self.assertEqual('review_pending', item['reviewStatus'])
        self.assertNotIn('ignored private body instruction', json.dumps(item))

    def test_as_of_reconstructs_before_later_rename_and_star_observation(self):
        self.collect([repository(1, stargazers_count=2)])
        before = self.now
        self.collect([repository(1, full_name='owner/renamed', stargazers_count=999), repository(2)])
        self.assertEqual('owner/renamed', self.db.execute('SELECT full_name FROM discovery_candidates WHERE github_id=1').fetchone()[0])
        self.publish(now=before)
        result = self.query()
        self.assertEqual(1, len(result['items']))
        self.assertEqual('owner/repo-1', result['items'][0]['name'])
        self.assertEqual(2, result['items'][0]['repository']['stars'])
        self.assertLessEqual(result['items'][0]['lastSeenAt'], timestamp(before))
        self.assertEqual(timestamp(before), result['discoveryCoverage']['asOf'])

    def test_future_manifest_and_future_lifecycle_state_do_not_leak(self):
        self.collect([repository(1)])
        before = self.now
        row = self.manifest()
        self.publish(now=before)
        self.assertEqual([], self.query(type='plugin')['items'])
        future = self.now + timedelta(days=1)
        with self.db:
            self.db.execute('INSERT INTO manifest_candidate_events VALUES (?,?,?)', (row['id'], 'identity_conflict', timestamp(future)))
            self.db.execute("UPDATE manifest_candidates SET state='identity_conflict' WHERE id=?", (row['id'],))
        self.publish(revision='two')
        self.assertEqual('review_pending', self.query(type='plugin')['items'][0]['reviewStatus'])

    def test_sources_are_deduplicated_and_do_not_publish_credentials_or_run_ids(self):
        first = self.collect([repository(1)])
        second = self.collect([repository(1)])
        self.collect([repository(1)], topic='claude-code')
        self.publish()
        service = QueryService(self.settings)
        sources = [source for source in service.sources()['items'] if source['kind'] == 'github_search']
        self.assertEqual(2, len(sources))
        self.assertTrue(all(source['status'] == 'complete' for source in sources))
        item = self.query()['items'][0]
        self.assertEqual(2, item['sourceCount'])
        text = json.dumps({'sources': sources, 'item': item})
        for forbidden in (SECRET, first, second, 'credential_identity', 'config_json', 'job_id', 'policy_json'):
            self.assertNotIn(forbidden, text)
        with patch('agent_forge_service.query.utc_now', return_value=self.now + timedelta(days=2)):
            self.assertTrue(all(source['stale'] for source in service.sources()['items'] if source['kind'] == 'github_search'))
            self.assertTrue(self.query()['freshnessSummary']['stale'])

    def test_pending_and_partial_are_not_success_or_global_coverage(self):
        self.collect([repository(1)], total=101)
        self.now += timedelta(seconds=7)
        with self.db:
            enqueue_window(self.db, topic='agent-skill', field='created', start=timestamp(self.now - timedelta(hours=8)), end=timestamp(self.now), generation='pending', now=self.now)
        self.publish()
        sources = [row for row in QueryService(self.settings).sources()['items'] if row['kind'] == 'github_search']
        by_topic = {row['topic']: row for row in sources}
        self.assertEqual('partial', by_topic['mcp-server']['status'])
        self.assertIsNone(by_topic['mcp-server']['lastSuccessAt'])
        self.assertEqual('pending', by_topic['agent-skill']['status'])
        self.assertTrue(all(row['partial'] for row in sources))
        self.assertTrue(all(row['coverage'] == 'query_window_only_not_global' for row in sources))

    def test_exact_count_empty_search_still_reports_success(self):
        self.collect([])
        manifest = self.publish()
        self.assertEqual(0, manifest['discoveryCoverage']['repositories'])
        source = [row for row in QueryService(self.settings).sources()['items'] if row['kind'] == 'github_search'][0]
        self.assertEqual('complete', source['status'])
        self.assertIsNotNone(source['lastSuccessAt'])
        self.assertEqual([], self.query()['items'])

    def test_cursor_is_bound_to_scope_sort_full_filters_and_revision(self):
        self.collect([repository(i) for i in range(1, 5)])
        self.publish()
        service = QueryService(self.settings)
        first = service.packages(Query(scope='discovery', limit=2))
        cursor = first['nextCursor']
        self.assertTrue(cursor)
        self.collect([repository(5)])
        self.publish(revision='two')
        second = service.packages(Query(scope='discovery', limit=2), cursor=cursor)
        self.assertEqual('one', second['revision'])
        self.assertEqual(4, len({item['id'] for page in (first, second) for item in page['items']}))
        for query in (Query(limit=2), Query(scope='discovery', limit=3), Query(scope='discovery', sort='name', limit=2), Query(scope='discovery', q='repo', limit=2), Query(scope='discovery', agent='dsh', limit=2)):
            self.code('cursor_mismatch', lambda q=query: service.packages(q, cursor=cursor))
        self.code('cursor_mismatch', lambda: service.packages(Query(scope='discovery', limit=2), cursor=cursor, revision='two'))

    def test_cursor_payloads_are_strict_and_retired_revision_is_explicit(self):
        self.collect([repository(i) for i in range(1, 4)])
        self.publish()
        result = self.query(limit=1)
        decoded = _decode_cursor(result['nextCursor'])
        for last in ([True, 'github-repository:1'], ['2026-99-99', 'github-repository:1'], [timestamp(self.now), 'wrong'], [None], [None, 'github-repository:1']):
            cursor = _encode_cursor(decoded['r'], decoded['q'], last)
            self.code('invalid_cursor', lambda c=cursor: QueryService(self.settings).packages(Query(scope='discovery', limit=1), cursor=c))
        moved = self.settings.snapshots / 'retired-one'
        (self.settings.snapshots / 'one').rename(moved)
        self.code('cursor_expired', lambda: QueryService(self.settings).packages(Query(scope='discovery', limit=1), cursor=result['nextCursor']))

    def test_supported_search_paths_and_no_implicit_metric_sort(self):
        self.collect([repository(1, topics=['terminal']), repository(2)])
        self.manifest(fields={'name': 'hello', 'description': '数据库工具', 'version': '1', 'license': 'MIT'})
        self.publish()
        self.assertEqual(1, len(self.query(q='terminal')['items']))
        self.assertEqual(1, len(self.query(q='数据库')['items']))
        self.assertEqual([], self.query(q='数据')['items'])
        self.assertEqual(1, len(self.query(q='hello', sort='relevance')['items']))
        self.assertEqual([], self.query(q='100%')['items'])
        for sort in ('stars', 'downloads', 'download_growth', 'canonical_updated', 'repo_pushed'):
            self.code('sort_unavailable', lambda s=sort: self.query(sort=s))
        self.code('invalid_query', lambda: self.query(sort='relevance'))

    def test_http_routes_etag_and_no_private_state_writes(self):
        self.collect([repository(1)])
        self.publish()
        before = self.settings.state_path.read_bytes()
        reader = HttpReadService(self.settings)
        with patch('subprocess.run', side_effect=AssertionError('read API cannot fetch')):
            response = reader.handle('GET', '/api/v1/packages', [('scope', 'discovery')])
            self.assertEqual(200, response.status, response.body)
            item = json.loads(response.body)['items'][0]
            detail = reader.handle('GET', '/api/v1/discoveries/' + item['id'])
            self.assertEqual(200, detail.status, detail.body)
            self.assertEqual(item, json.loads(detail.body)['item'])
            conditional = reader.handle('GET', '/api/v1/packages', [('scope', 'discovery')], if_none_match=response.headers['ETag'])
            self.assertEqual(304, conditional.status)
            missing = reader.handle('GET', '/api/v1/discoveries/github-repository:999')
            self.assertEqual(404, missing.status)
            self.assertEqual('discovery_not_found', json.loads(missing.body)['error']['code'])
            recommendations = reader.handle('GET', '/api/v1/packages/plugin.alpha/recommendations')
            self.assertEqual(200, recommendations.status)
            self.assertEqual([], json.loads(recommendations.body)["items"])
            self.assertEqual("unknown", json.loads(recommendations.body)["availability"])
        self.assertEqual(before, self.settings.state_path.read_bytes())

    def test_name_and_relevance_keysets_cover_each_candidate_once(self):
        self.collect([repository(i, topics=['terminal']) for i in range(1, 7)])
        self.publish()
        service = QueryService(self.settings)
        for sort in ('name', 'relevance', 'first_seen'):
            query = Query(scope='discovery', sort=sort, q='terminal', limit=2)
            cursor, identities = None, []
            for _ in range(6):
                page = service.packages(query, cursor=cursor)
                identities.extend(item['id'] for item in page['items'])
                cursor = page['nextCursor']
                if not cursor:
                    break
            self.assertEqual(6, len(identities))
            self.assertEqual(6, len(set(identities)))
            self.assertIsNone(cursor)

    def test_future_run_completion_does_not_claim_old_success(self):
        self.collect([repository(i) for i in range(1, 101)], total=101)
        before = self.now
        self.now += timedelta(seconds=7)
        self.body = {'total_count': 101, 'incomplete_results': False, 'items': [repository(101)]}
        result = run_discovery_jobs(self.db, self.client, owner='public-test-worker', max_requests=1, clock=lambda: self.now)
        self.assertEqual(1, result['succeeded'])
        self.publish(now=before)
        sources = [item for item in QueryService(self.settings).sources()['items'] if item['kind'] == 'github_search']
        self.assertEqual('partial', sources[0]['status'])
        self.assertIsNone(sources[0]['lastSuccessAt'])
        self.assertEqual(100, QueryService(self.settings).store.current()['discoveryCoverage']['repositories'])
        self.assertEqual(100, len(self.query(limit=100)['items']))

    def test_fresh_candidate_does_not_mask_stale_long_tail(self):
        self.collect([repository(1)])
        self.now += timedelta(days=2)
        self.collect([repository(2)])
        self.publish()
        with patch('agent_forge_service.query.utc_now', return_value=self.now):
            result = self.query()
            self.assertTrue(result['freshnessSummary']['stale'])
            by_id = {item['id']: item for item in result['items']}
            self.assertTrue(by_id['github-repository:1']['freshness']['stale'])
            self.assertFalse(by_id['github-repository:2']['freshness']['stale'])

    def test_source_evidence_is_bounded_without_hiding_total_source_count(self):
        from agent_forge_service.discovery_projection import MAX_SOURCES
        for i in range(MAX_SOURCES + 2):
            self.collect([repository(1)], topic=f'topic-{i}')
        self.publish()
        item = self.query()['items'][0]
        self.assertEqual(MAX_SOURCES, len(item['sourceEvidence']))
        self.assertEqual(MAX_SOURCES + 2, item['sourceCount'])
        self.assertTrue(item['evidenceTruncated'])

    def test_exact_adopted_declaration_leaves_discovery_without_changing_facts(self):
        self.collect([repository(1)])
        row = self.manifest()
        self.publish()
        item = self.query(type='plugin')['items'][0]
        adopted = json.loads(row['record_json'])
        adopted['updatedAt'] = timestamp(self.now - timedelta(hours=1))
        (self.canonical / 'adopted.json').write_text(json.dumps(adopted), encoding='utf8')
        self.publish(revision='two')
        self.assertEqual([], self.query(type='plugin')['items'])
        self.assertEqual(2, QueryService(self.settings).store.current()['counts']['packages'])
        self.assertEqual(1, len(QueryService(self.settings).packages(Query(scope='discovery', type='plugin'), revision='one')['items']))
        with self.assertRaises(ServiceError) as raised:
            QueryService(self.settings).discovery(item['id'])
        self.assertEqual('discovery_not_found', raised.exception.code)
        self.assertEqual(1, self.db.execute('SELECT count(*) FROM manifest_candidates').fetchone()[0])

    def test_adopted_repository_mapping_leaves_discovery(self):
        from agent_forge_service.canonical_mapping import sync_canonical_mappings
        from agent_forge_service.collector import enqueue_repository, run_repository_jobs
        from agent_forge_service.jobs import JobQueue
        canonical = record()
        canonical['links'] = {'repository': 'https://github.com/owner/repo-1'}
        (self.canonical / 'canonical.json').write_text(json.dumps(canonical), encoding='utf8')
        with self.db:
            sync_canonical_mappings(self.db, self.catalog, now=NOW - timedelta(days=1))
        self.collect([repository(1), repository(2)])
        self.now += timedelta(seconds=7)
        self.body = repository(1, stargazers_count=10)
        with self.db:
            job = enqueue_repository(JobQueue(self.db), repository=1, slot=timestamp(self.now), bootstrap=True, now=self.now)
        result = run_repository_jobs(self.db, self.client, owner='repository-worker', max_requests=1, clock=lambda: self.now, job_ids=(job,))
        self.assertEqual(1, result['succeeded'])
        self.publish()
        catalog = QueryService(self.settings).packages()['items'][0]
        self.assertEqual(1, catalog['repositoryId'])
        self.assertEqual(['github-repository:2'], [item['id'] for item in self.query()['items']])
        self.assertEqual(2, self.db.execute('SELECT count(*) FROM discovery_candidates').fetchone()[0])

    def test_broken_manifest_evidence_and_review_claims_are_rejected(self):
        from agent_forge_service.discovery_projection import validate_item
        self.collect([repository(1)])
        self.manifest()
        manifest = self.publish()
        original = self.query(type='plugin')['items'][0]
        original.pop('freshness')
        for edit in (lambda item: item['manifestEvidence'].update(commitSha='bad'),
                     lambda item: item.update(reviewStatus='recommended'),
                     lambda item: item.update(agents=['other-agent']),
                     lambda item: item['issues'].append({'code': 'operator_private', 'secret': SECRET}),
                     lambda item: item.update(lifecycle=[])):
            item = json.loads(json.dumps(original)); edit(item)
            with self.assertRaises(ValueError):
                validate_item(item, manifest['generatedAt'])

    def test_invalid_long_declared_name_is_reviewable_and_bounded(self):
        self.collect([repository(1)])
        self.manifest(fields={'name': 'x' * 5000, 'description': 'bad\x00name', 'version': '1', 'license': 'MIT'})
        self.publish()
        item = self.query(type='plugin')['items'][0]
        self.assertEqual(4096, len(item['name']))
        self.assertEqual(5000, len(item['declaredMetadata']['name']))
        self.assertEqual('bad name', item['summary'])
        self.assertEqual('needs_metadata', item['reviewStatus'])
        self.assertTrue(item['issues'])

    def test_malformed_candidate_shapes_fail_with_validation_error(self):
        from agent_forge_service.discovery_projection import validate_item
        self.collect([repository(1)])
        manifest = self.publish()
        original = self.query()['items'][0]
        original.pop('freshness')
        for edit in (lambda item: item.update(kind={}), lambda item: item.update(repository=[]),
                     lambda item: item.update(sourceEvidence=[{}]), lambda item: item.update(sourceCount=True),
                     lambda item: item.update(issues=None)):
            item = json.loads(json.dumps(original)); edit(item)
            with self.assertRaises(ValueError):
                validate_item(item, manifest['generatedAt'])

    def test_corrupt_private_search_evidence_cannot_publish(self):
        self.collect([repository(1)])
        with self.db:
            self.db.execute("UPDATE collection_evidence SET body_json='{}' WHERE representation LIKE '%search'")
        with self.assertRaisesRegex(ValueError, 'evidence hash'):
            self.publish()
        self.assertFalse((self.settings.snapshots / 'current.json').exists())

    def test_scoped_facets_use_unadopted_declarations_without_topic_inference(self):
        self.collect([repository(1,topics=['codex']), repository(2)])
        self.manifest(identity=1)
        self.publish()
        service=QueryService(self.settings)
        canonical=service.facets()
        discovered=service.facets(scope='discovery')
        self.assertEqual('catalog',canonical['scope'])
        self.assertEqual(['dsh'],canonical['agents'])
        self.assertEqual('discovery',discovered['scope'])
        self.assertEqual(['claude-code'],discovered['agents'])
        self.assertEqual([{'value':'plugin','count':1}],discovered['types'])
        self.assertEqual({},discovered['facets'])
        self.assertEqual({},discovered['customFacets'])
        self.assertEqual([],discovered['subtypes'])
        self.code('scope_unavailable',lambda:service.facets(scope='unknown'))

    def test_scoped_facet_http_cache_and_old_revision_remain_independent(self):
        self.collect([repository(1)])
        self.publish()
        reader=HttpReadService(self.settings)
        canonical=reader.handle('GET','/api/v1/facets',[('revision','one')])
        discovered=reader.handle('GET','/api/v1/facets',[('revision','one'),('scope','discovery')])
        self.assertEqual(200,discovered.status)
        self.assertNotEqual(canonical.headers['ETag'],discovered.headers['ETag'])
        self.assertEqual([],json.loads(discovered.body)['agents'])
        self.manifest();self.publish(revision='two')
        old=QueryService(self.settings).facets(scope='discovery',revision='one')
        new=QueryService(self.settings).facets(scope='discovery')
        self.assertEqual([],old['agents'])
        self.assertEqual(['claude-code'],new['agents'])
        response=reader.handle('GET','/api/v1/facets',[('scope','unknown')])
        self.assertEqual(400,response.status)
        self.assertEqual('scope_unavailable',json.loads(response.body)['error']['code'])

    def bundle(self):
        self.publish()
        directory = self.root / 'bundle'
        receipt = export_bundle(self.settings, directory)
        return directory, receipt

    def resign_bundle(self, directory):
        descriptor = json.loads((directory / 'bundle.json').read_text())
        manifest = json.loads((directory / 'manifest.json').read_text())
        database = directory / 'catalog.db'
        descriptor['database'].update(sha256=digest_file(database), bytes=database.stat().st_size)
        manifest['database'] = dict(descriptor['database'])
        atomic_json(directory / 'manifest.json', manifest)
        descriptor['manifest'].update(sha256=digest_file(directory / 'manifest.json'), bytes=(directory / 'manifest.json').stat().st_size)
        atomic_json(directory / 'bundle.json', descriptor)
        return digest_file(directory / 'bundle.json')

    def test_public_transfer_preserves_discovery_without_private_state(self):
        self.collect([repository(1), repository(2)])
        self.manifest()
        directory, receipt = self.bundle()
        target = Settings(self.catalog, self.root / 'receiver')
        imported = import_bundle(target, directory, expected_bundle_sha256=receipt['bundleSha256'])
        activated = activate_snapshot(target, 'one', expected_manifest_sha256=digest_file(target.snapshots / 'one/manifest.json'), expected_pointer_sha256='absent')
        self.assertTrue(imported['installed'])
        self.assertTrue(activated['changed'])
        with patch('agent_forge_service.query.utc_now', return_value=self.now):
            self.assertEqual(self.query(), QueryService(target).packages(Query(scope='discovery')))
            self.assertEqual(QueryService(self.settings).sources(), QueryService(target).sources())
        self.assertFalse(target.state_path.exists())
        self.assertNotIn(SECRET.encode(), (directory / 'catalog.db').read_bytes())

    def tamper(self, edit):
        self.collect([repository(1)])
        directory, receipt = self.bundle()
        db = sqlite3.connect(directory / 'catalog.db')
        item = json.loads(db.execute('SELECT data_json FROM discoveries').fetchone()[0])
        edit(item)
        db.execute('UPDATE discoveries SET data_json=?', (encode_json(item),))
        db.commit(); db.close()
        digest = self.resign_bundle(directory)
        target = Settings(self.catalog, self.root / 'rejected')
        with self.assertRaises((ValueError, ServiceError)):
            import_bundle(target, directory, expected_bundle_sha256=digest)
        self.assertFalse((target.snapshots / 'current.json').exists())
        self.assertFalse(target.state_path.exists())

    def test_rehashed_private_field_is_rejected(self):
        self.tamper(lambda item: item.update(credential_identity=SECRET))

    def test_rehashed_fake_profile_is_rejected(self):
        self.tamper(lambda item: item.update(type='plugin', agents=['claude-code']))

    def test_rehashed_future_observation_is_rejected(self):
        self.tamper(lambda item: item.update(lastSeenAt=timestamp(self.now + timedelta(days=1))))

    def test_rehashed_search_url_or_hash_is_rejected(self):
        self.tamper(lambda item: item['sourceEvidence'][0].update(url='https://private.test/token'))

    def test_cli_scope_and_revision_work_on_public_only_receiver(self):
        self.collect([repository(1)])
        directory, receipt = self.bundle()
        target = Settings(self.catalog, self.root / 'receiver')
        import_bundle(target, directory, expected_bundle_sha256=receipt['bundleSha256'])
        activate_snapshot(target, 'one', expected_manifest_sha256=digest_file(target.snapshots / 'one/manifest.json'), expected_pointer_sha256='absent')
        before = digest_file(self.settings.state_path)
        for command, arguments in (('query', ['--scope','discovery','--revision','one']), ('detail', ['--scope','discovery','--revision','one','github-repository:1'])):
            result = subprocess.run([sys.executable,'-m','agent_forge_service.worker',command,'--runtime',str(target.runtime_root),*arguments], capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual('one', payload['revision'])
            self.assertEqual('discovery', payload['scope'])
        self.assertFalse(target.state_path.exists())
        self.assertEqual(before, digest_file(self.settings.state_path))

    def test_rehashed_projection_columns_and_counts_are_rejected(self):
        self.collect([repository(1)])
        directory, receipt = self.bundle()
        db = sqlite3.connect(directory / 'catalog.db')
        db.execute("UPDATE discoveries SET first_seen_at='2020-01-01T00:00:00.000000Z'")
        db.commit(); db.close()
        digest = self.resign_bundle(directory)
        with self.assertRaisesRegex(ValueError, 'query columns differ'):
            import_bundle(Settings(self.catalog, self.root / 'rejected'), directory, expected_bundle_sha256=digest)

    def test_rehashed_projection_statistics_are_rejected(self):
        self.collect([repository(1)])
        directory, receipt = self.bundle()
        manifest = json.loads((directory / 'manifest.json').read_text())
        manifest['discoveryCoverage']['repositories'] = 99
        db = sqlite3.connect(directory / 'catalog.db')
        db.execute("UPDATE metadata SET value=? WHERE key='discoveryCoverage'", (encode_json(manifest['discoveryCoverage']),))
        db.commit(); db.close()
        atomic_json(directory / 'manifest.json', manifest)
        digest = self.resign_bundle(directory)
        with self.assertRaisesRegex(ValueError, 'coverage differs'):
            import_bundle(Settings(self.catalog, self.root / 'rejected'), directory, expected_bundle_sha256=digest)

    def test_private_discovery_source_fields_are_rejected_after_rehash(self):
        self.collect([repository(1)])
        directory, receipt = self.bundle()
        db = sqlite3.connect(directory / 'catalog.db')
        row = db.execute("SELECT id,data_json FROM sources WHERE id LIKE 'github-search:%'").fetchone()
        source = json.loads(row[1]); source['credential_identity'] = SECRET
        db.execute('UPDATE sources SET data_json=? WHERE id=?', (encode_json(source), row[0]))
        db.commit(); db.close()
        digest = self.resign_bundle(directory)
        with self.assertRaisesRegex(ValueError, 'source identity/fields'):
            import_bundle(Settings(self.catalog, self.root / 'rejected'), directory, expected_bundle_sha256=digest)

    def test_undeclared_discovery_tables_are_rejected(self):
        self.collect([repository(1)])
        directory, receipt = self.bundle()
        manifest = json.loads((directory / 'manifest.json').read_text())
        manifest['features'].pop('discovery')
        manifest.pop('discoveryCoverage')
        db = sqlite3.connect(directory / 'catalog.db')
        db.execute("UPDATE metadata SET value=? WHERE key='features'", (encode_json(manifest['features']),))
        db.execute("DELETE FROM metadata WHERE key='discoveryCoverage'")
        db.commit(); db.close()
        atomic_json(directory / 'manifest.json', manifest)
        digest = self.resign_bundle(directory)
        with self.assertRaisesRegex(ValueError, 'projection is not declared'):
            import_bundle(Settings(self.catalog, self.root / 'rejected'), directory, expected_bundle_sha256=digest)

    def test_rehashed_search_tokenizer_contract_is_rejected(self):
        self.collect([repository(1)])
        directory, receipt = self.bundle()
        db = sqlite3.connect(directory / 'catalog.db')
        db.execute('DROP TABLE discoveries_fts')
        db.execute("CREATE VIRTUAL TABLE discoveries_fts USING fts5(name,display_name,summary,search_text,content='discoveries',content_rowid='rowid',tokenize='ascii')")
        db.execute("INSERT INTO discoveries_fts(discoveries_fts) VALUES ('rebuild')")
        db.commit(); db.close()
        digest = self.resign_bundle(directory)
        with self.assertRaisesRegex(ValueError, 'SQL schema differs'):
            import_bundle(Settings(self.catalog, self.root / 'rejected'), directory, expected_bundle_sha256=digest)

    def test_legacy_snapshot_keeps_catalog_and_explicitly_refuses_discovery(self):
        self.publish()
        manifest_path = self.settings.snapshots / 'one/manifest.json'
        manifest = json.loads(manifest_path.read_text())
        db = sqlite3.connect(self.settings.snapshots / 'one/catalog.db')
        for table in ('discovery_agents', 'discovery_facets', 'discoveries_fts', 'discoveries_trigram', 'discoveries'):
            db.execute('DROP TABLE ' + table)
        manifest['features'].pop('discovery')
        manifest.pop('discoveryCoverage')
        db.execute("UPDATE metadata SET value=? WHERE key='features'", (encode_json(manifest['features']),))
        db.execute("DELETE FROM metadata WHERE key='discoveryCoverage'")
        db.commit(); db.close()
        manifest['database'].update(sha256=digest_file(self.settings.snapshots / 'one/catalog.db'), bytes=(self.settings.snapshots / 'one/catalog.db').stat().st_size)
        atomic_json(manifest_path, manifest)
        pointer = json.loads((self.settings.snapshots / 'current.json').read_text())
        pointer['manifestSha256'] = digest_file(manifest_path)
        atomic_json(self.settings.snapshots / 'current.json', pointer)
        self.assertEqual(1, len(QueryService(self.settings).packages()['items']))
        self.code('discovery_unavailable', lambda: self.query())
        self.code('discovery_unavailable', lambda: QueryService(self.settings).discovery('github-repository:1'))
        response = HttpReadService(self.settings).handle('GET', '/api/v1/packages', [('scope', 'discovery')])
        self.assertEqual(503, response.status)
        export_bundle(self.settings, self.root / 'legacy-bundle')


if __name__ == '__main__':
    unittest.main()
