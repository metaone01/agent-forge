"""Evidence-bound local metadata triage; no canonical adoption or safety approval."""
from __future__ import annotations
import hashlib,json,re,sqlite3
from .errors import ServiceError
from .github import GitHubRequest,credential_identity
from .storage import encode_json,timestamp

POLICY='candidate-metadata-review-v1'
AUTHORITY='local-worker-metadata-triage'
ACTIONS={'reject':('rejected','outside_catalog_scope'),'unsupported':('unsupported','unsupported_catalog_metadata'),
    'request_metadata':('needs_metadata','additional_metadata_required'),'reopen':('pending','reconsider_metadata')}
STATUSES={'not_recorded','rejected','unsupported','needs_metadata','pending','superseded'}
CONTEXT_FIELDS={'policyVersion','candidateId','kind','name','summary','type','agents','classification','reviewStatus','repository','declaredMetadata','issues','manifestEvidence'}
EVENT_FIELDS={'id','sequence','candidateId','evidenceFingerprint','action','reasonCode','reviewedAt','previousId','policyVersion','authority'}
MAX_CONTEXT_BYTES=256*1024


def _hash(value):return hashlib.sha256(encode_json(value).encode('utf8')).hexdigest()
def _sha(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):raise ValueError('invalid metadata review fingerprint')
    return value

def _identity(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:github-repository:[1-9][0-9]{0,18}|manifest:[0-9a-f]{64})',value):
        raise ValueError('metadata review needs a stable candidate identity')
    return value

def _json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate review JSON key')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite review JSON')))


def context_for(item):
    # Counts, read times and unrelated search-page changes do not change metadata triage.
    value={key:item[key] for key in CONTEXT_FIELDS if key not in {'policyVersion','candidateId','repository'}}
    value.update(policyVersion=POLICY,candidateId=item['id'],repository={key:item['repository'][key] for key in ['githubRepoId','fullName','url','topics','archived','disabled','defaultBranch']})
    validate_context(value)
    return value


def validate_context(value):
    if not isinstance(value,dict) or set(value)!=CONTEXT_FIELDS or len(encode_json(value).encode('utf8'))>MAX_CONTEXT_BYTES or value['policyVersion']!=POLICY:
        raise ValueError('invalid metadata review context')
    _identity(value['candidateId'])
    repo=value['repository']
    if not isinstance(repo,dict) or set(repo)!={'githubRepoId','fullName','url','topics','archived','disabled','defaultBranch'} or type(repo['githubRepoId']) is not int:
        raise ValueError('invalid metadata review repository context')
    GitHubRequest.repository(repo['githubRepoId']).validate();GitHubRequest.repository(repo['fullName']).validate()
    if repo['url']!='https://github.com/'+repo['fullName'] or not isinstance(repo['topics'],list) or len(repo['topics'])>100 or any(not isinstance(x,str) or len(x)>50 for x in repo['topics']):
        raise ValueError('invalid review repository URL/topics')
    if not isinstance(value['name'],str) or not 1<=len(value['name'])<=4096 or not isinstance(value['summary'],str) or len(value['summary'])>400 or any(ord(x)<32 for x in value['name']+value['summary']):
        raise ValueError('invalid review metadata text')
    if any(repo[key] is not None and type(repo[key]) is not bool for key in ['archived','disabled']) or repo['defaultBranch'] is not None and (not isinstance(repo['defaultBranch'],str) or len(repo['defaultBranch'])>255):
        raise ValueError('invalid metadata review repository flags')
    if value['kind']=='repository':
        if value['candidateId']!='github-repository:'+str(repo['githubRepoId']) or value['type'] is not None or value['agents']!=[] or value['declaredMetadata'] is not None or value['issues']!=[] or value['manifestEvidence'] is not None or value['classification']!='discovered' or value['reviewStatus']!='unreviewed':
            raise ValueError('review context invents a repository declaration')
    elif value['kind']=='manifest':
        from .declarations import declaration_path,declaration_association
        from .discovery_projection import ISSUE_CODES
        evidence=value['manifestEvidence']
        if not value['candidateId'].startswith('manifest:') or not isinstance(evidence,dict) or set(evidence)!={'path','format','agentAssociation','commitSha','blobSha','contentSha256','url','association'}:
            raise ValueError('invalid review manifest identity/evidence')
        declaration_path(evidence['path'],evidence['format']);declaration_association(evidence['format'],evidence['agentAssociation'])
        for key,length in [('commitSha',40),('blobSha',40),('contentSha256',64)]:
            if not isinstance(evidence[key],str) or not re.fullmatch('[0-9a-f]{'+str(length)+'}',evidence[key]):raise ValueError('invalid review Git hash')
        from urllib.parse import quote
        if evidence['url']!=repo['url']+'/blob/'+evidence['commitSha']+'/'+quote(evidence['path'],safe='/') or evidence['association']!='explicit-profile-not-compatibility-verification' or value['agents']!=[evidence['agentAssociation']] or value['type']!=('plugin' if evidence['format']=='claude_plugin' else 'skill'):
            raise ValueError('review context changes declared profile semantics')
        declared=value['declaredMetadata']
        if not isinstance(declared,dict) or set(declared)!={'name','description','version','license'} or any(x is not None and not isinstance(x,str) for x in declared.values()):raise ValueError('invalid review declared fields')
        if not isinstance(value['issues'],list) or len(value['issues'])>33 or any(not isinstance(x,dict) or x.get('code') not in ISSUE_CODES or set(x)-{'code','field','path','validator','canonicalId'} for x in value['issues']):raise ValueError('invalid review metadata issues')
        if value['classification']!='declared_profile' or value['reviewStatus'] not in {'needs_metadata','identity_conflict','review_pending'}:raise ValueError('invalid automated review context')
    else:raise ValueError('unsupported review candidate kind')
    return value


