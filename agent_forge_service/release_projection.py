"""Replayable public Release observations and exact declared URL associations."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from urllib.parse import quote,unquote,urlsplit
from .storage import encode_json,timestamp
from .public_git import PublicGitRequest,sanitize_releases
from .github import PUBLIC_FIELDS,PUBLIC_FIELDS_VERSION,GitHubRequest,_public
from .collector import normalize_repository
from .releases import RELEASE_FIELDS
from .canonical_mapping import parse_repository_url
from .errors import ServiceError

POLICY='public-release-observations-v1'
STALE_SECONDS=86400
MAX_PAGE_BYTES=4*1024*1024
COLUMNS={
    'public_release_pages':{'id','repository_id','scan_id','page','checked_at','data_json'},
    'public_release_repositories':{'repository_id','data_json'},
    'public_release_items':{'repository_id','release_id','published_at','page_id','data_json'},
    'public_release_checks':{'repository_id','data_json'},
    'public_release_package_links':{'package_id','version','repository_id','release_id','data_json'},
}


def _hash(value):return hashlib.sha256(encode_json(value).encode('utf-8')).hexdigest()
def _json(value):
    result=json.loads(value)
    if encode_json(result)!=value:raise ValueError('Release JSON is not canonical or has duplicate keys')
    return result

def _time(value,as_of):
    if not isinstance(value,str) or timestamp(value)!=value or value>as_of:raise ValueError('Release retrieval timestamp differs from cutoff')
    return value

def _id(value):
    if not isinstance(value,str) or not re.fullmatch('[1-9][0-9]{0,18}',value) or int(value)>2**63-1:raise ValueError('Release identity must be an exact decimal string')
    return int(value)


def create_schema(connection):
    connection.executescript('''
        CREATE TABLE public_release_pages (
            id TEXT PRIMARY KEY,repository_id INTEGER NOT NULL,scan_id TEXT NOT NULL,page INTEGER NOT NULL,
            checked_at TEXT NOT NULL,data_json TEXT NOT NULL,UNIQUE(scan_id,page)
        );
        CREATE INDEX public_release_page_history ON public_release_pages(repository_id,checked_at,id);
        CREATE TABLE public_release_repositories (repository_id INTEGER PRIMARY KEY,data_json TEXT NOT NULL);
        CREATE TABLE public_release_items (
            repository_id INTEGER NOT NULL,release_id INTEGER NOT NULL,published_at TEXT NOT NULL,
            page_id TEXT NOT NULL REFERENCES public_release_pages(id),data_json TEXT NOT NULL,
            PRIMARY KEY(repository_id,release_id)
        );
        CREATE INDEX public_release_published ON public_release_items(published_at DESC,repository_id,release_id);
        CREATE TABLE public_release_checks (repository_id INTEGER PRIMARY KEY,data_json TEXT NOT NULL);
        CREATE TABLE public_release_package_links (
            package_id TEXT NOT NULL,version TEXT NOT NULL,repository_id INTEGER NOT NULL,release_id INTEGER NOT NULL,
            data_json TEXT NOT NULL,PRIMARY KEY(package_id,version,repository_id,release_id),
            FOREIGN KEY(package_id,version) REFERENCES package_versions(package_id,version),
            FOREIGN KEY(repository_id,release_id) REFERENCES public_release_items(repository_id,release_id)
        );
    ''')


def _page(value,as_of):
    fields={'id','repositoryId','scanId','selectedAt','maxPages','page','observedAt','checkedAt','responseStatus','evidenceHash','endpoint','body','policyVersion'}
    if not isinstance(value,dict) or set(value)!=fields or value['policyVersion']!=POLICY:raise ValueError('invalid public Release page fields')
    repository=_id(value['repositoryId'])
    if type(value['maxPages']) is not int or not 1<=value['maxPages']<=10 or type(value['page']) is not int or not 1<=value['page']<=value['maxPages']:
        raise ValueError('invalid Release scan page bounds')
    for field in ('selectedAt','observedAt','checkedAt'):_time(value[field],as_of)
    if value['observedAt']>value['checkedAt'] or value['scanId']!=_hash(['public-release-scan-v1',value['repositoryId'],value['selectedAt'],value['maxPages']]):
        raise ValueError('Release scan identity or retrieval clock differs')
    if type(value['responseStatus']) is not int or value['responseStatus'] not in (200,304):raise ValueError('Release page is not a successful check')
    if value['endpoint']!=PublicGitRequest('releases',repository,page=value['page']).validate().endpoint:raise ValueError('Release page endpoint differs')
    if sanitize_releases(value['body'])!=value['body'] or _hash(value['body'])!=value['evidenceHash']:
        raise ValueError('Release page evidence changed or contains private data')
    if len(encode_json(value).encode('utf-8'))>MAX_PAGE_BYTES or value['id']!=_hash({key:item for key,item in value.items() if key!='id'}):
        raise ValueError('Release page digest or byte bound differs')
    return value


def _repository(value,as_of):
    fields={'repositoryId','fullName','observedAt','checkedAt','responseStatus','evidenceHash','body','representation'}
    if not isinstance(value,dict) or set(value)!=fields or value['representation']!=PUBLIC_FIELDS_VERSION:raise ValueError('invalid Release repository proof fields')
    identity=_id(value['repositoryId']);_time(value['observedAt'],as_of);_time(value['checkedAt'],as_of)
    if value['observedAt']>value['checkedAt'] or type(value['responseStatus']) is not int or value['responseStatus'] not in (200,304):raise ValueError('invalid Release repository retrieval')
    body=value['body']
    if not isinstance(body,dict) or set(body)-PUBLIC_FIELDS or _hash(body)!=value['evidenceHash']:raise ValueError('Release repository proof contains private or changed fields')
    _public(body,GitHubRequest.repository(identity).validate())
    metadata=normalize_repository(body)
    if metadata['fullName']!=value['fullName']:raise ValueError('Release repository name differs from evidence')
    if len(encode_json(value).encode('utf-8'))>MAX_PAGE_BYTES:raise ValueError('Release repository proof exceeds its byte bound')
    return value


def _replay(pages):
    grouped={};items={}
    for page in pages:grouped.setdefault(page['scanId'],[]).append(page)
    scans={}
    for scan,values in grouped.items():
        values.sort(key=lambda item:item['page']);seen=set();overlap=False;terminal=False
        for number,page in enumerate(values,1):
            if page['page']!=number or terminal or any(page[key]!=values[0][key] for key in ('repositoryId','selectedAt','maxPages')):
                raise ValueError('Release page chain is incomplete or has work after terminal page')
            if number>1 and page['checkedAt']<values[number-2]['checkedAt']:raise ValueError('Release page checks are reordered')
            release_ids={item['id'] for item in page['body']['releases']}
            overlap=bool(seen&release_ids);seen|=release_ids
            terminal=overlap or len(release_ids)<100 or number==page['maxPages']
            for release in page['body']['releases']:
                key=(int(page['repositoryId']),release['id']);rank=(page['observedAt'],page['checkedAt'],page['selectedAt'],page['page'],page['id'])
                old=items.get(key)
                if old is None or rank>old[0]:items[key]=(rank,_item(page,release))
        last=values[-1]
        state='partial' if overlap or len(last['body']['releases'])==100 and last['page']==last['maxPages'] else 'traversal_complete' if len(last['body']['releases'])<100 else 'in_progress'
        scans[scan]={'scanId':scan,'repositoryId':last['repositoryId'],'selectedAt':last['selectedAt'],'lastCheckedAt':last['checkedAt'],
            'pages':len(values),'maxPages':last['maxPages'],'state':state,'meaning':'mutable-page-traversal-not-snapshot-or-deletion-proof'}
    return {key:value[1] for key,value in items.items()},scans


def _item(page,value):
    return {'repositoryId':page['repositoryId'],'releaseId':str(value['id']),'tagName':value['tag_name'],'name':value['name'],
        'prerelease':value['prerelease'],'targetCommitish':value['target_commitish'],'createdAt':value['created_at'],'publishedAt':value['published_at'],
        'assets':[{'assetId':str(asset['id']),'name':asset['name'],'state':asset['state'],'sizeBytes':str(asset['size']),
            'downloadCount':str(asset['download_count']),'createdAt':asset['created_at'],'updatedAt':asset['updated_at'],
            'counterIdentity':'github-release-asset:'+page['repositoryId']+':'+str(value['id'])+':'+str(asset['id'])+':downloads','unit':'download_requests','measurementKind':'cumulative',
            'scope':'github_release_asset'} for asset in value['assets']],
        'observation':{'pageId':page['id'],'endpoint':page['endpoint'],'evidenceHash':page['evidenceHash'],'observedAt':page['observedAt'],'checkedAt':page['checkedAt']}}


def parse_release_url(url):
    if not isinstance(url,str) or len(url)>8192 or any(ord(ch)<33 or ord(ch)==127 for ch in url) or '\\' in url:return None
    try:
        value=urlsplit(url)
        if value.scheme!='https' or value.hostname!='github.com' or value.username or value.password or value.port or value.query or value.fragment:return None
        parts=value.path.split('/')[1:]
        if len(parts)<5 or parts[2]!='releases' or any('%' in part for part in parts[:2]):return None
        name='/'.join(parts[:2]);GitHubRequest.repository(name).validate()
        if parts[3]=='tag':tag=unquote('/'.join(parts[4:]),errors='strict');asset=None
        elif parts[3]=='download' and len(parts)==6:tag=unquote(parts[4],errors='strict');asset=unquote(parts[5],errors='strict')
        else:return None
        if not tag or len(tag)>512 or any(ord(ch)<32 or ord(ch)==127 for ch in tag) or '%' in tag:return None
        if asset is not None and (not asset or len(asset)>1000 or '/' in asset or '\\' in asset or '%' in asset or any(ord(ch)<32 or ord(ch)==127 for ch in asset)):return None
        return {'repositoryName':name.lower(),'tag':tag,'assetName':asset}
    except (ValueError,TypeError,UnicodeError):return None


def _name_index(repositories):
    names={}
    for repo in repositories.values():names.setdefault(repo['fullName'].lower(),set()).add(int(repo['repositoryId']))
    return {name:next(iter(ids)) for name,ids in names.items() if len(ids)==1}


def declared_scopes(record,repositories,*,names=None):
    if names is None:names=_name_index(repositories)
    result=[]
    assertions=[('/links/repository',record.get('links',{}).get('repository'),'repository_context',None)]
    assertions += [('/distributions/'+str(index)+'/url',value.get('url'),'repository_context' if value.get('type') in ('git','github-repo') else 'release_declaration',value.get('ref'))
        for index,value in enumerate(record.get('distributions',[])) if value.get('type') in ('git','github-repo','release','archive','github-mcpb')]
    for field,url,kind,ref in assertions:
        if not url:continue
        if kind=='repository_context':
            try:parsed=parse_repository_url(url,ref=ref,context=True)
            except (ValueError,TypeError):continue
            if not parsed or parsed['repository'] not in names:continue
            result.append({'repositoryId':names[parsed['repository']],'field':field,'url':url,'kind':kind})
        else:
            parsed=parse_release_url(url)
            if not parsed or parsed['repositoryName'] not in names:continue
            result.append({'repositoryId':names[parsed['repositoryName']],'field':field,'url':url,'kind':'exact_declared_asset' if parsed['assetName'] is not None else 'exact_declared_release',
                'tag':parsed['tag'],'assetName':parsed['assetName']})
    return result


def package_links(connection,repositories,items):
    by_repo={};tags={}
    for key,item in items.items():
        by_repo.setdefault(key[0],[]).append((key,item));tags.setdefault((key[0],item['tagName']),set()).add(key[1])
    links={};names=_name_index(repositories)
    for row in connection.execute('SELECT package_id,version,payload_hash,canonical_json FROM package_versions ORDER BY package_id,version'):
        record=_json(row['canonical_json'])
        if _hash(record)!=row['payload_hash'] or record.get('id')!=row['package_id'] or record.get('version')!=row['version']:
            raise ValueError('Release canonical record identity/hash differs')
        assertions=declared_scopes(record,repositories,names=names)
        grouped={}
        for assertion in assertions:
            repository=assertion['repositoryId']
            for key,item in by_repo.get(repository,[]):
                proof={key:value for key,value in assertion.items() if key!='repositoryId'}
                if assertion['kind']!='repository_context':
                    if assertion['tag']!=item['tagName'] or len(tags[repository,item['tagName']])!=1:continue
                    if assertion['assetName'] is not None:
                        assets=[asset for asset in item['assets'] if asset['name']==assertion['assetName']]
                        if len(assets)!=1:continue
                        proof['assetId']=assets[0]['assetId']
                grouped.setdefault(key,[]).append(proof)
        for key,proofs in grouped.items():
            links[(row['package_id'],row['version'],*key)]={'packageId':row['package_id'],'version':row['version'],'canonicalHash':row['payload_hash'],
                'repositoryId':str(key[0]),'releaseId':str(key[1]),'assertions':proofs,
                'association':'declared-url-only-not-version-or-install-verification','policyVersion':POLICY}
    return links


def repository_checks(pages,items,scans):
    result={}
    for page in pages:
        identity=int(page['repositoryId'])
        result.setdefault(identity,{'repositoryId':page['repositoryId'],'lastCheckedAt':page['checkedAt'],'latestTraversal':None,
            'observedReleaseIdentities':0,'traversals':0,'coverage':'observed-pages-only','deletionInference':'not_supported'})
    for scan in scans.values():
        current=result[int(scan['repositoryId'])];current['traversals']+=1
        current['lastCheckedAt']=max(current['lastCheckedAt'],scan['lastCheckedAt'])
        old=current['latestTraversal']
        if old is None or (scan['selectedAt'],scan['lastCheckedAt'],scan['scanId'])>(old['selectedAt'],old['lastCheckedAt'],old['scanId']):current['latestTraversal']=scan
    for repository,_release in items:result[repository]['observedReleaseIdentities']+=1
    return result


def coverage(pages,repositories,items,scans,links,as_of):
    states={name:sum(scan['state']==name for scan in scans.values()) for name in ('in_progress','partial','traversal_complete')}
    return {'policyVersion':POLICY,'asOf':as_of,'pages':len(pages),'repositories':len({page['repositoryId'] for page in pages}),
        'repositoryNamesProven':len(repositories),'scans':len(scans),'scanStates':states,'releaseIdentities':len(items),
        'assets':sum(len(item['assets']) for item in items.values()),'packageLinks':len(links),
        'exactDistributionLinks':sum(any(proof['kind']!='repository_context' for proof in link['assertions']) for link in links.values()),
        'scope':'observed-repository-metadata','deletionInference':'not_supported','downloadMeaning':'raw-asset-download-requests-not-users-or-npm-downloads',
        'staleAfterSeconds':STALE_SECONDS}


def project_releases(state,connection,*,as_of):
    as_of=timestamp(as_of);pages=[]
    rows=state.execute("SELECT p.*,t.scan_id,t.page,s.github_id,s.slot,s.max_pages,j.kind,j.scope,j.payload_json FROM release_pages p JOIN release_tasks t ON t.job_id=p.job_id JOIN release_scans s ON s.id=t.scan_id JOIN jobs j ON j.id=p.job_id WHERE p.observed_at<=? AND p.checked_at<=? AND s.slot<=? AND s.created_at<=? AND j.state='succeeded' AND j.finished_at<=? ORDER BY s.slot,t.page,p.job_id",(as_of,)*5)
    for row in rows:
        evidence=state.execute('SELECT representation,body_json FROM collection_evidence WHERE hash=?',(row['evidence_hash'],)).fetchone()
        if evidence is None or evidence['representation']!=RELEASE_FIELDS:raise ValueError('private Release page evidence missing')
        body=_json(evidence['body_json']);repository=str(row['github_id'])
        value={'repositoryId':repository,'scanId':_hash(['public-release-scan-v1',repository,row['slot'],row['max_pages']]),'selectedAt':row['slot'],'maxPages':row['max_pages'],'page':row['page'],
            'observedAt':row['observed_at'],'checkedAt':row['checked_at'],'responseStatus':row['response_status'],'evidenceHash':row['evidence_hash'],
            'endpoint':PublicGitRequest('releases',row['github_id'],page=row['page']).validate().endpoint,'body':body,'policyVersion':POLICY}
        value['id']=_hash(value);_page(value,as_of)
        if row['kind']!='github_releases' or row['scope']!=value['endpoint'] or _json(row['payload_json'])!={'scanId':row['scan_id'],'page':row['page']} or row['item_count']!=len(body['releases']):raise ValueError('private Release task/page identity differs')
        observations=[(r['release_id'],_json(r['metadata_json'])) for r in state.execute('SELECT * FROM release_observations WHERE job_id=? ORDER BY release_id',(row['job_id'],))]
        if observations!=sorted([(item['id'],item) for item in body['releases']],key=lambda item:item[0]):raise ValueError('private Release observation differs from original page')
        pages.append(value)
    if not pages:return None
    items,scans=_replay(pages);repositories={}
    for repository in sorted({int(page['repositoryId']) for page in pages}):
        row=state.execute('SELECT * FROM repository_observations WHERE github_id=? AND checked_at<=? AND observed_at<=? ORDER BY checked_at DESC,observed_at DESC,id DESC LIMIT 1',(repository,as_of,as_of)).fetchone()
        if not row:continue
        evidence=state.execute('SELECT * FROM collection_evidence WHERE hash=?',(row['evidence_hash'],)).fetchone()
        if evidence is None or evidence['representation']!=PUBLIC_FIELDS_VERSION:raise ValueError('Release repository evidence missing')
        body=_json(evidence['body_json']);metadata=normalize_repository(body)
        if metadata!=_json(row['metadata_json']):raise ValueError('Release repository metadata changed')
        value={'repositoryId':str(repository),'fullName':metadata['fullName'],'observedAt':row['observed_at'],'checkedAt':row['checked_at'],
            'responseStatus':row['response_status'],'evidenceHash':row['evidence_hash'],'body':body,'representation':PUBLIC_FIELDS_VERSION}
        repositories[repository]=_repository(value,as_of)
    links=package_links(connection,repositories,items);checks=repository_checks(pages,items,scans);create_schema(connection)
    connection.executemany('INSERT INTO public_release_pages VALUES (?,?,?,?,?,?)',[(page['id'],int(page['repositoryId']),page['scanId'],page['page'],page['checkedAt'],encode_json(page)) for page in pages])
    connection.executemany('INSERT INTO public_release_repositories VALUES (?,?)',[(identity,encode_json(value)) for identity,value in repositories.items()])
    connection.executemany('INSERT INTO public_release_items VALUES (?,?,?,?,?)',[(key[0],key[1],value['publishedAt'],value['observation']['pageId'],encode_json(value)) for key,value in items.items()])
    connection.executemany('INSERT INTO public_release_checks VALUES (?,?)',[(identity,encode_json(value)) for identity,value in checks.items()])
    connection.executemany('INSERT INTO public_release_package_links VALUES (?,?,?,?,?)',[(key[0],key[1],key[2],key[3],encode_json(value)) for key,value in links.items()])
    return coverage(pages,repositories,items,scans,links,as_of)


def validate_projection(connection,manifest):
    try:
        reference=sqlite3.connect(':memory:')
        try:
            create_schema(reference);expected={row[0]:row[1] for row in reference.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL')}
            actual={row[0]:row[1] for row in connection.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL')}
            if any(actual.get(name)!=sql for name,sql in expected.items()):raise ValueError('public Release SQL schema differs')
        finally:reference.close()
        as_of=timestamp(manifest['generatedAt']);pages=[];repositories={}
        for row in connection.execute('SELECT * FROM public_release_pages'):
            value=_page(_json(row['data_json']),as_of)
            if (row['id'],row['repository_id'],row['scan_id'],row['page'],row['checked_at'])!=(value['id'],int(value['repositoryId']),value['scanId'],value['page'],value['checkedAt']):raise ValueError('public Release page columns differ')
            pages.append(value)
        if not pages:raise ValueError('Release feature requires observed pages')
        items,scans=_replay(pages)
        observed_ids={int(page['repositoryId']) for page in pages}
        for row in connection.execute('SELECT * FROM public_release_repositories'):
            value=_repository(_json(row['data_json']),as_of)
            if row['repository_id']!=int(value['repositoryId']) or row['repository_id'] not in observed_ids:raise ValueError('unrelated Release repository proof')
            repositories[row['repository_id']]=value
        actual={}
        for row in connection.execute('SELECT * FROM public_release_items'):
            value=_json(row['data_json']);key=(row['repository_id'],row['release_id'])
            if value!=items.get(key) or row['published_at']!=value['publishedAt'] or row['page_id']!=value['observation']['pageId']:raise ValueError('Release item differs from page replay')
            actual[key]=value
        if actual!=items:raise ValueError('Release identity projection is incomplete')
        checks=repository_checks(pages,items,scans)
        actual_checks={row['repository_id']:_json(row['data_json']) for row in connection.execute('SELECT * FROM public_release_checks')}
        if actual_checks!=checks:raise ValueError('Release repository checks differ from page replay')
        links=package_links(connection,repositories,items)
        actual={(row['package_id'],row['version'],row['repository_id'],row['release_id']):_json(row['data_json']) for row in connection.execute('SELECT * FROM public_release_package_links')}
        if actual!=links:raise ValueError('Release package association differs from declared URLs')
        if coverage(pages,repositories,items,scans,links,as_of)!=manifest.get('releaseCoverage'):raise ValueError('Release coverage differs')
        return scans
    except (KeyError,TypeError,AttributeError,UnicodeError,OverflowError,RecursionError,json.JSONDecodeError,ServiceError) as error:
        raise ValueError('invalid public Release projection') from error
