"""Public curated reconstruction, snapshot cutoff, exact declarations and receiver isolation."""
from __future__ import annotations
import hashlib
import tempfile
import time
import errno
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import unittest
from datetime import timedelta
from unittest.mock import patch

from agent_forge_service.bundles import export_bundle, import_bundle, activate_snapshot
from agent_forge_service.errors import ServiceError
from agent_forge_service.http_contract import HttpReadService
from agent_forge_service.publishing import SnapshotStore, atomic_json, build_and_publish, digest_file
from agent_forge_service.query import QueryService
from agent_forge_service.recommendation_projection import COLUMNS, public_source_identity, validate_projection
from agent_forge_service.recommendations import enqueue_recommendation
from agent_forge_service.settings import Settings
from agent_forge_service.storage import encode_json, timestamp
from service_tests.test_publication_query import record
from service_tests import test_recommendations as collection_fixtures
from service_tests.test_recommendations import SOURCE, NOW, content


class ProjectionTests(unittest.TestCase):
    runner=collection_fixtures.CollectionTests.runner
    enqueue=collection_fixtures.CollectionTests.enqueue
    collect=collection_fixtures.CollectionTests.collect
    next=collection_fixtures.CollectionTests.next
    count=collection_fixtures.CollectionTests.count
    snapshot=collection_fixtures.CollectionTests.snapshot

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.cleanup_temporary)
        self.root=Path(self.temporary.name);self.db=collection_fixtures.open_state(self.root/'runtime/state.db');self.addCleanup(self.db.close)
        self.clock=collection_fixtures.Clock();self.raw=content('https://github.com/o/alpha','https://github.com/o/beta');self.commit=collection_fixtures.COMMIT
        self.calls=[];self.overrides={};self.headers={};self.code=0
        self.client=collection_fixtures.RecommendationClient(self.db,identity='fixture-profile',transport=collection_fixtures.GhRecommendationTransport(runner=self.runner),clock=self.clock,
            policy=collection_fixtures.RequestPolicy(core_spacing_seconds=.1,jitter_fraction=0),random_value=lambda:0)
        self.catalog=self.root/'catalog'
        self.packages=self.catalog/'sources/plugin/packages'
        self.packages.mkdir(parents=True)
        self.settings=Settings(self.catalog,self.root/'runtime',publication_delay_seconds=0)
        self.alpha=record('alpha',identity='pkg/alpha/推荐',version='1.0.0')
        self.alpha['distributions']=[{'id':'source','type':'git','url':'https://github.com/o/alpha'}]
        self.write(self.alpha)
        self.enqueue();self.collect()
        self.source=public_source_identity(SOURCE)
        self.build()

    def cleanup_temporary(self):
        for attempt in range(6):
            try:
                self.temporary.cleanup();return
            except OSError as error:
                if (error.errno != errno.ENOTEMPTY and getattr(error,'winerror',None) != 145) or attempt == 5:
                    raise
                time.sleep(.05*(attempt+1))

    def write(self,value):
        (self.packages/(value['name']+'-'+value['version']+'.json')).write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
    def build(self,revision='one',now=None):return build_and_publish(self.settings,now=now or self.clock.now+timedelta(minutes=1),revision=revision)
    def query(self):return QueryService(self.settings)
    def assert_code(self,expected,call):
        with self.assertRaises(ServiceError) as error:call()
        self.assertEqual(expected,error.exception.code)
    def public(self,revision='one'):
        db=sqlite3.connect((self.settings.snapshots/revision/'catalog.db').resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
        db.row_factory=sqlite3.Row;self.addCleanup(db.close);return db
    def bundle(self):
        path=self.root/('bundle-'+str(len(list(self.root.glob('bundle-*')))))
        return path,export_bundle(self.settings,path)
    def resign(self,path):
        manifest=json.loads((path/'manifest.json').read_text())
        manifest['database'].update(sha256=digest_file(path/'catalog.db'),bytes=(path/'catalog.db').stat().st_size)
        atomic_json(path/'manifest.json',manifest)
        descriptor=json.loads((path/'bundle.json').read_text())
        descriptor['database'].update(sha256=digest_file(path/'catalog.db'),bytes=(path/'catalog.db').stat().st_size)
        descriptor['manifest'].update(sha256=digest_file(path/'manifest.json'),bytes=(path/'manifest.json').stat().st_size)
        atomic_json(path/'bundle.json',descriptor)
        return digest_file(path/'bundle.json')

    def test_public_snapshot_contains_source_scoped_facts_and_no_private_bodies(self):
        result=self.query().recommendations(self.alpha['id']);self.assertEqual(1,len(result['items']))
        item=result['items'][0]
        self.assertEqual('active',item['status']);self.assertIsNone(item['sourceRank'])
        self.assertEqual('exact-declared-link',item['packageAssociation']['association'])
        self.assertEqual('not-inferred',item['packageAssociation']['endorsement'])
        self.assertEqual('source-listing-not-package-endorsement',item['endorsement'])
        self.assertIn('/blob/',item['snapshot']['url']);self.assertEqual(5,len(item['snapshot']['steps']))
        self.assertEqual('curated_listing',item['claimKind']);self.assertEqual('source-target',result['order'])
        self.assertNotIn('prose is not',encode_json(result));self.assertNotIn('fixture-profile',encode_json(result))
        tables={row[0] for row in self.public().execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(set(COLUMNS)<=tables);self.assertFalse({'recommendation_sources','http_cache','jobs','collection_evidence'}&tables)
        self.assertEqual(1,self.query().dashboard()['totals']['packages'])

    def test_cutoff_before_completion_does_not_publish_partial_or_future_claims(self):
        snapshot=self.snapshot();before=timestamp(snapshot['completed_at'])
        from datetime import datetime
        cutoff=datetime.fromisoformat(before.replace('Z','+00:00'))-timedelta(microseconds=1)
        self.build('before-complete',now=cutoff)
        self.assertEqual([],self.query().recommendation_sources()['items'])
        self.assertEqual([],self.query().recommendations(self.alpha['id'])['items'])
        self.assertEqual(1,self.query().packages()['items'].__len__())

    def test_retrospective_rebuild_preserves_old_facts_after_source_removal(self):
        first=self.query().recommendations(self.alpha['id']);old_time=self.clock.now+timedelta(minutes=1)
        self.next(content('https://github.com/o/beta'));self.build('two')
        removed=self.query().recommendations(self.alpha['id'])['items'][0]
        self.assertEqual('withdrawn',removed['status']);self.assertIsNotNone(removed['previousSnapshot'])
        self.assertEqual('complete_snapshot_absence',removed['reason'])
        self.assertEqual(1,len(removed['citations']));self.assertEqual(2,removed['previousSnapshot']['memberCount'])
        self.assertEqual(1,removed['snapshot']['memberCount'])
        self.assertEqual('active',self.query().recommendations(self.alpha['id'],revision='one')['items'][0]['status'])
        self.build('rebuilt-old',now=old_time)
        old=self.query().recommendations(self.alpha['id'])
        self.assertEqual(first['items'][0]['snapshot'],old['items'][0]['snapshot']);self.assertEqual('active',old['items'][0]['status'])

    def test_private_observation_status_is_not_trusted_as_public_truth(self):
        with self.db:self.db.execute("UPDATE recommendation_observations SET status='withdrawn'")
        self.build('rebuilt');self.assertEqual('active',self.query().recommendations(self.alpha['id'])['items'][0]['status'])

    def test_private_member_mutation_is_rejected_and_prior_pointer_preserved(self):
        before=digest_file(self.settings.snapshots/'current.json')
        with self.db:self.db.execute("UPDATE recommendation_members SET name='forged'")
        with self.assertRaisesRegex(ValueError,'members differ'):self.build('invalid')
        self.assertEqual(before,digest_file(self.settings.snapshots/'current.json'))

    def test_private_git_hash_or_complete_chain_mutation_is_rejected(self):
        with self.db:self.db.execute("UPDATE recommendation_fetches SET evidence_hash=? WHERE job_id=(SELECT job_id FROM recommendation_tasks WHERE step=2 LIMIT 1)",(self.db.execute('SELECT evidence_hash FROM recommendation_fetches LIMIT 1').fetchone()[0],))
        with self.assertRaises(ValueError):self.build('forged-chain')

    def test_private_source_definition_mutation_is_rejected(self):
        definition={**SOURCE,'label':'unverified replacement'}
        with self.db:self.db.execute('UPDATE recommendation_sources SET definition_json=?',(encode_json(definition),))
        with self.assertRaisesRegex(ValueError,'source identity'):self.build('changed-source')

    def test_alias_labels_and_parser_budgets_are_one_public_source_not_two_endorsements(self):
        self.clock.now+=timedelta(hours=1)
        with self.db:enqueue_recommendation(self.db,definition={**SOURCE,'label':'Another label','maxEntries':99},slot=self.clock.now,now=self.clock.now)
        self.collect();self.build('aliases')
        sources=self.query().recommendation_sources()
        self.assertEqual(1,len(sources['items']));self.assertEqual(['Another label',SOURCE['label']],sources['items'][0]['labels'])
        self.assertEqual(1,len(self.query().recommendations(self.alpha['id'])['items']))
        self.assertEqual(1,sources['recommendationCoverage']['sources'])

    def test_repository_context_shared_by_packages_is_disclosed_without_endorsement(self):
        beta=record('beta');beta['links']={'repository':'https://github.com/o/alpha'};self.write(beta)
        self.build('shared')
        item=self.query().recommendations(beta['id'])['items'][0]
        self.assertEqual('repository_context',item['packageAssociation']['assertions'][0]['kind'])
        self.assertEqual('not-inferred',item['packageAssociation']['endorsement'])
        self.assertEqual(1,len(self.query().recommendations(self.alpha['id'])['items']))

    def test_repository_root_does_not_match_an_arbitrary_monorepo_distribution_path(self):
        self.alpha['distributions'][0]['url']='https://github.com/o/alpha/tree/main/packages/plugin'
        self.write(self.alpha);self.build('subpath')
        self.assertEqual([],self.query().recommendations(self.alpha['id'])['items'])
        self.assertEqual(2,len(self.query().recommendations(source=self.source)['items']))

    def test_exact_subpath_and_root_are_independent_selected_targets(self):
        self.next(content('https://github.com/o/alpha/tree/main/packages/plugin','https://github.com/o/alpha'))
        self.alpha['distributions'][0]['url']='https://github.com/o/alpha/tree/main/packages/plugin'
        self.write(self.alpha);self.build('subpath-selected')
        item=self.query().recommendations(self.alpha['id'])['items'][0]
        self.assertEqual('https://github.com/o/alpha/tree/main/packages/plugin',item['targetUrl'])

    def test_version_is_exact_and_missing_versions_do_not_fall_back(self):
        newer={**self.alpha,'version':'2.0.0','distributions':[{'id':'new','type':'git','url':'https://github.com/not/selected'}]}
        self.write(newer)
        (self.catalog/'sources/plugin/index.json').write_text(json.dumps({'packages':{'alpha':{'latest':'2.0.0'}}}))
        self.build('versions')
        self.assertEqual([],self.query().recommendations(self.alpha['id'])['items'])
        old=self.query().recommendations(self.alpha['id'],version='1.0.0');self.assertEqual('1.0.0',old['selectedVersion']);self.assertEqual(1,len(old['items']))
        self.assert_code('version_not_found',lambda:self.query().recommendations(self.alpha['id'],version='9.9'))

    def test_global_pagination_scope_binding_and_revision_switch(self):
        service=self.query();first=service.recommendations(source=self.source,limit=1)
        self.assertIsNotNone(first['nextCursor']);self.assertEqual(1,len(first['items']))
        self.next(content('https://github.com/o/new'));self.build('two')
        second=self.query().recommendations(source=self.source,limit=1,cursor=first['nextCursor'])
        self.assertEqual('one',second['revision']);self.assertEqual('active',second['items'][0]['status'])
        self.assertNotEqual(first['items'][0]['targetUrl'],second['items'][0]['targetUrl'])
        self.assert_code('cursor_mismatch',lambda:self.query().recommendations(source=self.source,status='active',limit=1,cursor=first['nextCursor']))
        self.assert_code('cursor_mismatch',lambda:self.query().recommendations(self.alpha['id'],limit=1,cursor=first['nextCursor']))

    def test_source_and_status_filters_missing_packages_and_invalid_inputs_are_explicit(self):
        self.next(content('https://github.com/o/beta'));self.build('withdrawn')
        self.assertEqual([],self.query().recommendations(source=self.source,status='active',package_id=self.alpha['id'])['items'])
        self.assertEqual(1,len(self.query().recommendations(source=self.source,status='withdrawn')['items']))
        self.assert_code('not_found',lambda:self.query().recommendations('missing'))
        self.assert_code('recommendation_source_not_found',lambda:self.query().recommendations(source='curated-source:'+'0'*64))
        for kwargs in [{},{'source':self.source,'status':'deleted'},{'source':self.source,'version':'1.0.0'},{'source':self.source,'limit':0}]:
            self.assert_code('invalid_query',lambda:self.query().recommendations(**kwargs))

    def test_dynamic_freshness_exposes_stale_collection_without_new_claims(self):
        from datetime import datetime
        when=datetime.fromisoformat(self.snapshot()['completed_at'].replace('Z','+00:00'))
        with patch('agent_forge_service.query.utc_now',return_value=when+timedelta(hours=25)):
            item=self.query().recommendations(self.alpha['id'])['items'][0]
            self.assertTrue(item['freshness']['stale']);self.assertEqual('active',item['status'])

    def test_source_directory_pagination_is_bound_to_limit_and_revision(self):
        self.clock.now+=timedelta(hours=1)
        alternate={**SOURCE,'emptyStatement':'No entries.'}
        with self.db:enqueue_recommendation(self.db,definition=alternate,slot=self.clock.now,now=self.clock.now)
        self.collect();self.build('two-sources')
        first=self.query().recommendation_sources(limit=1)
        second=self.query().recommendation_sources(limit=1,cursor=first['nextCursor'])
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        self.assertIsNone(second['nextCursor'])
        self.assert_code('cursor_mismatch',lambda:self.query().recommendation_sources(limit=2,cursor=first['nextCursor']))

    def test_http_routes_get_only_etag_exact_version_and_readonly_receiver(self):
        reader=HttpReadService(self.settings)
        result=reader.handle('GET','/api/v1/packages/'+self.alpha['id']+'/recommendations')
        self.assertEqual(200,result.status);self.assertEqual(1,len(json.loads(result.body)['items']))
        self.assertEqual(304,reader.handle('GET','/api/v1/packages/'+self.alpha['id']+'/recommendations',if_none_match=result.headers['ETag']).status)
        self.assertEqual(405,reader.handle('POST','/api/v1/recommendations',[('source',self.source)]).status)
        self.assertEqual(200,reader.handle('GET','/api/v1/recommendation-sources').status)
        self.assertEqual(200,reader.handle('GET','/api/v1/recommendations',[('source',self.source),('status','active')]).status)
        self.assertEqual(400,reader.handle('GET','/api/v1/recommendations').status)
        self.assertEqual(400,reader.handle('GET','/api/v1/recommendations',[('source',self.source),('source',self.source)]).status)
        self.assertEqual(400,reader.handle('GET','/api/v1/recommendations',[('source',self.source),('sort','source_rank')]).status)

    def test_public_bundle_import_and_cli_receiver_do_not_need_private_state_or_network(self):
        path,receipt=self.bundle();receiver=Settings(self.catalog,self.root/'receiver')
        imported=import_bundle(receiver,path,expected_bundle_sha256=receipt['bundleSha256'])
        activate_snapshot(receiver,'one',expected_manifest_sha256=imported['manifestSha256'],expected_pointer_sha256='absent')
        self.assertFalse(receiver.state_path.exists())
        with patch('subprocess.Popen',side_effect=AssertionError('query attempted network')):
            value=QueryService(receiver).recommendations(self.alpha['id'])
        self.assertEqual('active',value['items'][0]['status'])
        response=HttpReadService(receiver).handle('GET','/api/v1/recommendation-sources')
        self.assertEqual(200,response.status)
        run=subprocess.run([sys.executable,'-m','agent_forge_service.worker','recommendation-query','--runtime',str(receiver.runtime_root),'--id',self.alpha['id'],'--revision','one'],capture_output=True,text=True)
        self.assertEqual(0,run.returncode,run.stderr)
        self.assertEqual('one',json.loads(run.stdout)['revision']);self.assertFalse(receiver.state_path.exists())

    def test_rehashed_public_claim_cannot_change_status_rank_or_endorsement(self):
        for field,value in [('status','withdrawn'),('sourceRank',1),('endorsement','official')]:
            path,_=self.bundle();db=sqlite3.connect(path/'catalog.db')
            row=db.execute('SELECT source_id,target_url,data_json FROM curated_claims LIMIT 1').fetchone()
            item=json.loads(row[2]);item[field]=value
            db.execute('UPDATE curated_claims SET data_json=? WHERE source_id=? AND target_url=?',(encode_json(item),row[0],row[1]));db.commit();db.close()
            digest=self.resign(path)
            with self.subTest(field=field),self.assertRaises(ValueError):import_bundle(Settings(self.catalog,self.root/('reject-'+field)),path,expected_bundle_sha256=digest)

    def test_rehashed_source_labels_private_fields_or_changed_freshness_are_rejected(self):
        for field,value in [('credentialIdentity','profile'),('staleAfterSeconds',1),('labels',['official'])]:
            path,_=self.bundle();db=sqlite3.connect(path/'catalog.db')
            row=db.execute('SELECT id,data_json FROM curated_sources LIMIT 1').fetchone();item=json.loads(row[1]);item[field]=value
            db.execute('UPDATE curated_sources SET data_json=? WHERE id=?',(encode_json(item),row[0]));db.commit();db.close()
            digest=self.resign(path)
            with self.subTest(field=field),self.assertRaises(ValueError):import_bundle(Settings(self.catalog,self.root/('reject-source-'+field)),path,expected_bundle_sha256=digest)

    def test_rehashed_package_association_or_future_snapshot_is_rejected(self):
        for table in ['curated_package_links','curated_snapshots']:
            path,_=self.bundle();db=sqlite3.connect(path/'catalog.db')
            row=db.execute('SELECT rowid,data_json FROM '+table+' LIMIT 1').fetchone();item=json.loads(row[1])
            item['canonicalHash' if table=='curated_package_links' else 'completedAt']='f'*64 if table=='curated_package_links' else timestamp(NOW+timedelta(days=10))
            db.execute('UPDATE '+table+' SET data_json=? WHERE rowid=?',(encode_json(item),row[0]));db.commit();db.close()
            digest=self.resign(path)
            with self.subTest(table=table),self.assertRaises(ValueError):import_bundle(Settings(self.catalog,self.root/('reject-'+table)),path,expected_bundle_sha256=digest)

    def test_rehashed_extra_column_or_undeclared_feature_is_rejected(self):
        path,_=self.bundle();db=sqlite3.connect(path/'catalog.db');db.execute('ALTER TABLE curated_claims ADD COLUMN private_secret TEXT');db.commit();db.close()
        digest=self.resign(path)
        with self.assertRaises(ValueError):import_bundle(Settings(self.catalog,self.root/'reject-column'),path,expected_bundle_sha256=digest)
        path,_=self.bundle();manifest=json.loads((path/'manifest.json').read_text());manifest['features'].pop('recommendations');manifest.pop('recommendationCoverage')
        db=sqlite3.connect(path/'catalog.db');db.execute("UPDATE metadata SET value=? WHERE key='features'",(encode_json(manifest['features']),));db.execute("DELETE FROM metadata WHERE key='recommendationCoverage'");db.commit();db.close()
        atomic_json(path/'manifest.json',manifest);digest=self.resign(path)
        with self.assertRaises(ValueError):import_bundle(Settings(self.catalog,self.root/'reject-feature'),path,expected_bundle_sha256=digest)

    def test_legacy_revision_keeps_catalog_but_recommendations_are_unavailable(self):
        path,_=self.bundle();manifest=json.loads((path/'manifest.json').read_text());manifest['features'].pop('recommendations');manifest.pop('recommendationCoverage')
        db=sqlite3.connect(path/'catalog.db')
        for table in ['curated_package_links','curated_claims','curated_members','curated_snapshots','curated_sources']:db.execute('DROP TABLE '+table)
        db.execute("UPDATE metadata SET value=? WHERE key='features'",(encode_json(manifest['features']),));db.execute("DELETE FROM metadata WHERE key='recommendationCoverage'");db.commit();db.close()
        atomic_json(path/'manifest.json',manifest);digest=self.resign(path)
        receiver=Settings(self.catalog,self.root/'legacy');receipt=import_bundle(receiver,path,expected_bundle_sha256=digest)
        activate_snapshot(receiver,'one',expected_manifest_sha256=receipt['manifestSha256'],expected_pointer_sha256='absent')
        self.assertEqual(1,len(QueryService(receiver).packages()['items']))
        self.assert_code('recommendations_unavailable',lambda:QueryService(receiver).recommendations(self.alpha['id']))
        self.assertEqual(503,HttpReadService(receiver).handle('GET','/api/v1/recommendation-sources').status)


    def test_malformed_nonobject_json_is_rejected_with_value_error(self):
        for index,raw in enumerate(['null','[]','42','"private"','{"id":"a","id":"b"}']):
            path,_=self.bundle();db=sqlite3.connect(path/'catalog.db')
            db.execute('UPDATE curated_sources SET data_json=?',(raw,));db.commit();db.close()
            digest=self.resign(path)
            with self.subTest(raw=raw),self.assertRaises(ValueError):
                import_bundle(Settings(self.catalog,self.root/('malformed-'+str(index))),path,expected_bundle_sha256=digest)

    def test_member_scope_rank_and_completeness_cannot_be_rehashed_away(self):
        mutations=[lambda item:item['positions'][0].update(line=1),lambda item:item.update(sourceRank=1),
                   lambda item:item['positions'][0].update(url='https://github.com/wrong/target'),
                   lambda item:item.update(privateCredential='secret')]
        for index,edit in enumerate(mutations):
            path,_=self.bundle();db=sqlite3.connect(path/'catalog.db')
            row=db.execute('SELECT rowid,data_json FROM curated_members LIMIT 1').fetchone();item=json.loads(row[1]);edit(item)
            db.execute('UPDATE curated_members SET data_json=? WHERE rowid=?',(encode_json(item),row[0]));db.commit();db.close()
            digest=self.resign(path)
            with self.subTest(index=index),self.assertRaises(ValueError):
                import_bundle(Settings(self.catalog,self.root/('member-'+str(index))),path,expected_bundle_sha256=digest)
        path,_=self.bundle();db=sqlite3.connect(path/'catalog.db');db.execute('DELETE FROM curated_members WHERE rowid=(SELECT min(rowid) FROM curated_members)');db.commit();db.close()
        digest=self.resign(path)
        with self.assertRaises(ValueError):import_bundle(Settings(self.catalog,self.root/'missing-member'),path,expected_bundle_sha256=digest)

    def test_reappearance_preserves_past_withdrawal_and_bounded_citations(self):
        self.next(content('https://github.com/o/beta'));self.build('removed')
        removed=self.query().recommendations(self.alpha['id'])['items'][0]
        self.assertEqual('withdrawn',removed['status'])
        self.next(content(*(['https://github.com/o/alpha']*25),'https://github.com/o/beta'));self.build('reappeared')
        active=self.query().recommendations(self.alpha['id'])['items'][0]
        self.assertEqual('active',active['status']);self.assertEqual(removed['snapshotId'],active['previousSnapshotId'])
        self.assertEqual(25,active['citationCount']);self.assertEqual(20,len(active['citations']));self.assertTrue(active['citationsTruncated'])
        self.assertEqual(removed['snapshot'],self.query().recommendations(self.alpha['id'],revision='removed')['items'][0]['snapshot'])

    def test_conflicting_same_time_source_pointers_are_not_arbitrarily_ranked(self):
        from agent_forge_service.recommendation_projection import _claims
        import copy
        first=json.loads(self.public().execute('SELECT data_json FROM curated_snapshots').fetchone()[0])
        second=copy.deepcopy(first);second['id']='f'*64;second['commitSha']='f'*40;second['completedAt']=timestamp(self.clock.now+timedelta(minutes=5))
        with self.assertRaisesRegex(ValueError,'conflicting public curated pointer'):
            _claims({first['id']:first,second['id']:second},{first['id']:{},second['id']:{}})


if __name__=='__main__':unittest.main()
