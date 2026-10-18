"""Retirement windows, guarded cleanup and active immutable readers."""
import contextlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from agent_forge_service.bundles import activate_snapshot, _pointer_hash
from agent_forge_service.errors import ServiceError
from agent_forge_service.http_contract import HttpReadService
from agent_forge_service.publishing import atomic_json, digest_file, SnapshotStore
from agent_forge_service.query import Query, QueryService
from agent_forge_service.retention import plan_cleanup, apply_cleanup, REGISTRY, DRAIN_SECONDS
from agent_forge_service.settings import Settings
from agent_forge_service.storage import read_snapshot, encode_json
from agent_forge_service.worker import main
from service_tests import test_publication_query as fixture

START = datetime(2026,10,15,8,tzinfo=timezone.utc)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.case=fixture.PublicationQueryTests('runTest');self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.settings=self.case.settings=replace(self.case.settings,snapshot_cursor_ttl_seconds=60)
        self.now=START
        self.clock=patch('agent_forge_service.retention.utc_now',side_effect=lambda:self.now);self.clock.start();self.addCleanup(self.clock.stop)
        self.case.write(fixture.record('alpha'));self.case.write(fixture.record('beta'))
        self.case.build('one')
        self.cursor=QueryService(self.settings).packages(Query(limit=1,sort='name'))['nextCursor']
        self.now+=timedelta(seconds=10);self.case.build('two')
        self.now+=timedelta(seconds=10);self.case.build('three')
        self.root=self.settings.snapshots

    def age(self):
        self.now=START+timedelta(seconds=151)

    def plan(self,**kwargs):
        return plan_cleanup(self.settings,keep_last=kwargs.pop('keep_last',1),**kwargs)

    def apply(self,receipt):
        return apply_cleanup(self.settings,receipt['plan'],expected_plan_sha256=receipt['planSha256'])

    def expect_code(self,code,function):
        with self.assertRaises(ServiceError) as caught:function()
        self.assertEqual(code,caught.exception.code)

    def test_explicit_registered_revision_does_not_require_an_active_pointer(self):
        (self.root/'current.json').unlink()
        self.assertEqual('one',QueryService(self.settings).packages(Query(limit=1),revision='one')['revision'])
        self.assertEqual('one',QueryService(self.settings).package('plugin.alpha',revision='one')['revision'])
        self.assertFalse((self.root/'current.json').exists())
        self.assertFalse((self.settings.runtime_root/'new-private.db').exists())

    def test_default_bounds_and_initial_format_stay_unchanged(self):
        self.assertEqual(86400,Settings(self.case.catalog,self.case.root/'unused').snapshot_cursor_ttl_seconds)
        for value in (True,0,59,604801):
            with self.assertRaises(ValueError):replace(self.settings,snapshot_cursor_ttl_seconds=value)
        for revision in ['one','two','three']:
            self.assertEqual(1,SnapshotStore(self.root).load(revision)['schemaVersion'])
        self.assertFalse((self.case.root/'unused').exists())

    def test_old_cursor_works_then_expires_while_database_is_still_retained(self):
        service=QueryService(self.settings)
        self.assertEqual('one',service.packages(Query(limit=1,sort='name'),cursor=self.cursor)['revision'])
        self.now=START+timedelta(seconds=71)
        self.expect_code('cursor_expired',lambda:service.packages(Query(limit=1,sort='name'),cursor=self.cursor))
        self.expect_code('revision_unavailable',lambda:service.packages(Query(limit=1),revision='one'))
        self.assertTrue((self.root/'one/catalog.db').exists())
        self.assertEqual('three',service.packages()['revision'])

    def test_expired_cursor_is_checked_before_cached_body_or_304(self):
        contract=HttpReadService(self.settings)
        pairs=[('limit','1'),('sort','name'),('cursor',self.cursor)]
        first=contract.handle('GET','/api/v1/packages',pairs)
        self.assertEqual(200,first.status)
        self.now=START+timedelta(seconds=71)
        second=contract.handle('GET','/api/v1/packages',pairs,if_none_match=first.headers['ETag'])
        self.assertEqual(410,second.status)
        self.assertEqual('cursor_expired',json.loads(second.body)['error']['code'])

    def test_http_cache_max_age_does_not_cross_retirement_deadline(self):
        self.now=START+timedelta(seconds=69)
        response=HttpReadService(self.settings).handle('GET','/api/v1/packages',[('limit','1'),('sort','name'),('cursor',self.cursor)])
        self.assertEqual(200,response.status)
        self.assertIn('max-age=1',response.headers['Cache-Control'])

    def test_plan_verification_work_is_bounded_as_well_as_deletions(self):
        from agent_forge_service import retention
        self.age()
        with patch('agent_forge_service.retention._verify_snapshot',wraps=retention._verify_snapshot) as verified:
            result=self.plan(max_remove=1)
        self.assertEqual(1,verified.call_count)
        self.assertEqual(2,result['plan']['dueCount'])
        self.assertEqual(1,result['plan']['inspectedCount'])

    def test_retirement_does_not_discard_a_valid_cached_body_etag(self):
        activate_snapshot(self.settings,'one',expected_manifest_sha256=digest_file(self.root/'one/manifest.json'),expected_pointer_sha256=_pointer_hash(self.root))
        service=HttpReadService(self.settings)
        pairs=[('limit','1'),('sort','name'),('cursor',self.cursor)]
        first=service.handle('GET','/api/v1/packages',pairs);self.assertEqual(200,first.status)
        self.now+=timedelta(seconds=1)
        activate_snapshot(self.settings,'three',expected_manifest_sha256=digest_file(self.root/'three/manifest.json'),expected_pointer_sha256=_pointer_hash(self.root))
        self.assertEqual(304,service.handle('GET','/api/v1/packages',pairs,if_none_match=first.headers['ETag']).status)

    def test_unattended_entry_is_dry_by_default_and_explicitly_bounded_when_applied(self):
        from agent_forge_service.retention import run_cleanup
        self.age();before=_pointer_hash(self.root)
        result=run_cleanup(self.settings,keep_last=1,max_remove=1)
        self.assertFalse(result['applied']);self.assertTrue((self.root/'one').exists())
        result=run_cleanup(self.settings,keep_last=1,max_remove=1,apply=True)
        self.assertTrue(result['complete']);self.assertEqual(1,len(result['deleted']))
        self.assertNotIn('plan',result);self.assertEqual(before,_pointer_hash(self.root))

    def test_timer_template_uses_a_separate_writer_and_explicit_apply(self):
        from agent_forge_service.worker import parser
        folder=Path(__file__).resolve().parents[1]/'deploy/query-service'
        service=(folder/'agent-forge-retention.service').read_text()
        self.assertIn('User=agent-forge-publisher',service)
        self.assertIn('Group=agent-forge-query',service);self.assertIn('UMask=0027',service)
        command=next(line.split(' -m agent_forge_service.worker ',1)[1] for line in service.splitlines() if line.startswith('ExecStart='))
        parsed=parser().parse_args(command.split())
        self.assertEqual('retention-run',parsed.command);self.assertTrue(parsed.apply)
        self.assertLessEqual(parsed.max_remove,20)
        self.assertIn('OnCalendar=hourly',(folder/'agent-forge-retention.timer').read_text())

    def test_readonly_plan_preserves_cursor_and_drain_windows(self):
        before={p:digest_file(p) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual([],self.plan()['plan']['candidates'])
        self.now=START+timedelta(seconds=129)
        self.assertEqual([],self.plan()['plan']['candidates'])
        self.now=START+timedelta(seconds=131)
        planned=self.plan()['plan']
        self.assertEqual(['one'],[r['revision'] for r in planned['candidates']])
        self.assertEqual(before,{p:digest_file(p) for p in before})
        self.assertFalse((self.root/'.trash').exists())

    def test_explicit_apply_removes_only_expired_unpinned_revisions(self):
        self.age();pointer=_pointer_hash(self.root);state=digest_file(self.settings.state_path)
        result=self.apply(self.plan())
        self.assertTrue(result['complete']);self.assertEqual(['one','two'],result['deleted'])
        self.assertTrue(result['currentPreserved']);self.assertEqual(pointer,_pointer_hash(self.root))
        self.assertFalse((self.root/'one').exists());self.assertFalse((self.root/'two').exists())
        self.assertTrue((self.root/'three/catalog.db').exists());self.assertEqual(state,digest_file(self.settings.state_path))
        self.assertEqual('three',QueryService(self.settings).packages()['revision'])

    def test_recent_count_and_max_remove_bound_the_work(self):
        self.age()
        planned=self.plan(keep_last=2,max_remove=1)
        self.assertEqual(1,len(planned['plan']['candidates']))
        self.assertTrue(self.apply(planned)['complete'])
        self.assertEqual(2,len([p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith('.')]))

    def test_open_reader_defers_cleanup_without_interrupting_its_sql(self):
        self.age()
        reader=read_snapshot(self.root/'one/catalog.db')
        try:
            result=self.apply(self.plan(max_remove=1))
            self.assertFalse(result['complete']);self.assertEqual([],result['deleted'])
            self.assertTrue((self.root/'one/catalog.db').exists())
            self.assertEqual(2,reader.execute('SELECT count(*) FROM packages').fetchone()[0])
        finally:reader.close()
        result=self.apply(self.plan(max_remove=1))
        self.assertTrue(result['complete']);self.assertFalse((self.root/'one').exists())

    def test_rollback_restarts_protection_and_invalidates_an_old_cleanup_plan(self):
        self.age();planned=self.plan();old_pointer=_pointer_hash(self.root)
        activate_snapshot(self.settings,'one',expected_manifest_sha256=digest_file(self.root/'one/manifest.json'),expected_pointer_sha256=old_pointer)
        self.expect_code('retention_conflict',lambda:self.apply(planned))
        self.assertEqual('one',QueryService(self.settings).packages(Query(limit=1,sort='name'),cursor=self.cursor)['revision'])
        self.assertTrue(all(r['revision'] not in ['one','three'] for r in self.plan()['plan']['candidates']))

    def test_shortening_new_setting_does_not_shorten_existing_protection(self):
        settings=replace(self.settings,snapshot_cursor_ttl_seconds=3600)
        self.now=START+timedelta(seconds=30)
        activate_snapshot(settings,'two',expected_manifest_sha256=digest_file(self.root/'two/manifest.json'),expected_pointer_sha256=_pointer_hash(self.root))
        before=json.loads((self.root/REGISTRY).read_text())['entries']['two']['protectedUntil']
        self.now+=timedelta(seconds=10)
        activate_snapshot(self.settings,'three',expected_manifest_sha256=digest_file(self.root/'three/manifest.json'),expected_pointer_sha256=_pointer_hash(self.root))
        self.assertEqual(before,json.loads((self.root/REGISTRY).read_text())['entries']['two']['protectedUntil'])

    def test_pointer_write_failure_keeps_previous_snapshot_and_protection(self):
        pointer=_pointer_hash(self.root)
        with patch('agent_forge_service.bundles.atomic_json',side_effect=OSError('pointer write failed')):
            with self.assertRaises(OSError):activate_snapshot(self.settings,'one',expected_manifest_sha256=digest_file(self.root/'one/manifest.json'),expected_pointer_sha256=pointer)
        self.assertEqual(pointer,_pointer_hash(self.root));self.assertEqual('three',QueryService(self.settings).packages()['revision'])
        self.assertTrue((self.root/'one/catalog.db').exists())

    def test_pointer_change_during_registry_write_cannot_bypass_cas(self):
        from agent_forge_service import retention
        saved=retention._save;expected=_pointer_hash(self.root)
        def external_pointer(root,registry):
            saved(root,registry)
            atomic_json(root/'current.json',{'revision':'two','manifestSha256':digest_file(root/'two/manifest.json'),'publishedAt':'external-write'})
        with patch('agent_forge_service.retention._save',side_effect=external_pointer):
            self.expect_code('publication_conflict',lambda:activate_snapshot(self.settings,'one',expected_manifest_sha256=digest_file(self.root/'one/manifest.json'),expected_pointer_sha256=expected))
        self.assertEqual('two',SnapshotStore(self.root).current()['revision'])

    def test_registry_write_failure_prevents_pointer_switch(self):
        pointer=_pointer_hash(self.root)
        with patch('agent_forge_service.retention._save',side_effect=OSError('registry unavailable')):
            with self.assertRaises(OSError):activate_snapshot(self.settings,'one',expected_manifest_sha256=digest_file(self.root/'one/manifest.json'),expected_pointer_sha256=pointer)
        self.assertEqual(pointer,_pointer_hash(self.root))

    def test_corrupt_registry_blocks_old_reads_and_cleanup_but_current_remains_readable(self):
        (self.root/REGISTRY).write_text('{bad')
        self.assertEqual('three',QueryService(self.settings).packages()['revision'])
        self.expect_code('retention_unavailable',lambda:QueryService(self.settings).packages(Query(limit=1),revision='one'))
        with self.assertRaises(ValueError):self.plan()
        self.assertTrue((self.root/'one/catalog.db').exists())

    def test_malformed_registry_time_returns_controlled_read_error(self):
        registry=json.loads((self.root/REGISTRY).read_text())
        registry['entries']['one']['protectedUntil']=False
        atomic_json(self.root/REGISTRY,registry)
        self.expect_code('retention_unavailable',lambda:QueryService(self.settings).packages(Query(limit=1),revision='one'))
        with self.assertRaises(ValueError):self.plan()
        self.assertTrue((self.root/'one/catalog.db').exists())

    def test_untracked_and_unverified_directories_are_kept(self):
        self.age();data=json.loads((self.root/REGISTRY).read_text());data['entries'].pop('one');atomic_json(self.root/REGISTRY,data)
        (self.root/'two/extra-private.txt').write_text('preserve')
        plan=self.plan()['plan'];self.assertEqual([],plan['candidates'])
        self.assertIn('untracked',[r['reason'] for r in plan['kept']]);self.assertIn('unverified',[r['reason'] for r in plan['kept']])
        self.assertTrue((self.root/'two/extra-private.txt').exists())

    def test_changed_database_is_not_deleted_even_after_a_plan_was_made(self):
        self.age();planned=self.plan()
        with (self.root/'one/catalog.db').open('ab') as stream:stream.write(b'changed')
        with self.assertRaises(ValueError):self.apply(planned)
        self.assertTrue((self.root/'one/catalog.db').exists())

    def test_wrong_anchor_runtime_and_expired_plans_cannot_delete(self):
        self.age();planned=self.plan()
        with self.assertRaises(ValueError):apply_cleanup(self.settings,planned['plan'],expected_plan_sha256='0'*64)
        other=Settings(self.case.catalog,self.case.root/'another')
        with self.assertRaises(ValueError):apply_cleanup(other,planned['plan'],expected_plan_sha256=planned['planSha256'])
        self.assertFalse(other.runtime_root.exists())
        self.now+=timedelta(seconds=601)
        with self.assertRaises(ValueError):self.apply(planned)
        self.assertTrue((self.root/'one/catalog.db').exists())

    def test_resigned_traversal_or_current_targets_are_rejected(self):
        self.age()
        for target in ['../outside','.staging','three',[],{},None]:
            receipt=self.plan();receipt['plan']['candidates'][0]['path']=target
            receipt['planSha256']=hashlib.sha256(encode_json(receipt['plan']).encode()).hexdigest()
            with self.subTest(target=target),self.assertRaises(ValueError):self.apply(receipt)
        self.assertTrue((self.root/'three/catalog.db').exists())

    def test_interrupted_cleanup_recovers_only_its_remaining_verified_files(self):
        self.age();original=Path.unlink
        def fail_manifest(path,*args,**kwargs):
            if path.name=='manifest.json' and '.trash' in path.parts:raise OSError('simulated unlink failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail_manifest):
            result=self.apply(self.plan(max_remove=1))
        self.assertFalse(result['complete']);self.assertFalse((self.root/'one').exists())
        remaining=list((self.root/'.trash').iterdir());self.assertEqual(1,len(remaining))
        self.assertEqual({'manifest.json'},{p.name for p in remaining[0].iterdir()})
        recovery=self.plan(max_remove=1)
        self.assertEqual('quarantine',recovery['plan']['candidates'][0]['location'])
        self.assertTrue(self.apply(recovery)['complete']);self.assertFalse(remaining[0].exists())

    def test_unknown_quarantine_children_are_preserved(self):
        self.age();trash=self.root/'.trash';trash.mkdir()
        unknown=trash/'not-owned';unknown.mkdir();(unknown/'secret').write_text('preserve')
        result=self.apply(self.plan())
        self.assertTrue(result['complete']);self.assertEqual('preserve',(unknown/'secret').read_text())

    def test_cli_requires_a_plan_and_reports_busy_as_nonzero(self):
        self.age();plan_file=self.case.root/'cleanup.json';stream=io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(0,main(['retention-plan','--runtime',str(self.settings.runtime_root),'--keep-last','1','--max-remove','1','--output',str(plan_file)]))
        receipt=json.loads(stream.getvalue());self.assertFalse(receipt['deleted'])
        reader=read_snapshot(self.root/'one/catalog.db')
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                status=main(['retention-apply','--runtime',str(self.settings.runtime_root),'--plan',str(plan_file),'--expected-plan-sha256',receipt['planSha256']])
            self.assertEqual(3,status)
        finally:reader.close()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0,main(['retention-apply','--runtime',str(self.settings.runtime_root),'--plan',str(plan_file),'--expected-plan-sha256',receipt['planSha256']]))

    def test_plan_of_absent_runtime_does_not_create_it(self):
        settings=Settings(self.case.catalog,self.case.root/'missing')
        with self.assertRaises(OSError):plan_cleanup(settings)
        self.assertFalse(settings.runtime_root.exists())


if __name__=='__main__':unittest.main()
