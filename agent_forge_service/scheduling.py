"""Explicit refresh policy; scheduling priority is not a popularity fact."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .storage import timestamp


def _time(value):
    return datetime.fromisoformat(timestamp(value).replace("Z", "+00:00"))


def utc_slot(now: str | datetime, hours: int = 8) -> str:
    if type(hours) is not int or hours <= 0 or 24 % hours:
        raise ValueError("slot hours must divide a UTC day")
    value = _time(now)
    return timestamp(value.replace(hour=(value.hour // hours) * hours, minute=0, second=0, microsecond=0))


@dataclass(frozen=True)
class RefreshPolicy:
    policy_id: str
    top_n: int
    protection_hours: float
    fast_hours: float
    ordinary_hours: float
    longtail_fraction: float

    def __post_init__(self):
        if not isinstance(self.policy_id, str) or not self.policy_id or type(self.top_n) is not int or not 0 <= self.top_n <= 10000:
            raise ValueError("refresh policy needs an explicit identity and Top N")
        values = (self.protection_hours, self.fast_hours, self.ordinary_hours, self.longtail_fraction)
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
            raise ValueError("refresh intervals must be finite")
        if not 0 <= self.protection_hours <= 24 * 365 or not 0 < self.fast_hours <= self.ordinary_hours <= 24 * 365 or not 0 < self.longtail_fraction <= 1:
            raise ValueError("invalid refresh intervals or long-tail reserve")


@dataclass(frozen=True)
class RefreshResource:
    identity: str
    memberships: tuple[tuple[str, str], ...]
    first_seen_at: str
    bootstrap: bool
    last_success_at: str | None
    next_due_at: str | None = None


@dataclass(frozen=True)
class RankedResource:
    identity: str
    agent: str
    type: str
    metric_identity: str
    rank: int
    revision: str
    eligible: bool = True


@dataclass(frozen=True)
class DueResource:
    identity: str
    tier: str
    reasons: tuple[str, ...]
    due_at: str
    overdue_hours: float


@dataclass(frozen=True)
class RefreshPlan:
    slot: str
    policy_id: str
    ranking_revision: str
    selected: tuple[DueResource, ...]
    deferred: tuple[DueResource, ...]
    fast_due: int
    ordinary_due: int
    partial: bool


def select_refresh(resources: list[RefreshResource], rankings: list[RankedResource], *,
                   policy: RefreshPolicy, ranking_revision: str,
                   now: str | datetime, max_jobs: int) -> RefreshPlan:
    if not isinstance(ranking_revision, str) or not ranking_revision or type(max_jobs) is not int or not 0 <= max_jobs <= 100000:
        raise ValueError("refresh selection requires a ranking revision and bounded budget")
    now = _time(now)
    by_id = {}
    for resource in resources:
        if not isinstance(resource.identity, str) or not resource.identity or resource.identity in by_id:
            raise ValueError("resources must already be deduplicated by source/entity/request-kind")
        if type(resource.bootstrap) is not bool or _time(resource.first_seen_at) > now or resource.last_success_at is not None and _time(resource.last_success_at) > now:
            raise ValueError("invalid discovery or success time")
        if not resource.memberships or any(not isinstance(pair, tuple) or len(pair) != 2 or any(not isinstance(x, str) or not x for x in pair) for pair in resource.memberships):
            raise ValueError("resource memberships must be explicit Agent/type pairs")
        by_id[resource.identity] = resource
    hot = {}
    for entry in rankings:
        if entry.revision != ranking_revision or not isinstance(entry.metric_identity, str) or not entry.metric_identity or type(entry.rank) is not int or entry.rank < 1 or type(entry.eligible) is not bool:
            raise ValueError("rankings must have explicit comparable groups and one revision")
        if entry.identity not in by_id or (entry.agent, entry.type) not in by_id[entry.identity].memberships:
            raise ValueError("ranking identity does not match resource membership")
        if entry.eligible and entry.rank <= policy.top_n:
            hot.setdefault(entry.identity, set()).add("hot:" + entry.metric_identity)
    fast, ordinary = [], []
    for resource in resources:
        reasons = set(hot.get(resource.identity, ()))
        if not resource.bootstrap and now - _time(resource.first_seen_at) < timedelta(hours=policy.protection_hours):
            reasons.add("new_discovery")
        interval = policy.fast_hours if reasons else policy.ordinary_hours
        due = (_time(resource.last_success_at) + timedelta(hours=interval)
               if resource.last_success_at else _time(resource.first_seen_at))
        if resource.next_due_at is not None:
            due = _time(resource.next_due_at)
        if due > now:
            continue
        entry = DueResource(resource.identity, "fast" if reasons else "ordinary",
                            tuple(sorted(reasons)) if reasons else ("daily_rotation",),
                            timestamp(due), (now - due).total_seconds() / 3600)
        (fast if reasons else ordinary).append(entry)
    for group in (fast, ordinary):
        group.sort(key=lambda x: (x.due_at, x.identity))
    reserved = min(len(ordinary), math.ceil(max_jobs * policy.longtail_fraction))
    selected = ordinary[:reserved]
    selected += fast[:max_jobs - len(selected)]
    selected += ordinary[reserved:reserved + max_jobs - len(selected)]
    chosen = {entry.identity for entry in selected}
    deferred = sorted((entry for entry in fast + ordinary if entry.identity not in chosen), key=lambda x: (x.due_at, x.identity))
    return RefreshPlan(utc_slot(now), policy.policy_id, ranking_revision, tuple(selected), tuple(deferred),
                       len(fast), len(ordinary), bool(deferred))


def refresh_capacity(policy: RefreshPolicy, *, slot_hours: int, max_jobs: int,
                     ordinary_resources: int, fast_resources: int) -> dict:
    """Necessary one-GET-per-check budget; this cannot prove successful coverage."""
    if (not isinstance(policy, RefreshPolicy) or type(slot_hours) is not int
            or slot_hours not in (4, 8) or policy.fast_hours != slot_hours or policy.ordinary_hours != 24):
        raise ValueError("refresh capacity needs daily ordinary and slot-aligned fast checks")
    if type(max_jobs) is not int or not 0 <= max_jobs <= 100000:
        raise ValueError("refresh capacity budget must be in [0,100000]")
    for value in (ordinary_resources, fast_resources):
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise ValueError("refresh resource counts must be nonnegative bounded integers")
    slots = 24 // slot_hours
    ordinary = ordinary_resources
    fast = fast_resources * slots
    budget = max_jobs * slots
    warnings = []
    if ordinary > budget:
        warnings.append("ordinary_daily_target_exceeds_refresh_budget")
    if fast_resources and ordinary + fast > budget:
        warnings.append("combined_refresh_target_exceeds_budget")
    return {"slotsPerDay": slots, "ordinaryResources": ordinary_resources, "fastResources": fast_resources,
            "ordinaryChecksPerDay": ordinary, "fastChecksPerDay": fast,
            "requiredChecksPerDayLowerBound": ordinary + fast, "refreshRequestBudgetPerDay": budget,
            "minimumAverageBudgetPerSlot": fast_resources + (ordinary + slots - 1) // slots,
            "minimumDailyShortfall": max(0, ordinary + fast - budget),
            "budgetCoversLowerBound": budget >= ordinary + fast, "warnings": warnings,
            "semantics": "one_get_per_check_necessary_not_sufficient",
            "excludedCosts": ["bootstrap", "discovery", "downloads", "manifests", "releases",
                              "recommendations", "retries", "rate_limit_wait", "publication"]}
