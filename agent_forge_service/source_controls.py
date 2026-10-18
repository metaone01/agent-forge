"""Local acquisition controls. Operator decisions never remove public metadata facts."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from .errors import ServiceError
from .github import GitHubRequest, credential_identity
from .storage import encode_json, timestamp

POLICY = 'source-collection-controls-v1'
AUTHORITY = 'local-worker-collection-control'
ACTIONS = {'disable': 'operator_collection_pause', 'enable': 'operator_collection_resume'}
EVENT_FIELDS = {'id','sequence','targetId','target','action','reasonCode','recordedAt','previousId','policyVersion','authority'}


def _hash(value):
    return hashlib.sha256(encode_json(value).encode('utf-8')).hexdigest()


def target(value):
    if not isinstance(value,dict) or not isinstance(value.get('kind'),str):
        raise ValueError('collection target needs an explicit kind')
    kind = value['kind']
    fields = {'provider': {'kind','provider'}, 'github_repository': {'kind','repositoryId'},
        'github_repository_name': {'kind','fullName'}, 'github_topic': {'kind','topic'},
        'npm_package': {'kind','package'}, 'curated_file': {'kind','repositoryId','path'}}
    if kind not in fields or set(value) != fields[kind]:
        raise ValueError('unsupported collection target fields')
    result = dict(value)
    if kind == 'provider':
        if value['provider'] not in ('github.com','npmjs.org'):
            raise ValueError('unsupported collection provider')
    if kind in ('github_repository','curated_file'):
        if type(value['repositoryId']) is not int or not 1 <= value['repositoryId'] < 2**53:
            raise ValueError('collection target needs a stable GitHub repository ID')
        GitHubRequest.repository(value['repositoryId']).validate()
    if kind == 'github_repository_name':
        GitHubRequest.repository(value['fullName']).validate()
        if not isinstance(value['fullName'],str):
            raise ValueError('collection target needs an explicit repository name')
        result['fullName'] = value['fullName'].lower()
    if kind == 'github_topic':
        GitHubRequest.search(topic=value['topic'],field='created',start='2000-01-01T00:00:00Z',end='2000-01-01T00:00:01Z')
        result['topic'] = value['topic'].lower()
    if kind == 'npm_package':
        from .downloads import package_name
        result['package'] = package_name(value['package'])
    if kind == 'curated_file':
        path = value['path']
        if not isinstance(path,str) or len(path)>1024 or not path.endswith('.md') or any(not re.fullmatch(r'[A-Za-z0-9_.-]+',part) or part in ('.','..','.git') for part in path.split('/')):
            raise ValueError('collection target needs a literal Markdown path')
    return result


def target_id(value):
    return 'collection-source:' + _hash(target(value))


def public_event(row):
    descriptor = json.loads(row['target_json'])
    if encode_json(descriptor)!=row['target_json'] or len(row['target_json'].encode('utf-8'))>2048:
        raise ValueError('private collection control target changed')
    value = {'sequence':row['seq'],'targetId':row['target_id'],'target':descriptor,
        'action':row['action'],'reasonCode':row['reason_code'],'recordedAt':row['recorded_at'],
        'previousId':row['previous_id'],'policyVersion':POLICY,'authority':AUTHORITY}
    value['id'] = _hash(value)
    if row['id'] != value['id']:
        raise ValueError('private collection control event changed')
    return value


def validate_event(value, as_of):
    if not isinstance(value,dict) or set(value)!=EVENT_FIELDS or type(value['sequence']) is not int or not 1<=value['sequence']<2**53:
        raise ValueError('invalid public collection control event')
    if target(value['target'])!=value['target'] or target_id(value['target'])!=value['targetId']:
        raise ValueError('collection control target identity differs')
    if not isinstance(value['action'],str) or value['action'] not in ACTIONS or value['reasonCode']!=ACTIONS[value['action']] or value['policyVersion']!=POLICY or value['authority']!=AUTHORITY:
        raise ValueError('invalid collection control policy/action')
    if not isinstance(value['recordedAt'],str) or timestamp(value['recordedAt'])!=value['recordedAt'] or value['recordedAt']>timestamp(as_of):
        raise ValueError('collection control event is beyond cutoff')
    if value['previousId'] is not None and (not isinstance(value['previousId'],str) or not re.fullmatch('[0-9a-f]{64}',value['previousId'])):
        raise ValueError('invalid collection control parent')
    if not isinstance(value['id'],str) or value['id']!=_hash({key:item for key,item in value.items() if key!='id'}):
        raise ValueError('collection control event digest differs')
    return value


def replay(events, as_of, *, complete=False):
    heads = {}; previous_sequence = 0; previous_time = None
    for event in events:
        validate_event(event,as_of)
        old = heads.get(event['targetId'])
        if complete and event['sequence']!=previous_sequence+1:
            raise ValueError('collection control ledger has a missing sequence')
        if event['sequence']<=previous_sequence or previous_time is not None and event['recordedAt']<previous_time:
            raise ValueError('collection controls are reordered')
        if event['previousId']!=(old['id'] if old else None) or event['action']==('disable' if old and old['action']=='disable' else 'enable'):
            raise ValueError('collection control chain/transition differs')
        heads[event['targetId']]=event
        previous_sequence,previous_time=event['sequence'],event['recordedAt']
    return heads


def job_targets(connection, kind, payload):
    if kind not in ('github_repository','github_discovery','github_manifest','github_releases','github_recommendation','npm_downloads'):
        return []
    values = [{'kind':'provider','provider':'npmjs.org' if kind=='npm_downloads' else 'github.com'}]
    if kind == 'github_repository':
        repository = payload.get('repository')
        if type(repository) is int:
            values.append({'kind':'github_repository','repositoryId':repository})
        elif isinstance(repository,str):
            values.append({'kind':'github_repository_name','fullName':repository})
    elif kind == 'npm_downloads' and isinstance(payload.get('package'),str):
        values.append({'kind':'npm_package','package':payload['package']})
    elif kind == 'github_discovery':
        row=connection.execute('SELECT topic FROM discovery_runs WHERE id=?',(payload.get('runId'),)).fetchone()
        if row:values.append({'kind':'github_topic','topic':row['topic']})
    elif kind in ('github_manifest','github_releases'):
        table='manifest_scans' if kind=='github_manifest' else 'release_scans'
        row=connection.execute('SELECT github_id FROM '+table+' WHERE id=?',(payload.get('scanId'),)).fetchone()
        if row:values.append({'kind':'github_repository','repositoryId':row['github_id']})
    elif kind == 'github_recommendation':
        row=connection.execute('SELECT s.definition_json FROM recommendation_scans r JOIN recommendation_sources s ON s.id=r.source_id WHERE r.id=?',(payload.get('scanId'),)).fetchone()
        if row:
            definition=json.loads(row['definition_json'])
            values += [{'kind':'github_repository','repositoryId':definition['repositoryId']},
                {'kind':'curated_file','repositoryId':definition['repositoryId'],'path':definition['path']}]
    normalized=[]
    for value in values:
        try:normalized.append(target(value))
        except (ValueError,TypeError):pass  # Invalid job payloads still reach their existing validation/dead path.
    return normalized


def bind_job(connection, identity, kind, payload):
    for value in job_targets(connection,kind,payload):
        connection.execute('INSERT OR IGNORE INTO collection_job_sources VALUES (?,?,?)',(identity,target_id(value),encode_json(value)))


def backfill_jobs(connection):
    rows=connection.execute('SELECT id,kind,payload_json FROM jobs WHERE NOT EXISTS(SELECT 1 FROM collection_job_sources b WHERE b.job_id=jobs.id)').fetchall()
    for row in rows:bind_job(connection,row['id'],row['kind'],json.loads(row['payload_json']))


def enabled_clause(alias='jobs'):
    if alias not in ('jobs','j'):
        raise ValueError('invalid collection job SQL alias')
    # Named lookups use the most recently observed alias IDs; this is not a new identity claim.
    return f'''NOT EXISTS (
        SELECT 1 FROM collection_control_events e
        WHERE e.action='disable' AND e.seq=(SELECT max(h.seq) FROM collection_control_events h WHERE h.target_id=e.target_id)
        AND (
            EXISTS(SELECT 1 FROM collection_job_sources b WHERE b.job_id={alias}.id AND b.target_id=e.target_id)
            OR (json_extract(e.target_json,'$.kind')='github_repository' AND {alias}.kind='github_repository'
                AND {alias}.scope LIKE 'repos/%' AND EXISTS(
                    SELECT 1 FROM repository_aliases a WHERE a.alias=lower(substr({alias}.scope,7))
                    AND a.github_id=json_extract(e.target_json,'$.repositoryId')
                    AND a.last_checked_at=(SELECT max(x.last_checked_at) FROM repository_aliases x WHERE x.alias=a.alias)))
            OR (json_extract(e.target_json,'$.kind')='github_repository_name' AND EXISTS(
                SELECT 1 FROM collection_job_sources b JOIN repositories r ON r.github_id=json_extract(b.target_json,'$.repositoryId')
                WHERE b.job_id={alias}.id AND lower(r.full_name)=json_extract(e.target_json,'$.fullName')))
        ))'''


def has_controls(connection):
    return bool(connection.execute('SELECT 1 FROM collection_control_events LIMIT 1').fetchone())


def inspect_control(connection, value, *, limit=20):
    value=target(value);identity=target_id(value)
    if type(limit) is not int or not 1<=limit<=100:raise ValueError('control history limit must be in [1,100]')
    present=connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_control_events'").fetchone()
    all_events=[public_event(row) for row in connection.execute('SELECT * FROM collection_control_events ORDER BY seq')] if present else []
    if all_events:replay(all_events,all_events[-1]['recordedAt'],complete=True)
    events=[event for event in all_events if event['targetId']==identity]
    return {'targetId':identity,'target':value,'collectionState':'disabled' if events and events[-1]['action']=='disable' else 'enabled',
        'latestEvent':events[-1] if events else None,'history':events[-limit:],'historyCount':len(events),'historyTruncated':len(events)>limit,
        'policyVersion':POLICY,'authority':AUTHORITY,'stateVersion':connection.execute('PRAGMA user_version').fetchone()[0]}


def record_control(connection, *, value, action, reason_code, expected_head, request_key, operator_label, now):
    value=target(value);identity=target_id(value);now=timestamp(now)
    if action not in ACTIONS or reason_code!=ACTIONS[action]:raise ValueError('control action needs its fixed factual reason')
    for label in (request_key,operator_label):credential_identity(label)
    if expected_head!='none' and (not isinstance(expected_head,str) or not re.fullmatch('[0-9a-f]{64}',expected_head)):
        raise ValueError('control needs an explicit expected head')
    if connection.in_transaction:raise ValueError('source control requires its own transaction')
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        old=connection.execute('SELECT * FROM collection_control_events WHERE request_key=?',(request_key,)).fetchone()
        if old:
            if (old['target_id'],old['action'],old['reason_code'],old['previous_id'],old['operator_label'])!=(identity,action,reason_code,None if expected_head=='none' else expected_head,operator_label):
                raise ServiceError('control_request_conflict','control request key has different input',409)
            event=public_event(old);validate_event(event,max(now,event['recordedAt']))
            return {'event':event,'replayed':True,'canonicalWrites':0}
        rows=connection.execute('SELECT * FROM collection_control_events ORDER BY seq').fetchall()
        if rows and now<rows[-1]['recorded_at']:raise ServiceError('control_clock_wait','collection control clock moved backwards',409)
        heads=replay([public_event(row) for row in rows],now,complete=True)
        head=heads.get(identity)
        if expected_head!=(head['id'] if head else 'none'):raise ServiceError('control_head_conflict','collection control head changed',409)
        if action==('disable' if head and head['action']=='disable' else 'enable'):
            raise ServiceError('control_transition_invalid','collection state already has the requested value',409)
        event={'sequence':rows[-1]['seq']+1 if rows else 1,'targetId':identity,'target':value,'action':action,'reasonCode':reason_code,
            'recordedAt':now,'previousId':head['id'] if head else None,'policyVersion':POLICY,'authority':AUTHORITY}
        event['id']=_hash(event);validate_event(event,now)
        backfill_jobs(connection)
        connection.execute('INSERT INTO collection_control_events VALUES (?,?,?,?,?,?,?,?,?,?)',
            (event['sequence'],event['id'],identity,encode_json(value),action,reason_code,now,event['previousId'],request_key,operator_label))
        return {'event':event,'replayed':False,'canonicalWrites':0}


def read_control(path,value,*,limit=20):
    if path.is_symlink() or not path.is_file():raise ValueError('control inspect needs an existing unlinked private state')
    connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);connection.row_factory=sqlite3.Row
    try:return inspect_control(connection,value,limit=limit)
    finally:connection.close()
