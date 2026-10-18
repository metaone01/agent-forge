"""Public local-triage facts, strict cutoff replay and bounded discovery summaries."""
from __future__ import annotations
import sqlite3
from .candidate_reviews import (POLICY,AUTHORITY,ACTIONS,STATUSES,_hash,_json,context_for,validate_context,public_event,validate_event)
from .storage import encode_json

COLUMNS={'discovery_review_contexts':{'fingerprint','candidate_id','data_json'},
    'discovery_review_events':{'seq','id','candidate_id','fingerprint','reviewed_at','data_json'}}
SUMMARY_FIELDS={'policyVersion','authority','status','evidenceFingerprint','evidenceMatches','latestDecision','history','historyCount','historyTruncated'}
HISTORY_LIMIT=20


def create_schema(connection):
    connection.executescript('''
        CREATE TABLE discovery_review_contexts (
            fingerprint TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,data_json TEXT NOT NULL,
            UNIQUE(candidate_id,fingerprint)
        );
        CREATE TABLE discovery_review_events (
            seq INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,candidate_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,reviewed_at TEXT NOT NULL,data_json TEXT NOT NULL,
            FOREIGN KEY(candidate_id,fingerprint) REFERENCES discovery_review_contexts(candidate_id,fingerprint)
        );
        CREATE INDEX discovery_review_history ON discovery_review_events(candidate_id,seq);
    ''')


def _replay(events,as_of):
    heads={}
    for event in events:
        validate_event(event,as_of);old=heads.get(event['candidateId'])
        if event['previousId']!=(old['id'] if old else None) or old and (event['sequence']<=old['sequence'] or event['reviewedAt']<old['reviewedAt']):
            raise ValueError('metadata review chain is incomplete or reordered')
        if event['action']=='reopen' and (old is None or old['evidenceFingerprint']!=event['evidenceFingerprint'] or old['action']=='reopen'):
            raise ValueError('invalid metadata reopen transition')
        heads[event['candidateId']]=event
    return heads


def project_reviews(state,connection,*,as_of):
    rows=state.execute('SELECT * FROM candidate_review_decisions WHERE reviewed_at<=? ORDER BY seq',(as_of,)).fetchall()
    if not rows:return None
    events=[public_event(row) for row in rows];_replay(events,as_of)
    create_schema(connection)
    contexts={}
    for row,event in zip(rows,events):
        raw=state.execute('SELECT data_json,candidate_id FROM candidate_review_contexts WHERE fingerprint=?',(row['fingerprint'],)).fetchone()
        if raw is None:raise ValueError('metadata review context missing')
        value=_json(raw['data_json']);validate_context(value)
        if encode_json(value)!=raw['data_json'] or _hash(value)!=row['fingerprint'] or value['candidateId']!=row['candidate_id'] or raw['candidate_id']!=row['candidate_id']:
            raise ValueError('private metadata review context changed')
        contexts[row['fingerprint']]=value
    connection.executemany('INSERT INTO discovery_review_contexts VALUES (?,?,?)',((fp,value['candidateId'],encode_json(value)) for fp,value in sorted(contexts.items())))
    connection.executemany('INSERT INTO discovery_review_events VALUES (?,?,?,?,?,?)',((row['seq'],row['id'],row['candidate_id'],row['fingerprint'],row['reviewed_at'],encode_json(event)) for row,event in zip(rows,events)))
    return review_coverage(connection,as_of)


def summary(connection,item):
    target=_hash(context_for(item))
    rows=connection.execute('SELECT data_json FROM discovery_review_events WHERE candidate_id=? ORDER BY seq',(item['id'],)).fetchall()
    events=[_json(row[0]) for row in rows]
    latest=events[-1] if events else None;matches=latest is not None and latest['evidenceFingerprint']==target
    status=ACTIONS[latest['action']][0] if matches else 'superseded' if latest else 'not_recorded'
    return {'policyVersion':POLICY,'authority':AUTHORITY,'status':status,'evidenceFingerprint':target,'evidenceMatches':matches,
        'latestDecision':latest,'history':events[-HISTORY_LIMIT:],'historyCount':len(events),'historyTruncated':len(events)>HISTORY_LIMIT}