def fingerprint(item):return _hash(context_for(item))

def public_event(row):
    value={'sequence':row['seq'],'candidateId':row['candidate_id'],'evidenceFingerprint':row['fingerprint'],'action':row['action'],
        'reasonCode':row['reason_code'],'reviewedAt':row['reviewed_at'],'previousId':row['previous_id'],'policyVersion':POLICY,'authority':AUTHORITY}
    value['id']=_hash(value)
    if value['id']!=row['id']:raise ValueError('private metadata review event changed')
    return value


def validate_event(value,as_of):
    if not isinstance(value,dict) or set(value)!=EVENT_FIELDS or type(value['sequence']) is not int or not 1<=value['sequence']<=2**53-1:
        raise ValueError('invalid metadata review event shape/order')
    _identity(value['candidateId']);_sha(value['evidenceFingerprint']);_sha(value['id'])
    if value['previousId'] is not None:_sha(value['previousId'])
    if value['policyVersion']!=POLICY or value['authority']!=AUTHORITY or value['action'] not in ACTIONS or value['reasonCode']!=ACTIONS[value['action']][1]:raise ValueError('invalid metadata triage meaning')
    if not isinstance(value['reviewedAt'],str) or timestamp(value['reviewedAt'])!=value['reviewedAt'] or value['reviewedAt']>as_of:raise ValueError('review is beyond publication cutoff')
    if value['id']!=_hash({key:entry for key,entry in value.items() if key!='id'}):raise ValueError('metadata review event hash differs')


def _has_reviews(connection):return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE name='candidate_review_decisions'").fetchone())

def lookup_candidate(connection,candidate_id,now):
    from .discovery_projection import _repositories,_manifests,validate_item
    _identity(candidate_id);now=timestamp(now)
    items=_repositories(connection,now,repository_id=int(candidate_id.split(':')[1])) if candidate_id.startswith('github-repository:') else _manifests(connection,now,candidate_id=candidate_id.split(':')[1])
    item=next(iter(items),None)
    if item is None:raise ServiceError('candidate_not_found','candidate has no eligible metadata evidence',404)
    validate_item(item,now);return item


