"""Synthetic SQLite response fixtures for curated UI; no live upstream/HTTP server."""
from __future__ import annotations
import argparse,json
from datetime import timedelta
from unittest.mock import patch
from pathlib import Path
from service_tests.test_recommendation_projection import ProjectionTests,content
from service_tests.test_publication_query import record
from agent_forge_service.http_contract import HttpReadService
from agent_forge_service.bundles import export_bundle

def build(output):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    case=ProjectionTests();case.setUp()
    try:
        latest=record('alpha',identity=case.alpha['id'],version='2.0.0')
        latest['distributions'][0]['url']='https://github.com/not-selected/other'
        case.write(latest)
        (case.catalog/'sources/plugin/index.json').write_text(json.dumps({'packages':{'alpha':{'latest':'2.0.0'}}}),encoding='utf8')
        targets=['https://github.com/o/alpha','https://github.com/o/beta']+[f'https://github.com/o/member-{index:02}' for index in range(30)]
        raw=content(*targets,*(['https://github.com/o/alpha']*24)).replace(b'[alpha]',b'[<img src=x onerror="window.__injected=true">alpha]')
        case.next(raw);case.build('rec-one')
        case.next(content(*targets[1:]));case.build('rec-two')
        export_bundle(case.settings,output/'bundle-one',revision='rec-one')
        export_bundle(case.settings,output/'bundle-two',revision='rec-two')
        ticks=[0.0];reader=HttpReadService(case.settings,clock=lambda:ticks[0]);cases=[];seen=set()
        def add(revision,path,parameters):
            key=json.dumps([revision,path,sorted(parameters.items())])
            if key in seen:return None
            seen.add(key);ticks[0]+=1
            with patch('agent_forge_service.query.utc_now',return_value=case.clock.now+timedelta(days=2)):
                response=reader.handle('GET',path,list({**parameters,'revision':revision}.items()))
            assert response.status==200,(path,parameters,response.body)
            body=json.loads(response.body);cases.append({'revision':revision,'path':path,'parameters':parameters,'status':200,'body':body});return body
        for revision in ('rec-one','rec-two'):
            for pinned in [False,True]:
                extra={'revision':revision} if pinned else {}
                add(revision,'/api/v1/manifest',extra)
                add(revision,'/api/v1/facets',{'revision':revision})
                add(revision,'/api/v1/packages',{'sort':'canonical_updated','limit':'20',**extra})
                add(revision,'/api/v1/recommendation-sources',{'limit':'20',**extra})
                for version in ['1.0.0','2.0.0',None]:
                    params={**extra,**({'version':version} if version else {})}
                    add(revision,'/api/v1/packages/'+case.alpha['id']+'/detail',params)
                add(revision,'/api/v1/packages/'+case.alpha['id']+'/versions',{'limit':'20',**extra})
                for status in ['all','active','withdrawn']:
                    params={'limit':'20','status':status,'source':case.source,**extra}
                    page=add(revision,'/api/v1/recommendations',params)
                    if page and page['nextCursor']:
                        add(revision,'/api/v1/recommendations',{**params,'revision':revision,'cursor':page['nextCursor']})
                    for version in ['1.0.0','2.0.0']:
                        add(revision,'/api/v1/packages/'+case.alpha['id']+'/recommendations',{'limit':'20','status':status,'version':version,'revision':revision})
        value={'kind':'Synthetic curated Git GET and canonical versions; actual SQLite/HTTP contract DTOs; no real FastAPI',
               'freshnessClock':'latest fixture collection plus two days', 'packageId':case.alpha['id'],'sourceId':case.source,'cases':cases}
        (output/'recommendation-browser-fixture.json').write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print(json.dumps({'cases':len(cases),'source':case.source,'membersBefore':32,'membersAfter':31,'canonicalVersions':2}))
    finally:case.doCleanups()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);build(parser.parse_args().output)
