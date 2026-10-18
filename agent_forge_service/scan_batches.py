"""Explicit declaration/Release scan rotation on existing durable scan/job tables."""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from .declarations import declaration_path, declaration_association
from .errors import ServiceError
from .manifests import enqueue_manifest
from .releases import enqueue_releases
from .storage import encode_json,timestamp
from .scheduling import _time,utc_slot

LANES=('manifests','releases')
KINDS={'manifests':'github_manifest','releases':'github_releases'}
PREFIX='public-scan-rotation:v1:'


def _hash(value):return hashlib.sha256(encode_json(value).encode('utf-8')).hexdigest()


def _bounds(budget,max_scans,interval_hours,retry_hours):
    for value,maximum in [(budget,1000),(max_scans,100)]:
        if type(value) is not int or not 0<=value<=maximum:raise ValueError('scan budgets must be bounded integers')
    if bool(budget)!=bool(max_scans):raise ValueError('scan GET and scan-count budgets must be enabled together')
    for value in [interval_hours,retry_hours]:
        if type(value) is not int or not 4<=value<=168:raise ValueError('scan intervals must be explicit hours in [4,168]')


@dataclass(frozen=True)
class ManifestProfile:
    path:str
    kind:str
    agent:str

    def __post_init__(self):
        declaration_path(self.path,self.kind)
        declaration_association(self.kind,self.agent)


@dataclass(frozen=True)
class ManifestBatchPolicy:
    budget:int
    max_scans:int
    interval_hours:int
    retry_hours:int
    profiles:tuple[ManifestProfile,...]

    def __post_init__(self):
        _bounds(self.budget,self.max_scans,self.interval_hours,self.retry_hours)
        if not isinstance(self.profiles,tuple) or not 1<=len(self.profiles)<=20 or any(not isinstance(profile,ManifestProfile) for profile in self.profiles) or len(set(self.profiles))!=len(self.profiles):
            raise ValueError('manifest profiles must be explicit, distinct and bounded')


@dataclass(frozen=True)
class ReleaseBatchPolicy:
    budget:int
    max_scans:int
    interval_hours:int
    retry_hours:int
    max_pages:int

    def __post_init__(self):
        _bounds(self.budget,self.max_scans,self.interval_hours,self.retry_hours)
        if type(self.max_pages) is not int or not 1<=self.max_pages<=10:raise ValueError('Release page cap must be in [1,10]')


def _state(connection,policy,lane,now):
    key=PREFIX+policy.policy_id+':'+lane
    row=connection.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
    try:
        if row:
            def pairs(items):
                result={}
                for name,value in items:
                    if name in result:raise ValueError('duplicate scan rotation state key')
                    result[name]=value
                return result
            if len(row[0].encode('utf-8'))>4096:raise ValueError('scan state too large')
            state=json.loads(row[0],object_pairs_hook=pairs)
            if not isinstance(state,dict) or set(state)!={'policyId','lane','cursor','lastPlannedAt'} or state['policyId']!=policy.policy_id or state['lane']!=lane:
                raise ValueError('scan rotation identity differs')
            if state['cursor'] is not None and (not isinstance(state['cursor'],str) or len(state['cursor'])!=64 or any(ch not in '0123456789abcdef' for ch in state['cursor'])):
                raise ValueError('invalid scan rotation cursor')
            if not isinstance(state['lastPlannedAt'],str) or timestamp(state['lastPlannedAt'])!=state['lastPlannedAt']:raise ValueError('invalid scan rotation clock')
            if now<state['lastPlannedAt']:raise ServiceError('scan_rotation_clock_wait','scan planning clock moved backwards',409)
            return key,state
        return key,{'policyId':policy.policy_id,'lane':lane,'cursor':None,'lastPlannedAt':now}
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError) as error:
        raise ServiceError('scan_rotation_state_invalid','scan rotation state needs recovery before planning',503) from error


def reconcile_scans(connection,now):
    for lane in LANES:
        table='manifest_scans' if lane=='manifests' else 'release_scans'
        tasks='manifest_tasks' if lane=='manifests' else 'release_tasks'
        connection.execute('UPDATE '+table+" SET state='failed',reason='terminal_job_failure',finished_at=? WHERE state IN ('queued','running') AND EXISTS(SELECT 1 FROM "+tasks+' t JOIN jobs j ON j.id=t.job_id WHERE t.scan_id='+table+".id AND j.state='dead')",(timestamp(now),))


def _targets(connection,config,lane,now):
    if lane=='manifests':
        ids=[row[0] for row in connection.execute('SELECT github_id FROM repositories WHERE first_seen_at<=? UNION SELECT github_id FROM discovery_candidates WHERE first_seen_at<=? ORDER BY github_id',(now,now))]
        return { _hash([lane,identity,profile.path,profile.kind,profile.agent]):(identity,profile) for identity in ids for profile in config.profiles }
    return {_hash([lane,row[0]]):(row[0],None) for row in connection.execute('SELECT github_id FROM repositories WHERE first_seen_at<=? ORDER BY github_id',(now,))}


def _facts(connection,lane,targets,now):
    table='manifest_scans' if lane=='manifests' else 'release_scans'
    result={}
    # One chronological pass avoids scanning the full history once per repository/profile.
    for row in connection.execute('SELECT * FROM '+table+' WHERE slot<=? AND created_at<=? ORDER BY slot DESC,created_at DESC,id DESC',(now,now)):
        identity=_hash([lane,row['github_id'],row['path'],row['format'],row['agent']]) if lane=='manifests' else _hash([lane,row['github_id']])
        if identity not in targets:continue
        item=result.setdefault(identity,{'active':None,'latest':None})
        if row['state'] in ('queued','running'):item['active']=row
        if item['latest'] is None and row['finished_at'] is not None and row['finished_at']<=now:item['latest']=row
    return result


