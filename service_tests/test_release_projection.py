"""Replay Release evidence, explicit distribution associations and cutoff-bound public query."""
import json
import sqlite3
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from agent_forge_service.publishing import build_and_publish,digest_file,atomic_json
from agent_forge_service.query import QueryService
from agent_forge_service.http_contract import HttpReadService
from agent_forge_service.bundles import export_bundle,import_bundle
from agent_forge_service.settings import Settings
from agent_forge_service.collector import enqueue_repository,run_repository_jobs
from agent_forge_service.jobs import JobQueue
from agent_forge_service.github import GhTransport
from agent_forge_service.github_client import GitHubClient,RequestPolicy
from agent_forge_service.releases import enqueue_releases,run_release_jobs
from agent_forge_service.release_projection import POLICY,parse_release_url
from agent_forge_service.storage import encode_json,timestamp,open_state
from agent_forge_service.errors import ServiceError
from service_tests import test_scan_batches as scans
from service_tests.test_collector import BODY
from service_tests.test_releases import item
from service_tests.test_publication_query import record


class ReleaseProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=scans.ScanBatchTests('runTest');self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.db=self.fixture.db;self.settings=self.fixture.settings;self.root=self.fixture.root;self.packages=self.settings.catalog_root/'sources/plugin/packages'
        self.now=scans.NOW
        self.repo_body={**BODY,'id':123,'full_name':'owner/repo'}
        self.repo_client=GitHubClient(self.db,identity='private-fixture-identity',transport=GhTransport(runner=self.repo_response),clock=lambda:self.fixture.now,policy=RequestPolicy(jitter_fraction=0))
        with self.db:enqueue_repository(JobQueue(self.db),repository=123,slot=timestamp(self.now),bootstrap=True,now=self.now)
        run_repository_jobs(self.db,self.repo_client,owner='worker',max_requests=1,clock=lambda:self.fixture.now)
        self.fixture.opener.requests.clear()
        self.write_catalog()

    def repo_response(self,*args):return 0,('HTTP/2.0 200 OK\r\n\r\n'+json.dumps(self.repo_body)).encode()

    def write_catalog(self):
        alpha=record('alpha');alpha['updatedAt']=timestamp(scans.NOW-timedelta(hours=1));alpha['links']={'repository':'https://github.com/owner/repo'}
        other=record('alpha',version='2.0.0');other['updatedAt']=alpha['updatedAt'];other['distributions']=[{'id':'release','type':'release','url':'https://github.com/owner/repo/releases/tag/v1.0.0'}]
        beta=record('beta');beta['updatedAt']=alpha['updatedAt'];beta['distributions']=[{'id':'zip','type':'archive','url':'https://github.com/owner/repo/releases/download/v1.0.0/plugin.zip'}]
        gamma=record('gamma');gamma['updatedAt']=alpha['updatedAt'];gamma['distributions']=[{'id':'release','type':'release','url':'https://github.com/owner/repo/releases/tag/1.0.0'}]
        for name,value in [('alpha.json',alpha),('alpha2.json',other),('beta.json',beta),('gamma.json',gamma)]:
            (self.packages/name).write_text(json.dumps(value),encoding='utf-8')
        (self.packages.parent/'index.json').write_text(json.dumps({'packages':{'alpha':{'latest':'2.0.0'},'beta':{'latest':'1.0.0'},'gamma':{'latest':'1.0.0'}}}),encoding='utf-8')

    def collect(self,values=None,*,pages=2,slot=None):
        self.fixture.now=max(self.fixture.now,self.now)+timedelta(seconds=2)
        self.fixture.release_items=values if values is not None else [{**item(),'tag_name':'v1.0.0'}]
        with self.db:identity=enqueue_releases(self.db,github_id=123,slot=slot or timestamp(self.fixture.now),max_pages=pages,now=self.fixture.now)
        for _ in range(pages+1):
            self.fixture.now+=timedelta(seconds=2)
            run_release_jobs(self.db,self.fixture.client,owner='release-worker',max_requests=1,clock=lambda:self.fixture.now)
            if self.db.execute('SELECT state FROM release_scans WHERE id=?',(identity,)).fetchone()[0]!='running':break
        self.now=self.fixture.now
        return identity

    def build(self,revision='one',as_of=None):return build_and_publish(self.settings,revision=revision,now=as_of or self.fixture.now)
    def query(self,*args,**kwargs):return QueryService(self.settings).releases(*args,**kwargs)
    def code(self,code,function):
        with self.assertRaises(ServiceError) as caught:function()
        self.assertEqual(code,caught.exception.code)

    def test_exact_release_asset_and_repository_context_are_separate(self):
        self.collect();self.build()
        alpha=self.query('plugin.alpha');old=self.query('plugin.alpha',version='1.0.0');beta=self.query('plugin.beta');gamma=self.query('plugin.gamma')
        self.assertEqual('2.0.0',alpha['selectedVersion']);self.assertEqual('v1.0.0',alpha['items'][0]['release']['tagName'])
        self.assertEqual('exact_declared_release',alpha['items'][0]['association']['assertions'][0]['kind'])
        self.assertEqual('repository_context',old['items'][0]['association']['assertions'][0]['kind'])
        proof=beta['items'][0]['association']['assertions'][0];self.assertEqual('exact_declared_asset',proof['kind']);self.assertEqual('1001',proof['assetId'])
        self.assertEqual([],gamma['items']);self.assertEqual('unknown',gamma['availability'])
        self.assertEqual('10',beta['items'][0]['release']['assets'][0]['downloadCount'])
        self.assertEqual(2,alpha['releaseCoverage']['exactDistributionLinks'])

    def test_published_desc_stable_pagination_uses_all_observed_identities(self):
        values=[{**item(identity=number),'tag_name':'v'+str(number)+'.0.0'} for number in range(1,24)]
        self.collect(values);self.build()
        page=self.query('plugin.alpha',version='1.0.0',limit=20);tail=self.query('plugin.alpha',version='1.0.0',limit=20,cursor=page['nextCursor'])
        self.assertEqual(20,len(page['items']));self.assertEqual(3,len(tail['items']))
        self.assertEqual(23,len({row['release']['releaseId'] for row in page['items']+tail['items']}));self.assertIsNone(tail['nextCursor'])
        self.assertEqual(1,len(self.query('plugin.alpha')['items']))
        self.code('cursor_mismatch',lambda:self.query('plugin.alpha',limit=20,cursor=page['nextCursor']))

    def test_old_snapshot_and_explicit_missing_version_never_fall_back(self):
        self.collect();self.build('one');self.code('version_not_found',lambda:self.query('plugin.alpha',version='9.9'))
        self.code('not_found',lambda:self.query('missing'))
        self.code('invalid_query',lambda:self.query(version='1.0.0'))
        self.code('invalid_query',lambda:self.query(repository_id='0123'))
        self.code('invalid_query',lambda:self.query('plugin.alpha',association='recommended'))

    def test_empty_page_is_observed_check_not_global_absence_or_deletion(self):
        self.collect([]);manifest=self.build();result=self.query(repository_id='123')
        self.assertTrue(manifest['features']['releases']);self.assertEqual([],result['items']);self.assertEqual('unknown',result['availability'])
        self.assertEqual('traversal_complete',result['repositoryChecks'][0]['latestTraversal']['state']);self.assertEqual(0,result['repositoryChecks'][0]['observedReleaseIdentities'])
        self.assertEqual(1,result['releaseCoverage']['pages'])

    def test_absent_pages_and_old_cutoff_keep_feature_unavailable(self):
        self.build('old');self.code('releases_unavailable',self.query)
        cutoff=self.fixture.now;self.collect();manifest=self.build('before-read',as_of=cutoff)
        self.assertNotIn('releases',manifest['features']);self.code('releases_unavailable',self.query)
        self.assertEqual(503,HttpReadService(self.settings).handle('GET','/api/v1/releases').status)

    def test_counter_history_zero_and_decrease_stay_raw_not_users_or_rates(self):
        self.collect([item(downloads=20)]);self.build('before')
        self.now+=timedelta(hours=8);self.fixture.now=self.now;self.collect([item(downloads=0)]);self.build('after')
        current=self.query('plugin.beta');old=self.query('plugin.beta',revision='before')
        self.assertEqual('0',current['items'][0]['release']['assets'][0]['downloadCount']);self.assertEqual('20',old['items'][0]['release']['assets'][0]['downloadCount'])
        self.assertEqual('cumulative',current['items'][0]['release']['assets'][0]['measurementKind']);self.assertEqual(2,current['releaseCoverage']['pages'])
        self.assertNotIn('perHour',json.dumps(current));self.assertNotIn('users',current['items'][0]['release']['assets'][0])

    def test_tag_reuse_and_duplicate_asset_names_never_make_exact_links(self):
        first=item(identity=1);second=item(identity=2)
        self.collect([first,second]);self.build();self.assertEqual([],self.query('plugin.alpha')['items']);self.assertEqual([],self.query('plugin.beta')['items'])
        self.assertEqual(2,len(self.query('plugin.alpha',version='1.0.0')['items']))
        self.now+=timedelta(hours=8);self.fixture.now=self.now
        value=item();value['assets'].append({**value['assets'][0],'id':2001})
        self.collect([value]);self.build('duplicate-assets');self.assertEqual([],self.query('plugin.beta')['items'])

    def test_asset_rename_and_empty_new_page_do_not_claim_deletion(self):
        self.collect();self.build('before');self.now+=timedelta(hours=8);self.fixture.now=self.now
        value=item();value['assets'][0]['name']='renamed.zip';self.collect([value]);self.build('renamed');self.assertEqual([],self.query('plugin.beta')['items'])
        self.now+=timedelta(hours=8);self.fixture.now=self.now;self.collect([]);self.build('empty')
        self.assertEqual(1,len(self.query()['items']));self.assertEqual('not_supported',self.query()['releaseCoverage']['deletionInference'])

    def test_partial_page_cap_and_page_overlap_are_disclosed(self):
        values=[{**item(identity=number),'tag_name':'v'+str(number)} for number in range(1,101)]
        self.collect(values,pages=1);self.build();result=self.query(repository_id='123')
        self.assertEqual('partial',result['repositoryChecks'][0]['latestTraversal']['state']);self.assertEqual(1,result['releaseCoverage']['scanStates']['partial'])
        self.now+=timedelta(hours=8);self.fixture.now=self.now;self.collect(values,pages=2);self.build('overlap')
        self.assertEqual('partial',self.query()['repositoryChecks'][0]['latestTraversal']['state'])
        self.assertEqual(100,self.query()['releaseCoverage']['releaseIdentities'])

    def test_old_repository_name_is_not_guessed_after_rename(self):
        self.collect();self.build('before');self.now+=timedelta(hours=8);self.fixture.now=self.now
        self.repo_body={**self.repo_body,'full_name':'owner/renamed'}
        with self.db:enqueue_repository(JobQueue(self.db),repository=123,slot=timestamp(self.now),bootstrap=False,now=self.now)
        run_repository_jobs(self.db,self.repo_client,owner='worker',max_requests=1,clock=lambda:self.fixture.now)
        self.build('renamed');self.assertEqual([],self.query('plugin.beta')['items']);self.assertEqual(1,len(self.query()['items']))

    def test_repository_identity_can_be_browsed_without_a_name_proof(self):
        with self.db:self.db.execute('DELETE FROM repository_aliases');self.db.execute('DELETE FROM metric_observations');self.db.execute('DELETE FROM repository_observations')
        self.collect();self.build();result=self.query(repository_id='123')
        self.assertEqual(1,len(result['items']));self.assertEqual(0,result['releaseCoverage']['repositoryNamesProven']);self.assertEqual([],self.query('plugin.beta')['items'])

    def test_parser_rejects_mutable_latest_untrusted_or_ambiguous_links(self):
        for url in ['http://github.com/owner/repo/releases/tag/v1','https://user:token@github.com/owner/repo/releases/tag/v1','https://github.com/owner/repo/releases/latest','https://github.com/owner/repo/releases/tag/v1?token=secret','https://github.com/owner/repo/releases/download/tag/dir/file.zip','https://github.com/owner/repo/releases/download/v1/a%252F.zip']:
            with self.subTest(url=url):self.assertIsNone(parse_release_url(url))
        value=parse_release_url('https://github.com/Owner/Repo/releases/download/release%2Fv1/plugin.zip')
        self.assertEqual('release/v1',value['tag']);self.assertEqual('owner/repo',value['repositoryName'])

    def test_full_integer_counters_are_exact_decimal_strings(self):
        value=item(identity=2**53+1,downloads=2**63-1);self.collect([value]);self.build();asset=self.query()['items'][0]['release']['assets'][0]
        self.assertEqual(str(2**63-1),asset['downloadCount']);self.assertEqual(str(2**53+1001),asset['assetId'])

    def test_public_receiver_and_cli_need_no_canonical_or_private_database(self):
        self.collect();self.build();directory=self.root/'bundle';receipt=export_bundle(self.settings,directory)
        receiver=Settings(self.root/'absent',self.root/'receiver');import_bundle(receiver,directory,expected_bundle_sha256=receipt['bundleSha256'])
        result=QueryService(receiver).releases('plugin.beta',revision='one');self.assertEqual(1,len(result['items']));self.assertFalse(receiver.state_path.exists())
        command=[sys.executable,'-m','agent_forge_service.worker','release-query','--runtime',str(receiver.runtime_root),'--revision','one','--id','plugin.beta']
        process=subprocess.run(command,cwd=Path(__file__).resolve().parents[1],capture_output=True)
        self.assertEqual(0,process.returncode,process.stderr);self.assertEqual('one',json.loads(process.stdout)['revision']);self.assertNotIn('private-fixture-identity',process.stdout.decode())

    def test_http_exact_version_duplicate_parameter_and_get_only_contract(self):
        self.collect();self.build();reader=HttpReadService(self.settings)
        value=reader.handle('GET','/api/v1/packages/plugin.alpha/releases',[('version','1.0.0')]);self.assertEqual(200,value.status)
        self.assertEqual('repository_context',json.loads(value.body)['items'][0]['association']['assertions'][0]['kind'])
        for pairs in [[('limit','0')],[('limit','1'),('limit','2')],[('repositoryId','bad')],[('association','bogus')]]:
            self.assertEqual(400,reader.handle('GET','/api/v1/packages/plugin.alpha/releases',pairs).status)
        self.assertEqual(405,reader.handle('POST','/api/v1/releases').status)

    def test_private_page_corruption_keeps_old_publication(self):
        self.collect();self.build('safe')
        with self.db:self.db.execute("UPDATE release_observations SET metadata_json='{}'")
        with self.assertRaises(ValueError):self.build('broken')
        self.assertEqual('safe',QueryService(self.settings).store.current()['revision'])

    def resign(self,directory,update):
        db=sqlite3.connect(directory/'catalog.db');db.row_factory=sqlite3.Row;update(db);db.commit();db.close()
        manifest=json.loads((directory/'manifest.json').read_text());manifest['database'].update(sha256=digest_file(directory/'catalog.db'),bytes=(directory/'catalog.db').stat().st_size);atomic_json(directory/'manifest.json',manifest)
        receipt=json.loads((directory/'bundle.json').read_text());receipt['database']=manifest['database'];receipt['manifest'].update(sha256=digest_file(directory/'manifest.json'),bytes=(directory/'manifest.json').stat().st_size);atomic_json(directory/'bundle.json',receipt)
        return digest_file(directory/'bundle.json')

    def test_rehashed_asset_counter_link_and_coverage_tampering_is_rejected(self):
        self.collect();self.build()
        def change_counter(db):
            row=db.execute('SELECT * FROM public_release_items').fetchone();value=json.loads(row['data_json']);value['assets'][0]['downloadCount']='9999';db.execute('UPDATE public_release_items SET data_json=?',(encode_json(value),))
        def change_link(db):db.execute("UPDATE public_release_package_links SET data_json='{}'")
        def drop_rows(db):db.execute('DELETE FROM public_release_package_links')
        for number,change in enumerate([change_counter,change_link,drop_rows]):
            directory=self.root/('tampered'+str(number));export_bundle(self.settings,directory);checksum=self.resign(directory,change)
            with self.assertRaises(ValueError):import_bundle(Settings(self.root/'absent',self.root/('rejected'+str(number))),directory,expected_bundle_sha256=checksum)

    def test_public_private_columns_unsupported_page_fields_and_sql_are_rejected(self):
        self.collect();self.build()
        for number,change in enumerate([lambda db:db.execute('ALTER TABLE public_release_items ADD COLUMN credential_identity TEXT'),lambda db:db.execute('DROP INDEX public_release_published')]):
            directory=self.root/('bad-schema'+str(number));export_bundle(self.settings,directory);checksum=self.resign(directory,change)
            with self.assertRaises(ValueError):import_bundle(Settings(self.root/'absent',self.root/('rejected-schema'+str(number))),directory,expected_bundle_sha256=checksum)

    def test_304_keeps_original_counter_observation_time_but_updates_check(self):
        from service_tests.test_manifests import Response
        self.collect([item(downloads=0)]);self.build('before');before=self.query()['items'][0]['release']
        self.now+=timedelta(hours=8);self.fixture.now=self.now
        self.fixture.opener.callback=lambda request:Response(None,request.full_url,304)
        self.collect();self.build('checked');after=self.query()['items'][0]['release']
        self.assertEqual('0',after['assets'][0]['downloadCount']);self.assertEqual(before['observation']['observedAt'],after['observation']['observedAt'])
        self.assertGreater(after['observation']['checkedAt'],before['observation']['checkedAt'])

    def test_retrospective_cutoff_replays_old_pages_without_future_asset_counts(self):
        self.collect([item(downloads=4)]);cutoff=self.fixture.now
        self.now+=timedelta(hours=8);self.fixture.now=self.now;self.collect([item(downloads=99)])
        self.build('past',as_of=cutoff);value=self.query('plugin.beta')
        self.assertEqual('4',value['items'][0]['release']['assets'][0]['downloadCount']);self.assertEqual(1,value['releaseCoverage']['pages'])

    def test_repository_name_collision_is_not_a_package_identity_join(self):
        self.collect();self.fixture.seed(124);self.now+=timedelta(hours=8);self.fixture.now=self.now
        self.repo_body={**self.repo_body,'id':124,'full_name':'owner/repo'}
        with self.db:enqueue_repository(JobQueue(self.db),repository=124,slot=timestamp(self.now),bootstrap=False,now=self.now)
        run_repository_jobs(self.db,self.repo_client,owner='worker',max_requests=1,clock=lambda:self.fixture.now)
        with self.db:enqueue_releases(self.db,github_id=124,slot=timestamp(self.now),max_pages=1,now=self.now)
        self.fixture.now+=timedelta(seconds=2);run_release_jobs(self.db,self.fixture.client,owner='worker',max_requests=1,clock=lambda:self.fixture.now)
        self.build();self.assertEqual([],self.query('plugin.beta')['items']);self.assertEqual(2,len(self.query()['items']))
        identities={row['release']['assets'][0]['counterIdentity'] for row in self.query()['items']};self.assertEqual(2,len(identities))

    def test_rehashed_private_raw_release_fields_are_rejected_even_with_valid_derived_hash(self):
        from agent_forge_service.release_projection import _hash
        self.collect();self.build();directory=self.root/'private-page';export_bundle(self.settings,directory)
        def change(db):
            row=db.execute('SELECT * FROM public_release_pages').fetchone();value=json.loads(row['data_json']);value['body']['releases'][0]['body']='unwanted upstream prose';value['evidenceHash']=_hash(value['body']);value.pop('id');value['id']=_hash(value)
            # Keep the original page ID column to avoid changing FK targets; field validation still rejects the raw prose first.
            db.execute('UPDATE public_release_pages SET data_json=?',(encode_json(value),))
        checksum=self.resign(directory,change)
        with self.assertRaises(ValueError):import_bundle(Settings(self.root/'absent',self.root/'rejected-private'),directory,expected_bundle_sha256=checksum)

    def test_rehashed_unadvertised_release_tables_and_wrong_coverage_are_rejected(self):
        self.collect();self.build()
        for mode in ['no-feature','coverage']:
            directory=self.root/mode;export_bundle(self.settings,directory)
            manifest=json.loads((directory/'manifest.json').read_text())
            if mode=='no-feature':manifest['features'].pop('releases');manifest.pop('releaseCoverage')
            else:manifest['releaseCoverage']['assets']+=1
            def update(db):
                db.execute('UPDATE metadata SET value=? WHERE key=?',(encode_json(manifest['features']),'features'))
                if mode=='no-feature':db.execute("DELETE FROM metadata WHERE key='releaseCoverage'")
                else:db.execute('UPDATE metadata SET value=? WHERE key=?',(encode_json(manifest['releaseCoverage']),'releaseCoverage'))
            atomic_json(directory/'manifest.json',manifest);checksum=self.resign(directory,update)
            with self.assertRaisesRegex(ValueError,'not declared|coverage differs'):
                import_bundle(Settings(self.root/'absent',self.root/('rejected-'+mode)),directory,expected_bundle_sha256=checksum)

    def test_missing_page_one_cannot_be_hidden_by_rehashing_outer_bundle(self):
        values=[{**item(identity=number),'tag_name':'v'+str(number)} for number in range(1,101)]
        self.collect(values,pages=2);self.build();directory=self.root/'page-gap';export_bundle(self.settings,directory)
        def update(db):
            first=db.execute('SELECT id FROM public_release_pages WHERE page=1').fetchone()[0]
            # The latest replay comes from page 2, so page 1 has no item FK reference.
            db.execute('DELETE FROM public_release_pages WHERE id=?',(first,))
        checksum=self.resign(directory,update)
        with self.assertRaisesRegex(ValueError,'incomplete'):
            import_bundle(Settings(self.root/'absent',self.root/'rejected-gap'),directory,expected_bundle_sha256=checksum)

    def test_rehashed_canonical_payload_hash_cannot_rewrite_release_association_evidence(self):
        self.collect();self.build();directory=self.root/'canonical-hash';export_bundle(self.settings,directory)
        checksum=self.resign(directory,lambda db:db.execute("UPDATE package_versions SET payload_hash=? WHERE package_id='plugin.beta'",('0'*64,)))
        with self.assertRaisesRegex(ValueError,'canonical record identity/hash'):
            import_bundle(Settings(self.root/'absent',self.root/'rejected-hash'),directory,expected_bundle_sha256=checksum)

    def test_association_replay_builds_repository_name_index_once_per_snapshot(self):
        from unittest.mock import patch
        from agent_forge_service import release_projection as projection
        self.collect();self.build()
        db=sqlite3.connect(self.settings.snapshots/'one/catalog.db');db.row_factory=sqlite3.Row
        try:
            repositories={row['repository_id']:json.loads(row['data_json']) for row in db.execute('SELECT * FROM public_release_repositories')}
            items={(row['repository_id'],row['release_id']):json.loads(row['data_json']) for row in db.execute('SELECT * FROM public_release_items')}
            with patch.object(projection,'_name_index',wraps=projection._name_index) as lookup:
                values=projection.package_links(db,repositories,items)
                self.assertEqual(1,lookup.call_count);self.assertEqual(3,len(values))
        finally:db.close()

if __name__=='__main__':unittest.main()