def inspect_review(connection,candidate_id,*,now,limit=20):
    if type(limit) is not int or not 1<=limit<=100:raise ValueError('review history limit outside range')
    item=lookup_candidate(connection,candidate_id,now);rows=[]
    if _has_reviews(connection):rows=connection.execute('SELECT * FROM candidate_review_decisions WHERE candidate_id=? ORDER BY seq DESC LIMIT ?',(candidate_id,limit)).fetchall()
    return {'candidateId':candidate_id,'evidenceFingerprint':fingerprint(item),'expectedHead':rows[0]['id'] if rows else 'none',
        'metadataContext':context_for(item),'history':[public_event(row) for row in reversed(rows)],'reviewSupported':_has_reviews(connection),
        'policyVersion':POLICY,'authority':AUTHORITY,'canonicalWrites':0}


def record_review(connection,*,candidate_id,expected_fingerprint,expected_head,action,reason_code,request_key,operator_label,now):
    if connection.in_transaction:raise ValueError('review needs its own compare-and-set transaction')
    _identity(candidate_id);_sha(expected_fingerprint)
    if expected_head!='none':_sha(expected_head)
    if action not in ACTIONS or reason_code!=ACTIONS[action][1]:raise ValueError('review needs an explicit supported action and matching rule code')
    credential_identity(request_key);credential_identity(operator_label);now=timestamp(now)
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        old=connection.execute('SELECT * FROM candidate_review_decisions WHERE request_key=?',(request_key,)).fetchone()
        if old:
            if (old['candidate_id'],old['fingerprint'],old['previous_id'] or 'none',old['action'],old['reason_code'],old['operator_label'])!=(candidate_id,expected_fingerprint,expected_head,action,reason_code,operator_label):
                raise ServiceError('review_request_conflict','request key cannot change its review operation',409)
            return {'decision':public_event(old),'replayed':True,'canonicalWrites':0}
        item=lookup_candidate(connection,candidate_id,now);context=context_for(item);actual=_hash(context)
        if actual!=expected_fingerprint:raise ServiceError('review_evidence_changed','candidate metadata changed since inspection',409)
        head=connection.execute('SELECT * FROM candidate_review_decisions WHERE candidate_id=? ORDER BY seq DESC LIMIT 1',(candidate_id,)).fetchone()
        if (head['id'] if head else 'none')!=expected_head:raise ServiceError('review_head_conflict','review history changed since inspection',409)
        if head and now<head['reviewed_at']:raise ServiceError('review_clock_wait','review clock moved backward',409)
        if action=='reopen' and (head is None or head['fingerprint']!=actual or head['action']=='reopen'):
            raise ServiceError('review_transition_invalid','reopen needs a current triage decision',409)
        stored=connection.execute('SELECT data_json FROM candidate_review_contexts WHERE fingerprint=?',(actual,)).fetchone()
        if stored and stored[0]!=encode_json(context):raise ValueError('review context hash collision or mutation')
        connection.execute('INSERT OR IGNORE INTO candidate_review_contexts VALUES (?,?,?)',(actual,candidate_id,encode_json(context)))
        seq=connection.execute('SELECT coalesce(max(seq),0)+1 FROM candidate_review_decisions').fetchone()[0]
        event={'sequence':seq,'candidateId':candidate_id,'evidenceFingerprint':actual,'action':action,'reasonCode':reason_code,'reviewedAt':now,
            'previousId':None if expected_head=='none' else expected_head,'policyVersion':POLICY,'authority':AUTHORITY}
        identity=_hash(event);event['id']=identity;validate_event(event,now)
        connection.execute('INSERT INTO candidate_review_decisions VALUES (?,?,?,?,?,?,?,?,?,?)',(seq,identity,candidate_id,actual,action,reason_code,now,event['previousId'],request_key,operator_label))
        return {'decision':event,'replayed':False,'canonicalWrites':0}


def read_review(path,candidate_id,*,now,limit=20):
    if path.is_symlink() or not path.is_file():raise ValueError('review inspect needs an existing unlinked private state')
    connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);connection.row_factory=sqlite3.Row
    try:return inspect_review(connection,candidate_id,now=now,limit=limit)
    finally:connection.close()
