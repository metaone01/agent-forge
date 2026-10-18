"""Curated-source evidence, complete-snapshot withdrawals and durable read-only collection."""
from __future__ import annotations
import base64
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_forge_service.errors import ServiceError
from agent_forge_service.github import GitHubRequest
from agent_forge_service.github_client import CollectionError, RequestPolicy
from agent_forge_service.public_git import PublicGitRequest, sanitize_object
from agent_forge_service.recommendations import (MAX_LIST_BYTES, GhRecommendationTransport, RecommendationClient, RecommendationGitRequest,
    enqueue_recommendation, load_source, parse_curated_list, read_recommendation_status, run_recommendation_jobs, source_identity, validate_source)
from agent_forge_service.settings import Settings
from agent_forge_service.state_recovery import create_state_backup, inspect_state_backup, restore_state_backup
from agent_forge_service.storage import encode_json, open_state, remember_package, timestamp

NOW = datetime(2026, 10, 12, 0, tzinfo=timezone.utc)
COMMIT, TREE = 'a' * 40, 'b' * 40
SOURCE = {'schemaVersion':1,'provider':'github-curated-markdown-v1','repositoryId':123,'path':'README.md','label':'Source curated tools',
    'selectionStatement':'A curated list of public tools.','startHeading':'## Servers','endHeading':'## Frameworks','minEntries':1,'maxEntries':100,'emptyStatement':None}
PUBLIC = {'id':123,'full_name':'source/list','private':False,'default_branch':'main'}


def content(*targets, extra='', empty=False):
    rows = ['# Source', SOURCE['selectionStatement'], SOURCE['startHeading'], '* tools - [Category](#tools)', '### Tools']
    rows += ['- ['+url.rstrip('/').split('/')[-1]+']('+url+') - prose is not an indexed recommendation body' for url in targets]
    rows += [extra, 'No entries.' if empty else '', SOURCE['endHeading'], '- [excluded](https://github.com/elsewhere/excluded)']
    return ('\n'.join(rows)+'\n').encode()


def blob(raw):
    sha = hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()
    return {'sha':sha,'size':len(raw),'encoding':'base64','content':base64.b64encode(raw).decode()}


def wire(body=None, status=200, headers=None):
    lines = ['HTTP/2.0 '+str(status)+' status'] + [key+': '+str(value) for key,value in (headers or {}).items()]
    return ('\r\n'.join(lines)+'\r\n\r\n'+(json.dumps(body) if body is not None else '')).encode()


class Clock:
    def __init__(self): self.now = NOW
    def __call__(self):
        self.now += timedelta(seconds=2)
        return self.now