def validate_summary(value,item,as_of):
    if not isinstance(value,dict) or set(value)!=SUMMARY_FIELDS or value['policyVersion']!=POLICY or value['authority']!=AUTHORITY or value['status'] not in STATUSES or value['evidenceFingerprint']!=_hash(context_for(item)) or type(value['evidenceMatches']) is not bool:
        raise ValueError('invalid bounded metadata review summary')
    if type(value['historyCount']) is not int or value['historyCount']<0 or type(value['historyTruncated']) is not bool or not isinstance(value['history'],list) or len(value['history'])!=min(HISTORY_LIMIT,value['historyCount']) or value['historyTruncated']!=(value['historyCount']>HISTORY_LIMIT):
        raise ValueError('invalid metadata review history budget')
    if value['historyCount']==0:
        if value['latestDecision'] is not None or value['status']!='not_recorded' or value['evidenceMatches']:raise ValueError('empty review invents a decision')
        return
    for event in value['history']:
        validate_event(event,as_of)
        if event['candidateId']!=item['id']:raise ValueError('review history belongs to another candidate')
    if value['latestDecision']!=value['history'][-1]:raise ValueError('metadata review head differs from history')
    matches=value['latestDecision']['evidenceFingerprint']==value['evidenceFingerprint']
    expected=ACTIONS[value['latestDecision']['action']][0] if matches else 'superseded'
    if value['status']!=expected or value['evidenceMatches']!=matches:raise ValueError('review disposition differs from evidence')
    for old,new in zip(value['history'],value['history'][1:]):
        if new['previousId']!=old['id'] or new['sequence']<=old['sequence'] or new['reviewedAt']<old['reviewedAt']:raise ValueError('bounded review history reordered')


def review_coverage(connection,as_of):
    events=connection.execute('SELECT count(*),count(DISTINCT candidate_id) FROM discovery_review_events').fetchone()
    statuses={row[0]:row[1] for row in connection.execute("SELECT json_extract(data_json,'$.metadataReview.status'),count(*) FROM discoveries GROUP BY 1") if row[0] is not None}
    return {'policyVersion':POLICY,'authority':AUTHORITY,'asOf':as_of,'events':events[0],'candidatesWithRecordedHistory':events[1],
        'visibleDispositions':statuses,'meaning':'metadata-scope-triage-not-canonical-adoption-or-safety-approval'}


def _validate_projection(connection,manifest):
    as_of=manifest['generatedAt'];reference=sqlite3.connect(':memory:')
    try:
        create_schema(reference);expected={row[0]:row[1] for row in reference.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL')}
        actual={row[0]:row[1] for row in connection.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL')}
        if any(actual.get(name)!=sql for name,sql in expected.items()):raise ValueError('public review SQL schema differs')
    finally:reference.close()
    contexts={}
    for row in connection.execute('SELECT * FROM discovery_review_contexts'):
        value=_json(row['data_json']);validate_context(value)
        if encode_json(value)!=row['data_json'] or _hash(value)!=row['fingerprint'] or value['candidateId']!=row['candidate_id']:raise ValueError('public review context hash/identity differs')
        contexts[row['fingerprint']]=value
    events=[];used=set()
    for row in connection.execute('SELECT * FROM discovery_review_events ORDER BY seq'):
        event=_json(row['data_json']);validate_event(event,as_of)
        if encode_json(event)!=row['data_json'] or (row['seq'],row['id'],row['candidate_id'],row['fingerprint'],row['reviewed_at'])!=(event['sequence'],event['id'],event['candidateId'],event['evidenceFingerprint'],event['reviewedAt']) or event['evidenceFingerprint'] not in contexts or contexts[event['evidenceFingerprint']]['candidateId']!=event['candidateId']:
            raise ValueError('public review columns/context differ')
        events.append(event);used.add(event['evidenceFingerprint'])
    if not events or used!=set(contexts):raise ValueError('empty or unreferenced review projection')
    _replay(events,as_of)
    for row in connection.execute('SELECT data_json FROM discoveries'):
        item=_json(row[0]);value=item.get('metadataReview')
        validate_summary(value,item,as_of)
        if value!=summary(connection,item):raise ValueError('discovery review differs from complete ledger')
    if manifest.get('discoveryReviewCoverage')!=review_coverage(connection,as_of):raise ValueError('review coverage differs')


def validate_projection(connection,manifest):
    try:_validate_projection(connection,manifest)
    except (AttributeError,KeyError,TypeError,RecursionError,OverflowError) as error:
        raise ValueError("invalid public metadata review shape") from error
