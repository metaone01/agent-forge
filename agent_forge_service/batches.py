"""Durable bounded batches; refresh priority never changes canonical or public metrics."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from .collector import enqueue_repository, run_repository_jobs
from .discovery import DiscoveryPolicy, enqueue_history, enqueue_incremental, run_discovery_jobs
from .errors import ServiceError
from .github import GitHubRequest
from .jobs import JobQueue, _text
from .scheduling import RefreshPolicy, RefreshResource, RankedResource, _time, select_refresh, utc_slot, refresh_capacity
from .storage import encode_json, timestamp, utc_now
from .download_batches import DownloadsBatchPolicy, DOWNLOAD_LANES, plan_download_batch, satisfy_download_check
from .downloads import DownloadsClient, run_download_jobs
from .recommendation_batches import (LANE as RECOMMENDATION_LANE, RecommendationsBatchPolicy, plan_recommendation_batch, selected_scans, finish_recommendation_checks)
from .recommendations import RecommendationClient, run_recommendation_jobs

from .scan_batches import (LANES as SCAN_LANES, ManifestProfile, ManifestBatchPolicy, ReleaseBatchPolicy, plan_scan_batches, selected_scan_ids, task_ids, scan_states, reconcile_scans)
from .public_git import AnonymousGitClient
from .manifests import run_manifest_jobs
from .releases import run_release_jobs

LANES = ('ordinary', 'fast', 'bootstrap', 'incremental', 'history')


def _hash(value):
    return hashlib.sha256(encode_json(value).encode('utf-8')).hexdigest()


def _coverage_start(value):
    if not isinstance(value,str): raise ValueError('coverage starts must be timestamp strings')
    result = timestamp(value)
    if not result.endswith('.000000Z'): raise ValueError('coverage starts need whole UTC seconds')
    return result


def _keys(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError('batch policy has missing or unsupported fields')


@dataclass(frozen=True)
class BatchPolicy:
    policy_id: str
    slot_hours: int
    refresh: RefreshPolicy
    refresh_budget: int
    bootstrap_budget: int
    discovery_budget: int
    history_budget: int
    topics: tuple[str, ...]
    discovery_start: str
    overlap_hours: int
    history_start: str
    history_lag_hours: int
    history_chunk_hours: int
    discovery: DiscoveryPolicy
    downloads: DownloadsBatchPolicy | None = None
    recommendations: RecommendationsBatchPolicy | None = None
    manifests: ManifestBatchPolicy | None = None
    releases: ReleaseBatchPolicy | None = None

    def __post_init__(self):
        if not isinstance(self.policy_id, str) or not self.policy_id or len(self.policy_id) > 80 or any(ord(c) < 33 or ord(c) == 127 for c in self.policy_id):
            raise ValueError('policy needs a bounded non-secret identity')
        if type(self.slot_hours) is not int or self.slot_hours not in (4, 8):
            raise ValueError('batch slots must explicitly be four or eight hours')
        if not isinstance(self.refresh, RefreshPolicy) or self.refresh.policy_id != self.policy_id or self.refresh.fast_hours != self.slot_hours or self.refresh.ordinary_hours != 24:
            raise ValueError('batch refresh requires matching policy, fast slot and daily ordinary interval')
        if type(self.refresh_budget) is not int or not 0<=self.refresh_budget<=100000:
            raise ValueError('refresh job budget must explicitly be in [0,100000]')
        for budget in (self.bootstrap_budget,self.discovery_budget,self.history_budget):
            if type(budget) is not int or not 0 <= budget <= 1000:
                raise ValueError('each batch budget must be explicitly in [0,1000]')
        if not isinstance(self.topics, tuple) or len(self.topics) > 20 or any(not isinstance(t,str) for t in self.topics) or len(set(self.topics)) != len(self.topics):
            raise ValueError('batch topics must be a distinct bounded tuple')
        for topic in self.topics:
            GitHubRequest.search(topic=topic, field='created', start='2000-01-01T00:00:00Z', end='2000-01-01T00:00:01Z')
        for value in (self.discovery_start,self.history_start):
            _coverage_start(value)
        for value, low, high in ((self.overlap_hours,0,168),(self.history_lag_hours,0,8760),(self.history_chunk_hours,1,8760)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError('invalid explicit discovery/history interval')
        if self.downloads is not None and not isinstance(self.downloads, DownloadsBatchPolicy):
            raise ValueError("download policy must be explicit")
        if self.recommendations is not None:
            if not isinstance(self.recommendations, RecommendationsBatchPolicy) or any(hours % self.slot_hours for hours in (self.recommendations.interval_hours,self.recommendations.retry_hours)):
                raise ValueError('curated intervals must align with explicit batch slots')
        for lane,expected in (("manifests",ManifestBatchPolicy),("releases",ReleaseBatchPolicy)):
            config=getattr(self,lane)
            if config is not None and (not isinstance(config,expected) or any(hours%self.slot_hours for hours in (config.interval_hours,config.retry_hours))):
                raise ValueError('scan policy type and intervals must match the batch slots')
        if not isinstance(self.discovery, DiscoveryPolicy):
            raise ValueError('batch discovery shard policy is required')


def load_policy(path: Path) -> BatchPolicy:
    if path.stat().st_size > 65536:
        raise ValueError('batch policy too large')
    def pairs(items):
        value = {}
        for key,item in items:
            if key in value: raise ValueError('duplicate batch policy field')
            value[key] = item
        return value
    data = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs)
    if not isinstance(data, dict) or type(data.get('schemaVersion')) is not int or data['schemaVersion'] != 1:
        raise ValueError('unsupported batch policy schema')
    required = {'schemaVersion','policyId','slotHours','refresh','budgets','discovery'}
    optional = {'downloads','recommendations','manifests','releases'}
    if not required <= data.keys() or data.keys() - required - optional:
        raise ValueError('batch policy has missing or unsupported fields')
    download_policy = None
    if data.get('downloads') is not None:
        _keys(data['downloads'], ('budget','topN','protectionHours','longtailFraction'))
        value = data['downloads']
        download_policy = DownloadsBatchPolicy(value['budget'], value['topN'], value['protectionHours'], value['longtailFraction'])
    recommendation_policy = None
    if data.get('recommendations') is not None:
        _keys(data['recommendations'], ('budget','maxSources','intervalHours','retryHours','sources'))
        value = data['recommendations']
        if not isinstance(value['sources'], list):
            raise ValueError('curated source configurations must be an array')
        recommendation_policy = RecommendationsBatchPolicy(value['budget'], value['maxSources'], value['intervalHours'],value['retryHours'],tuple(encode_json(source) for source in value['sources']))
    manifest_policy=release_policy=None
    value=data.get('manifests')
    if value is not None:
        _keys(value,('budget','maxScans','intervalHours','retryHours','profiles'))
        if not isinstance(value['profiles'],list):raise ValueError('manifest profiles must be an array')
        for profile in value['profiles']:_keys(profile,('path','kind','agent'))
        manifest_policy=ManifestBatchPolicy(value['budget'],value['maxScans'],value['intervalHours'],value['retryHours'],tuple(ManifestProfile(**profile) for profile in value['profiles']))
    value=data.get('releases')
    if value is not None:
        _keys(value,('budget','maxScans','intervalHours','retryHours','maxPages'))
        release_policy=ReleaseBatchPolicy(value['budget'],value['maxScans'],value['intervalHours'],value['retryHours'],value['maxPages'])
    refresh, budgets, discovery = data['refresh'],data['budgets'],data['discovery']
    _keys(refresh, ('topN','protectionHours','fastHours','ordinaryHours','longtailFraction'))
    _keys(budgets, ('refresh','bootstrap','discovery','history'))
    _keys(discovery, ('topics','start','overlapHours','historyStart','historyLagHours','historyChunkHours','minShardSeconds','maxShards'))
    if not isinstance(discovery['topics'], list):
        raise ValueError('topics must be an array')
    return BatchPolicy(data['policyId'],data['slotHours'],
        RefreshPolicy(data['policyId'],refresh['topN'],refresh['protectionHours'],refresh['fastHours'],refresh['ordinaryHours'],refresh['longtailFraction']),
        budgets['refresh'],budgets['bootstrap'],budgets['discovery'],budgets['history'],tuple(discovery['topics']),
        _coverage_start(discovery['start']),discovery['overlapHours'],_coverage_start(discovery['historyStart']),
        discovery['historyLagHours'],discovery['historyChunkHours'],DiscoveryPolicy(discovery['minShardSeconds'],discovery['maxShards']),download_policy,recommendation_policy,manifest_policy,release_policy)


def _resources(connection):
    if connection.execute('SELECT 1 FROM canonical_mapping_records WHERE active=1 LIMIT 1').fetchone() and not connection.execute("SELECT 1 FROM metadata WHERE key='canonical_targets_sync'").fetchone():
        raise ServiceError('mapping_targets_missing','run map-canonical to import explicit Agent memberships',409)
    resources = {}
    rows = connection.execute(
        "SELECT l.github_id,min(l.first_seen_at) AS first_seen FROM canonical_repository_links l "
        "WHERE l.active=1 AND l.resolution='resolved' GROUP BY l.github_id")
    for row in rows:
        resources[row['github_id']] = {'first':row['first_seen'],'bootstrap':True,'memberships':set()}
    for row in connection.execute(
        "SELECT DISTINCT l.github_id,t.agent_id,r.package_type FROM canonical_repository_links l "
        "JOIN canonical_mapping_records r ON r.package_id=l.package_id AND r.version=l.version "
        "JOIN canonical_mapping_targets t ON t.package_id=r.package_id AND t.version=r.version "
        "WHERE l.active=1 AND l.resolution='resolved' AND r.active=1"):
        resources[row['github_id']]['memberships'].add((row['agent_id'],row['package_type']))
    for row in connection.execute('SELECT github_id,first_seen_at,bootstrap FROM discovery_candidates'):
        old = resources.setdefault(row['github_id'],{'first':row['first_seen_at'],'bootstrap':bool(row['bootstrap']),'memberships':set()})
        old['first'] = min(old['first'],row['first_seen_at'])
        old['bootstrap'] = old['bootstrap'] or bool(row['bootstrap'])
    successes = {}
    for row in connection.execute('SELECT github_id,first_seen_at,last_success_at FROM repositories'):
        successes[row['github_id']] = row['last_success_at']
        if row['github_id'] in resources:
            resources[row['github_id']]['first'] = min(resources[row['github_id']]['first'],row['first_seen_at'])
    return [RefreshResource('github-repository:'+str(repo_id), tuple(sorted(value['memberships'])) or (('unclassified','repository'),),
        value['first'],value['bootstrap'],successes.get(repo_id)) for repo_id,value in sorted(resources.items())]


def _cohort(connection, policy, now, *, rankings, ranking_revision, input_status="provided_input"):
    day = timestamp(now)[:10]
    identity = _hash([policy.policy_id,day])
    old = connection.execute('SELECT * FROM refresh_cohorts WHERE id=?',(identity,)).fetchone()
    if old: return old
    if rankings is None:
        if ranking_revision is not None: raise ValueError('a ranking revision needs explicit inputs')
        rankings,ranking_revision,status = (), 'unavailable', 'unavailable'
    else:
        if not isinstance(rankings,tuple) or len(rankings)>100000 or not isinstance(ranking_revision,str) or not ranking_revision or len(ranking_revision)>200:
            raise ValueError('cohort inputs require a bounded immutable ranking revision')
        if any(not isinstance(r,RankedResource) or r.revision!=ranking_revision for r in rankings):
            raise ValueError('cohort ranking revisions differ')
        status = input_status
    connection.execute('INSERT INTO refresh_cohorts VALUES (?,?,?,?,?,?,?)',
        (identity,policy.policy_id,day,ranking_revision,status,encode_json([asdict(r) for r in rankings]),timestamp(now)))
    return connection.execute('SELECT * FROM refresh_cohorts WHERE id=?',(identity,)).fetchone()


def _initial_due(resource, policy):
    if not resource.bootstrap:
        return resource.first_seen_at
    value = _time(resource.first_seen_at)
    phase = int(_hash(['github_repository',resource.identity])[:16],16) % (24//policy.slot_hours)
    due = value.replace(hour=phase*policy.slot_hours,minute=0,second=0,microsecond=0)
    if due < value: due += timedelta(days=1)
    return timestamp(due)


def _refresh_due(checked_at, hours, slot_hours):
    # A late GET must not miss the next scheduled boundary by its completion seconds.
    return timestamp(_time(utc_slot(checked_at, slot_hours)) + timedelta(hours=hours))


def _persist_due(connection, resources, rankings, policy, now):
    connection.execute('UPDATE refresh_resources SET active=0')
    result = []
    for resource in resources:
        # Tier is determined even when not yet due, without changing its success clock.
        hot = tuple(sorted({'hot:'+x.metric_identity for x in rankings if x.identity==resource.identity and x.eligible and x.rank<=policy.refresh.top_n}))
        protected = not resource.bootstrap and _time(now)-_time(resource.first_seen_at)<timedelta(hours=policy.refresh.protection_hours)
        reasons = tuple(sorted(set(hot) | ({'new_discovery'} if protected else set()))) or ('daily_rotation',)
        tier = 'fast' if hot or protected else 'ordinary'
        interval = policy.refresh.fast_hours if tier=='fast' else policy.refresh.ordinary_hours
        desired = _refresh_due(resource.last_success_at, interval, policy.slot_hours) if resource.last_success_at else (resource.first_seen_at if tier=='fast' else _initial_due(resource,policy))
        old = connection.execute('SELECT * FROM refresh_resources WHERE id=?',(resource.identity,)).fetchone()
        if old and (resource.last_success_at or '') <= (old['last_success_at'] or ''):
            desired = min(desired,old['next_due_at'])
        connection.execute(
            'INSERT INTO refresh_resources VALUES (?,?,?,?,?,1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
            'memberships_json=excluded.memberships_json,first_seen_at=min(first_seen_at,excluded.first_seen_at),'
            'bootstrap=max(bootstrap,excluded.bootstrap),active=1,last_success_at=excluded.last_success_at,'
            'next_due_at=excluded.next_due_at,tier=excluded.tier,reasons_json=excluded.reasons_json,policy_id=excluded.policy_id',
            (resource.identity,int(resource.identity.split(':')[1]),encode_json(resource.memberships),resource.first_seen_at,int(resource.bootstrap),
             resource.last_success_at,desired,tier,encode_json(reasons),policy.policy_id))
        result.append(RefreshResource(resource.identity,resource.memberships,resource.first_seen_at,resource.bootstrap,resource.last_success_at,desired))
    return result


def _attach_stream(connection, batch_id, run_id, lane):
    if run_id is None: return
    stream_id = connection.execute('SELECT stream_id FROM discovery_runs WHERE id=?',(run_id,)).fetchone()[0]
    connection.execute('INSERT OR IGNORE INTO batch_streams VALUES (?,?,?,?)',(batch_id,stream_id,lane,run_id))


def _discovery(connection, policy, batch_id, slot, now):
    for topic in sorted(policy.topics):
        if policy.discovery_budget and timestamp(policy.discovery_start)<slot:
            for field in ('created','pushed'):
                # Successful coverage beyond an old replay slot is already sufficient.
                prior = connection.execute("SELECT * FROM discovery_streams WHERE mode='incremental' AND topic=? AND time_field=?",(topic,field)).fetchone()
                if prior and prior['watermark'] and prior['watermark']>=slot:
                    continue
                run_id = enqueue_incremental(connection,topic=topic,field=field,bootstrap_start=policy.discovery_start,
                    cutoff=slot,generation='batch-'+batch_id,overlap_seconds=policy.overlap_hours*3600,now=now,policy=policy.discovery)
                _attach_stream(connection,batch_id,run_id,'incremental')
        end = timestamp(_time(slot)-timedelta(hours=policy.history_lag_hours))
        if not policy.history_budget or timestamp(policy.history_start)>=end:
            continue
        old = connection.execute('SELECT * FROM batch_history_cycles WHERE policy_id=? AND topic=?',(policy.policy_id,topic)).fetchone()
        stream = connection.execute('SELECT * FROM discovery_streams WHERE id=?',(old['stream_id'],)).fetchone() if old else None
        cycle = old['cycle'] if old else 1
        if old and stream['watermark'] and stream['watermark']>=old['range_end'] and stream['active_run_id'] is None:
            cycle += 1
        else:
            end = old['range_end'] if old else end
        run_id = enqueue_history(connection,topic=topic,start=policy.history_start,end=end,
            cycle=policy.policy_id+':'+str(cycle),chunk_seconds=policy.history_chunk_hours*3600,now=now,policy=policy.discovery)
        if run_id is not None:
            stream_id = connection.execute('SELECT stream_id FROM discovery_runs WHERE id=?',(run_id,)).fetchone()[0]
            connection.execute('INSERT INTO batch_history_cycles VALUES (?,?,?,?,?) ON CONFLICT(policy_id,topic) DO UPDATE SET cycle=excluded.cycle,range_end=excluded.range_end,stream_id=excluded.stream_id',
                (policy.policy_id,topic,cycle,end,stream_id))
            _attach_stream(connection,batch_id,run_id,'history')


def plan_batch(connection, policy: BatchPolicy, *, now, rankings=None, ranking_revision=None, ranking_store=None) -> dict:
    """Plan the latest slot once; missed slots collapse into overdue work, not a burst."""
    if connection.in_transaction or not isinstance(policy,BatchPolicy):
        raise ValueError('batch planning requires its own transaction and explicit policy')
    if ranking_store is not None and (rankings is not None or ranking_revision is not None):
        raise ValueError("choose verified published rankings or explicit internal inputs, not both")
    slot = utc_slot(now,policy.slot_hours)
    batch_id = _hash([policy.policy_id,slot])
    config = asdict(policy)
    encoded = encode_json(config)
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        old = connection.execute('SELECT config_json FROM batch_policies WHERE id=?',(policy.policy_id,)).fetchone()
        if old and old[0]!=encoded:
            raise ServiceError('batch_policy_conflict','policy ID cannot change its parameters',409)
        connection.execute('INSERT OR IGNORE INTO batch_policies VALUES (?,?)',(policy.policy_id,encoded))
        if connection.execute('SELECT 1 FROM batch_runs WHERE id=?',(batch_id,)).fetchone():
            return batch_status(connection,batch_id)
        resources = _resources(connection)
        input_status = "provided_input"
        if ranking_store is not None and not connection.execute("SELECT 1 FROM refresh_cohorts WHERE policy_id=? AND utc_day=?", (policy.policy_id, timestamp(now)[:10])).fetchone():
            from .observations import published_star_rankings
            published = published_star_rankings(ranking_store, now=now)
            if published is not None:
                rankings, ranking_revision, input_status = published
        if rankings is not None and input_status == "provided_input":
            select_refresh(resources,list(rankings),policy=policy.refresh,ranking_revision=ranking_revision,now=now,max_jobs=0)
        cohort = _cohort(connection,policy,now,rankings=rankings,ranking_revision=ranking_revision,input_status=input_status)
        by_id = {r.identity:r for r in resources}
        values = [RankedResource(**r) for r in json.loads(cohort['rankings_json'])]
        effective = [r for r in values if r.identity in by_id and (r.agent,r.type) in by_id[r.identity].memberships]
        resources = _persist_due(connection,resources,effective,policy,now)
        plan = select_refresh(resources,effective,policy=policy.refresh,ranking_revision=cohort['ranking_revision'],now=now,max_jobs=policy.refresh_budget)
        quotas = {'ordinary':sum(r.tier=='ordinary' for r in plan.selected),'fast':sum(r.tier=='fast' for r in plan.selected),
                  'bootstrap':policy.bootstrap_budget,'incremental':policy.discovery_budget,'history':policy.history_budget}
        summary = {'refreshResources':len(resources),'fastDue':plan.fast_due,'ordinaryDue':plan.ordinary_due,
            'refreshSelected':len(plan.selected),'refreshDeferred':len(plan.deferred),'refreshPartial':plan.partial,
            'rankingStatus':cohort['ranking_status'],'rankingRevision':cohort['ranking_revision'],
            'ignoredCohortEntries':len(values)-len(effective),'effectiveRankingEntries':len(effective),'longtailFraction':policy.refresh.longtail_fraction,
            'ordinaryResourceCount':sum(r['tier']=='ordinary' for r in connection.execute('SELECT tier FROM refresh_resources WHERE active=1')),
            'refreshRequestBudgetPerDay':policy.refresh_budget*(24//policy.slot_hours),
            'oldestOverdueHours':max((r.overdue_hours for r in plan.selected+plan.deferred),default=0),
            'discoveryTopics':list(policy.topics),'selectionSemantics':'runtime_priority_not_public_quality',
            'sourceCoverageSemantics':'search_traversal_not_snapshot_guarantee'}
        summary['pendingCanonicalNamesAtPlanning'] = connection.execute("SELECT count(DISTINCT repository) FROM canonical_repository_links WHERE active=1 AND resolution='pending'").fetchone()[0]
        summary['conflictingCanonicalNamesAtPlanning'] = connection.execute("SELECT count(DISTINCT repository) FROM canonical_repository_links WHERE active=1 AND resolution='conflict'").fetchone()[0]
        capacity = refresh_capacity(policy.refresh, slot_hours=policy.slot_hours, max_jobs=policy.refresh_budget,
            ordinary_resources=summary['ordinaryResourceCount'],
            fast_resources=len(resources)-summary['ordinaryResourceCount'])
        summary['refreshCapacity'] = capacity
        summary['capacityWarnings'] = capacity['warnings']
        download_quotas, download_summary, download_jobs = plan_download_batch(connection, policy, now=now, slot=slot, store=ranking_store, hash_value=_hash)
        quotas.update(download_quotas)
        recommendation_quotas, recommendation_summary = plan_recommendation_batch(connection,policy,now=now,slot=slot)
        quotas.update(recommendation_quotas)
        scan_quotas,scan_summary=plan_scan_batches(connection,policy,now=now,slot=slot)
        quotas.update(scan_quotas);summary.update(scan_summary)
        if policy.recommendations is not None:
            summary['recommendations'] = recommendation_summary
        if policy.downloads is not None:
            summary['downloads'] = download_summary
        connection.execute('INSERT INTO batch_runs(id,policy_id,slot,cohort_id,planned_at,summary_json,quotas_json) VALUES (?,?,?,?,?,?,?)',
            (batch_id,policy.policy_id,slot,cohort['id'],timestamp(now),encode_json(summary),encode_json(quotas)))
        queue = JobQueue(connection)
        for job_id, lane in download_jobs:
            connection.execute('INSERT OR IGNORE INTO batch_jobs VALUES (?,?,?,NULL)', (batch_id, job_id, lane))
        for item in plan.selected:
            resource = by_id[item.identity]
            repo_id = int(resource.identity.split(':')[1])
            endpoint = GitHubRequest.repository(repo_id).endpoint
            prior = connection.execute("SELECT id FROM jobs WHERE kind='github_repository' AND scope=? AND state IN ('pending','retry_wait','leased') ORDER BY created_at,id LIMIT 1",(endpoint,)).fetchone()
            job_id = prior[0] if prior else enqueue_repository(queue,repository=repo_id,slot=slot,bootstrap=resource.bootstrap,now=now)
            connection.execute('INSERT OR IGNORE INTO batch_jobs VALUES (?,?,?,?)',(batch_id,job_id,item.tier,item.identity))
        if policy.bootstrap_budget:
            from .canonical_mapping import enqueue_pending_mapping_jobs
            inflight = connection.execute(
                "SELECT j.id FROM jobs j WHERE j.kind='github_repository' AND j.state IN ('pending','retry_wait','leased') AND "
                "EXISTS(SELECT 1 FROM canonical_repository_links l WHERE l.active=1 AND l.resolution='pending' AND j.scope='repos/'||l.repository) "
                "ORDER BY j.next_run,j.created_at,j.id LIMIT ?",(policy.bootstrap_budget,)).fetchall()
            bootstrap_ids = [r[0] for r in inflight]
            bootstrap_ids += enqueue_pending_mapping_jobs(connection,slot=slot,limit=policy.bootstrap_budget-len(bootstrap_ids),now=now)
            for job_id in bootstrap_ids:
                connection.execute('INSERT OR IGNORE INTO batch_jobs VALUES (?,?,?,NULL)',(batch_id,job_id,'bootstrap'))
        _discovery(connection,policy,batch_id,slot,now)
    return batch_status(connection,batch_id)


def _lane_filter(connection, batch_id, lane):
    if lane in SCAN_LANES:return task_ids(connection,batch_id,lane),None
    if lane == RECOMMENDATION_LANE:
        return None,tuple('recommendation:'+identity for identity in selected_scans(connection,batch_id))
    if lane in ('incremental','history'):
        return None,tuple(r[0] for r in connection.execute(
            'SELECT s.active_run_id FROM batch_streams b JOIN discovery_streams s ON s.id=b.stream_id '
            'WHERE b.batch_id=? AND b.lane=? AND s.active_run_id IS NOT NULL ORDER BY s.id',(batch_id,lane)))
    return tuple(r[0] for r in connection.execute("SELECT b.job_id FROM batch_jobs b JOIN jobs j ON j.id=b.job_id WHERE b.batch_id=? AND b.lane=? AND j.state IN ('pending','retry_wait','leased') ORDER BY CASE WHEN j.state='leased' THEN j.lease_until ELSE j.next_run END,j.created_at,j.id LIMIT 1000",(batch_id,lane))),None


def _due_job(connection, now, *, job_ids, scopes):
    column, values = ('id',job_ids) if job_ids is not None else ('scope',scopes)
    if not values: return False
    from .source_controls import has_controls,enabled_clause
    controls = " AND " + enabled_clause() if has_controls(connection) else ""
    return bool(connection.execute(
        "SELECT 1 FROM jobs WHERE (state IN ('pending','retry_wait') AND next_run<=? OR state='leased' AND lease_until<=?) AND "
        +column+' IN ('+','.join('?' for _ in values)+')'+controls+' LIMIT 1', (timestamp(now),timestamp(now),*values)).fetchone())


def _usage(connection, batch_id):
    return {r[0]:{'charged':r[1],'requests':r[2],'uncertain':r[3]} for r in connection.execute(
        'SELECT lane,sum(coalesce(requests,1)),coalesce(sum(requests),0),sum(requests IS NULL) FROM batch_attempts WHERE batch_id=? GROUP BY lane',(batch_id,))}


def run_batch(connection, client, batch_id: str, *, owner: str, max_requests: int, clock=utc_now, downloads_client=None, recommendation_client=None, anonymous_client=None, should_stop=None) -> dict:
    """Reserve durably before each attempt. Crashes never refund an uncertain GET."""
    if type(max_requests) is not int or not 1<=max_requests<=100 or connection.in_transaction or client is not None and client.connection is not connection:
        raise ValueError('batch run needs one worker state and an explicit request limit in [1,100]')
    if should_stop is not None and not callable(should_stop):
        raise ValueError("batch cancellation must be callable")
    _text(owner,'lease owner')
    run = connection.execute('SELECT * FROM batch_runs WHERE id=?',(batch_id,)).fetchone()
    if run is None: raise ServiceError('batch_not_found','unknown batch',404)
    quotas = json.loads(run['quotas_json'])
    lanes = LANES + tuple(lane for lane in DOWNLOAD_LANES if lane in quotas) + ((RECOMMENDATION_LANE,) if RECOMMENDATION_LANE in quotas else ()) + tuple(lane for lane in SCAN_LANES if lane in quotas)
    if any(quotas[lane] for lane in LANES) and client is None:
        raise ValueError('GitHub lanes require an explicit GitHub identity/client')
    if any(quotas.get(lane, 0) for lane in DOWNLOAD_LANES):
        downloads_client = downloads_client or DownloadsClient(connection, clock=clock)
        if downloads_client.connection is not connection:
            raise ValueError('download client and batch must share the single worker state')
    if quotas.get(RECOMMENDATION_LANE):
        if recommendation_client is None and client is not None:
            recommendation_client = RecommendationClient(connection,identity=client.identity,policy=client.policy,clock=clock,random_value=client.random_value)
        if not isinstance(recommendation_client,RecommendationClient) or recommendation_client.connection is not connection or client is not None and recommendation_client.identity != client.identity:
            raise ValueError('curated lanes require the isolated client and the same GitHub quota identity')
    if any(quotas.get(lane,0) for lane in SCAN_LANES):
        anonymous_client=anonymous_client or AnonymousGitClient(connection,clock=clock)
        if not isinstance(anonymous_client,AnonymousGitClient) or anonymous_client.connection is not connection:
            raise ValueError('declarations/Releases need the shared credential-free client and Worker state')
    requests,actions = 0,0
    filters = {lane:_lane_filter(connection,batch_id,lane) for lane in ('ordinary','fast','bootstrap') + tuple(lane for lane in DOWNLOAD_LANES if lane in quotas)}
    # No busy waiting: once currently-due work is exhausted, return its next_run.
    while requests<max_requests and actions<max_requests*len(lanes) and not (should_stop and should_stop()):
        satisfied = None
        for lane in ('ordinary','fast'):
            satisfied = _satisfy_fresh_check(connection,batch_id,lane,run['slot'],owner,clock())
            if satisfied is not None: break
        if satisfied is None:
            for lane in DOWNLOAD_LANES:
                if lane not in quotas:
                    continue
                satisfied = satisfy_download_check(connection,batch_id,lane,owner,clock())
                if satisfied is not None:
                    break
        if satisfied is not None:
            actions += 1
            continue
        usage = _usage(connection,batch_id)
        next_lane = connection.execute('SELECT next_lane FROM batch_runs WHERE id=?',(batch_id,)).fetchone()[0]
        chosen = None
        for offset in range(len(lanes)):
            index = (next_lane+offset)%len(lanes); lane = lanes[index]
            if usage.get(lane,{}).get('charged',0)>=quotas[lane]: continue
            active_client = anonymous_client if lane in SCAN_LANES else recommendation_client if lane == RECOMMENDATION_LANE else downloads_client if lane in DOWNLOAD_LANES else client
            resource = 'downloads' if lane in DOWNLOAD_LANES else 'search' if lane in ('incremental','history') else 'core'
            if active_client is None or active_client.next_allowed_at(resource,now=clock()): continue
            job_ids,scopes = filters[lane] if lane in filters else _lane_filter(connection,batch_id,lane)
            if _due_job(connection,clock(),job_ids=job_ids,scopes=scopes):
                chosen = index,lane,job_ids,scopes; break
        if chosen is None or should_stop and should_stop(): break
        index,lane,job_ids,scopes = chosen
        reservation = uuid.uuid4().hex
        with connection:
            connection.execute('INSERT INTO batch_attempts VALUES (?,?,?,?,NULL,NULL)',(reservation,batch_id,lane,timestamp(clock())))
            connection.execute('UPDATE batch_runs SET next_lane=? WHERE id=?',((index+1)%len(lanes),batch_id))
        if lane == 'manifests':
            result=run_manifest_jobs(connection,anonymous_client,owner=owner,max_requests=1,clock=clock,job_ids=job_ids,should_stop=should_stop)
        elif lane == 'releases':
            result=run_release_jobs(connection,anonymous_client,owner=owner,max_requests=1,clock=clock,job_ids=job_ids,should_stop=should_stop)
        elif lane == RECOMMENDATION_LANE:
            result = run_recommendation_jobs(connection,recommendation_client,owner=owner,max_requests=1,clock=clock,scan_ids=selected_scans(connection,batch_id),should_stop=should_stop)
        elif lane in DOWNLOAD_LANES:
            result = run_download_jobs(connection,downloads_client,owner=owner,max_requests=1,clock=clock,job_ids=job_ids,should_stop=should_stop)
        elif lane in ('incremental','history'):
            result = run_discovery_jobs(connection,client,owner=owner,max_requests=1,clock=clock,scopes=scopes,should_stop=should_stop)
        else:
            result = run_repository_jobs(connection,client,owner=owner,max_requests=1,clock=clock,job_ids=job_ids,should_stop=should_stop)
        with connection:
            connection.execute('UPDATE batch_attempts SET requests=?,result_json=? WHERE id=?',(result['requests'],encode_json(result),reservation))
            if lane in SCAN_LANES:reconcile_scans(connection,clock())
            if lane == RECOMMENDATION_LANE:
                finish_recommendation_checks(connection,run['policy_id'],clock())
        requests += result['requests']; actions += 1
    network = {resource:client.next_allowed_at(resource,now=clock()) if client else None for resource in ('core','search')}
    if any(lane in quotas for lane in DOWNLOAD_LANES):
        network['downloads'] = downloads_client.next_allowed_at('downloads',now=clock()) if downloads_client else None
    if recommendation_client is not None:
        network['core'] = recommendation_client.next_allowed_at('core',now=clock())
    if anonymous_client is not None:
        network['anonymousCore']=anonymous_client.next_allowed_at('core',now=clock())
    return {'requestsThisInvocation':requests,'actionsThisInvocation':actions,
        'nextNetworkAt':network,
        **batch_status(connection,batch_id)}


def batch_status(connection, batch_id: str) -> dict:
    run = connection.execute('SELECT * FROM batch_runs WHERE id=?',(batch_id,)).fetchone()
    if run is None: raise ServiceError('batch_not_found','unknown batch',404)
    quotas = json.loads(run['quotas_json']); usage = _usage(connection,batch_id)
    lanes = {}
    from .source_controls import has_controls,enabled_clause
    controlled = has_controls(connection)
    for lane in LANES + tuple(lane for lane in DOWNLOAD_LANES if lane in quotas) + ((RECOMMENDATION_LANE,) if RECOMMENDATION_LANE in quotas else ()) + tuple(lane for lane in SCAN_LANES if lane in quotas):
        parameters = (batch_id,lane)
        if lane in SCAN_LANES:
            ids=task_ids(connection,batch_id,lane,pending=False)
            if not ids:
                lanes[lane]={'quota':quotas[lane],**usage.get(lane,{'charged':0,'requests':0,'uncertain':0}),'jobs':{},'nextRunAt':None,'scanStates':scan_states(connection,batch_id,lane)}
                continue
            from_sql='FROM jobs j WHERE j.id IN ('+','.join('?' for _ in ids)+')'
            parameters=ids
        elif lane == RECOMMENDATION_LANE:
            scopes = tuple('recommendation:'+identity for identity in selected_scans(connection,batch_id))
            if not scopes:
                lanes[lane] = {'quota':quotas[lane],**usage.get(lane,{'charged':0,'requests':0,'uncertain':0}),'jobs':{},'nextRunAt':None}
                continue
            from_sql = 'FROM jobs j WHERE j.scope IN ('+','.join('?' for _ in scopes)+')'
            parameters = scopes
        elif lane in ('ordinary','fast','bootstrap') + DOWNLOAD_LANES:
            from_sql = 'FROM batch_jobs b JOIN jobs j ON j.id=b.job_id WHERE b.batch_id=? AND b.lane=?'
        else:
            from_sql = 'FROM batch_streams b JOIN discovery_streams s ON s.id=b.stream_id JOIN jobs j ON j.scope=s.active_run_id WHERE b.batch_id=? AND b.lane=?'
        states = dict(connection.execute('SELECT j.state,count(*) '+from_sql+' GROUP BY j.state',parameters))
        eligibility = " AND " + enabled_clause('j') if controlled else ""
        next_run = connection.execute("SELECT min(CASE WHEN j.state='leased' THEN j.lease_until ELSE j.next_run END) "+from_sql+" AND j.state IN ('pending','retry_wait','leased')"+eligibility,parameters).fetchone()[0]
        lanes[lane] = {'quota':quotas[lane],**usage.get(lane,{'charged':0,'requests':0,'uncertain':0}),'jobs':states,'nextRunAt':next_run}
        if lane in SCAN_LANES:lanes[lane]['scanStates']=scan_states(connection,batch_id,lane)
        if controlled:
            paused=connection.execute("SELECT count(*) "+from_sql+" AND j.state IN ('pending','retry_wait') AND NOT ("+enabled_clause('j')+")",parameters).fetchone()[0]
            lanes[lane]['collectionPausedJobs']=paused
    streams = [dict(r) for r in connection.execute(
        'SELECT b.lane,s.id,s.topic,s.time_field,s.watermark,s.active_run_id,r.state AS run_state '
        'FROM batch_streams b JOIN discovery_streams s ON s.id=b.stream_id LEFT JOIN discovery_runs r ON r.id=s.active_run_id WHERE b.batch_id=? ORDER BY s.id',(batch_id,))]
    pending = any(any(row['jobs'].get(s,0) for s in ('pending','leased','retry_wait')) for row in lanes.values())
    uncertain = any(row['uncertain'] for row in lanes.values())
    failed = any(row['jobs'].get('dead',0) for row in lanes.values()) or any(r['run_state']=='partial' for r in streams)
    failed = failed or any(any(value for key,value in row.get('scanStates',{}).items() if key in ('partial','failed')) for row in lanes.values())
    exhausted = any(row['charged']>=row['quota'] and any(row['jobs'].get(s,0) for s in ('pending','leased','retry_wait')) for row in lanes.values())
    state = 'uncertain' if uncertain else 'budget_exhausted' if exhausted else 'active' if pending else 'partial' if failed else 'settled'
    return {'batchId':batch_id,'policyId':run['policy_id'],'slot':run['slot'],'plannedAt':run['planned_at'],
        'cohortId':run['cohort_id'],'state':state,'summary':json.loads(run['summary_json']),'lanes':lanes,'streams':streams,
        'catalogCoverage':'not_guaranteed','selectedWorkSettled':not pending and not uncertain and not failed}


def recent_batches(connection, limit=10):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='batch_runs'").fetchone():
        return {'batchSchema':'upgrade_required','recent':[]}
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError('batch status limit must be in [1,100]')
    return {'batchSchema':'ready','recent':[batch_status(connection,r[0]) for r in connection.execute('SELECT id FROM batch_runs ORDER BY planned_at DESC,id LIMIT ?',(limit,))]}


def read_batch_status(path: Path, batch_id=None, limit=20):
    import sqlite3
    if not path.is_file() or type(limit) is not int or not 1<=limit<=100:
        raise ValueError('batch state must exist and status limit must be in [1,100]')
    connection = sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if batch_id is None: return recent_batches(connection,limit)
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='batch_runs'").fetchone():
            raise ServiceError('batch_upgrade_required','worker state needs upgrade',409)
        return batch_status(connection,batch_id)
    finally: connection.close()


def remember_refresh_success(connection, *, github_id: int, checked_at: str):
    row = connection.execute('SELECT * FROM refresh_resources WHERE github_id=?',(github_id,)).fetchone()
    if row is None or row['last_success_at'] and row['last_success_at']>=checked_at:
        return
    config = json.loads(connection.execute('SELECT config_json FROM batch_policies WHERE id=?',(row['policy_id'],)).fetchone()[0])
    hours = config['refresh']['fast_hours' if row['tier']=='fast' else 'ordinary_hours']
    repository = connection.execute('SELECT first_seen_at,bootstrap FROM repositories WHERE github_id=?',(github_id,)).fetchone()
    connection.execute('UPDATE refresh_resources SET last_success_at=?,next_due_at=?,first_seen_at=min(first_seen_at,?),bootstrap=max(bootstrap,?) WHERE id=?',
        (checked_at,_refresh_due(checked_at,hours,config['slot_hours']),repository['first_seen_at'],repository['bootstrap'],row['id']))


def _satisfy_fresh_check(connection, batch_id, lane, slot, owner, now):
    row = connection.execute(
        "SELECT j.id,(SELECT o.id FROM repository_observations o WHERE o.github_id=r.github_id AND o.checked_at>=? ORDER BY o.checked_at DESC,o.id DESC LIMIT 1) AS evidence "
        "FROM batch_jobs b JOIN jobs j ON j.id=b.job_id JOIN refresh_resources f ON f.id=b.resource_id JOIN repositories r ON r.github_id=f.github_id "
        "WHERE b.batch_id=? AND b.lane=? AND j.scope='repositories/'||r.github_id AND r.last_success_at>=? AND "
        "(j.state IN ('pending','retry_wait') AND j.next_run<=? OR j.state='leased' AND j.lease_until<=?) "
        "ORDER BY j.next_run,j.created_at,j.id LIMIT 1", (slot,batch_id,lane,slot,timestamp(now),timestamp(now))).fetchone()
    if row is None or row['evidence'] is None: return None
    queue = JobQueue(connection)
    lease = queue.claim(owner=owner,now=now,kind='github_repository',job_ids=(row['id'],))
    if lease is None: return None
    result = {'claimed':1,'succeeded':1,'requests':0,'satisfiedByObservation':row['evidence']}
    queue.finish(lease,now=now,apply=lambda c:c.execute('INSERT INTO batch_attempts VALUES (?,?,?,?,0,?)',
        (uuid.uuid4().hex,batch_id,lane,timestamp(now),encode_json(result))))
    return result
