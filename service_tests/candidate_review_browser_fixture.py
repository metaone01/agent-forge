"""SQLite response fixtures for local metadata-triage UI; synthetic discovery only."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from service_tests import test_discovery_projection as fixtures
from agent_forge_service.candidate_reviews import inspect_review,record_review,ACTIONS
from agent_forge_service.http_contract import HttpReadService
from agent_forge_service.bundles import export_bundle


def build(output):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    assert not (output/'candidate-review-browser-fixture.json').exists()
    case=fixtures.DiscoveryProjectionTests();case.setUp()
    try:
        case.collect([fixtures.repository(index,topics=['mcp-server'],stargazers_count=index) for index in range(1,31)])
        valid=case.manifest(identity=1,fields={'name':'hello','description':'Metadata <img src=x onerror="window.__injected=true">','version':'1.0.0','license':'MIT'})
        missing=case.manifest(identity=2,fields={'name':'hello'})
        counter=[0]
        def review(identity,action):
            counter[0]+=1;item=inspect_review(case.db,identity,now=case.now)
            return record_review(case.db,candidate_id=identity,expected_fingerprint=item['evidenceFingerprint'],expected_head=item['expectedHead'],action=action,
                reason_code=ACTIONS[action][1],request_key='ui-'+str(counter[0]),operator_label='private-review-ui-sentinel',now=case.now)
        for index in range(1,24):review('github-repository:'+str(index),'unsupported' if index==2 else 'reject')
        valid_id='manifest:'+valid['id'];missing_id='manifest:'+missing['id']
        review(valid_id,'request_metadata');review(valid_id,'reopen');review(missing_id,'request_metadata')
        case.publish(revision='triage-one')
        case.now+=timedelta(hours=8);case.collect([fixtures.repository(1,topics=['mcp-server','terminal']),fixtures.repository(2,topics=['mcp-server'],stargazers_count=200)])
        review('github-repository:3','reopen')
        from agent_forge_service.manifests import enqueue_manifest,run_manifest_jobs
        from agent_forge_service.public_git import AnonymousGitClient,AnonymousGitTransport
        from agent_forge_service.github_client import RequestPolicy
        from agent_forge_service.storage import timestamp
        new_commit='d'*40;new_blob=fixtures.blob(json.dumps({'name':'hello','description':'Updated declaration','version':'2.0.0','license':'MIT'}).encode())
        def respond(request):
            endpoint=request.full_url.removeprefix('https://api.github.com/')
            if endpoint=='repositories/1':return fixtures.repository(1,default_branch='main')
            if endpoint.endswith('/commits/main'):return {'sha':new_commit,'commit':{'tree':{'sha':fixtures.ROOT}}}
            if endpoint.endswith('/git/trees/'+fixtures.ROOT):return fixtures.tree(fixtures.ROOT,[fixtures.entry('.claude-plugin',fixtures.DIRECTORY,'040000','tree')])
            if endpoint.endswith('/git/trees/'+fixtures.DIRECTORY):return fixtures.tree(fixtures.DIRECTORY,[fixtures.entry('plugin.json',new_blob['sha'],size=new_blob['size'])])
            if endpoint.endswith('/git/blobs/'+new_blob['sha']):return new_blob
            raise AssertionError(endpoint)
        client=AnonymousGitClient(case.db,transport=AnonymousGitTransport(opener=fixtures.Opener(respond)),policy=RequestPolicy(jitter_fraction=0),clock=lambda:case.now)
        with case.db:enqueue_manifest(case.db,github_id=1,path='.claude-plugin/plugin.json',kind='claude_plugin',agent='claude-code',slot=case.now,now=case.now)
        for _ in range(8):
            case.now+=timedelta(seconds=2);run_manifest_jobs(case.db,client,owner='new-declaration',max_requests=1,clock=lambda:case.now)
        newer=case.db.execute('SELECT * FROM manifest_candidates WHERE github_id=1 AND commit_sha=?',(new_commit,)).fetchone();assert newer is not None
        newer_id='manifest:'+newer['id'];assert newer_id!=valid_id
        case.publish(revision='triage-two')
        export_bundle(case.settings,output/'bundle-one',revision='triage-one');export_bundle(case.settings,output/'bundle-two',revision='triage-two')
        ticks=[0.0];reader=HttpReadService(case.settings,clock=lambda:ticks[0]);cases=[];seen=set()
        def add(revision,path,parameters):
            key=json.dumps([revision,path,sorted(parameters.items())])
            if key in seen:return None
            seen.add(key);ticks[0]+=1
            with patch('agent_forge_service.query.utc_now',return_value=case.now+timedelta(days=2)):
                response=reader.handle('GET',path,list({**parameters,'revision':revision}.items()))
            assert response.status==200,(path,parameters,response.body)
            body=json.loads(response.body);cases.append({'revision':revision,'path':path,'parameters':parameters,'status':200,'body':body});return body
        for revision in ['triage-one','triage-two']:
            for pinned in [False,True]:
                extra={'revision':revision} if pinned else {}
                add(revision,'/api/v1/manifest',extra)
                add(revision,'/api/v1/facets',{'scope':'discovery','revision':revision})
                add(revision,'/api/v1/facets',{'revision':revision})
                add(revision,'/api/v1/packages',{'sort':'canonical_updated','limit':'20',**extra})
                add(revision,'/api/v1/recommendation-sources',{'revision':revision,'limit':'20'})
                for disposition in [None,'rejected','unsupported','needs_metadata','pending','not_recorded','superseded']:
                    for sort in ['first_seen','name']:
                        params={'scope':'discovery','sort':sort,'limit':'20',**extra,**({'reviewDisposition':disposition} if disposition else {})}
                        page=add(revision,'/api/v1/packages',params)
                        while page and page['nextCursor']:page=add(revision,'/api/v1/packages',{**params,'revision':revision,'cursor':page['nextCursor']})
                for identity in ['github-repository:1','github-repository:2',valid_id,missing_id,*([newer_id] if revision=='triage-two' else [])]:
                    add(revision,'/api/v1/discoveries/'+identity,extra)
            add(revision,'/api/v1/packages/plugin.alpha/detail',{'revision':revision})
            add(revision,'/api/v1/packages/plugin.alpha/versions',{'revision':revision,'limit':'20'})
            add(revision,'/api/v1/packages/plugin.alpha/recommendations',{'revision':revision,'version':'1.0.0','status':'all','limit':'20'})
        value={'kind':'Actual SQLite/decoded GET snapshots; synthetic discovery and local metadata triage, no canonical adoption or real FastAPI',
            'validId':valid_id,'missingId':missing_id,'newerId':newer_id,'cases':cases,'candidatesBefore':32,'candidatesAfter':33,
            'privateOperatorOmitted':True,'freshnessClock':'latest fixture metadata read plus two days'}
        (output/'candidate-review-browser-fixture.json').write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print(json.dumps({'cases':len(cases),'valid':valid_id,'missing':missing_id,'newer':newer_id}))
    finally:case.doCleanups()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);build(parser.parse_args().output)
