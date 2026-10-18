"""Public control history fixtures from SQLite publication and read-only decoded GET."""
import argparse
import json
from datetime import timedelta
from pathlib import Path
from agent_forge_service.source_controls import ACTIONS, inspect_control, target_id, record_control
from agent_forge_service.storage import open_state
from agent_forge_service.settings import Settings
from agent_forge_service.publishing import build_and_publish
from agent_forge_service.http_contract import HttpReadService
from service_tests.test_publication_query import NOW, record


def build(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    path=output/'source-controls-browser-fixture.json'
    if path.exists():raise ValueError('Use a fresh output')
    catalog=output/'catalog';packages=catalog/'sources/plugin/packages';packages.mkdir(parents=True)
    for name in ['alpha','beta']:(packages/(name+'.json')).write_text(json.dumps(record(name)),encoding='utf-8')
    (packages.parent/'index.json').write_text(json.dumps({'packages':{'alpha':{'latest':'1.0.0'},'beta':{'latest':'1.0.0'}}}),encoding='utf-8')
    settings=Settings(catalog,output/'runtime');state=open_state(settings.state_path)
    targets=[{'kind':'github_repository','repositoryId':number} for number in range(100,117)]+[
        {'kind':'provider','provider':'github.com'},{'kind':'provider','provider':'npmjs.org'},
        {'kind':'github_repository_name','fullName':'example/repo'},{'kind':'github_topic','topic':'mcp'},
        {'kind':'npm_package','package':'@org/fixture'},{'kind':'curated_file','repositoryId':123,'path':'README.md'}]
    assert len(targets)==23
    counter=0
    def decide(value,action,seconds):
        nonlocal counter
        head=inspect_control(state,value)['latestEvent'];counter+=1
        return record_control(state,value=value,action=action,reason_code=ACTIONS[action],expected_head=head['id'] if head else 'none',
            request_key='private-request-'+str(counter),operator_label='private-control-operator',now=NOW+timedelta(seconds=seconds))
    for index,value in enumerate(targets):decide(value,'disable',index)
    for index in range(21):decide(targets[0],'enable' if index%2==0 else 'disable',30+index)
    build_and_publish(settings,revision='controls-one',now=NOW+timedelta(seconds=51))
    decide({'kind':'provider','provider':'github.com'},'enable',60);decide(targets[1],'enable',61)
    build_and_publish(settings,revision='controls-two',now=NOW+timedelta(seconds=62))
    state.close();reader=HttpReadService(settings);cases=[]
    def add(selected,route,params):
        result=reader.handle('GET',route,list(params.items()));body=json.loads(result.body)
        cases.append({'revision':selected,'path':route,'parameters':params,'status':result.status,'body':body});return body
    for selected in ['controls-one','controls-two']:
        for bind in [{},{'revision':selected}]:
            # Explicitly request each immutable revision; bindless cases are replay aliases for its actual response.
            for route in ['/api/v1/manifest']:
                result=reader.handle('GET',route,[('revision',selected)])
                cases.append({'revision':selected,'path':route,'parameters':bind,'status':result.status,'body':json.loads(result.body)})
            for scope,sort in [('catalog','canonical_updated'),('discovery','first_seen')]:
                params={'limit':'20','sort':sort,**({'scope':scope} if scope=='discovery' else {}),**bind}
                actual={**params,'revision':selected};result=reader.handle('GET','/api/v1/packages',list(actual.items()))
                cases.append({'revision':selected,'path':'/api/v1/packages','parameters':params,'status':result.status,'body':json.loads(result.body)})
        for scope in ['catalog','discovery']:add(selected,'/api/v1/facets',{'revision':selected,**({'scope':scope} if scope=='discovery' else {})})
        add(selected,'/api/v1/recommendation-sources',{'revision':selected,'limit':'20'})
        params={'revision':selected,'limit':'20'};page=add(selected,'/api/v1/source-controls',params)
        assert len(page['items'])==20 and page['sourceControlCoverage']['targets']==23
        tail=add(selected,'/api/v1/source-controls',{**params,'cursor':page['nextCursor']})
        assert len(tail['items'])==3 and tail['nextCursor'] is None
        add(selected,'/api/v1/packages/plugin.alpha/detail',{'revision':selected})
        add(selected,'/api/v1/packages/plugin.alpha/versions',{'revision':selected,'limit':'20'})
        add(selected,'/api/v1/packages/plugin.alpha/recommendations',{'revision':selected,'limit':'20','status':'all','version':'1.0.0'})
    # Include a legacy immutable revision with the optional projection absent.
    legacy_state=Settings(catalog,output/'legacy-runtime');build_and_publish(legacy_state,revision='controls-legacy',now=NOW)
    old=HttpReadService(legacy_state)
    for route,params in [('/api/v1/manifest',{'revision':'controls-legacy'}),('/api/v1/facets',{'revision':'controls-legacy'}),('/api/v1/recommendation-sources',{'revision':'controls-legacy','limit':'20'}),('/api/v1/packages',{'revision':'controls-legacy','limit':'20','sort':'canonical_updated'})]:
        result=old.handle('GET',route,list(params.items()));cases.append({'revision':'controls-legacy','path':route,'parameters':params,'status':result.status,'body':json.loads(result.body)})
    payload={'kind':'Synthetic local collection controls over actual SQLite/decoded GET; no real source decisions, upstream requests or FastAPI',
        'cases':cases,'targets':23,'historyTarget':target_id(targets[0])}
    assert 'private-control-operator' not in json.dumps(cases) and 'private-request-' not in json.dumps(cases)
    path.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'cases':len(cases),'targets':23,'fixture':str(path)}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);build(parser.parse_args().output)