class ParsingTests(unittest.TestCase):
    def parse(self, raw=None, definition=None):
        return parse_curated_list(raw or content('https://github.com/owner/tool'), definition or SOURCE)

    def test_complete_scope_excludes_navigation_and_frameworks_and_never_infers_rank(self):
        result = self.parse()
        self.assertTrue(result['complete']);self.assertEqual(1,result['entryCount'])
        self.assertEqual('curated_listing',result['claimKind']);self.assertIsNone(result['sourceRank'])
        self.assertEqual(['https://github.com/owner/tool'],[member['targetUrl'] for member in result['members']])
        self.assertEqual('### Tools',result['members'][0]['positions'][0]['section'])
        self.assertNotIn('prose',encode_json(result))

    def test_duplicate_and_case_aliases_keep_all_citations_without_duplicate_members(self):
        result = self.parse(content('https://github.com/Owner/Tool','https://github.com/owner/tool/'))
        self.assertEqual(2,result['entryCount']);self.assertEqual(1,len(result['members']))
        self.assertEqual(2,len(result['members'][0]['positions']))

    def test_readme_fragment_is_citation_but_tree_and_blob_paths_keep_distinct_subjects(self):
        result=self.parse(content('https://github.com/o/r#readme','https://github.com/o/r?tab=readme-ov-file#mcp','https://github.com/o/r/tree/main','https://github.com/o/r/tree/main/tool','https://github.com/o/r/blob/main/tool.md'))
        self.assertEqual(4,len(result['members']));self.assertEqual(5,result['entryCount'])
        self.assertEqual(2,len(result['members'][0]['positions']))

    def test_code_examples_and_comments_do_not_become_curated_members(self):
        extra='```markdown\n- [example](https://github.com/invalid/example)\n```\n<!-- - [comment](https://github.com/invalid/comment) -->'
        self.assertEqual(1,len(self.parse(content('https://github.com/o/r',extra=extra))['members']))

    def test_missing_ambiguous_or_reordered_boundaries_reject_complete_claim(self):
        raw=content('https://github.com/o/r')
        for value in (raw.replace(b'## Servers',b'## Renamed'),raw+b'## Servers\n',raw.replace(b'## Servers',b'## Frameworks').replace(b'## Frameworks\n- [excluded]',b'## Servers\n- [excluded]')):
            with self.subTest(value=value),self.assertRaises(ValueError):self.parse(value)

    def test_statement_must_still_exist_in_the_public_file(self):
        with self.assertRaisesRegex(ValueError,'statement'):self.parse(content('https://github.com/o/r').replace(SOURCE['selectionStatement'].encode(),b'Ordinary links'))

    def test_empty_requires_explicit_source_statement(self):
        with self.assertRaises(ValueError):self.parse(content())
        definition={**SOURCE,'emptyStatement':'No entries.'}
        self.assertEqual([],self.parse(content(empty=True),definition)['members'])
        with self.assertRaises(ValueError):self.parse(content(),definition)

    def test_unknown_bullet_syntax_is_rejected_instead_of_withdrawing_silently(self):
        for extra in ['- tool without a primary link','- [tool][reference]','- <a href="https://github.com/o/r">tool</a>']:
            with self.subTest(extra=extra),self.assertRaises(ValueError):self.parse(content('https://github.com/o/r',extra=extra))

    def test_public_target_urls_are_strict_and_do_not_admit_tokens_or_other_hosts(self):
        for target in ['http://github.com/o/r','https://user@github.com/o/r','https://github.com:443/o/r','https://evil.test/o/r','https://github.com/o/r?token=secret','https://github.com/o/r/tree/main/..','https://github.com/o/r/issues','https://github.com/o/r/tree/main/a%2fb']:
            with self.subTest(target=target),self.assertRaises(ValueError):self.parse(content(target))

    def test_limits_and_invalid_utf8_control_characters_are_rejected(self):
        for raw in [b'x'*(MAX_LIST_BYTES+1),b'\xff',content('https://github.com/o/r')+b'\0',content('https://github.com/o/r',extra='```')]:
            with self.subTest(raw=raw[:40]),self.assertRaises((ValueError,UnicodeError)):self.parse(raw)
        with self.assertRaises(ValueError):self.parse(content('https://github.com/o/r','https://github.com/o/s'),{**SOURCE,'maxEntries':1})

    def test_source_definition_is_explicit_and_stable(self):
        self.assertEqual(source_identity(SOURCE),source_identity(dict(reversed(list(SOURCE.items())))))
        self.assertNotEqual(source_identity(SOURCE),source_identity({**SOURCE,'startHeading':'## Other'}))
        for edit in [{'unexpected':True},{'repositoryId':'o/r'},{'repositoryId':True},{'path':'../README.md'},{'path':'hooks.sh'},{'selectionStatement':'This is not curated.'},{'minEntries':0},{'maxEntries':10001},{'startHeading':'# Servers'},{'label':'bad\nlabel'}]:
            with self.subTest(edit=edit),self.assertRaises(ValueError):validate_source({**SOURCE,**edit})

    def test_definition_file_rejects_duplicate_keys_and_oversize(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.json'
            path.write_bytes(b'{"schemaVersion":1,"schemaVersion":1}')
            with self.assertRaises(ValueError):load_source(path)
            path.write_bytes(b' '*16385)
            with self.assertRaises(ValueError):load_source(path)

    def test_curated_blob_budget_does_not_expand_manifest_budget(self):
        data=blob(b'x'*150000);request=RecommendationGitRequest('blob','source/list',data['sha'])
        with self.assertRaises(ValueError):sanitize_object(data,request)
        self.assertEqual(data,sanitize_object(data,request,max_blob_bytes=MAX_LIST_BYTES))


class TransportTests(unittest.TestCase):
    def test_only_allowlisted_get_noninteractive_and_no_debug_environment(self):
        calls=[]
        def runner(*args):calls.append(args);return 0,wire(PUBLIC)
        with patch.dict('os.environ',{'GH_DEBUG':'api','GH_FORCE_TTY':'1','GH_HTTP_UNIX_SOCKET':'hidden'}):
            response=GhRecommendationTransport(runner=runner).get(RecommendationGitRequest('repository',123))
        argv,env,timeout,maximum=calls[0]
        self.assertEqual('GET',argv[argv.index('-X')+1]);self.assertEqual('repositories/123',argv[-1])
        self.assertEqual('1',env['GH_PROMPT_DISABLED']);self.assertNotIn('GH_DEBUG',env)
        self.assertNotIn('GH_HTTP_UNIX_SOCKET',env);self.assertEqual(30,timeout);self.assertEqual(4*1024*1024,maximum)
        self.assertFalse(response.body['private'])
        for request in [GitHubRequest.repository(123),PublicGitRequest('repository',123),RecommendationGitRequest('releases',123)]:
            with self.assertRaises(ValueError):GhRecommendationTransport(runner=runner).get(request)

    def test_private_repository_numeric_mismatch_and_forged_blob_are_rejected(self):
        requests=[(RecommendationGitRequest('repository',123),{**PUBLIC,'private':True}),
                  (RecommendationGitRequest('repository',123),{**PUBLIC,'id':124}),
                  (RecommendationGitRequest('blob','source/list','a'*40),blob(b'not the requested blob'))]
        for request,body in requests:
            with self.subTest(body=body),self.assertRaises(ServiceError):GhRecommendationTransport(runner=lambda *_:(0,wire(body))).get(request)

    def test_response_and_header_budgets_and_304_exit_one(self):
        request=RecommendationGitRequest('repository',123)
        response=GhRecommendationTransport(runner=lambda *_:(1,wire(status=304))).get(request)
        self.assertEqual(304,response.status)
        with self.assertRaises(ValueError):GhRecommendationTransport().get(request,etag='x\r\nInjected: yes')
        with self.assertRaises(ServiceError):GhRecommendationTransport(runner=lambda *_:(0,b'x'*(4*1024*1024+1))).get(request)

    def test_request_cache_cannot_share_manifest_or_another_credential_identity(self):
        request=RecommendationGitRequest('repository',123)
        self.assertNotEqual(request.cache_key('profile-a'),request.cache_key('profile-b'))
        self.assertNotEqual(request.cache_key('profile-a'),GitHubRequest.repository(123).cache_key('profile-a'))
        with self.assertRaises(ValueError):request.cache_key('ghp_secret')


class CollectionTests(unittest.TestCase):
    def setUp(self):
        from service_tests.temporary import cleanup_temporary
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name).resolve();self.addCleanup(cleanup_temporary,self.temporary,self.root)
        self.db=open_state(self.root/'runtime/state.db');self.addCleanup(self.db.close)
        self.clock=Clock();self.raw=content('https://github.com/o/alpha','https://github.com/o/beta');self.commit=COMMIT
        self.calls=[];self.overrides={};self.headers={};self.code=0
        self.client=RecommendationClient(self.db,identity='fixture-profile',transport=GhRecommendationTransport(runner=self.runner),clock=self.clock,
            policy=RequestPolicy(core_spacing_seconds=.1,jitter_fraction=0),random_value=lambda:0)

    def runner(self, argv, env, timeout, maximum):
        endpoint=argv[-1];self.calls.append((endpoint,argv))
        current_blob=blob(self.raw)
        if endpoint=='repositories/123':body=PUBLIC
        elif '/commits/' in endpoint:body={'sha':self.commit,'commit':{'tree':{'sha':TREE}}}
        elif '/git/trees/' in endpoint:body={'sha':TREE,'truncated':False,'tree':[{'path':'README.md','mode':'100644','type':'blob','sha':current_blob['sha'],'size':len(self.raw)}]}
        elif '/git/blobs/' in endpoint:body=current_blob
        else:raise AssertionError(endpoint)
        for token,value in self.overrides.items():
            if token in endpoint:return value
        return self.code,wire(body,headers=self.headers)

    def enqueue(self,definition=None,slot=None):
        with self.db:return enqueue_recommendation(self.db,definition=definition or SOURCE,slot=slot or self.clock.now,now=self.clock.now)

    def collect(self,maximum=20,scan_ids=None):
        return run_recommendation_jobs(self.db,self.client,owner='fixture-worker',max_requests=maximum,clock=self.clock,scan_ids=scan_ids)

    def count(self,table):return self.db.execute('SELECT count(*) FROM '+table).fetchone()[0]
    def snapshot(self):return self.db.execute('SELECT * FROM recommendation_snapshots ORDER BY selection_at DESC LIMIT 1').fetchone()
    def statuses(self):return [tuple(row) for row in self.db.execute('SELECT target_url,status,reason FROM recommendation_observations ORDER BY observed_at,id')]
    def next(self,raw=None):
        self.clock.now += timedelta(hours=8);self.commit='c'*40
        if raw is not None:self.raw=raw
        self.enqueue();return self.collect()

    def test_complete_pinned_public_scan_preserves_facts_and_never_writes_canonical(self):
        self.enqueue();result=self.collect();snapshot=self.snapshot()
        self.assertEqual(5,result['requests']);self.assertEqual(0,result['canonicalWrites']);self.assertEqual(2,snapshot['member_count'])
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(),snapshot['content_hash'])
        self.assertEqual(COMMIT,snapshot['commit_sha']);self.assertEqual(blob(self.raw)['sha'],snapshot['blob_sha'])
        self.assertEqual(2,self.count('recommendation_observations'));self.assertEqual(0,self.count('package_units'));self.assertEqual(0,self.count('change_events'))
        self.assertTrue(all(row[1]=='active' for row in self.statuses()))
        self.assertEqual(5,self.count('recommendation_fetches'))
        self.assertEqual(1,self.db.execute('SELECT applied FROM recommendation_snapshots').fetchone()[0])

    def test_idempotent_enqueue_and_restart_resume_only_remaining_steps(self):
        scan=self.enqueue();self.assertEqual(scan,self.enqueue());self.assertEqual(1,self.count('jobs'))
        self.assertEqual(2,self.collect(2)['requests']);self.assertEqual(0,self.count('recommendation_snapshots'))
        self.db.close();self.db=open_state(self.root/'runtime/state.db');self.addCleanup(self.db.close)
        self.client=RecommendationClient(self.db,identity='fixture-profile',transport=GhRecommendationTransport(runner=self.runner),clock=self.clock,policy=RequestPolicy(core_spacing_seconds=.1))
        self.assertEqual(3,self.collect()['requests']);self.assertEqual(1,self.count('recommendation_snapshots'))
        self.assertEqual(0,self.collect()['requests']);self.assertEqual(2,self.count('recommendation_observations'))

    def test_complete_comparison_withdraws_only_absent_source_membership(self):
        self.enqueue();self.collect();previous=self.snapshot()['id']
        self.next(content('https://github.com/o/beta','https://github.com/o/gamma'))
        snapshot=self.snapshot();self.assertEqual(previous,snapshot['previous_id'])
        removed=self.db.execute("SELECT * FROM recommendation_observations WHERE status='withdrawn'").fetchone()
        self.assertEqual('https://github.com/o/alpha',removed['target_url']);self.assertEqual(previous,removed['previous_id'])
        evidence=json.loads(removed['evidence_json']);self.assertEqual('source-list-membership-only',evidence['withdrawalMeaning']);self.assertEqual('not_inferred',evidence['packageEndorsement'])
        self.assertEqual(3,self.count('recommendation_observations')-2)

    def test_reappearance_and_unchanged_content_keep_history_and_no_fake_rank(self):
        self.enqueue();self.collect();self.next(content('https://github.com/o/beta'));self.next(content('https://github.com/o/alpha','https://github.com/o/beta'))
        alpha=[status for target,status,_ in self.statuses() if target.endswith('/alpha')]
        self.assertEqual(['active','withdrawn','active'],alpha)
        self.assertEqual(0,self.db.execute('SELECT count(*) FROM recommendation_members WHERE source_rank IS NOT NULL').fetchone()[0])

    def test_truncated_tree_or_absent_file_does_not_imply_withdrawal(self):
        self.enqueue();self.collect();before=self.count('recommendation_observations')
        for tree in [{'sha':TREE,'truncated':True,'tree':[]},{'sha':TREE,'truncated':False,'tree':[]}]:
            self.overrides={'/git/trees/':(0,wire(tree))};self.next()
            self.assertEqual(before,self.count('recommendation_observations'))
        self.assertEqual(1,self.count('recommendation_snapshots'))

    def test_parse_error_empty_list_and_missing_selection_statement_do_not_withdraw(self):
        self.enqueue();self.collect();before=self.count('recommendation_observations')
        for raw in [content(),content('https://github.com/o/beta',extra='- malformed reference'),content('https://github.com/o/beta').replace(SOURCE['selectionStatement'].encode(),b'Ordinary links')]:
            self.next(raw);self.assertEqual(before,self.count('recommendation_observations'))
        self.assertEqual(1,self.count('recommendation_snapshots'))

    def test_explicit_complete_empty_snapshot_can_withdraw_all_members(self):
        definition={**SOURCE,'emptyStatement':'No entries.'};self.enqueue(definition);self.collect()
        self.clock.now += timedelta(hours=8);self.raw=content(empty=True);self.commit='d'*40
        self.enqueue(definition);self.collect()
        self.assertEqual(0,self.snapshot()['member_count']);self.assertEqual(2,sum(row[1]=='withdrawn' for row in self.statuses()))

    def test_source_definitions_are_isolated_and_cannot_withdraw_another_list(self):
        self.enqueue();self.collect();self.raw=content('https://github.com/o/beta');self.clock.now += timedelta(hours=8)
        self.enqueue({**SOURCE,'label':'Second public list'});self.collect()
        self.assertEqual(2,self.count('recommendation_sources'));self.assertEqual(0,sum(row[1]=='withdrawn' for row in self.statuses()))

    def test_repository_rename_during_read_rejects_complete_claim(self):
        calls=0
        original=self.runner
        def runner(*args):
            nonlocal calls
            if args[0][-1]=='repositories/123':
                calls+=1
                if calls==2:return 0,wire({**PUBLIC,'full_name':'other/name'})
            return original(*args)
        self.client.transport=GhRecommendationTransport(runner=runner);self.enqueue();self.collect()
        self.assertEqual(0,self.count('recommendation_snapshots'));self.assertEqual(0,self.count('recommendation_observations'))

    def test_actual_core_limit_and_budget_wait_are_shared_with_existing_gh_lanes(self):
        self.enqueue();self.overrides={'repositories/':(1,wire({'message':'API rate limit exceeded'},403,{'X-RateLimit-Remaining':'0','X-RateLimit-Limit':'5000','X-RateLimit-Reset':str(int((NOW+timedelta(hours=2)).timestamp()))}))}
        self.assertEqual(1,self.collect(1)['requests']);self.assertEqual(0,self.count('recommendation_snapshots'))
        self.assertIsNotNone(self.client.next_allowed_at('core',now=self.clock.now))
        self.assertEqual(0,self.collect(1)['requests'])
        self.assertEqual(1,len(self.calls))

    def test_304_commit_and_blob_reuse_keep_original_content_observation_and_refresh_pointer_check(self):
        self.headers={'ETag':'"fixture"'};self.enqueue();self.collect();first=self.snapshot()
        self.overrides={'/commits/':(1,wire(status=304,headers=self.headers)),'/git/blobs/':(1,wire(status=304,headers=self.headers))}
        self.clock.now += timedelta(hours=8);self.enqueue();self.collect();second=self.snapshot()
        self.assertEqual(first['observed_at'],second['observed_at']);self.assertGreater(second['selection_at'],first['selection_at'])
        self.assertEqual(0,sum(row[1]=='withdrawn' for row in self.statuses()))
        self.assertTrue(any('If-None-Match: "fixture"' in argv for _,argv in self.calls))

    def test_cancelled_or_budget_exhausted_partial_scan_has_no_public_claim(self):
        self.enqueue();self.collect(3)
        self.assertEqual(0,self.count('recommendation_snapshots'));self.assertEqual(0,self.count('recommendation_observations'))
        result=run_recommendation_jobs(self.db,self.client,owner='fixture-worker',max_requests=5,clock=self.clock,should_stop=lambda:True)
        self.assertEqual(0,result['requests']);self.assertEqual(0,self.count('recommendation_snapshots'))
        self.assertEqual(2,self.collect()['requests'])

    def test_late_old_pointer_observation_is_preserved_but_cannot_retract_newer_snapshot(self):
        self.enqueue();self.collect()
        self.clock.now += timedelta(hours=1);self.commit='e'*40;self.raw=content('https://github.com/o/old')
        old_scan=self.enqueue();self.collect(4,scan_ids=(old_scan,))
        self.clock.now += timedelta(hours=8);self.commit='c'*40;self.raw=content('https://github.com/o/beta')
        new_scan=self.enqueue();self.collect(scan_ids=(new_scan,))
        before=self.count('recommendation_observations');latest=self.snapshot()
        self.collect(scan_ids=(old_scan,))
        old=self.db.execute("SELECT * FROM recommendation_snapshots WHERE commit_sha=?",('e'*40,)).fetchone()
        self.assertEqual(0,old['applied']);self.assertEqual(before,self.count('recommendation_observations'))
        self.assertEqual(latest['id'],self.snapshot()['id'])

    def test_mutated_source_definition_cannot_reuse_an_existing_source_identity(self):
        scan=self.enqueue()
        with self.db:self.db.execute('UPDATE recommendation_sources SET definition_json=?',(encode_json({**SOURCE,'label':'Different list'}),))
        result=self.collect()
        self.assertEqual(0,result['requests']);self.assertEqual(0,self.count('recommendation_snapshots'))
        self.assertEqual('failed',self.db.execute('SELECT state FROM recommendation_scans WHERE id=?',(scan,)).fetchone()[0])

    def test_scan_filters_are_bounded_and_do_not_consume_unselected_jobs(self):
        self.enqueue()
        self.assertEqual(0,self.collect(scan_ids=())['requests'])
        for scan_ids in [('invalid',),['a'*64],tuple('a'*64 for _ in range(1001))]:
            with self.subTest(scan_ids=str(scan_ids)[:30]),self.assertRaises(ValueError):self.collect(scan_ids=scan_ids)
        self.assertEqual(0,len(self.calls))

    def test_304_without_cache_defers_instead_of_manufacturing_a_complete_snapshot(self):
        self.overrides={'repositories/':(1,wire(status=304))};self.enqueue();result=self.collect(1)
        self.assertEqual(1,result['deferred']);self.assertEqual(0,self.count('recommendation_snapshots'))
        self.assertEqual('github_cache_miss',self.db.execute('SELECT last_error FROM jobs').fetchone()[0])

    def test_failed_transport_preserves_previous_membership_and_durable_retry(self):
        self.enqueue();self.collect();before=self.count('recommendation_observations')
        self.clock.now += timedelta(hours=8);self.enqueue()
        def fail(*_):raise ServiceError('github_timeout','bounded query timed out',504)
        self.client.transport=GhRecommendationTransport(runner=fail)
        result=self.collect(1)
        self.assertEqual(1,result['deferred']);self.assertEqual(before,self.count('recommendation_observations'))
        self.assertEqual(1,self.count('recommendation_snapshots'))

    def test_read_only_status_does_not_create_state_or_mutate_database(self):
        self.enqueue();self.collect();self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)');self.db.commit()
        digest=hashlib.sha256((self.root/'runtime/state.db').read_bytes()).hexdigest()
        result=read_recommendation_status(self.root/'runtime/state.db',limit=1)
        self.assertEqual(1,len(result['snapshots']));self.assertEqual(0,result['canonicalWrites'])
        self.assertEqual(digest,hashlib.sha256((self.root/'runtime/state.db').read_bytes()).hexdigest())
        missing=self.root/'absent/state.db'
        with self.assertRaises(ValueError):read_recommendation_status(missing)
        self.assertFalse(missing.parent.exists())


    def test_private_backup_restore_includes_source_history_and_pending_jobs(self):
        self.enqueue();self.collect();self.clock.now += timedelta(hours=8);self.enqueue();self.collect(2)
        source=Settings(self.root/'catalog',self.root/'runtime');target=Settings(self.root/'catalog',self.root/'restored')
        receipt=create_state_backup(source,self.root/'backup')
        info=inspect_state_backup(self.root/'backup',expected_backup_sha256=receipt['backupSha256'])
        self.assertEqual(1,info['descriptor']['stateSchemaVersion']);self.assertEqual(1,info['descriptor']['tableCounts']['recommendation_snapshots'])
        restore_state_backup(target,self.root/'backup',expected_backup_sha256=receipt['backupSha256'])
        db=open_state(target.state_path);self.addCleanup(db.close)
        self.assertEqual(2,db.execute('SELECT count(*) FROM recommendation_observations').fetchone()[0])
        self.assertEqual(1,db.execute("SELECT count(*) FROM jobs WHERE kind='github_recommendation' AND state='pending'").fetchone()[0])
        self.assertFalse(target.snapshots.exists())


if __name__ == '__main__':unittest.main()
