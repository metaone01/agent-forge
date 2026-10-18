"""Complete public ledger of local collection controls; no operator labels or request keys."""
import json
import sqlite3
from .source_controls import POLICY, AUTHORITY, public_event, replay, target_id
from .storage import encode_json, timestamp

COLUMNS={'source_control_events': {'seq','id','target_id','recorded_at','data_json'}}
COVERAGE_FIELDS={'policyVersion','authority','asOf','events','targets','disabledTargets','enabledTargets'}
HISTORY_LIMIT=20


def create_schema(connection):
    connection.executescript('''
        CREATE TABLE source_control_events (
            seq INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,target_id TEXT NOT NULL,
            recorded_at TEXT NOT NULL,data_json TEXT NOT NULL
        );
        CREATE INDEX source_control_history ON source_control_events(target_id,seq);
    ''')


def coverage(events,as_of):
    heads=replay(events,as_of,complete=True)
    disabled=sum(event['action']=='disable' for event in heads.values())
    return {'policyVersion':POLICY,'authority':AUTHORITY,'asOf':timestamp(as_of),'events':len(events),
        'targets':len(heads),'disabledTargets':disabled,'enabledTargets':len(heads)-disabled}


def project_controls(state,connection,*,as_of):
    rows=state.execute('SELECT * FROM collection_control_events WHERE recorded_at<=? ORDER BY seq',(timestamp(as_of),)).fetchall()
    if not rows:return None
    events=[public_event(row) for row in rows]
    result=coverage(events,as_of)
    create_schema(connection)
    connection.executemany('INSERT INTO source_control_events VALUES (?,?,?,?,?)',
        [(event['sequence'],event['id'],event['targetId'],event['recordedAt'],encode_json(event)) for event in events])
    return result


def validate_projection(connection,manifest):
    try:
        reference=sqlite3.connect(':memory:')
        try:
            create_schema(reference)
            expected={row[0]:row[1] for row in reference.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL')}
            actual={row[0]:row[1] for row in connection.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL')}
            if any(actual.get(name)!=sql for name,sql in expected.items()):
                raise ValueError('public source control SQL schema differs')
        finally:reference.close()
        events=[]
        for row in connection.execute('SELECT * FROM source_control_events ORDER BY seq'):
            value=json.loads(row['data_json'])
            if encode_json(value)!=row['data_json'] or (row['seq'],row['id'],row['target_id'],row['recorded_at'])!=(value['sequence'],value['id'],value['targetId'],value['recordedAt']):
                raise ValueError('source control row differs from its event')
            events.append(value)
        if not events or coverage(events,manifest['generatedAt'])!=manifest.get('sourceControlCoverage'):
            raise ValueError('source control coverage differs')
        return events
    except (KeyError,TypeError,AttributeError,json.JSONDecodeError) as error:
        raise ValueError('invalid source control projection') from error


def summary(connection,identity):
    rows=connection.execute('SELECT data_json FROM source_control_events WHERE target_id=? ORDER BY seq',(identity,)).fetchall()
    events=[json.loads(row['data_json']) for row in rows]
    if not events:raise ValueError('source control target has no event')
    head=events[-1]
    return {'id':identity,'target':head['target'],'collectionState':'disabled' if head['action']=='disable' else 'enabled',
        'policyVersion':POLICY,'authority':AUTHORITY,'latestEvent':head,'history':events[-HISTORY_LIMIT:],
        'historyCount':len(events),'historyTruncated':len(events)>HISTORY_LIMIT}
