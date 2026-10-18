"""Native FTS rank ordering must preserve the original full-sort page contract."""
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_forge_service.query import Query,QueryService,_encode_cursor
from agent_forge_service.publishing import atomic_json,digest_file
from agent_forge_service.storage import read_snapshot
from service_tests import test_publication_query as publication
from service_tests import test_recommendation_filter as sources
from service_tests import test_release_projection as releases


def full_sort_page(service,query,*,cursor=None,revision=None):
    """Reference SQL from before rank-stream optimization; no early truncation."""
    parameters=query.normalized(service.settings)
    manifest,decoded,binding=service._context(parameters,cursor,revision)
    conditions,values,joins,table=service._conditions(parameters,manifest)
    assert table is not None and parameters['sort']=='relevance'
    columns,extra=service._repository_columns(manifest);joins+=extra
    c,j=service._download_columns(manifest);columns+=c;joins+=j
    c,j=service._release_sort_columns(manifest);columns+=c;joins+=j
    source=parameters.get('recommendationSource')
    if source:
        values['recommendation_source']=source
        conditions.append("EXISTS(SELECT 1 FROM curated_package_links l JOIN curated_claims c ON c.source_id=l.source_id AND c.target_url=l.target_url WHERE l.package_id=p.id AND l.version=p.canonical_latest AND l.source_id=:recommendation_source AND c.status='active')")
        joins+=' JOIN package_versions selected_version ON selected_version.package_id=p.id AND selected_version.version=p.canonical_latest'
        columns+=',selected_version.payload_hash AS recommendation_canonical_hash,:recommendation_source AS recommendation_source'
    expression=table+'.rank'
    if decoded:
        null,key,identity=decoded['last'];values['last_id']=identity
        if null:conditions.append(f'({expression} IS NULL AND p.id>:last_id)')
        else:
            values['last_value']=key
            conditions.append(f'({expression} IS NULL OR {expression}>:last_value OR ({expression}=:last_value AND p.id>:last_id))')
    values['page_limit']=parameters['limit']+1
    sql=f"SELECT p.*{columns},{expression} AS sort_value,({expression} IS NULL) AS sort_null FROM packages p{joins} WHERE {' AND '.join(conditions)} ORDER BY {expression} ASC,p.id ASC LIMIT :page_limit"
    with service._connection(manifest['revision']) as db:rows=db.execute(sql,values).fetchall()
    page=rows[:parameters['limit']]
    next_cursor=None
    if len(rows)>len(page):
        row=page[-1];next_cursor=_encode_cursor(manifest['revision'],binding,[row['sort_null'],row['sort_value'],row['id']])
    return {'items':[service._summary(row) for row in page],'nextCursor':next_cursor,'revision':manifest['revision']}


def compare_pages(test,service,query,*,revision=None):
    cursor=None;seen=[]
    while True:
        expected=full_sort_page(service,query,cursor=cursor,revision=revision)
        actual=service.packages(query,cursor=cursor,revision=revision)
        test.assertEqual(expected['items'],actual['items']);test.assertEqual(expected['nextCursor'],actual['nextCursor'])
        test.assertEqual(expected['revision'],actual['revision'])
        seen.extend(item['id'] for item in actual['items']);cursor=actual['nextCursor']
        if cursor is None:break
    test.assertEqual(len(seen),len(set(seen)))
    return seen


