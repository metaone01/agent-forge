"""Private, read-only coverage diagnostics for the currently planned repository cohort."""
from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import time

from .batches import BatchPolicy
from .errors import ServiceError
from .schema_contracts import STATE_SCHEMA_VERSION, STATE_CONTRACT
from .scheduling import _time, refresh_capacity
from .storage import encode_json, timestamp, utc_now


def _counts():
    return {"resources": 0, "checkedWithinTarget": 0, "notCheckedWithinTarget": 0,
            "neverChecked": 0, "observationWithinTarget": 0, "dueNow": 0, "overdue": 0,
            "oldestCheckAgeHours": None, "oldestOverdueHours": 0.0}


def _add(counts, *, checked, observed, due, now, hours):
    counts["resources"] += 1
    fresh = checked is not None and checked >= now - timedelta(hours=hours)
    counts["checkedWithinTarget"] += int(fresh)
    counts["notCheckedWithinTarget"] += int(not fresh)
    counts["neverChecked"] += int(checked is None)
    counts["observationWithinTarget"] += int(observed is not None and observed >= now - timedelta(hours=hours))
    counts["dueNow"] += int(due <= now)
    counts["overdue"] += int(due < now)
    if checked is not None:
        age = (now - checked).total_seconds() / 3600
        counts["oldestCheckAgeHours"] = max(counts["oldestCheckAgeHours"] or 0, age)
    counts["oldestOverdueHours"] = max(counts["oldestOverdueHours"], (now - due).total_seconds() / 3600)


def _finish(counts):
    counts = dict(counts)
    counts["checkedCoverage"] = (counts["checkedWithinTarget"] / counts["resources"] if counts["resources"] else None)
    for key in ("oldestCheckAgeHours", "oldestOverdueHours"):
        if counts[key] is not None:
            counts[key] = round(counts[key], 6)
    return counts


