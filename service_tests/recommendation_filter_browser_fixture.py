"""SQLite-backed source-filter UI responses; synthetic metadata and Git evidence."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from datetime import timedelta
from unittest.mock import patch
from service_tests import test_recommendation_projection as projection_fixtures
from service_tests.test_publication_query import record
from service_tests.test_recommendations import SOURCE,content
from agent_forge_service.query import Query,QueryService
from agent_forge_service.http_contract import HttpReadService
from agent_forge_service.bundles import export_bundle

def build(output):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    assert not (output/'source-filter-browser-fixture.json').exists(),'Use a fresh fixture output'
    case=projection_fixtures.ProjectionTests();case.setUp()
    try:
        for index in range(31):
            value=record(f'matched-{index:02}',identity=f'pkg/matched/{index:02}',agents=('claude-code',) if index<5 else ('dsh',))
            value['distributions'][0]['url']='https://github.com/o/beta' if index==0 else 'https://github.com/o/alpha'
            case.write(value)
        for index in range(30):case.write(record(f'earlier-unmatched-{index:02}',identity=f'pkg/unmatched/{index:02}'))
        latest=record('alpha',identity=case.alpha['id'],version='2.0.0');case.write(latest)
        index_path=case.catalog/'sources/plugin/index.json';index_path.write_text(json.dumps({'packages':{'alpha':{'latest':'1.0.0'}}}),encoding='utf8')
        extra=[{**SOURCE,'label':f'Fixture source {number:02}','selectionStatement':f'A curated list of fixture source {number:02} tools.'} for number in range(20)]
        statements='\n'.join(definition['selectionStatement'] for definition in extra)+'\n'
        raw=content('https://github.com/o/alpha','https://github.com/o/beta').replace(b'# Source\n',('# Source\n'+statements).encode())
        case.next(raw)
        for definition in extra:case.enqueue(definition);case.collect()
        case.build('filter-one')
        sources=QueryService(case.settings).recommendation_sources(limit=20)
        more=QueryService(case.settings).recommendation_sources(limit=20,cursor=sources['nextCursor'])
        later_id=more['items'][0]['id'];first_id=sources['items'][0]['id']
        source_second_page=not any(row['id']==case.source for row in sources['items'])
        index_path.write_text(json.dumps({'packages':{'alpha':{'latest':'2.0.0'}}}),encoding='utf8')
        case.next(content('https://github.com/o/beta'));case.build('filter-two')
        export_bundle(case.settings,output/'bundle-one',revision='filter-one');export_bundle(case.settings,output/'bundle-two',revision='filter-two')
        ticks=[0.0];reader=HttpReadService(case.settings,clock=lambda:ticks[0]);cases=[];seen=set()
        def add(revision,path,parameters):
            key=json.dumps([revision,path,sorted(parameters.items())])
            if key in seen:return None
            seen.add(key);ticks[0]+=1
            with patch('agent_forge_service.query.utc_now',return_value=case.clock.now+timedelta(days=2)):
                response=reader.handle('GET',path,list({**parameters,'revision':revision}.items()))
            assert response.status==200,(path,parameters,response.body)
            body=json.loads(response.body);cases.append({'revision':revision,'path':path,'parameters':parameters,'status':200,'body':body});return body
        for revision in ('filter-one','filter-two'):
            for pinned in [False,True]:
                binding={'revision':revision} if pinned else {}
                add(revision,'/api/v1/manifest',binding)
                add(revision,'/api/v1/facets',{'revision':revision})
                add(revision,'/api/v1/facets',{'scope':'discovery','revision':revision})
                add(revision,'/api/v1/packages',{'scope':'discovery','sort':'first_seen','limit':'20',**binding})
                page=add(revision,'/api/v1/recommendation-sources',{'limit':'20',**binding})
                if page and page['nextCursor']:add(revision,'/api/v1/recommendation-sources',{'limit':'20','revision':revision,'cursor':page['nextCursor']})
                for source in [None,*sorted({case.source,later_id,first_id})]:
                    for sort in ['name','canonical_updated']:
                        for extra_filter in [{},{'agent':'dsh'},{'q':'terminal'},{'q':'slowterm'}]:
                            params={'limit':'20','sort':sort,**binding,**extra_filter,**({'recommendationSource':source} if source else {})}
                            page=add(revision,'/api/v1/packages',params)
                            while page and page['nextCursor']:
                                page=add(revision,'/api/v1/packages',{**params,'revision':revision,'cursor':page['nextCursor']})
            for version in ['1.0.0','2.0.0',None]:
                params={'revision':revision,**({'version':version} if version else {})}
                add(revision,'/api/v1/packages/'+case.alpha['id']+'/detail',params)
                add(revision,'/api/v1/packages/'+case.alpha['id']+'/recommendations',{'revision':revision,'version':version or ('1.0.0' if revision=='filter-one' else '2.0.0'),'status':'all','limit':'20'})
            add(revision,'/api/v1/packages/'+case.alpha['id']+'/versions',{'revision':revision,'limit':'20'})
        result={'kind':'Actual SQLite and decoded HTTP contract responses over synthetic canonical/Git fixtures; no real FastAPI',
            'sourceId':case.source,'sourceOnSecondPage':source_second_page,'moreSourceId':later_id,'firstSourceId':first_id,
            'packageId':case.alpha['id'],'matchedBefore':32,'matchedAfter':1,'totalPackages':62,'sources':21,
            'freshnessClock':'latest fixture observation plus two days','cases':cases}
        (output/'source-filter-browser-fixture.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print(json.dumps({key:result[key] for key in ['matchedBefore','matchedAfter','totalPackages','sources','sourceOnSecondPage']}))
    finally:case.doCleanups()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);build(parser.parse_args().output)
