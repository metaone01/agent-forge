"""Daily npm windows within the single worker's durable, source-scoped batches."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import timedelta

from .downloads import SOURCE, enqueue_downloads, request_for_day
from .jobs import JobQueue
from .metrics import Growth, WindowPolicy
from .scheduling import RankedResource, RefreshPolicy, RefreshResource, _time, select_refresh
from .storage import encode_json, read_snapshot, timestamp

DOWNLOAD_LANES = ("downloads_ordinary", "downloads_fast")


@dataclass(frozen=True)
class DownloadsBatchPolicy:
    budget: int
    top_n: int
    protection_hours: float
    longtail_fraction: float

    def __post_init__(self):
        if type(self.budget) is not int or not 0 <= self.budget <= 1000:
            raise ValueError("npm batch budget must be explicit and between 0 and 1000")
        RefreshPolicy("npm-policy-validation", self.top_n, self.protection_hours, 8, 24, self.longtail_fraction)


def published_download_inputs(store, *, now):
    if store is None or not (store.root / "current.json").is_file():
        return [], (), "unavailable", "unavailable"
    manifest = store.current()
    if _time(manifest["generatedAt"]) > _time(now):
        raise ValueError("download input snapshot is in the future")
    policy = WindowPolicy(**manifest["downloadCoverage"]["policy"])
    db = read_snapshot(store.database(manifest["revision"]))
    try:
        resources = {}
        for row in db.execute("SELECT l.npm_package,p.first_seen_at,p.bootstrap,a.agent,p.type FROM package_npm_links l "
            "JOIN packages p ON p.id=l.package_id JOIN package_agents a ON a.package_id=p.id ORDER BY l.npm_package"):
            old = resources.setdefault(row["npm_package"], {"first": row["first_seen_at"], "bootstrap": bool(row["bootstrap"]), "memberships": set()})
            old["first"] = min(old["first"], row["first_seen_at"])
            old["bootstrap"] = old["bootstrap"] or bool(row["bootstrap"])
            old["memberships"].add((row["agent"], row["type"]))
        if len(resources) > 100000:
            raise ValueError("published npm resources exceed the planning bound")
        rows = db.execute("SELECT p.id,s.package,s.signal_json,a.agent,p.type FROM packages p "
            "JOIN package_agents a ON a.package_id=p.id JOIN package_download_sort_keys k ON k.package_id=p.id "
            "JOIN npm_download_signals s ON s.package=k.npm_package WHERE k.growth_value IS NOT NULL "
            "ORDER BY a.agent,p.type,k.growth_value DESC,p.id LIMIT 100001").fetchall()
    finally:
        db.close()
    if len(rows) > 100000:
        raise ValueError("published download ranking memberships exceed the planning bound")
    groups = {}
    for row in rows:
        signal = json.loads(row["signal_json"])
        data = signal["downloadGrowth"]
        growth = Growth(**{field.name: data[field.name] for field in fields(Growth)})
        check_age = (_time(now) - _time(signal["checkedAt"])).total_seconds() / 3600
        if policy.qualification(growth, as_of=timestamp(now))["eligible"] and 0 <= check_age <= manifest["downloadCoverage"]["maximumCheckAgeHours"]:
            groups.setdefault((row["agent"], row["type"]), []).append((row["id"], row["package"], growth.value_per_hour))
    metric = encode_json([SOURCE, "registry_package", "downloads", "downloads/hour", "npm_public_registry_all_versions", policy.policy_id, policy.version])
    rankings = []
    for (agent, category), items in sorted(groups.items()):
        package_ranks = {}
        # First rank the full directory package board; request deduplication must not fill rank gaps.
        for rank, (_, name, _) in enumerate(sorted(items, key=lambda item: (-item[2], item[0])), 1):
            package_ranks.setdefault(name, rank)
        rankings.extend(RankedResource("npm-package:" + name, agent, category, metric, rank, manifest["revision"]) for name, rank in package_ranks.items())
    values = [RefreshResource("npm-package:" + name, tuple(sorted(value["memberships"])), value["first"], value["bootstrap"], None)
        for name, value in sorted(resources.items())]
    status = "published_snapshot" if rankings else "published_snapshot_no_eligible_samples" if manifest["downloadCoverage"]["sharedNpmPackages"] else "published_snapshot_no_observations"
    return values, tuple(rankings), manifest["revision"], status


def _phase(resource, slot_hours, hash_value):
    return int(hash_value(["npm-package-phase", resource.identity])[:16], 16) % (24 // slot_hours) * slot_hours


def plan_download_batch(connection, policy, *, now, slot, store, hash_value):
    """Called inside the encompassing batch transaction; no network or independent commit."""
    config = policy.downloads
    if config is None or config.budget == 0:
        return {}, {"status": "disabled"}, []
    resources, published, revision, status = published_download_inputs(store, now=now)
    cohort_id = hash_value([policy.policy_id, timestamp(now)[:10], "npm-downloads"])
    cohort = connection.execute("SELECT * FROM download_refresh_cohorts WHERE id=?", (cohort_id,)).fetchone()
    if cohort is None:
        connection.execute("INSERT INTO download_refresh_cohorts VALUES (?,?,?,?,?,?,?)", (cohort_id, policy.policy_id, timestamp(now)[:10], revision,
            status, encode_json([asdict(item) for item in published]), timestamp(now)))
        cohort = connection.execute("SELECT * FROM download_refresh_cohorts WHERE id=?", (cohort_id,)).fetchone()
    frozen = [RankedResource(**value) for value in json.loads(cohort["rankings_json"])]
    by_id = {resource.identity: resource for resource in resources}
    effective = [entry for entry in frozen if entry.identity in by_id and (entry.agent, entry.type) in by_id[entry.identity].memberships]
    refresh = RefreshPolicy(policy.policy_id, config.top_n, config.protection_hours, policy.slot_hours, 24, config.longtail_fraction)
    hot = {entry.identity for entry in effective if entry.eligible and entry.rank <= config.top_n}
    target = request_for_day("placeholder", as_of=slot)
    collected = {row[0] for row in connection.execute("SELECT DISTINCT package FROM npm_download_observations WHERE start_day=? AND end_day=?", (target.start, target.end))}
    # A dead task for this exact measurement window is not given a fresh retry budget every eight hours.
    jobs = list(connection.execute("SELECT id,scope,state,payload_json FROM jobs WHERE kind='npm_downloads' "
        "AND json_extract(payload_json,'$.start')=? AND json_extract(payload_json,'$.end')=? ORDER BY created_at,id LIMIT 100001", (target.start, target.end)))
    if len(jobs) > 100000:
        raise ValueError("npm jobs for one measurement window exceed the planning bound")
    prior, dead = {}, set()
    for row in jobs:
        payload = json.loads(row["payload_json"])
        if payload.get("start") != target.start or payload.get("end") != target.end:
            continue
        if row["state"] in ("pending", "retry_wait", "leased"):
            prior.setdefault(row["scope"], row["id"])
        elif row["state"] == "dead":
            dead.add(row["scope"])
    windows = {}
    for resource in resources:
        name = resource.identity[len("npm-package:"):]
        latest = connection.execute("SELECT max(end_day) FROM npm_download_observations WHERE package=?", (name,)).fetchone()[0]
        if latest:
            windows[name] = latest
    eligible, tier_counts = [], {"fast": 0, "ordinary": 0}
    for resource in resources:
        name = resource.identity[len("npm-package:"):]
        protected = not resource.bootstrap and _time(now) - _time(resource.first_seen_at) < timedelta(hours=config.protection_hours)
        fast = resource.identity in hot or protected
        tier_counts["fast" if fast else "ordinary"] += 1
        if name in collected or name in dead and name not in prior:
            continue
        if name in windows:
            # Daily counter windows, not the latest polling timestamp, drive the next daily target.
            last = _time(windows[name] + "T00:00:00Z") + timedelta(days=1)
            due = last + timedelta(days=1)
            if not fast:
                due += timedelta(hours=_phase(resource, policy.slot_hours, hash_value))
        else:
            last = None
            due = _time(resource.first_seen_at)
            if not fast:
                due = due.replace(hour=_phase(resource, policy.slot_hours, hash_value), minute=0, second=0, microsecond=0)
                # Startup may follow today's hash phase: seed it now under the same budget,
                # without pretending an imported resource is a newly discovered package.
                due = max(due, _time(resource.first_seen_at))
        if name in prior:
            due = min(due, _time(slot))
        eligible.append(RefreshResource(resource.identity, resource.memberships, resource.first_seen_at, resource.bootstrap,
            timestamp(last) if last else None, timestamp(due)))
    eligible_ids = {resource.identity for resource in eligible}
    plan = select_refresh(eligible, [entry for entry in effective if entry.identity in eligible_ids], policy=refresh, ranking_revision=cohort["ranking_revision"], now=now, max_jobs=config.budget)
    attached = []
    midnight = timestamp(_time(slot).replace(hour=0, minute=0, second=0, microsecond=0))
    for item in plan.selected:
        name = item.identity[len("npm-package:"):]
        job_id = prior.get(name) or enqueue_downloads(connection, package=name, slot=midnight, now=now)
        attached.append((job_id, "downloads_" + item.tier))
    summary = {"status": status, "revision": revision, "rankingStatus": cohort["ranking_status"], "rankingRevision": cohort["ranking_revision"],
        "cohortId": cohort_id, "resources": len(resources), "selected": len(attached), "deferred": len(plan.deferred),
        "fastDue": plan.fast_due, "ordinaryDue": plan.ordinary_due, "alreadyCollected": sum("npm-package:" + name in by_id for name in collected),
        "deadWindowResources": sum("npm-package:" + name in by_id for name in dead), "ignoredCohortEntries": len(frozen) - len(effective),
        "effectiveRankingEntries": len(effective), "longtailFraction": config.longtail_fraction, "ordinaryResources": tier_counts["ordinary"],
        "requestBudgetPerDay": config.budget * (24 // policy.slot_hours), "sourceInterval": "complete_utc_days",
        "targetStartDay": target.start, "targetEndDay": target.end, "selectionSemantics": "runtime_priority_not_public_quality"}
    summary["capacityWarnings"] = ["npm_daily_target_exceeds_download_budget"] if summary["requestBudgetPerDay"] < len(resources) else []
    quotas = {"downloads_" + tier: sum(lane == "downloads_" + tier for _, lane in attached) for tier in ("ordinary", "fast")}
    return quotas, summary, attached


def satisfy_download_check(connection, batch_id, lane, owner, now):
    rows = connection.execute("SELECT j.id,j.payload_json,j.scope FROM batch_jobs b JOIN jobs j ON j.id=b.job_id "
        "WHERE b.batch_id=? AND b.lane=? AND j.kind='npm_downloads' AND "
        "(j.state IN ('pending','retry_wait') AND j.next_run<=? OR j.state='leased' AND j.lease_until<=?) "
        "ORDER BY j.next_run,j.created_at,j.id LIMIT 1000", (batch_id, lane, timestamp(now), timestamp(now)))
    for row in rows:
        payload = json.loads(row["payload_json"])
        observed = connection.execute("SELECT id FROM npm_download_observations WHERE package=? AND start_day=? AND end_day=? "
            "AND checked_at<=? ORDER BY checked_at DESC,id LIMIT 1", (row["scope"], payload["start"], payload["end"], timestamp(now))).fetchone()
        if observed is None:
            continue
        queue = JobQueue(connection)
        lease = queue.claim(owner=owner, now=now, kind="npm_downloads", job_ids=(row["id"],))
        if lease is None:
            continue
        result = {"claimed": 1, "succeeded": 1, "requests": 0, "satisfiedByObservation": observed["id"]}
        queue.finish(lease, now=now, apply=lambda db: db.execute("INSERT INTO batch_attempts VALUES (?,?,?,?,0,?)",
            (uuid.uuid4().hex, batch_id, lane, timestamp(now), encode_json(result))))
        return result
    return None
