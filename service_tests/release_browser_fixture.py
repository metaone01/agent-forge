"""SQLite/decoded GET fixtures for observed Release detail panels."""
import argparse,json,shutil,itertools
from pathlib import Path
from datetime import timedelta
from agent_forge_service.publishing import build_and_publish
from agent_forge_service.http_contract import HttpReadService
from agent_forge_service.settings import Settings
from agent_forge_service.bundles import export_bundle,import_bundle
from service_tests import test_release_projection as fixture_module
from service_tests.test_releases import item


def build(output, *, sort_packages=0):
    output=Path(output);output.mkdir(parents=True,exist_ok=True);target=output/'release-browser-fixture.json'
    if target.exists():raise ValueError('Use a fresh fixture output')
    case=fixture_module.ReleaseProjectionTests('runTest');case.setUp()
    try:
        values=[{**item(identity=number,downloads=0 if number==1 else number),'tag_name':'v'+str(number)+'.0.0',
            'name':'<script>window.__releaseInjected=1</script>' if number==1 else 'Fixture release '+str(number)} for number in range(1,24)]
        if sort_packages:
            from service_tests.test_publication_query import record
            for number in range(sort_packages):
                value=record('sorted-'+str(number).zfill(2))
                value['updatedAt']='2026-10-01T00:00:00Z'
                value['distributions']=[{'id':'release','type':'release','url':'https://github.com/owner/repo/releases/tag/v1.0.0'}]
                (case.packages/('sorted-'+str(number)+'.json')).write_text(json.dumps(value),encoding='utf-8')
        case.collect(values);case.build('release-one')
        case.now+=timedelta(hours=8);case.fixture.now=case.now
        changed=[{**values[0],'assets':[{**values[0]['assets'][0],'download_count':200}]}]
        case.collect(changed);case.build('release-two')
        receiver=Settings(output/'absent-canonical',output/'receiver')
        for revision in ['release-one','release-two']:
            directory=output/(revision+'-bundle');receipt=export_bundle(case.settings,directory,revision=revision)
            import_bundle(receiver,directory,expected_bundle_sha256=receipt['bundleSha256'])
        reader=HttpReadService(receiver,clock=itertools.count(start=0,step=0.1).__next__);cases=[]
        def add(revision,route,parameters):
            result=reader.handle('GET',route,list(parameters.items()));body=json.loads(result.body)
            if result.status!=200:raise AssertionError((route,parameters,result.status,body))
            cases.append({'revision':revision,'path':route,'parameters':parameters,'status':result.status,'body':body});return body
        for revision in ['release-one','release-two']:
            for bind in [{},{'revision':revision}]:
                result=reader.handle('GET','/api/v1/manifest',[('revision',revision)])
                cases.append({'revision':revision,'path':'/api/v1/manifest','parameters':bind,'status':result.status,'body':json.loads(result.body)})
            add(revision,'/api/v1/facets',{'revision':revision})
            add(revision,'/api/v1/recommendation-sources',{'revision':revision,'limit':'20'})
            add(revision,'/api/v1/packages',{'revision':revision,'limit':'20','sort':'canonical_updated'})
            result=reader.handle('GET','/api/v1/packages',[('revision',revision),('limit','20'),('sort','canonical_updated')])
            cases.append({'revision':revision,'path':'/api/v1/packages','parameters':{'limit':'20','sort':'canonical_updated'},'status':result.status,'body':json.loads(result.body)})
            for extra in [{},{'q':'gamma'},{'agent':'dsh','type':'plugin'}]:
                params={'revision':revision,'limit':'20','sort':'latest_release',**extra}
                page=add(revision,'/api/v1/packages',params)
                cases.append({'revision':revision,'path':'/api/v1/packages','parameters':{k:v for k,v in params.items() if k!='revision'},'status':200,'body':page})
                while page.get('nextCursor'):
                    params={**params,'cursor':page['nextCursor']}
                    page=add(revision,'/api/v1/packages',params)
            add(revision,'/api/v1/releases',{'revision':revision,'limit':'20'})
            for package,versions in [('plugin.alpha',[None,'1.0.0','2.0.0']),('plugin.beta',[None,'1.0.0']),('plugin.gamma',[None,'1.0.0'])]:
                for version in versions:
                    binding={'revision':revision,**({'version':version} if version else {})}
                    detail=add(revision,'/api/v1/packages/'+package+'/detail',binding)
                    add(revision,'/api/v1/packages/'+package+'/versions',{'revision':revision,'limit':'20'})
                    add(revision,'/api/v1/packages/'+package+'/recommendations',{'revision':revision,'version':detail['canonicalRecord']['version'],'limit':'20','status':'all'})
                    params={'revision':revision,'version':detail['canonicalRecord']['version'],'association':'all','limit':'20'}
                    page=add(revision,'/api/v1/packages/'+package+'/releases',params)
                    if page.get('nextCursor'):add(revision,'/api/v1/packages/'+package+'/releases',{**params,'cursor':page['nextCursor']})
        payload={'kind':'Synthetic Release metadata through actual anonymous transport opener/SQLite/public bundle receiver/decoded GET; no real upstream requests, FastAPI or automatic canonical adoption',
            'cases':cases,'contextId':'plugin.alpha','assetId':'plugin.beta','unknownId':'plugin.gamma','releaseIdentities':23}
        assert not receiver.state_path.exists()
        assert 'private-fixture-identity' not in json.dumps(payload)
        target.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'cases':len(cases),'releaseIdentities':23,'fixture':str(target)}))
    finally:case.doCleanups()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--sort-packages',type=int,default=0);args=parser.parse_args();build(args.output,sort_packages=args.sort_packages)