def _remaining(connection,lane,scan,profile,config):
    if lane=='releases':return max(0,(scan['max_pages']-scan['next_page']+1) if scan else config.max_pages)
    total=5+profile.path.count('/')
    if not scan:return total
    progress=json.loads(scan['progress_json']);stage=progress['stage']
    if stage=='repository':return total
    if stage=='commit':return total-1
    if stage=='tree':return 2+len(progress['remaining'])
    if stage=='blob':return 2
    if stage=='verify':return 1
    raise ValueError('invalid active declaration stage')


def plan_scan_batches(connection,policy,*,now,slot):
    if all(getattr(policy,lane) is None for lane in LANES):return {},{}
    now,slot=timestamp(now),timestamp(slot);reconcile_scans(connection,now)
    quotas={};summaries={}
    for lane in LANES:
        config=getattr(policy,lane)
        if config is None:continue
        key,state=_state(connection,policy,lane,now);targets=_targets(connection,config,lane,now);facts=_facts(connection,lane,targets,now);due={}
        for identity,(repository,profile) in targets.items():
            item=facts.get(identity,{})
            active,last=item.get('active'),item.get('latest')
            success=last and last['state'] in (('complete','not_found','rejected') if lane=='manifests' else ('traversal_complete',))
            interval=config.interval_hours if success else config.retry_hours
            next_due=timestamp(_time(utc_slot(last['finished_at'],policy.slot_hours))+timedelta(hours=interval)) if last else now
            if active is not None or next_due<=now:due[identity]=(repository,profile,active)
        identities=sorted(due)
        if state['cursor'] is not None:identities=[item for item in identities if item>state['cursor']]+[item for item in identities if item<=state['cursor']]
        selected=identities[:config.max_scans];scans=[];remaining={};resumed=0
        for identity in selected:
            repository,profile,active=due[identity]
            if active is not None:
                if lane=='releases' and active['max_pages']!=config.max_pages:
                    raise ServiceError('scan_policy_conflict','active Release scan has a different page cap; finish or reconcile it before changing policy',409)
                scan_id=active['id'];resumed+=1
            elif lane=='manifests':scan_id=enqueue_manifest(connection,github_id=repository,path=profile.path,kind=profile.kind,agent=profile.agent,slot=slot,now=now)
            else:scan_id=enqueue_releases(connection,github_id=repository,slot=slot,max_pages=config.max_pages,now=now)
            scan=connection.execute('SELECT * FROM '+('manifest_scans' if lane=='manifests' else 'release_scans')+' WHERE id=?',(scan_id,)).fetchone()
            scans.append(scan_id);remaining[scan_id]=_remaining(connection,lane,scan,profile,config)
        if selected:state['cursor']=selected[-1]
        state['lastPlannedAt']=now
        connection.execute('INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,encode_json(state)))
        quotas[lane]=config.budget
        summaries[lane]={'scope':'known-public-repositories-explicit-profiles' if lane=='manifests' else 'observed-repository-release-metadata',
            'targets':len(targets),'due':len(due),'selected':len(scans),'deferred':len(due)-len(scans),'resumedScans':resumed,
            'selectedScanIds':scans,'remainingPathRequestsWithoutRetries':remaining,'intervalHours':config.interval_hours,'retryHours':config.retry_hours,
            'dailyRequestBudget':config.budget*(24//policy.slot_hours),'coverage':'not_guaranteed',
            'selectionSemantics':'fair-runtime-rotation-not-canonical-adoption-or-package-version-mapping',
            'capacityWarnings':['selected_full_paths_may_exceed_slot_budget'] if sum(remaining.values())>config.budget else []}
        if lane=='manifests':summaries[lane]['profiles']=[{'path':p.path,'kind':p.kind,'agent':p.agent} for p in config.profiles]
        else:summaries[lane]['maxPagesPerScan']=config.max_pages
    return quotas,summaries


def selected_scan_ids(connection,batch_id,lane):
    if lane not in LANES:raise ValueError('unsupported scan lane')
    row=connection.execute('SELECT summary_json FROM batch_runs WHERE id=?',(batch_id,)).fetchone()
    values=json.loads(row[0]).get(lane,{}).get('selectedScanIds',[]) if row else []
    if not isinstance(values,list) or len(values)>100 or any(not isinstance(value,str) or len(value)!=64 or any(ch not in '0123456789abcdef' for ch in value) for value in values) or len(set(values))!=len(values):
        raise ValueError('invalid scan batch membership')
    return tuple(values)


def task_ids(connection,batch_id,lane,*,pending=True):
    scans=selected_scan_ids(connection,batch_id,lane)
    if not scans:return ()
    table='manifest_tasks' if lane=='manifests' else 'release_tasks'
    return tuple(row[0] for row in connection.execute('SELECT t.job_id FROM '+table+' t JOIN jobs j ON j.id=t.job_id WHERE t.scan_id IN ('+','.join('?' for _ in scans)+')'+(" AND j.state IN ('pending','retry_wait','leased')" if pending else '')+' ORDER BY j.created_at,j.id',scans))


def scan_states(connection,batch_id,lane):
    scans=selected_scan_ids(connection,batch_id,lane)
    if not scans:return {}
    table='manifest_scans' if lane=='manifests' else 'release_scans'
    return dict(connection.execute('SELECT state,count(*) FROM '+table+' WHERE id IN ('+','.join('?' for _ in scans)+') GROUP BY state',scans))
