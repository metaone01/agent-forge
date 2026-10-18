"""Real public snapshot packaging and stand-alone runtime/security checks."""
import hashlib
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import unittest

import yaml
from agent_forge_service.bundles import export_bundle
from service_tests import test_publication_query as fixture
from tools import package_query_service as delivery


class QueryDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.PublicationQueryTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.write(fixture.record('alpha'))
        self.case.build()
        self.bundle = self.case.root / 'public-bundle'
        self.exported = export_bundle(self.case.settings, self.bundle)
        self.data = self.case.root / 'data'
        self.data.mkdir()
        (self.data / 'manifest.json').write_text(json.dumps({'schemaVersion': 2, 'revision': 'one', 'sources': []}))
        (self.data / 'dashboard.json').write_text(json.dumps({'agents': [], 'totals': {'packages': 1, 'versions': 1}}))
        self.output = self.case.root / 'delivery'

    def make(self):
        return delivery.build(delivery.ROOT, self.data, self.bundle, self.exported['bundleSha256'], self.output)

    def reseal(self, descriptor):
        path = self.output / 'delivery.json'
        path.write_text(json.dumps(descriptor, ensure_ascii=False), encoding='utf-8')
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_complete_delivery_preserves_bundle_and_excludes_private_inputs(self):
        result = self.make()
        checked = delivery.verify(self.output, result['deliverySha256'])
        self.assertTrue(checked['valid'])
        self.assertEqual({'packages': 1, 'versions': 1}, checked['snapshot']['counts'])
        self.assertEqual((self.bundle/'catalog.db').read_bytes(), (self.output/'bundle/catalog.db').read_bytes())
        self.assertFalse((self.output/'sources').exists())
        self.assertFalse((self.output/'.venv').exists())
        self.assertFalse((self.output/'state.db').exists())
        self.assertTrue((self.output/'agent_forge_service/offline.py').is_file())
        self.assertTrue((self.output/'tools/snapshot.py').is_file())
        self.assertTrue((self.output/'agent_forge_service/retention.py').is_file())
        self.assertTrue((self.output/'agent_forge_service/reader_leases.py').is_file())
        self.assertTrue((self.output/'agent_forge_service/resource_budget.py').is_file())
        self.assertTrue((self.output/'agent_forge_service/operational_log.py').is_file())
        self.assertTrue((self.output/'agent_forge_service/refresh_status.py').is_file())
        self.assertTrue((self.output/'deploy/query-service/agent-forge-retention.timer').is_file())
        self.assertFalse((self.output/'site/data/dsh/plugin/index.json').exists())
        self.assertNotIn('@@', (self.output/'README-DEPLOYMENT.md').read_text(encoding='utf-8'))
        self.assertEqual(self.exported['bundleSha256'], result['snapshot']['bundleSha256'])

    def test_generated_worker_commands_match_the_actual_cli(self):
        from agent_forge_service.worker import parser
        self.make()
        commands = [line for line in (self.output/'README-DEPLOYMENT.md').read_text(encoding='utf-8').splitlines()
                    if line.startswith('python -m agent_forge_service.worker ')]
        self.assertEqual(10, len(commands))
        for command in commands:
            with self.subTest(command=command):
                self.assertIn(parser().parse_args(shlex.split(command)[3:]).command,
                              ('snapshot-import', 'snapshot-status', 'snapshot-activate', 'offline-export', 'offline-verify', 'retention-plan', 'retention-apply', 'retention-run', 'storage-status', 'refresh-status'))

    def test_bad_trusted_bundle_digest_does_not_create_output(self):
        with self.assertRaises(ValueError):
            delivery.build(delivery.ROOT, self.data, self.bundle, '0'*64, self.output)
        self.assertFalse(self.output.exists())

    def test_existing_and_protected_outputs_are_rejected_without_changes(self):
        self.output.mkdir()
        marker = self.output/'keep.txt'
        marker.write_text('preserve')
        for output in [self.output, delivery.ROOT/'.git/query-delivery-test', delivery.ROOT/'.venv/query-delivery-test', self.bundle/'nested']:
            with self.assertRaises(ValueError):
                delivery.build(delivery.ROOT, self.data, self.bundle, self.exported['bundleSha256'], output)
        self.assertEqual('preserve', marker.read_text())

    def test_extra_private_bundle_file_rejected_before_copy(self):
        (self.bundle/'state.db').write_bytes(b'private')
        with self.assertRaises(ValueError):
            self.make()
        self.assertFalse(self.output.exists())

    def test_changed_service_file_rejected(self):
        result = self.make()
        with (self.output/'agent_forge_service/app.py').open('ab') as stream:
            stream.write(b'\n# changed\n')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            delivery.verify(self.output, result['deliverySha256'])

    def test_extra_private_delivery_file_rejected(self):
        result = self.make()
        (self.output/'state.db').write_bytes(b'private')
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            delivery.verify(self.output, result['deliverySha256'])

    def test_missing_code_file_rejected(self):
        result = self.make()
        (self.output/'tools/validate.py').unlink()
        with self.assertRaises((ValueError, OSError)):
            delivery.verify(self.output, result['deliverySha256'])

    def test_traversal_absolute_alias_and_private_manifest_paths_rejected(self):
        self.make()
        descriptor = json.loads((self.output/'delivery.json').read_text())
        for name in ['../escape.py', '/absolute.py', 'site/../state.db', 'site\\assets\\styles.css', 'site/.git/config', 'sources/plugin/index.json']:
            with self.subTest(name=name):
                changed = json.loads(json.dumps(descriptor))
                changed['files'][0]['path'] = name
                trusted = self.reseal(changed)
                with self.assertRaisesRegex(ValueError, 'unsafe'):
                    delivery.verify(self.output, trusted)

    def test_duplicate_file_and_boolean_byte_count_rejected(self):
        self.make()
        descriptor = json.loads((self.output/'delivery.json').read_text())
        duplicate = json.loads(json.dumps(descriptor))
        duplicate['files'].append(duplicate['files'][0])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            delivery.verify(self.output, self.reseal(duplicate))
        descriptor['files'][0]['bytes'] = True
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            delivery.verify(self.output, self.reseal(descriptor))

    def test_resealed_wrong_snapshot_counts_rejected(self):
        self.make()
        descriptor = json.loads((self.output/'delivery.json').read_text())
        descriptor['snapshot']['counts']['packages'] += 1
        with self.assertRaisesRegex(ValueError, 'snapshot evidence'):
            delivery.verify(self.output, self.reseal(descriptor))

    def test_duplicate_json_keys_rejected(self):
        self.make()
        path = self.output/'delivery.json'
        original = path.read_text()
        path.write_text('{"schemaVersion":1,' + original[1:])
        trusted = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            delivery.verify(self.output, trusted)

    def test_standalone_modules_schemas_and_cli_do_not_import_original_repo(self):
        self.make()
        script = """from pathlib import Path
import agent_forge_service.app as app
import tools.project as project
import tools.validate as validate
from agent_forge_service.worker import parser
root=Path.cwd().resolve()
assert all(Path(module.__file__).resolve().is_relative_to(root) for module in (app,project,validate))
validate.build_validator('package.schema.json')
assert parser().parse_args(['snapshot-status','--runtime','not-created']).command=='snapshot-status'
assert not (root/'sources').exists()
print('standalone imports and schemas ready')
"""
        environment = {key:value for key,value in os.environ.items() if key.upper() not in ('PYTHONPATH','PYTHONHOME')}
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        result = subprocess.run([sys.executable, '-c', script], cwd=self.output, env=environment, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn('standalone imports', result.stdout)
        self.assertFalse(list(self.output.rglob('__pycache__')))

    def test_container_recipe_keeps_read_only_query_and_explicit_operator_inputs(self):
        config = yaml.safe_load((delivery.ROOT/'deploy/compose.yaml').read_text())['services']['query']
        self.assertTrue(config['read_only'])
        self.assertEqual('10001:10001', config['user'])
        self.assertEqual(['ALL'], config['cap_drop'])
        self.assertTrue(config['volumes'][0]['read_only'])
        self.assertFalse(config['volumes'][0]['bind']['create_host_path'])
        self.assertIn('127.0.0.1:', config['ports'][0])
        self.assertIn(':?', config['image'])
        self.assertEqual({'driver': 'local', 'options': {'max-size': '1m', 'max-file': '4'}}, config['logging'])
        service = (delivery.ROOT/'deploy/query-service/agent-forge-query.service').read_text()
        self.assertIn('LogsDirectory=agent-forge-query', service)
        self.assertIn('--log-directory /var/log/agent-forge-query', service)
        self.assertIn('ReadOnlyPaths=/srv/agent-forge/public', service)
        gateway = (delivery.ROOT/'deploy/query-service/nginx.conf.example').read_text()
        self.assertIn('access_log off;', gateway)
        self.assertIn('error_log /dev/null;', gateway)
        ignore = (delivery.ROOT/'deploy/query-service/dockerignore').read_text()
        self.assertEqual(ignore, (delivery.ROOT/'deploy/Dockerfile.dockerignore').read_text())
        self.assertNotIn('!bundle', ignore)
        self.assertNotIn('!site', ignore)
        self.assertNotIn('!tools/*.py', ignore)


if __name__ == '__main__':
    unittest.main()
