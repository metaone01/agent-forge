"""Canonical assertions do not merge packages, authorize code, or manufacture metrics."""
import errno
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_forge_service.canonical_mapping import (
    enqueue_mapping_repositories, mapping_status, parse_repository_url, sync_canonical_mappings,
)
from agent_forge_service.collector import enqueue_repository, run_repository_jobs
from agent_forge_service.discovery import enqueue_window, run_discovery_jobs
from agent_forge_service.github import GhTransport
from agent_forge_service.github_client import GitHubClient, RequestPolicy
from agent_forge_service.jobs import JobQueue
from agent_forge_service.publishing import build_and_publish
from agent_forge_service.settings import Settings
from agent_forge_service.storage import open_state, timestamp
from service_tests.test_collector import BODY
from service_tests.test_publication_query import record

NOW = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)


class RepositoryURLTests(unittest.TestCase):
    def test_root_and_context_have_different_semantics(self):
        self.assertEqual({'repository': 'example/repo', 'ref': None, 'subpath': '', 'pathStatus': 'root'}, parse_repository_url('git+https://GitHub.com/Example/Repo.git/'))
        self.assertIsNone(parse_repository_url('https://github.com/example/repo', context=True)['subpath'])
        self.assertIsNone(parse_repository_url('https://elsewhere.test/example/repo'))

    def test_explicit_ref_supports_slashes_without_guessing(self):
        result = parse_repository_url('https://github.com/example/repo/tree/feature/a/plugins/%E4%B8%AD%E6%96%87', ref='feature/a')
        self.assertEqual('plugins/中文', result['subpath'])
        self.assertEqual('feature/a', result['ref'])
        self.assertEqual('explicit', result['pathStatus'])
        with self.assertRaises(ValueError):
            parse_repository_url('https://github.com/example/repo/tree/main/alpha', ref='other')

    def test_head_and_full_commit_have_explicit_boundaries(self):
        for ref in ('HEAD', 'a' * 40):
            parsed = parse_repository_url(f'https://github.com/example/repo/tree/{ref}/skills/alpha')
            self.assertEqual('skills/alpha', parsed['subpath'])
            self.assertEqual(ref, parsed['ref'])

    def test_invalid_explicit_refs_are_not_stored(self):
        for ref in ('../secret','main\\other','a//b','main\nother','/main'):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                parse_repository_url('https://github.com/example/repo',ref=ref)

    def test_undeclared_branch_is_not_mistaken_for_subpath(self):
        parsed = parse_repository_url('https://github.com/example/repo/tree/feature/alpha/plugins')
        self.assertEqual('ambiguous', parsed['pathStatus'])
        self.assertIsNone(parsed['subpath']); self.assertIsNone(parsed['ref'])

    def test_reject_credentials_traversal_and_nonrepository_urls(self):
        for url in (
            'https://secret@github.com/example/repo', 'https://github.com/example/repo?token=secret',
            'http://github.com/example/repo', 'https://github.com:443/example/repo',
            'https://github.com/example/repo/issues', 'https://github.com/example/..',
            'https://github.com/example/repo/tree/HEAD/%2e%2e/secret',
            'https://github.com/example/repo/tree/HEAD/a%5cb',
            'https://github.com/example/repo/tree/HEAD/a%00b',
            'https://github.com/example/repo/tree/HEAD/a//b',
            'https://github.com/example/repo/tree/HEAD/%ff',
        ):
            with self.subTest(url=url), self.assertRaises((ValueError, UnicodeError)):
                parse_repository_url(url)


class CanonicalMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='forge-mapping-test-')
        self.addCleanup(self.cleanup_temporary)
        self.root = Path(self.temp.name)
        self.catalog = self.root / 'catalog'
        self.packages = self.catalog / 'sources/plugin/packages'
        self.packages.mkdir(parents=True)
        self.dbpath = self.root / 'runtime/state.db'
        self.db = open_state(self.dbpath); self.addCleanup(self.db.close)
        self.now = NOW; self.responses = []; self.calls = []
        def runner(*args):
            self.calls.append(args[0]); status, body = self.responses.pop(0)
            text = f'HTTP/2.0 {status} status\r\nETag: "same"\r\n\r\n' + (json.dumps(body) if body is not None else '')
            return (0 if status < 400 else 1), text.encode()
        self.client = GitHubClient(self.db, identity='mapping-test', transport=GhTransport(runner=runner),
                                   policy=RequestPolicy(jitter_fraction=0), clock=lambda: self.now)

    def cleanup_temporary(self):
        for attempt in range(6):
            try:
                self.temp.cleanup(); return
            except OSError as error:
                if (error.errno != errno.ENOTEMPTY and getattr(error, 'winerror', None) not in (5,32,145)) or attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def write(self, name='alpha', *, repository='example/public-repo', subpath='', version='1.0.0', value=None, filename=None):
        value = value or record(name, version=version)
        if repository is not None:
            value['links'] = {'repository': 'https://github.com/' + repository}
            value['distributions'] = [{'id': 'source', 'type': 'github-repo', 'url': 'https://github.com/' + repository + ('/tree/HEAD/' + subpath if subpath else '')}]
        path = self.packages / (filename or name.replace('/', '_') + '-' + version + '.json')
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def sync(self):
        return sync_canonical_mappings(self.db, self.catalog, now=self.now)

    def links(self):
        return self.db.execute('SELECT * FROM canonical_repository_links WHERE active=1 ORDER BY package_id,version,path_status').fetchall()

    def fetch_repository(self, *, repository=123, body=BODY, status=200, bootstrap=False):
        with self.db:
            enqueue_repository(JobQueue(self.db), repository=repository, slot=timestamp(self.now), bootstrap=bootstrap, now=self.now)
        self.responses.append((status, body))
        result = run_repository_jobs(self.db, self.client, owner='mapping-worker', max_requests=1, clock=lambda: self.now)
        self.now += timedelta(seconds=7)
        return result

    def discover(self, body=BODY):
        with self.db:
            enqueue_window(self.db, topic='mcp-server', field='created', start='2026-10-10T00:00:00Z', end='2026-10-11T00:00:00Z', generation=timestamp(self.now), now=self.now)
        self.responses.append((200, {'total_count': 1, 'incomplete_results': False, 'items': [body]}))
        return run_discovery_jobs(self.db, self.client, owner='mapping-worker', max_requests=1, clock=lambda: self.now)

    def test_monorepo_versions_keep_distinct_canonical_identity(self):
        self.write('alpha', subpath='plugins/alpha'); self.write('alpha', subpath='plugins/alpha', version='2.0.0')
        self.write('beta', subpath='plugins/beta')
        report = self.sync()
        self.assertEqual(3, report['records']); self.assertEqual(2, report['packages'])
        self.assertEqual({'pending': 6}, report['mappingLinks'])
        self.assertEqual({'plugins/alpha', 'plugins/beta'}, {r['subpath'] for r in self.links() if r['path_status'] == 'explicit'})
        self.assertEqual(0, self.db.execute('SELECT count(*) FROM package_units').fetchone()[0])
        self.assertEqual(0, self.db.execute('SELECT count(*) FROM metric_observations').fetchone()[0])
        self.assertEqual(0, self.db.execute('SELECT count(*) FROM change_events').fetchone()[0])
        self.assertEqual(report['inputHash'], self.sync()['inputHash'])
        self.assertEqual(6, len(self.links()))

    def test_no_name_homepage_or_provenance_based_matching(self):
        value = record('example/public-repo'); value['links'] = {'homepage': 'https://github.com/example/public-repo'}
        value['_meta'] = {'org.agentforge/collection': {'sources': [{'repository': 'example/public-repo'}]}}
        self.write(value=value, repository=None)
        self.assertEqual(1, self.sync()['recordsWithoutGitHub'])
        self.assertFalse(self.links())

    def test_ambiguous_paths_and_invalid_urls_are_visible_not_guessed(self):
        value = record(); value['links'] = {'repository': 'https://secret@github.com/example/public-repo'}
        value['distributions'][0]['url'] = 'https://github.com/example/public-repo/tree/feature/alpha/other'
        self.write(repository=None, value=value)
        self.assertEqual(2, self.sync()['diagnostics'])
        link, = self.links(); self.assertEqual('ambiguous', link['path_status']); self.assertIsNone(link['subpath'])
        diagnostics = self.db.execute('SELECT diagnostics_json FROM canonical_mapping_records').fetchone()[0]
        self.assertNotIn('secret', diagnostics)

    def test_conflicting_declared_repositories_are_preserved_as_assertions(self):
        path = self.write(); value = json.loads(path.read_text()); value['distributions'][0]['url'] = 'https://github.com/other/repo'; path.write_text(json.dumps(value))
        self.sync(); self.assertEqual({'example/public-repo', 'other/repo'}, {r['repository'] for r in self.links()})
        self.assertEqual(0, mapping_status(self.db)['mappedRepositories'])

    def test_full_scan_failure_rolls_back_removals_and_partial_insertions(self):
        path = self.write(); self.sync(); old = [dict(r) for r in self.links()]
        self.write('beta'); self.write('bad', value={'id':'invalid'})
        with self.assertRaises(ValueError): self.sync()
        self.assertEqual(old, [dict(r) for r in self.links()])
        self.assertEqual(1, mapping_status(self.db)['mappingRecords'])
        self.assertTrue(path.is_file())

    def test_removal_deactivates_mapping_without_mutating_canonical_state(self):
        first = self.write(); self.write('beta'); self.sync(); first.unlink(); self.sync()
        self.assertEqual({'plugin.beta'}, {r['package_id'] for r in self.links()})
        self.assertEqual(2, self.db.execute('SELECT count(*) FROM canonical_repository_links WHERE active=0').fetchone()[0])
        self.assertEqual(0, self.db.execute('SELECT count(*) FROM change_events').fetchone()[0])
        for path in self.packages.glob('*.json'): path.unlink()
        with self.assertRaises(ValueError): self.sync()
        self.assertEqual(2, len(self.links()))

    def test_duplicate_version_or_reassigned_id_rejected(self):
        self.write(); self.sync()
        self.write(filename='duplicate.json')
        with self.assertRaisesRegex(ValueError, 'duplicate'): self.sync()
        (self.packages / 'duplicate.json').unlink()
        self.write('beta', value=record('beta', identity='plugin.alpha', version='2.0.0'), version='2.0.0')
        with self.assertRaisesRegex(ValueError, 'id collision'): self.sync()
        self.assertEqual(2, len(self.links()))

    def test_bounded_enqueue_deduplicates_monorepo_and_is_idempotent(self):
        self.write(); self.write('beta'); self.write('other', repository='other/repo'); self.sync()
        first = enqueue_mapping_repositories(self.db, slot=timestamp(self.now), limit=1, now=self.now)
        self.assertEqual(1, first['selected'])
        second = enqueue_mapping_repositories(self.db, slot=timestamp(self.now), limit=1, now=self.now)
        self.assertEqual(1, second['selected'])
        self.assertEqual(0, enqueue_mapping_repositories(self.db, slot=timestamp(self.now), limit=1, now=self.now)['selected'])
        self.assertEqual(2, self.db.execute('SELECT count(*) FROM jobs').fetchone()[0])
        self.assertEqual([], self.calls)
        for limit in (0,1001,True):
            with self.assertRaises(ValueError): enqueue_mapping_repositories(self.db, slot=timestamp(self.now), limit=limit, now=self.now)

    def test_failed_alias_does_not_starve_unchecked_repositories(self):
        self.write(repository='aaa/failing'); self.write('beta', repository='zzz/other'); self.sync()
        enqueue_mapping_repositories(self.db, slot=timestamp(self.now), limit=1, now=self.now)
        with self.db: self.db.execute("UPDATE jobs SET state='dead',last_error='invalid' WHERE scope='repos/aaa/failing'")
        self.now += timedelta(hours=8)
        result = enqueue_mapping_repositories(self.db, slot=timestamp(self.now), limit=1, now=self.now)
        self.assertEqual('repos/zzz/other', self.db.execute('SELECT scope FROM jobs WHERE id=?', (result['jobIds'][0],)).fetchone()[0])

    def test_repository_response_resolves_aliases_and_preserves_seed_time(self):
        self.write(); self.sync(); self.now += timedelta(hours=8)
        self.assertEqual(1, self.fetch_repository(repository='example/public-repo')['succeeded'])
        self.assertEqual({'resolved'}, {r['resolution'] for r in self.links()})
        self.assertEqual({123}, {r['github_id'] for r in self.links()})
        row = self.db.execute('SELECT * FROM repositories').fetchone()
        self.assertEqual(timestamp(NOW), row['first_seen_at']); self.assertTrue(row['bootstrap'])
        sample = self.db.execute('SELECT observed_at FROM metric_observations').fetchone()[0]
        self.assertEqual(timestamp(NOW + timedelta(hours=8)), sample)
        self.assertEqual(0, self.db.execute('SELECT count(*) FROM package_units').fetchone()[0])

    def test_redirect_and_rename_keep_numeric_identity_and_old_alias(self):
        self.write(); self.sync()
        self.fetch_repository(repository='example/public-repo', body={**BODY,'full_name':'transferred/renamed'})
        self.fetch_repository(body={**BODY,'full_name':'transferred/newer'})
        self.assertEqual({123}, {r['github_id'] for r in self.links()})
        aliases = {r[0] for r in self.db.execute('SELECT alias FROM repository_aliases')}
        self.assertEqual({'example/public-repo','transferred/renamed','transferred/newer'}, aliases)
        self.assertEqual(1, self.db.execute('SELECT count(*) FROM repositories').fetchone()[0])
        self.assertEqual(timestamp(NOW), self.db.execute('SELECT first_seen_at FROM repositories').fetchone()[0])

    def test_reused_name_never_silently_moves_a_package_to_another_repo(self):
        self.write(); self.sync(); self.fetch_repository(repository='example/public-repo')
        self.fetch_repository(repository='example/public-repo', body={**BODY,'id':456})
        self.assertEqual({'conflict'}, {r['resolution'] for r in self.links()})
        self.assertEqual({123}, {r['github_id'] for r in self.links()})
        self.assertEqual({'repository_name_reused'}, {r['conflict_reason'] for r in self.links()})
        self.assertEqual(0, mapping_status(self.db)['mappedRepositories'])
        self.fetch_repository(repository=123)
        self.sync()
        self.assertEqual({'conflict'}, {r['resolution'] for r in self.links()})

    def test_existing_public_metadata_resolves_import_without_another_request(self):
        self.fetch_repository(repository='example/public-repo')
        calls = len(self.calls); self.write(); self.sync()
        self.assertEqual(calls, len(self.calls)); self.assertEqual(1, mapping_status(self.db)['mappedRepositories'])
        self.assertTrue(self.db.execute('SELECT bootstrap FROM repositories').fetchone()[0])
        self.now += timedelta(hours=8)
        self.fetch_repository(repository='example/public-repo', status=304, body=None)
        self.assertEqual(1, self.db.execute('SELECT count(*) FROM metric_observations').fetchone()[0])
        self.assertEqual({'resolved'}, {r['resolution'] for r in self.links()})

    def test_pending_seed_discovery_is_known_but_does_not_resolve_from_search(self):
        self.write(); self.sync(); self.now += timedelta(hours=8)
        self.assertEqual(1, self.discover()['succeeded'])
        candidate = self.db.execute('SELECT * FROM discovery_candidates').fetchone()
        self.assertTrue(candidate['bootstrap']); self.assertEqual(timestamp(NOW), candidate['first_seen_at'])
        self.assertEqual('discovered', candidate['classification'])
        self.assertEqual({'pending'}, {r['resolution'] for r in self.links()})
        self.assertEqual(0, self.db.execute('SELECT count(*) FROM repositories').fetchone()[0])
        self.assertEqual(0, self.db.execute('SELECT count(*) FROM metric_observations').fetchone()[0])

    def test_resolved_seed_discovery_after_rename_and_late_import(self):
        self.discover(); self.now += timedelta(seconds=7)
        self.write(); self.sync()
        self.fetch_repository(repository='example/public-repo', body={**BODY,'full_name':'transferred/renamed'})
        self.discover({**BODY,'full_name':'transferred/renamed'})
        row = self.db.execute('SELECT * FROM discovery_candidates').fetchone()
        self.assertTrue(row['bootstrap']); self.assertEqual(timestamp(NOW), row['first_seen_at'])
        self.assertEqual('transferred/renamed', row['full_name'])

    def test_alias_binding_and_observation_ack_rollback_together(self):
        self.write(); self.sync()
        with self.db: enqueue_repository(JobQueue(self.db), repository='example/public-repo', slot=timestamp(self.now), bootstrap=True, now=self.now)
        self.responses.append((200,BODY))
        with patch('agent_forge_service.collector.save_checkpoint', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError): run_repository_jobs(self.db,self.client,owner='worker',max_requests=1,clock=lambda:self.now)
        self.assertEqual(0, self.db.execute('SELECT count(*) FROM repository_aliases').fetchone()[0])
        self.assertEqual({'pending'}, {r['resolution'] for r in self.links()})
        self.now += timedelta(minutes=2); self.responses.append((304,None))
        report = run_repository_jobs(self.db,self.client,owner='worker',max_requests=1,clock=lambda:self.now)
        self.assertEqual(1, report['succeeded']); self.assertEqual({'resolved'}, {r['resolution'] for r in self.links()})
        self.assertEqual(1, self.db.execute('SELECT count(*) FROM metric_observations').fetchone()[0])

    def test_numeric_request_cannot_bind_a_different_repository_id(self):
        self.write(); self.sync()
        report = self.fetch_repository(repository=999)
        self.assertEqual(1,report['dead'])
        self.assertEqual('github_identity_mismatch',self.db.execute('SELECT last_error FROM jobs').fetchone()[0])
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM repositories').fetchone()[0])
        self.assertEqual({'pending'},{r['resolution'] for r in self.links()})

    def test_new_version_keeps_package_seed_time_even_before_identity_resolution(self):
        path = self.write(); self.sync(); self.now += timedelta(days=1)
        path.unlink(); self.write(version='2.0.0'); self.sync()
        self.discover()
        self.assertEqual(timestamp(NOW),self.db.execute('SELECT first_seen_at FROM discovery_candidates').fetchone()[0])

    def test_inactive_seed_does_not_mark_an_unrelated_discovery_as_known(self):
        path = self.write(); self.write('other',repository='other/repo'); self.sync()
        path.unlink(); self.sync(); self.discover()
        self.assertFalse(self.db.execute('SELECT bootstrap FROM discovery_candidates').fetchone()[0])



    def test_mapping_and_alias_state_are_not_published(self):
        self.write(); self.sync(); self.fetch_repository(repository='example/public-repo')
        settings = Settings(self.catalog,self.root/'runtime')
        result = build_and_publish(settings,now=self.now,revision='mapping-private')
        self.assertEqual({'packages':1,'versions':1},result['counts'])
        public = sqlite3.connect(settings.snapshots/'mapping-private/catalog.db')
        try:
            tables = {r[0] for r in public.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn('canonical_repository_links',tables)
            self.assertNotIn('repository_aliases',tables)
        finally: public.close()

    def test_read_status_of_v3_state_requires_upgrade_without_writing(self):
        with self.db:
            self.db.execute('DROP TABLE canonical_mapping_targets')
            self.db.execute('DROP TABLE canonical_repository_links')
            self.db.execute('DROP TABLE canonical_mapping_records')
            self.db.execute('PRAGMA user_version=3')
        self.assertEqual('upgrade_required',mapping_status(self.db)['mappingSchema'])
        self.assertIsNone(mapping_status(self.db)['mappingRecords'])
        self.assertEqual(3,self.db.execute('PRAGMA user_version').fetchone()[0])

    def test_bootstrap_and_due_query_use_indexes(self):
        self.write(); self.sync()
        plans = ' '.join(str(r[3]) for r in self.db.execute("EXPLAIN QUERY PLAN SELECT min(first_seen_at) FROM canonical_repository_links WHERE active=1 AND github_id=123 AND resolution='resolved'"))
        self.assertIn('canonical_links_identity',plans)
        plans = ' '.join(str(r[3]) for r in self.db.execute("EXPLAIN QUERY PLAN SELECT repository FROM canonical_repository_links WHERE active=1 AND resolution='pending' GROUP BY repository"))
        self.assertIn('canonical_links_pending',plans)

    def test_source_local_name_collision_is_not_deduplicated(self):
        self.write(); self.write(filename='different-id.json',value=record(identity='different-id',version='2.0.0'))
        with self.assertRaisesRegex(ValueError,'name collision'): self.sync()
        self.assertEqual(0,mapping_status(self.db)['mappingRecords'])

    def test_cli_import_and_enqueue_do_not_invoke_gh(self):
        self.write()
        def cli(*args):
            call = subprocess.run([sys.executable,'-m','agent_forge_service.worker',*args,'--runtime',str(self.root/'cli-runtime')],capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(0,call.returncode,call.stderr); return json.loads(call.stdout)
        self.assertEqual({'pending':2},cli('map-canonical','--catalog',str(self.catalog))['mappingLinks'])
        self.assertEqual(1,cli('map-enqueue','--slot',timestamp(NOW),'--limit','1')['selected'])
        self.assertEqual(0,cli('map-enqueue','--slot',timestamp(NOW),'--limit','1')['selected'])
        self.assertEqual({'pending':2},cli('collect-status')['canonicalMapping']['mappingLinks'])


if __name__ == '__main__':
    unittest.main()
