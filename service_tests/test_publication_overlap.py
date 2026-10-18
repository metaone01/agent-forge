"""Real subprocess publication alongside original QueryService readers."""
import json
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch
import subprocess
from agent_forge_service.publishing import digest_file
from tools.publication_overlap import run_overlap,main
from service_tests import test_publication_query as fixture


class PublicationOverlapTests(unittest.TestCase):
    def setUp(self):
        self.case=fixture.PublicationQueryTests('runTest');self.case.setUp();self.addCleanup(self.case.doCleanups)
        for number in range(35):self.case.write(fixture.record('mcp-'+str(number)))
        self.case.build()

    def test_real_writer_switches_current_while_old_pages_remain_valid(self):
        before=digest_file(self.case.settings.snapshots/'one/catalog.db')
        result=run_overlap(self.case.settings,self.case.root/'overlap',new_revision='two',workers=2,max_seconds=20,max_per_worker=3000,cursor_depth=2)
        self.assertTrue(result['passed'],result)
        self.assertEqual(0,result['writerExitCode']);self.assertEqual(0,result['summary']['errors'])
        self.assertGreater(result['overlapSummary']['requests'],0)
        self.assertEqual('two',result['currentRevisionAfter']);self.assertEqual('one',result['oldCursorRevisionAfter'])
        self.assertTrue(result['canonicalInputUnchanged']);self.assertTrue(result['oldSnapshotUnchanged'])
        self.assertEqual(before,digest_file(self.case.settings.snapshots/'one/catalog.db'))
        self.assertFalse(result['httpVerified']);self.assertFalse(result['productionCapacityVerified'])

    def test_reader_budget_exhaustion_does_not_become_success(self):
        result=run_overlap(self.case.settings,self.case.root/'limited',new_revision='limited',workers=1,max_seconds=20,max_per_worker=1,cursor_depth=0)
        self.assertFalse(result['passed']);self.assertTrue(any(result['readersHitBudget']))
        self.assertTrue(result['oldSnapshotUnchanged'])

    def test_existing_output_and_unsafe_paths_are_rejected(self):
        existing=self.case.root/'existing';existing.mkdir();(existing/'sentinel').write_text('keep')
        with self.assertRaises(ValueError):run_overlap(self.case.settings,existing)
        with self.assertRaises(ValueError):run_overlap(self.case.settings,self.case.settings.runtime_root/'forbidden')
        with redirect_stderr(StringIO()):
            self.assertEqual(2,main(['--catalog',str(self.case.catalog),'--output',str(existing)]))
        self.assertEqual('keep',(existing/'sentinel').read_text())
        self.assertFalse((self.case.settings.runtime_root/'forbidden').exists())

    def test_nonzero_writer_status_still_inspects_the_real_current_pointer(self):
        original=subprocess.Popen
        class NonzeroStatus:
            def __init__(self,*args,**kwargs):self.inner=original(*args,**kwargs)
            def __getattr__(self,name):return getattr(self.inner,name)
            @property
            def returncode(self):
                value=self.inner.returncode
                return 1 if value==0 else value
            def poll(self):
                value=self.inner.poll()
                return 1 if value==0 else value
        with patch('tools.publication_overlap.subprocess.Popen',NonzeroStatus):
            result=run_overlap(self.case.settings,self.case.root/'nonzero',new_revision='published',workers=1,max_seconds=20,max_per_worker=3000,cursor_depth=0)
        self.assertEqual(1,result['writerExitCode']);self.assertFalse(result['passed'])
        self.assertEqual('published',result['currentRevisionAfter'])
        self.assertTrue(result['oldSnapshotUnchanged'])
