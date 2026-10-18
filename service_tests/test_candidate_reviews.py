"""Local metadata triage CAS, neutral publication and unsigned operator evidence boundaries."""
from __future__ import annotations
import json,sqlite3,subprocess,sys,unittest
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from service_tests import test_discovery_projection as discovery_fixtures
from agent_forge_service.candidate_reviews import inspect_review,record_review,read_review,ACTIONS,fingerprint
from agent_forge_service.query import Query,QueryService
from agent_forge_service.errors import ServiceError
from agent_forge_service.storage import encode_json,open_state,timestamp
from agent_forge_service.bundles import export_bundle,import_bundle,activate_snapshot
from agent_forge_service.publishing import atomic_json,digest_file
from agent_forge_service.settings import Settings
from agent_forge_service.state_recovery import create_state_backup,restore_state_backup

class CandidateReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture=discovery_fixtures.DiscoveryProjectionTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.db=self.fixture.db;self.settings=self.fixture.settings;self.root=self.fixture.root
        self.fixture.collect([discovery_fixtures.repository(1,stargazers_count=10),discovery_fixtures.repository(2)])
        self.identity='github-repository:1';self.actor='private-operator-sentinel';self.counter=0
    def inspect(self,identity=None):return inspect_review(self.db,identity or self.identity,now=self.fixture.now)
    def record(self,action='reject',*,identity=None,request=None,head=None,fp=None,now=None):
        current=self.inspect(identity);self.counter+=1
        return record_review(self.db,candidate_id=identity or self.identity,expected_fingerprint=fp or current['evidenceFingerprint'],expected_head=head or current['expectedHead'],
            action=action,reason_code=ACTIONS[action][1],request_key=request or 'request-'+str(self.counter),operator_label=self.actor,now=now or self.fixture.now)
    def publish(self,revision='one',now=None):return self.fixture.publish(revision=revision,now=now)
    def item(self,identity=None,revision=None):return QueryService(self.settings).discovery(identity or self.identity,revision=revision)['item']
    def assert_code(self,code,call):
        with self.assertRaises(ServiceError) as error:call()
        self.assertEqual(code,error.exception.code)
    def resign(self,path):
        manifest=json.loads((path/'manifest.json').read_text());manifest['database'].update(sha256=digest_file(path/'catalog.db'),bytes=(path/'catalog.db').stat().st_size);atomic_json(path/'manifest.json',manifest)
        descriptor=json.loads((path/'bundle.json').read_text());descriptor['database'].update(sha256=digest_file(path/'catalog.db'),bytes=(path/'catalog.db').stat().st_size)
        descriptor['manifest'].update(sha256=digest_file(path/'manifest.json'),bytes=(path/'manifest.json').stat().st_size);atomic_json(path/'bundle.json',descriptor);return digest_file(path/'bundle.json')

    def test_local_record_is_metadata_only_and_public_actor_is_omitted(self):
        before=self.db.execute('SELECT count(*) FROM change_events').fetchone()[0];event=self.record()['decision']
        self.assertEqual(before,self.db.execute('SELECT count(*) FROM change_events').fetchone()[0]);manifest=self.publish()
        value=self.item();review=value['metadataReview'];self.assertEqual('rejected',review['status']);self.assertEqual('unreviewed',value['reviewStatus'])
        self.assertEqual('local-worker-metadata-triage',review['authority']);self.assertEqual(event,review['latestDecision'])
        self.assertEqual(1,manifest['counts']['packages'])
        self.assertNotIn(self.actor,json.dumps(value));self.assertNotIn(self.actor,(self.settings.snapshots/'one/catalog.db').read_bytes().decode('utf8',errors='ignore'))
        self.assertEqual(1,self.db.execute('PRAGMA user_version').fetchone()[0])

    def test_idempotent_request_returns_original_receipt_and_conflicts_do_not_append(self):
        initial=self.inspect();first=self.record(request='retry-key');self.fixture.now+=timedelta(seconds=1)
        replay=self.record(request='retry-key',head=initial['expectedHead'],fp=initial['evidenceFingerprint']);self.assertTrue(replay['replayed']);self.assertEqual(first['decision'],replay['decision'])
        self.assert_code('review_request_conflict',lambda:self.record('unsupported',request='retry-key',head=initial['expectedHead']))
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM candidate_review_decisions').fetchone()[0])

    def test_stale_head_and_changed_metadata_are_compare_and_set_conflicts(self):
        original=self.inspect();self.record()
        self.assert_code('review_head_conflict',lambda:self.record('unsupported',head='none'))
        self.fixture.collect([discovery_fixtures.repository(1,topics=['new-topic'])])
        self.assert_code('review_evidence_changed',lambda:self.record(fp=original['evidenceFingerprint']))
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM candidate_review_decisions').fetchone()[0])

    def test_review_does_not_change_canonical_or_raw_candidate_metadata(self):
        candidate=self.inspect()['metadataContext'];raw=self.db.execute('SELECT * FROM discovery_candidates ORDER BY github_id').fetchall()
        for action in ['request_metadata','unsupported','reject','reopen']:self.record(action)
        self.assertEqual(candidate,self.inspect()['metadataContext']);self.assertEqual(raw,self.db.execute('SELECT * FROM discovery_candidates ORDER BY github_id').fetchall())
        self.publish();self.assertEqual('pending',self.item()['metadataReview']['status']);self.assertEqual('unreviewed',self.item()['reviewStatus'])

    def test_reopen_requires_current_matching_decision(self):
        self.assert_code('review_transition_invalid',lambda:self.record('reopen'))
        self.record();self.record('reopen');self.assert_code('review_transition_invalid',lambda:self.record('reopen'))
        self.fixture.collect([discovery_fixtures.repository(1,topics=['changed'])]);self.assert_code('review_transition_invalid',lambda:self.record('reopen'))

    def test_counters_and_refresh_do_not_invalidate_metadata_review_but_topics_do(self):
        self.record();self.publish('old')
        self.fixture.collect([discovery_fixtures.repository(1,stargazers_count=200,forks_count=40)])
        self.publish('counters');self.assertEqual('rejected',self.item()['metadataReview']['status'])
        self.fixture.collect([discovery_fixtures.repository(1,topics=['mcp-server','terminal'])]);self.publish('changed')
        self.assertEqual('superseded',self.item()['metadataReview']['status']);self.assertFalse(self.item()['metadataReview']['evidenceMatches'])
        self.assertEqual('rejected',self.item(revision='old')['metadataReview']['status'])

    def test_reviewed_cutoff_is_not_backfilled_and_no_feature_when_empty(self):
        before=self.fixture.now;self.fixture.now+=timedelta(hours=1);self.record()
        old=self.publish('before',now=before);self.assertNotIn('discoveryReviews',old['features']);self.assertNotIn('metadataReview',self.item(revision='before'))
        self.publish('after');self.assertEqual('rejected',self.item()['metadataReview']['status'])

    def test_manifest_missing_metadata_cannot_be_approved_by_reopen(self):
        self.fixture.collect([discovery_fixtures.repository(3)])
        row=self.fixture.manifest(identity=3,fields={'name':'hello'});identity='manifest:'+row['id']
        self.record('request_metadata',identity=identity);self.record('reopen',identity=identity);self.publish()
        item=self.item(identity);self.assertEqual('needs_metadata',item['reviewStatus']);self.assertEqual('pending',item['metadataReview']['status'])
        self.assertTrue(item['issues']);self.assertNotIn('approved',json.dumps(item))

    def test_history_is_complete_in_public_table_and_dto_is_bounded(self):
        for index in range(22):self.record('reject' if index%2==0 else 'request_metadata');self.fixture.now+=timedelta(seconds=1)
        self.publish();review=self.item()['metadataReview'];self.assertEqual(22,review['historyCount']);self.assertEqual(20,len(review['history']));self.assertTrue(review['historyTruncated'])
        db=sqlite3.connect(self.settings.snapshots/'one/catalog.db');self.addCleanup(db.close);self.assertEqual(22,db.execute('SELECT count(*) FROM discovery_review_events').fetchone()[0])

    def test_dispositions_filter_whole_discovery_and_keep_default_visibility(self):
        self.record();self.publish();reader=QueryService(self.settings)
        self.assertEqual(2,len(reader.packages(Query(scope='discovery'))['items']))
        self.assertEqual([self.identity],[row['id'] for row in reader.packages(Query(scope='discovery',review_disposition='rejected'))['items']])
        self.assertEqual('github-repository:2',reader.packages(Query(scope='discovery',review_disposition='not_recorded'))['items'][0]['id'])
        self.assert_code('invalid_query',lambda:reader.packages(Query(review_disposition='rejected')))
        self.assert_code('invalid_query',lambda:reader.packages(Query(scope='discovery',review_disposition='approved')))

    def test_old_feature_does_not_pretend_no_reviews(self):
        self.publish();self.assert_code('reviews_unavailable',lambda:QueryService(self.settings).packages(Query(scope='discovery',review_disposition='not_recorded')))

    def test_bundle_receiver_works_without_private_state_and_replay_checks_hold(self):
        self.record();self.publish();path=self.root/'bundle';export=export_bundle(self.settings,path)
        receiver=Settings(self.root/'absent',self.root/'receiver');receipt=import_bundle(receiver,path,expected_bundle_sha256=export['bundleSha256'])
        activate_snapshot(receiver,'one',expected_manifest_sha256=receipt['manifestSha256'],expected_pointer_sha256='absent')
        self.assertFalse(receiver.state_path.exists());self.assertEqual('rejected',QueryService(receiver).discovery(self.identity)['item']['metadataReview']['status'])

    def test_rehashed_discovery_disposition_or_private_actor_is_rejected(self):
        self.record();self.publish()
        for index,modify in enumerate([lambda item:item['metadataReview'].update(status='pending'),lambda item:item['metadataReview'].update(operatorLabel='secret')]):
            path=self.root/('bundle-'+str(index));export_bundle(self.settings,path);db=sqlite3.connect(path/'catalog.db')
            row=db.execute('SELECT id,data_json FROM discoveries WHERE id=?',(self.identity,)).fetchone();item=json.loads(row[1]);modify(item)
            db.execute('UPDATE discoveries SET data_json=? WHERE id=?',(encode_json(item),row[0]));db.commit();db.close();digest=self.resign(path)
            with self.subTest(index=index),self.assertRaises(ValueError):import_bundle(Settings(self.root/'absent',self.root/('reject-'+str(index))),path,expected_bundle_sha256=digest)

    def test_private_event_or_context_mutation_preserves_previous_public_pointer(self):
        self.record();self.publish('good');before=digest_file(self.settings.snapshots/'current.json')
        with self.db:self.db.execute("UPDATE candidate_review_decisions SET reason_code='quality_score'")
        with self.assertRaises(ValueError):self.publish('broken')
        self.assertEqual(before,digest_file(self.settings.snapshots/'current.json'))

    def test_operator_input_rules_and_clock_backwards_do_not_append(self):
        head=self.inspect();self.record();self.fixture.now-=timedelta(seconds=1)
        self.assert_code('review_clock_wait',lambda:self.record('unsupported'))
        for fields in [dict(action='approve',reason_code='safe'),dict(operator_label='ghp_secret'),dict(request_key='ghp_secret')]:
            args=dict(candidate_id=self.identity,expected_fingerprint=head['evidenceFingerprint'],expected_head=head['expectedHead'],action='reject',reason_code=ACTIONS['reject'][1],request_key='new',operator_label=self.actor,now=self.fixture.now);args.update(fields)
            with self.subTest(fields=fields),self.assertRaises(ValueError):record_review(self.db,**args)
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM candidate_review_decisions').fetchone()[0])

    def test_backup_restore_retains_review_and_request_idempotency(self):
        original=self.inspect();event=self.record(request='restore-key')['decision'];receipt=create_state_backup(self.settings,self.root/'backup')
        restored=Settings(self.fixture.catalog,self.root/'restored');restore_state_backup(restored,self.root/'backup',expected_backup_sha256=receipt['backupSha256'])
        db=open_state(restored.state_path);self.addCleanup(db.close)
        result=record_review(db,candidate_id=self.identity,expected_fingerprint=original['evidenceFingerprint'],expected_head='none',action='reject',reason_code=ACTIONS['reject'][1],request_key='restore-key',operator_label=self.actor,now=self.fixture.now)
        self.assertTrue(result['replayed']);self.assertEqual(event,result['decision']);self.assertFalse(restored.snapshots.exists())

    def test_cli_inspect_is_readonly_and_record_requires_existing_state(self):
        command=[sys.executable,'-m','agent_forge_service.worker','candidate-review-inspect','--runtime',str(self.settings.runtime_root),'--candidate-id',self.identity]
        before=self.settings.state_path.read_bytes();result=subprocess.run(command,cwd=Path(__file__).resolve().parents[1],capture_output=True)
        self.assertEqual(0,result.returncode,result.stderr);data=json.loads(result.stdout);self.assertEqual('none',data['expectedHead'])
        self.assertEqual(before,self.settings.state_path.read_bytes());self.assertNotIn(self.actor,result.stdout.decode())
        record_command=[sys.executable,'-m','agent_forge_service.worker','candidate-review-record','--runtime',str(self.settings.runtime_root),'--candidate-id',self.identity,
            '--expected-evidence-sha256',data['evidenceFingerprint'],'--expected-head','none','--action','reject','--reason-code',ACTIONS['reject'][1],
            '--request-key','cli-key','--operator-label',self.actor]
        result=subprocess.run(record_command,cwd=Path(__file__).resolve().parents[1],capture_output=True);self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual('reject',json.loads(result.stdout)['decision']['action'])


    def test_replayed_request_after_metadata_change_does_not_reapply_old_decision(self):
        original=self.inspect();event=self.record(request='old-key')['decision']
        self.fixture.collect([discovery_fixtures.repository(1,topics=['changed'])])
        result=self.record(request='old-key',head='none',fp=original['evidenceFingerprint']);self.assertTrue(result['replayed']);self.assertEqual(event,result['decision'])
        self.publish();self.assertEqual('superseded',self.item()['metadataReview']['status'])
        self.assertEqual(1,self.db.execute('SELECT count(*) FROM candidate_review_decisions').fetchone()[0])

    def test_same_timestamp_order_is_sequence_bound_and_other_candidate_history_is_independent(self):
        first=self.record();other=self.record('unsupported',identity='github-repository:2');last=self.record('reopen')
        self.assertGreater(last['decision']['sequence'],other['decision']['sequence'])
        self.assertEqual(first['decision']['id'],last['decision']['previousId']);self.publish()
        self.assertEqual('pending',self.item()['metadataReview']['status']);self.assertEqual('unsupported',self.item('github-repository:2')['metadataReview']['status'])

    def test_malformed_public_review_shapes_and_event_chain_rehash_are_rejected(self):
        self.record();self.record('request_metadata');self.publish()
        for index,raw in enumerate(['null','[]','{"policyVersion":"x","policyVersion":"y"}']):
            path=self.root/('malformed-'+str(index));export_bundle(self.settings,path);db=sqlite3.connect(path/'catalog.db')
            db.execute('UPDATE discovery_review_events SET data_json=? WHERE seq=(SELECT max(seq) FROM discovery_review_events)',(raw,));db.commit();db.close();digest=self.resign(path)
            with self.subTest(raw=raw),self.assertRaises(ValueError):import_bundle(Settings(self.root/'none',self.root/('reject-malformed-'+str(index))),path,expected_bundle_sha256=digest)
        path=self.root/'missing-history';export_bundle(self.settings,path);db=sqlite3.connect(path/'catalog.db')
        db.execute('DELETE FROM discovery_review_events WHERE seq=(SELECT min(seq) FROM discovery_review_events)');db.commit();db.close();digest=self.resign(path)
        with self.assertRaises(ValueError):import_bundle(Settings(self.root/'none',self.root/'reject-chain'),path,expected_bundle_sha256=digest)

    def test_undeclared_feature_and_extra_private_column_are_rejected(self):
        self.record();self.publish()
        for case in ['feature','column']:
            path=self.root/('bad-'+case);export_bundle(self.settings,path);db=sqlite3.connect(path/'catalog.db')
            if case=='column':db.execute('ALTER TABLE discovery_review_events ADD COLUMN operator_label TEXT')
            else:
                manifest=json.loads((path/'manifest.json').read_text());manifest['features'].pop('discoveryReviews');manifest.pop('discoveryReviewCoverage')
                db.execute("UPDATE metadata SET value=? WHERE key='features'",(encode_json(manifest['features']),));db.execute("DELETE FROM metadata WHERE key='discoveryReviewCoverage'");atomic_json(path/'manifest.json',manifest)
            db.commit();db.close();digest=self.resign(path)
            with self.subTest(case=case),self.assertRaises(ValueError):import_bundle(Settings(self.root/'none',self.root/('reject-'+case)),path,expected_bundle_sha256=digest)

    def test_review_filter_cursor_scope_revision_and_http_get_only(self):
        self.record();self.record(identity='github-repository:2');self.publish('reviewed')
        reader=QueryService(self.settings);query=Query(scope='discovery',review_disposition='rejected',limit=1)
        first=reader.packages(query);self.assertTrue(first['nextCursor'])
        self.assert_code('cursor_mismatch',lambda:reader.packages(Query(scope='discovery',limit=1),cursor=first['nextCursor']))
        self.assertEqual(1,len(reader.packages(query,cursor=first['nextCursor'])['items']))
        from agent_forge_service.http_contract import HttpReadService
        http=HttpReadService(self.settings);before=self.settings.state_path.read_bytes()
        with patch('subprocess.run',side_effect=AssertionError('GET cannot fetch')):
            result=http.handle('GET','/api/v1/packages',[('scope','discovery'),('reviewDisposition','rejected')]);self.assertEqual(200,result.status)
            self.assertEqual(405,http.handle('POST','/api/v1/discoveries/'+self.identity,[('action','reject')]).status)
            self.assertEqual(400,http.handle('GET','/api/v1/packages',[('scope','discovery'),('reviewDisposition','rejected'),('reviewDisposition','pending')]).status)
        self.assertEqual(before,self.settings.state_path.read_bytes())



if __name__=='__main__':unittest.main()
