"""Whole-catalog exact canonical-version source filtering and stable pagination."""
from __future__ import annotations
import json,sqlite3,subprocess,sys,unittest
from pathlib import Path
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from service_tests import test_recommendation_projection as projection_fixtures
from service_tests.test_recommendations import SOURCE,content
from service_tests.test_publication_query import record
from agent_forge_service.query import Query,QueryService
from agent_forge_service.errors import ServiceError
from agent_forge_service.http_contract import HttpReadService
from agent_forge_service.recommendation_projection import public_source_identity
from agent_forge_service.storage import encode_json
from agent_forge_service.bundles import import_bundle,activate_snapshot
from agent_forge_service.publishing import atomic_json
from agent_forge_service.settings import Settings

class RecommendationFilterTests(unittest.TestCase):
    def setUp(self):
        self.case=projection_fixtures.ProjectionTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.settings=self.case.settings;self.source=self.case.source;self.alpha=self.case.alpha
    def result(self,**values):return QueryService(self.settings).packages(Query(recommendation_source=self.source,**values))
    def assert_code(self,code,call):
        with self.assertRaises(ServiceError) as caught:call()
        self.assertEqual(code,caught.exception.code)
    def add(self,name,**fields):
        value=record(name,**fields);value['distributions'][0]['url']='https://github.com/o/alpha';self.case.write(value);return value

    def test_filter_metadata_and_version_hash_are_explicit_without_endorsement(self):
        result=self.result();self.assertEqual([self.alpha['id']],[row['id'] for row in result['items']])
        self.assertTrue(result['recommendationFilteringAvailable']);meta=result['recommendationFilter']
        self.assertEqual(self.source,meta['sourceId']);self.assertEqual('active',meta['status']);self.assertEqual('canonical-latest-only',meta['canonicalVersionPolicy'])
        association=result['items'][0]['recommendationAssociation'];self.assertEqual('1.0.0',association['version']);self.assertIsNone(association['sourceRank'])
        self.assertEqual('not-inferred',association['endorsement']);self.assertEqual(64,len(association['canonicalHash']))
        relation=QueryService(self.settings).recommendations(self.alpha['id'])['items'][0]['packageAssociation']
        self.assertEqual(relation['canonicalHash'],association['canonicalHash'])

    def test_plain_catalog_has_no_implicit_source_filter(self):
        value=QueryService(self.settings).packages();self.assertIsNone(value['recommendationFilter'])
        self.assertNotIn('recommendationAssociation',value['items'][0])
        self.assertEqual('canonical-v1',value['sortPolicyVersion'])

    def test_filter_runs_before_limit_and_covers_all_members_once(self):
        wanted=[self.alpha['id']]
        for index in range(32):wanted.append(self.add(f'matched-{index:02}',identity=f'pkg/matched/{index:02}')['id'])
        for index in range(30):self.case.write(record(f'earlier-{index:02}',identity=f'pkg/unmatched/{index:02}',updated='2026-10-11T00:00:00Z'))
        self.case.build('many');query=Query(recommendation_source=self.source,sort='name',limit=20)
        one=QueryService(self.settings).packages(query);two=QueryService(self.settings).packages(query,cursor=one['nextCursor'],revision='many')
        self.assertEqual(20,len(one['items']));self.assertEqual(13,len(two['items']));self.assertIsNone(two['nextCursor'])
        actual=[row['id'] for row in one['items']+two['items']];self.assertEqual(set(wanted),set(actual));self.assertEqual(33,len(set(actual)))

    def test_latest_canonical_without_selected_link_excludes_old_version(self):
        latest=record('alpha',identity=self.alpha['id'],version='2.0.0');self.case.write(latest)
        (self.case.catalog/'sources/plugin/index.json').write_text(json.dumps({'packages':{'alpha':{'latest':'2.0.0'}}}),encoding='utf8')
        self.case.build('changed');self.assertEqual([],self.result()['items'])
        self.assertEqual(1,len(QueryService(self.settings).recommendations(self.alpha['id'],version='1.0.0')['items']))

    def test_unknown_selected_canonical_version_is_excluded(self):
        self.case.write({**self.alpha,'version':'2.0.0'})
        self.case.build('ambiguous');self.assertEqual([],self.result()['items'])
        item=QueryService(self.settings).packages()['items'][0];self.assertIsNone(item['canonicalLatest'])

    def test_repository_root_does_not_cover_an_unlisted_child_distribution(self):
        child=record('child');child['distributions'][0]['url']='https://github.com/o/alpha/tree/main/plugins/child';self.case.write(child)
        self.case.build('child');self.assertEqual([self.alpha['id']],[row['id'] for row in self.result()['items']])

    def test_multiple_matching_declared_urls_do_not_duplicate_package(self):
        self.alpha['links']={'repository':'https://github.com/o/alpha'}
        self.alpha['distributions'].append({'id':'mirror','type':'github-repo','url':'https://github.com/o/beta'})
        self.case.write(self.alpha);self.case.build('two-links')
        self.assertEqual(1,len(self.result()['items']));self.assertEqual(2,len(QueryService(self.settings).recommendations(self.alpha['id'])['items']))

    def test_withdrawal_excludes_active_filter_and_old_revision_stays_available(self):
        self.case.next(content('https://github.com/o/beta'));self.case.build('withdrawn')
        self.assertEqual([],self.result()['items'])
        old=QueryService(self.settings).packages(Query(recommendation_source=self.source),revision='one');self.assertEqual(1,len(old['items']))
        self.assertEqual('withdrawn',QueryService(self.settings).recommendations(self.alpha['id'])['items'][0]['status'])

    def test_source_filter_combines_agent_type_facet_and_search_before_paging(self):
        self.add('alpha-terminal',identity='other/alpha',agents=('claude-code',))
        self.case.build('filters')
        self.assertEqual(1,len(self.result(agent='dsh',type='plugin',facets={'capabilities':['gui.observe']},q='alpha')['items']))
        self.assertEqual('other/alpha',self.result(agent='claude-code',sort='relevance',q='terminal')['items'][0]['id'])
        self.assertEqual([],self.result(facets={'capabilities':['not-declared']})['items'])

    def test_all_supported_sorts_preserve_source_scope_and_null_metric_semantics(self):
        for sort in ['canonical_updated','first_seen','name','stars','star_growth','repo_pushed','downloads','download_growth','relevance']:
            extra={'metric_source':'npmjs.org'} if sort in ['downloads','download_growth'] else {'q':'terminal'} if sort=='relevance' else {}
            with self.subTest(sort=sort):
                result=self.result(sort=sort,**extra);self.assertEqual(1,len(result['items']));self.assertEqual(self.source,result['recommendationFilter']['sourceId'])
                self.assertEqual(sort,result['sort'])
        self.assert_code('sort_unavailable',lambda:self.result(sort='source_rank'))

    def test_cursor_binding_includes_source_filter_policy_and_original_sort(self):
        self.add('second');self.case.build('two');query=Query(recommendation_source=self.source,limit=1,sort='name')
        page=QueryService(self.settings).packages(query);self.assertTrue(page['nextCursor'])
        for changed in [Query(limit=1,sort='name'),replace(query,sort='first_seen'),replace(query,recommendation_source='curated-source:'+'a'*64)]:
            with self.subTest(changed=changed):self.assert_code('cursor_mismatch',lambda:QueryService(self.settings).packages(changed,cursor=page['nextCursor']))

    def test_missing_invalid_cross_scope_and_legacy_sources_are_explicit(self):
        self.assert_code('recommendation_source_not_found',lambda:QueryService(self.settings).packages(Query(recommendation_source='curated-source:'+'f'*64)))
        for value in ['wrong','curated-source:'+'A'*64,True,'']:
            with self.subTest(source=value):self.assert_code('invalid_query',lambda:QueryService(self.settings).packages(Query(recommendation_source=value)))
        self.assert_code('invalid_query',lambda:QueryService(self.settings).packages(Query(scope='discovery',recommendation_source=self.source)))
        path,_=self.case.bundle();manifest=json.loads((path/'manifest.json').read_text());manifest['features'].pop('recommendations');manifest.pop('recommendationCoverage')
        db=sqlite3.connect(path/'catalog.db')
        for table in ['curated_package_links','curated_claims','curated_members','curated_snapshots','curated_sources']:db.execute('DROP TABLE '+table)
        db.execute("UPDATE metadata SET value=? WHERE key='features'",(encode_json(manifest['features']),));db.execute("DELETE FROM metadata WHERE key='recommendationCoverage'");db.commit();db.close()
        atomic_json(path/'manifest.json',manifest);digest=self.case.resign(path)
        target=Settings(self.case.catalog,self.case.root/'legacy');receipt=import_bundle(target,path,expected_bundle_sha256=digest)
        activate_snapshot(target,'one',expected_manifest_sha256=receipt['manifestSha256'],expected_pointer_sha256='absent')
        self.assertEqual(1,len(QueryService(target).packages()['items']));self.assertFalse(QueryService(target).packages()['recommendationFilteringAvailable'])
        self.assert_code('recommendations_unavailable',lambda:QueryService(target).packages(Query(recommendation_source=self.source)))

    def test_http_get_parameters_etag_and_private_state_untouched(self):
        reader=HttpReadService(self.settings);before=self.settings.state_path.read_bytes()
        with patch('subprocess.run',side_effect=AssertionError('read-only query cannot fetch')):
            response=reader.handle('GET','/api/v1/packages',[('recommendationSource',self.source),('sort','name')]);self.assertEqual(200,response.status)
            self.assertEqual(304,reader.handle('GET','/api/v1/packages',[('recommendationSource',self.source),('sort','name')],if_none_match=response.headers['ETag']).status)
            self.assertEqual(400,reader.handle('GET','/api/v1/packages',[('recommendationSource',self.source),('recommendationSource',self.source)]).status)
            self.assertEqual(404,reader.handle('GET','/api/v1/packages',[('recommendationSource','curated-source:'+'f'*64)]).status)
            self.assertEqual(405,reader.handle('POST','/api/v1/packages',[('recommendationSource',self.source)]).status)
        self.assertEqual(before,self.settings.state_path.read_bytes())

    def test_cli_source_filter_uses_only_public_snapshot(self):
        result=subprocess.run([sys.executable,'-m','agent_forge_service.worker','query','--runtime',str(self.settings.runtime_root),'--recommendation-source',self.source],cwd=Path(__file__).resolve().parents[1],capture_output=True)
        self.assertEqual(0,result.returncode,result.stderr.decode());self.assertEqual(self.source,json.loads(result.stdout)['recommendationFilter']['sourceId'])

    def test_explain_uses_declared_link_and_claim_identity_indexes(self):
        self.case.next(content('https://github.com/o/alpha','https://github.com/o/beta',*(f'https://github.com/o/target-{index}' for index in range(98))));self.case.build('indexed')
        db=self.case.public("indexed");rows=db.execute("EXPLAIN QUERY PLAN SELECT p.id FROM packages p WHERE EXISTS(SELECT 1 FROM curated_package_links l JOIN curated_claims c ON c.source_id=l.source_id AND c.target_url=l.target_url WHERE l.package_id=p.id AND l.version=p.canonical_latest AND l.source_id=? AND c.status='active')",(self.source,)).fetchall()
        plan=' '.join(row['detail'] for row in rows);self.assertIn('SEARCH l USING',plan);self.assertIn('SEARCH c USING',plan)


    def test_distinct_source_statements_are_independent_filters(self):
        beta=record('beta');beta['distributions'][0]['url']='https://github.com/o/beta';self.case.write(beta)
        definition={**SOURCE,'selectionStatement':'A curated list of alternate tools.','label':'Alternate source'}
        self.case.clock.now+=timedelta(hours=8);self.case.commit='d'*40
        self.case.raw=content('https://github.com/o/beta').replace(b'# Source\n',('# Source\n'+definition['selectionStatement']+'\n').encode())
        self.case.enqueue(definition);self.case.collect();self.case.build('independent')
        source_b=public_source_identity(definition)
        a=QueryService(self.settings).packages(Query(recommendation_source=self.source,sort='name'))
        b=QueryService(self.settings).packages(Query(recommendation_source=source_b,sort='name'))
        self.assertEqual(2,len(a['items']));self.assertEqual([beta['id']],[row['id'] for row in b['items']])
        self.assertEqual(source_b,b['recommendationFilter']['sourceId']);self.assertEqual(['Alternate source'],b['recommendationFilter']['labels'])

    def test_filtered_cursor_survives_publish_and_retirement_is_explicit(self):
        from agent_forge_service.publishing import atomic_json,digest_file
        self.add('second');self.case.build('two');query=Query(recommendation_source=self.source,sort='name',limit=1)
        first=QueryService(self.settings).packages(query)
        self.case.next(content('https://github.com/o/beta'));self.case.build('three')
        second=QueryService(self.settings).packages(query,cursor=first['nextCursor']);self.assertEqual('two',second['revision']);self.assertEqual(1,len(second['items']))
        self.assertEqual([],QueryService(self.settings).packages(query)['items'])
        old=self.settings.snapshots/'two';retired=self.case.root/'retired-two'
        self.assertTrue(old.resolve().is_relative_to(self.case.root.resolve()));self.assertTrue(retired.resolve().is_relative_to(self.case.root.resolve()))
        old.rename(retired)
        self.assert_code('cursor_expired',lambda:QueryService(self.settings).packages(query,cursor=first['nextCursor']))


if __name__=='__main__':unittest.main()