def _report(connection, policy, now, deadline):
    encoded = encode_json(asdict(policy))
    saved = connection.execute("SELECT config_json FROM batch_policies WHERE id=?", (policy.policy_id,)).fetchone()
    if saved is not None and saved[0] != encoded:
        raise ServiceError("batch_policy_conflict", "policy ID cannot change its parameters", 409)
    cutoff = timestamp(now)
    rows = connection.execute(
        "SELECT f.id,f.tier,f.memberships_json,f.next_due_at,"
        "max(o.checked_at) AS checked,max(CASE WHEN o.observed_at<=? THEN o.observed_at END) AS observed "
        "FROM refresh_resources f LEFT JOIN repository_observations o "
        "ON o.github_id=f.github_id AND o.checked_at<=? AND o.response_status IN (200,304) "
        "WHERE f.active=1 AND f.policy_id=? GROUP BY f.id ORDER BY f.id",
        (cutoff, cutoff, policy.policy_id))
    totals = _counts()
    tiers = {tier: _counts() for tier in ("ordinary", "fast")}
    groups = {}
    for row in rows:
        if time.monotonic() > deadline:
            raise ServiceError("refresh_status_timeout", "refresh report exceeded its read deadline", 503)
        tier = row["tier"]
        if tier not in tiers:
            raise ValueError("unknown planned refresh tier")
        checked = _time(row["checked"]) if row["checked"] else None
        observed = _time(row["observed"]) if row["observed"] else None
        due = _time(row["next_due_at"])
        hours = policy.refresh.fast_hours if tier == "fast" else policy.refresh.ordinary_hours
        facts = dict(checked=checked, observed=observed, due=due, now=now, hours=hours)
        _add(totals, **facts)
        _add(tiers[tier], **facts)
        raw = row["memberships_json"]
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 65536:
            raise ValueError("refresh membership exceeds report bound")
        members = json.loads(raw)
        if not isinstance(members, list) or not 1 <= len(members) <= 1000:
            raise ValueError("refresh membership needs bounded explicit pairs")
        unique = set()
        for pair in members:
            if (not isinstance(pair, list) or len(pair) != 2
                    or any(not isinstance(value, str) or not 1 <= len(value) <= 200 for value in pair)):
                raise ValueError("refresh membership needs explicit Agent/type pairs")
            unique.add(tuple(pair))
        for agent, category in unique:
            key = (agent, category, tier)
            if key not in groups:
                if len(groups) >= 1000:
                    raise ValueError("refresh groups exceed report bound")
                groups[key] = _counts()
            _add(groups[key], **facts)
    capacity = refresh_capacity(policy.refresh, slot_hours=policy.slot_hours, max_jobs=policy.refresh_budget,
                                ordinary_resources=tiers["ordinary"]["resources"], fast_resources=tiers["fast"]["resources"])
    mapping = {"mappedCanonicalPackages": connection.execute(
        "SELECT count(DISTINCT package_id) FROM canonical_mapping_records WHERE active=1").fetchone()[0],
        "indexedPackages": connection.execute("SELECT count(*) FROM package_units WHERE active=1").fetchone()[0],
        "indexedPackagesNotMapped": connection.execute(
            "SELECT count(*) FROM package_units p WHERE p.active=1 AND NOT EXISTS "
            "(SELECT 1 FROM canonical_mapping_records m WHERE m.package_id=p.id AND m.active=1)").fetchone()[0],
        "mappingInitialized": connection.execute("SELECT 1 FROM metadata WHERE key='canonical_targets_sync'").fetchone() is not None}
    links = (" FROM canonical_repository_links l JOIN canonical_mapping_records m "
             "ON m.package_id=l.package_id AND m.version=l.version WHERE l.active=1 AND m.active=1")
    for key, expression, condition in (
        ("packagesWithRepositoryLinks", "count(DISTINCT l.package_id)", ""),
        ("packagesWithResolvedRepositories", "count(DISTINCT l.package_id)", " AND l.resolution='resolved'"),
        ("pendingRepositoryNames", "count(DISTINCT l.repository)", " AND l.resolution='pending'"),
        ("conflictingRepositoryNames", "count(DISTINCT l.repository)", " AND l.resolution='conflict'")):
        mapping[key] = connection.execute("SELECT " + expression + links + condition).fetchone()[0]
    mapping["packagesWithoutRepositoryLinks"] = mapping["mappedCanonicalPackages"] - mapping["packagesWithRepositoryLinks"]
    mapping["resolvedRepositoriesNotPlanned"] = connection.execute(
        "SELECT count(DISTINCT l.github_id)" + links + " AND l.resolution='resolved' AND NOT EXISTS "
        "(SELECT 1 FROM refresh_resources f WHERE f.github_id=l.github_id AND f.active=1 AND f.policy_id=?)",
        (policy.policy_id,)).fetchone()[0]
    other = connection.execute("SELECT count(*) FROM refresh_resources WHERE active=1 AND policy_id<>?", (policy.policy_id,)).fetchone()[0]
    latest = connection.execute("SELECT max(planned_at) FROM batch_runs WHERE policy_id=?", (policy.policy_id,)).fetchone()[0]
    cohort = connection.execute("SELECT utc_day,ranking_revision,ranking_status FROM refresh_cohorts WHERE policy_id=? "
                                "ORDER BY utc_day DESC LIMIT 1", (policy.policy_id,)).fetchone()
    warnings = list(capacity["warnings"])
    if saved is None:
        warnings.append("policy_not_planned")
    if totals["notCheckedWithinTarget"]:
        warnings.append("planned_resources_outside_check_target")
    if other:
        warnings.append("active_resources_assigned_to_other_policy")
    if not mapping["mappingInitialized"]:
        warnings.append("canonical_mapping_not_initialized")
    if (mapping["pendingRepositoryNames"] or mapping["conflictingRepositoryNames"]
            or mapping["packagesWithoutRepositoryLinks"] or mapping["indexedPackagesNotMapped"]):
        warnings.append("canonical_repository_coverage_incomplete")
    return {"asOf": cutoff, "policyId": policy.policy_id, "policyPlanned": saved is not None,
            "scope": "current_planned_github_repository_refresh", "assignmentBasis": "current_state_not_historical_cohort_reconstruction",
            "lastPlannedAt": latest, "assignmentsAfterCutoff": bool(latest and latest > cutoff),
            "rankingCohort": dict(cohort) if cohort else None,
            "totals": _finish(totals), "tiers": {key: _finish(value) for key, value in tiers.items()},
            "groups": [{"agent": key[0], "type": key[1], "tier": key[2], **_finish(value)} for key, value in sorted(groups.items())],
            "groupCountsOverlap": True, "otherPolicyActiveResources": other,
            "capacity": capacity, "mapping": mapping, "warnings": warnings,
            "checkSemantics": "successful_200_or_304_checked_at_at_or_before_cutoff",
            "observationSemantics": "observed_at_not_advanced_by_304",
            "controlSemantics": "paused_resources_remain_in_current_target_denominator",
            "catalogCoverage": "not_guaranteed", "upstreamRequests": 0, "stateWrites": 0}


def read_refresh_status(path: Path, policy: BatchPolicy, *, now=None, timeout_seconds=5.0):
    """One SQLite read transaction, including committed WAL; no missing runtime creation."""
    path = Path(path).absolute()
    if path != path.resolve() or not path.is_file() or not isinstance(policy, BatchPolicy):
        raise ValueError("refresh status requires an existing ordinary current-format state and explicit policy")
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 30:
        raise ValueError("refresh report timeout must be in (0,30]")
    now = _time(now if now is not None else utc_now())
    deadline = time.monotonic() + timeout_seconds
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=timeout_seconds)
    connection.row_factory = sqlite3.Row
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        if connection.execute("PRAGMA user_version").fetchone()[0] != STATE_SCHEMA_VERSION:
            raise ServiceError("state_version", "unsupported state format", 503)
        try:
            marker = connection.execute("SELECT value FROM metadata WHERE key='storageContract'").fetchone()
            recognized = marker is not None and json.loads(marker[0]) == STATE_CONTRACT
        except (sqlite3.Error, ValueError, TypeError):
            if time.monotonic() > deadline:
                raise ServiceError("refresh_status_timeout", "refresh report exceeded its read deadline", 503)
            recognized = False
        if not recognized:
            raise ServiceError("state_version", "unrecognized state contract", 503)
        result = _report(connection, policy, now, deadline)
        if time.monotonic() > deadline:
            raise ServiceError("refresh_status_timeout", "refresh report exceeded its read deadline", 503)
        return result
    except sqlite3.OperationalError as error:
        if time.monotonic() > deadline:
            raise ServiceError("refresh_status_timeout", "refresh report exceeded its read deadline", 503) from error
        raise ServiceError("state_unavailable", "refresh report state is unavailable", 503) from error
    finally:
        connection.close()
