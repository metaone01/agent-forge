"""Public-only preflight checks over real isolated SQLite publications."""
import json
import subprocess
import sys
import unittest
from unittest.mock import patch
from agent_forge_service.preflight import inspect
from agent_forge_service.publishing import digest_file
from service_tests import test_publication_query as fixture


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.case=fixture.PublicationQueryTests('runTest');self.case.setUp();self.addCleanup(self.case.doCleanups)

    def test_absent_runtime_is_not_created_or_called_ready(self):
        path=self.case.settings.runtime_root
        self.assertFalse(path.exists())
        result=inspect(self.case.settings)
        self.assertFalse(result['ready']);self.assertIn('snapshot_unavailable',result['errors'])
        self.assertFalse(path.exists())

    def test_publication_validation_does_not_change_files_or_open_private_state(self):
        self.case.write(fixture.record());self.case.build()
        root=self.case.settings.runtime_root
        before={str(p.relative_to(root)):digest_file(p) for p in root.rglob('*') if p.is_file()}
        result=inspect(self.case.settings,allowed_origins=['https://example.test'])
        self.assertEqual('one',result['snapshot']['revision']);self.assertEqual(['https://example.test'],result['allowedOrigins'])
        self.assertFalse(result['httpStarted']);self.assertFalse(result['deploymentVerified'])
        after={str(p.relative_to(root)):digest_file(p) for p in root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)
        with patch('agent_forge_service.preflight.dependency_status',return_value={'fastapi':{'available':True,'version':'fixture'},'uvicorn':{'available':True,'version':'fixture'}}):
            self.assertTrue(inspect(self.case.settings)['ready'])

    def test_missing_dependencies_remain_explicit_even_when_snapshot_is_valid(self):
        self.case.write(fixture.record());self.case.build()
        with patch('agent_forge_service.preflight.dependency_status',return_value={'fastapi':{'available':False,'version':None}}):
            result=inspect(self.case.settings)
        self.assertFalse(result['ready']);self.assertEqual(['http_dependencies_missing'],result['errors'])
        self.assertEqual('one',result['snapshot']['revision'])

    def test_inactive_and_corrupt_snapshots_cannot_pass_readiness(self):
        self.case.write(fixture.record());self.case.build('one');self.case.build('two')
        result=inspect(self.case.settings,revision='one');self.assertIn('snapshot_not_current',result['errors'])
        database=self.case.settings.snapshots/'two/catalog.db'
        with database.open('ab') as target:target.write(b'corrupt')
        result=inspect(self.case.settings)
        self.assertFalse(result['ready']);self.assertIn('snapshot_unavailable',result['errors'])

    def test_cli_invalid_configuration_is_structured_and_nonzero(self):
        command=[sys.executable,'-m','agent_forge_service.preflight','--runtime',str(self.case.settings.runtime_root),'--allow-origin','*']
        from pathlib import Path
        process=subprocess.run(command,capture_output=True,cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(2,process.returncode,process.stderr)
        self.assertEqual(['invalid_configuration'],json.loads(process.stdout)['errors'])
        self.assertFalse(self.case.settings.runtime_root.exists())
