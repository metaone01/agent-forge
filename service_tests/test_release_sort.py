"""Full snapshot ordering and replay protection for explicit canonical releases."""
import json
import sqlite3
import unittest
from datetime import timedelta
from agent_forge_service.query import Query, QueryService
from agent_forge_service.release_sort import POLICY
from agent_forge_service.settings import Settings
from agent_forge_service.bundles import export_bundle, import_bundle
from service_tests import test_release_projection as release_fixture
from service_tests.test_publication_query import record
from service_tests.test_releases import item


class ReleaseSortTests(unittest.TestCase):
    def setUp(self):
        self.case = release_fixture.ReleaseProjectionTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.service = QueryService(self.case.settings)

    def all(self, **filters):
        result = []; cursor = None
        while True:
            page = self.service.packages(Query(sort='latest_release',limit=1,**filters),cursor=cursor)
            result.extend(page['items'])
            cursor = page['nextCursor']
            if cursor is None:
                return result

    def test_explicit_association_unknown_last_ties_and_detail(self):
        self.case.collect(); manifest=self.case.build()
        self.assertTrue(manifest['features']['releaseSort'])
        self.assertEqual(POLICY,manifest['releaseSortPolicyVersion'])
        page=self.service.packages(Query(sort='latest_release',limit=1))
        self.assertEqual(POLICY,page['sortPolicyVersion'])
        self.assertEqual('canonical-latest-explicit-distribution',page['sortScope'])
        rows=self.all()
        self.assertEqual(['plugin.alpha','plugin.beta','plugin.gamma'],[r['id'] for r in rows])
        self.assertEqual(['known','known','unknown'],[r['latestRelease']['status'] for r in rows])
        proof=rows[0]['latestRelease']
        self.assertEqual('2.0.0',proof['canonicalVersion'])
        self.assertEqual('v1.0.0',proof['selected']['tagName'])
        self.assertIsNone(rows[0]['observedLatest'])
        self.assertEqual(2,page['releaseSortCoverage']['known'])
        self.assertEqual(proof,self.service.package('plugin.alpha')['latestRelease'])
        self.case.code('cursor_mismatch',lambda:self.service.packages(Query(sort='name',limit=1),cursor=page['nextCursor']))

    def test_historical_exact_link_cannot_replace_canonical_context(self):
        self.case.collect()
        path=self.case.packages/'alpha2.json'
        value=json.loads(path.read_text());value['distributions']=[{'id':'source','type':'git','url':'https://github.com/owner/repo'}]
        path.write_text(json.dumps(value),encoding='utf-8')
        old=json.loads((self.case.packages/'alpha.json').read_text());old['distributions']=[{'id':'release','type':'release','url':'https://github.com/owner/repo/releases/tag/v1.0.0'}]
        (self.case.packages/'alpha.json').write_text(json.dumps(old),encoding='utf-8')
        self.case.build()
        self.assertEqual(['plugin.beta','plugin.alpha','plugin.gamma'],[r['id'] for r in self.all()])
        self.assertEqual('unknown',self.service.package('plugin.alpha')['latestRelease']['status'])
        self.assertIn('exact_declared_release',[p['kind'] for p in self.case.query('plugin.alpha',version='1.0.0',association='declared')['items'][0]['association']['assertions']])

    def test_filters_search_and_full_directory_pagination(self):
        for number in range(25):
            value=record('indexed-'+str(number).zfill(2),agents=('other',) if number%2 else ('dsh',))
            value['updatedAt']='2026-10-01T00:00:00Z'
            value['distributions']=[{'id':'release','type':'release','url':'https://github.com/owner/repo/releases/tag/v1.0.0'}]
            (self.case.packages/('extra'+str(number)+'.json')).write_text(json.dumps(value),encoding='utf-8')
        self.case.collect();self.case.build()
        filtered=self.all(q='indexed',agent='dsh',type='plugin',facets={'capabilities':['gui.observe']})
        self.assertEqual(13,len(filtered));self.assertEqual(13,len({r['id'] for r in filtered}))
        self.assertTrue(all(r['latestRelease']['status']=='known' for r in filtered))
        full=self.all();self.assertEqual(28,len(full));self.assertEqual('plugin.gamma',full[-1]['id'])

    def test_multiple_declared_releases_prerelease_and_future_exclusion(self):
        path=self.case.packages/'alpha2.json';value=json.loads(path.read_text())
        for identity,tag in [(2,'v2'),(3,'future')]:
            value['distributions'].append({'id':'release'+str(identity),'type':'release','url':'https://github.com/owner/repo/releases/tag/'+tag})
        path.write_text(json.dumps(value),encoding='utf-8')
        self.case.collect([item(),{**item(2,prerelease=True),'tag_name':'v2','published_at':'2026-10-11T00:00:00Z'},
            {**item(3),'tag_name':'future','published_at':'2027-01-01T00:00:00Z'}])
        self.case.build()
        proof=self.service.package('plugin.alpha')['latestRelease']
        self.assertEqual('2',proof['selected']['releaseId']);self.assertTrue(proof['selected']['prerelease'])
        self.assertEqual(3,proof['candidateCount']);self.assertEqual(2,proof['eligibleCount']);self.assertEqual(1,proof['futureCount'])

    def test_future_only_unknown_and_same_tag_identity_ambiguity(self):
        self.case.collect([{**item(),'published_at':'2027-01-01T00:00:00Z'}]);self.case.build('future')
        self.assertEqual('future_published_time',self.service.package('plugin.beta')['latestRelease']['reason'])
        self.case.collect([item(),item(2)]);self.case.build('ambiguous')
        self.assertTrue(all(r['latestRelease']['status']=='unknown' for r in self.all()))

    def test_receiver_rebuild_verification_rejects_tampering(self):
        self.case.collect();self.case.build()
        changes=[lambda db:db.execute("UPDATE public_release_sort_keys SET published_at='2099-01-01T00:00:00.000000Z'"),
            lambda db:db.execute('DELETE FROM public_release_sort_keys'),
            lambda db:db.execute('DROP INDEX public_release_package_rank'),
            lambda db:db.execute('ALTER TABLE public_release_sort_keys ADD COLUMN private_identity TEXT'),
            lambda db:db.execute("UPDATE public_release_sort_keys SET data_json='{}'"),
            lambda db:db.execute("UPDATE public_release_sort_keys SET data_json='[]'"),
            lambda db:db.execute("UPDATE public_release_sort_keys SET data_json=replace(data_json,'\"candidateCount\":1','\"candidateCount\":true')") ]
        for index,change in enumerate(changes):
            with self.subTest(index=index):
                target=self.case.root/('bad-sort-'+str(index));export_bundle(self.case.settings,target)
                checksum=self.case.resign(target,change)
                with self.assertRaises(ValueError):import_bundle(Settings(self.case.root/'absent',self.case.root/('reject-'+str(index))),target,expected_bundle_sha256=checksum)

    def test_public_receiver_and_no_release_sort_capability(self):
        self.case.build('empty')
        self.case.code('sort_unavailable',lambda:self.service.packages(Query(sort='latest_release')))
        self.case.code('sort_unavailable',lambda:Query(scope='discovery',sort='latest_release').normalized(self.case.settings))
        self.case.collect();self.case.build('ready')
        directory=self.case.root/'bundle';receipt=export_bundle(self.case.settings,directory)
        receiver=Settings(self.case.root/'absent',self.case.root/'receiver')
        import_bundle(receiver,directory,expected_bundle_sha256=receipt['bundleSha256'])
        self.assertEqual('plugin.alpha',QueryService(receiver).packages(Query(sort='latest_release'),revision='ready')['items'][0]['id'])
        self.assertFalse(receiver.state_path.exists())

    def test_query_plan_uses_materialized_order_index(self):
        self.case.collect();self.case.build()
        with self.service._connection('one') as db:
            plan=[r[3] for r in db.execute('EXPLAIN QUERY PLAN SELECT p.id FROM public_release_sort_keys rs INDEXED BY public_release_package_rank CROSS JOIN packages p ON p.id=rs.package_id ORDER BY rs.published_at DESC,rs.package_id ASC LIMIT 20')]
        self.assertTrue(any('public_release_package_rank' in row for row in plan),plan)
        self.assertFalse(any('TEMP B-TREE' in row for row in plan),plan)

    def test_sorted_http_parameters_and_explicit_discovery_rejection(self):
        from agent_forge_service.http_contract import HttpReadService
        self.case.collect();self.case.build()
        reader=HttpReadService(self.case.settings)
        response=reader.handle('GET','/api/v1/packages',[('sort','latest_release'),('agent','dsh'),('type','plugin'),('limit','1')])
        self.assertEqual(200,response.status)
        body=json.loads(response.body);self.assertEqual('plugin.alpha',body['items'][0]['id'])
        self.assertEqual(POLICY,body['sortPolicyVersion'])
        self.assertEqual(400,reader.handle('GET','/api/v1/packages',[('scope','discovery'),('sort','latest_release')]).status)
        self.assertEqual(400,reader.handle('GET','/api/v1/packages',[('sort','latest_release'),('metricSource','npmjs.org')]).status)

    def test_equal_time_release_tie_uses_numeric_identity(self):
        path=self.case.packages/'alpha2.json';value=json.loads(path.read_text())
        value['distributions'] += [{'id':'two','type':'release','url':'https://github.com/owner/repo/releases/tag/two'},
            {'id':'ten','type':'release','url':'https://github.com/owner/repo/releases/tag/ten'}]
        # Exclude the old v1 declaration from this particular tie.
        value['distributions']=value['distributions'][1:]
        path.write_text(json.dumps(value),encoding='utf-8')
        self.case.collect([{**item(10),'tag_name':'ten'},{**item(2),'tag_name':'two'}]);self.case.build()
        self.assertEqual('2',self.service.package('plugin.alpha')['latestRelease']['selected']['releaseId'])
