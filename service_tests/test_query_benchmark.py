"""Benchmark equivalence, actual read-only SQLite and bounded report paths."""
import json
import sqlite3
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from agent_forge_service.publishing import digest_file
from agent_forge_service.query import QueryService,Query
from tools.query_benchmark import ReusedConnectionExperiment,workload,run_phase,percentile,main
from service_tests import test_publication_query as fixture


class QueryBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.case=fixture.PublicationQueryTests('runTest');self.case.setUp();self.addCleanup(self.case.doCleanups)
        for name in ('alpha','beta','gamma'):self.case.write(fixture.record(name))
        self.manifest=self.case.build()
        self.service=QueryService(self.case.settings)

    def test_real_query_variants_have_same_results_and_keep_source_bytes(self):
        path=self.case.settings.snapshots/'one/catalog.db';before=digest_file(path)
        cases=workload(self.service,self.manifest,depth=2)
        for strategy in ('per_request','per_worker_reuse'):
            phase=run_phase(self.case.settings,'one',cases,workers=2,requests=2*len(cases),strategy=strategy)
            self.assertEqual(0,phase['summary']['errors']);self.assertEqual(2*len(cases),len(phase['records']))
            self.assertTrue(all(r['items']==cases[r['index']%len(cases)]['expectedItems'] for r in phase['records']))
            self.assertEqual(2,len(phase['preparationPerWorkerMs']))
        self.assertEqual(before,digest_file(path))

    def test_reuse_experiment_is_readonly_revision_bound_and_explicitly_closed(self):
        service=ReusedConnectionExperiment(self.case.settings,'one')
        try:
            with self.assertRaises(sqlite3.OperationalError):
                with service._connection('one') as db:db.execute('DELETE FROM packages')
            self.assertIsNone(service.connection)
            self.assertEqual(3,len(service.packages(Query(limit=20),revision='one')['items']))
            with self.assertRaises(ValueError):
                with service._connection('another'):pass
        finally:service.close()
        self.assertIsNone(service.connection)

    def test_result_mismatch_is_reported_instead_of_counted_success(self):
        cases=workload(self.service,self.manifest,depth=0)
        cases[0]['expectedSignature']='wrong'
        result=run_phase(self.case.settings,'one',cases[:1],workers=2,requests=4,strategy='per_request')
        self.assertEqual(4,result['summary']['errors'])
        self.assertTrue(all(row['error']=='result_mismatch' for row in result['records']))

    def test_new_report_path_is_required_and_protected_runtime_is_unchanged(self):
        existing=self.case.root/'report.json';existing.write_text('sentinel',encoding='utf-8')
        command=['--runtime',str(self.case.settings.runtime_root),'--revision','one','--requests','2','--workers','1','--cursor-depth','0']
        with redirect_stderr(StringIO()):
            self.assertEqual(2,main(command+['--output',str(existing)]))
            self.assertEqual(2,main(command+['--output',str(self.case.settings.runtime_root/'forbidden.json')]))
        self.assertEqual('sentinel',existing.read_text());self.assertFalse((self.case.settings.runtime_root/'forbidden.json').exists())

    def test_percentiles_are_nearest_rank_and_empty_is_explicit(self):
        self.assertIsNone(percentile([],.95));self.assertEqual(95,percentile(list(range(1,101)),.95))