class FtsRelevanceTests(unittest.TestCase):
    def setUp(self):
        self.case=publication.PublicationQueryTests('runTest');self.case.setUp();self.addCleanup(self.case.doCleanups)

    def populate(self,count=65):
        for number in range(count):
            value=publication.record('equal-'+str(number).zfill(3),identity='plugin/数据库/'+str(number).zfill(3),agents=('dsh',) if number%2 else ('other',))
            value['description']='terminal metadata tool 数据库工具'
            if number%3==0:value['facets']['capabilities']=['fs.read']
            self.case.write(value)
        self.case.build()
        return self.case.service()

    def mutate(self,callback):
        path=self.case.settings.snapshots/'one/catalog.db';db=sqlite3.connect(path);callback(db);db.commit();db.close()
        manifest=json.loads((path.parent/'manifest.json').read_text())
        manifest['database']['sha256']=digest_file(path);manifest['database']['bytes']=path.stat().st_size
        atomic_json(path.parent/'manifest.json',manifest)

    def test_equal_scores_unicode_ids_and_all_pages_match_original_cursors(self):
        service=self.populate()
        with service._connection('one') as db:
            ranks=[r[0] for r in db.execute("SELECT rank FROM packages_fts WHERE packages_fts MATCH 'terminal'")]
        self.assertEqual(1,len(set(ranks)))
        ids=compare_pages(self,service,Query(q='terminal',sort='relevance',limit=7))
        self.assertEqual(65,len(ids));self.assertEqual(sorted(ids),ids)

    def test_filters_are_applied_to_the_entire_match_stream(self):
        service=self.populate()
        query=Query(q='terminal',sort='relevance',limit=3,agent='dsh',type='plugin',facets={'capabilities':['fs.read']},custom_facets={'other':['personal-tag']})
        ids=compare_pages(self,service,query)
        self.assertEqual(11,len(ids))
        self.assertEqual([],service.packages(Query(q='terminal',sort='relevance',facets={'capabilities':['missing']}))['items'])

    def test_literal_multiword_and_chinese_trigram_scores_match(self):
        service=self.populate(13)
        for text in ['terminal tool','"terminal" OR 1=1 --','数据库工具']:
            with self.subTest(text=text):compare_pages(self,service,Query(q=text,sort='relevance',limit=4))

    def test_custom_fts_rank_configuration_is_preserved(self):
        self.populate(17)
        self.mutate(lambda db:db.execute("INSERT INTO packages_fts(packages_fts,rank) VALUES('rank','bm25(5.0,1.0,2.0,1.0)')"))
        compare_pages(self,self.case.service(),Query(q='terminal',sort='relevance',limit=4),revision='one')

    def test_missing_auxiliary_rows_do_not_truncate_the_page_early(self):
        self.populate(30)
        def change(db):
            db.execute("DELETE FROM package_sort_keys WHERE package_id IN (SELECT id FROM packages ORDER BY id LIMIT 8)")
            db.execute("DELETE FROM package_download_sort_keys WHERE package_id IN (SELECT id FROM packages ORDER BY id LIMIT 15)")
        self.mutate(change)
        ids=compare_pages(self,self.case.service(),Query(q='terminal',sort='relevance',limit=7),revision='one')
        self.assertEqual(15,len(ids));self.assertEqual('plugin/数据库/015',ids[0])

    def test_actual_sql_streams_native_rank_and_only_sorts_ties(self):
        service=self.populate(25);statements=[]
        def traced(path,timeout):
            db=read_snapshot(path,timeout);db.set_trace_callback(statements.append);return db
        with patch('agent_forge_service.query.read_snapshot',side_effect=traced):service.packages(Query(q='terminal',sort='relevance',limit=3))
        sql=next(s for s in statements if s.startswith('SELECT p.*'))
        self.assertIn('ORDER BY rank LIMIT -1',sql)
        with service._connection('one') as db:plan=[row[3] for row in db.execute('EXPLAIN QUERY PLAN '+sql)]
        self.assertTrue(any('CO-ROUTINE' in row for row in plan),plan)
        self.assertFalse(any(row.endswith('TEMP B-TREE FOR ORDER BY') for row in plan),plan)

    def test_old_cursor_keeps_its_revision_and_can_cross_query_implementations(self):
        service=self.populate(20);query=Query(q='terminal',sort='relevance',limit=6)
        old=full_sort_page(service,query)
        self.case.write(publication.record('new-one'));self.case.build('two',now=publication.NOW+publication.timedelta(hours=8))
        new=service.packages(query,cursor=old['nextCursor'])
        self.assertEqual('one',new['revision'])
        self.assertEqual(full_sort_page(service,query,cursor=old['nextCursor'])['items'],new['items'])
        self.assertEqual(new['nextCursor'],full_sort_page(service,query,cursor=old['nextCursor'])['nextCursor'])

    def test_source_filter_and_release_projection_preserve_original_pages(self):
        source=sources.RecommendationFilterTests('runTest');source.setUp();self.addCleanup(source.doCleanups)
        for number in range(26):source.add('source-'+str(number),identity='pkg/source/'+str(number))
        source.case.build('filtered')
        service=QueryService(source.settings)
        ids=compare_pages(self,service,Query(q='terminal',sort='relevance',limit=5,recommendation_source=source.source))
        self.assertEqual(27,len(ids))
        release=releases.ReleaseProjectionTests('runTest');release.setUp();self.addCleanup(release.doCleanups)
        release.collect();release.build()
        rows=compare_pages(self,QueryService(release.settings),Query(q='terminal',sort='relevance',limit=1))
        self.assertEqual(3,len(rows))
