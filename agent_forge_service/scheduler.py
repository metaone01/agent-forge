"""UTC batch driver using the existing queue, quotas and immutable publisher."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta

from .batches import BatchPolicy, batch_status, plan_batch, run_batch
from .bundles import DEFAULT_MAX_DATABASE_BYTES, _directory, _pointer_hash, _regular, _sha, _verify_snapshot
from .canonical_mapping import sync_canonical_mappings
from .downloads import DownloadsClient
from .errors import ServiceError
from .resource_budget import ResourceLimits, DEFAULT_LIMITS, storage_status
from .github_client import GitHubClient
from .recommendations import RecommendationClient
from .publishing import SnapshotStore, _build_and_publish_locked, atomic_json, digest_file, validate_revision
from .scheduling import _time, utc_slot
from .storage import encode_json, open_state, timestamp, utc_now, writer_lock

PREFIX = "scheduler:v1:"
HISTORY_LIMIT = 20
STATE_BYTES = 131072
COMPLETION_REASONS = frozenset(("selected_work_settled", "collection_deadline", "unresolved_or_partial_work", "request_budgets_exhausted"))


@dataclass(frozen=True)
class ScheduleOptions:
    max_requests: int
    max_batch_seconds: int
    poll_seconds: int = 60
    include_undated: bool = False
    resource_limits: ResourceLimits = DEFAULT_LIMITS

    def validate(self, policy):
        if not isinstance(self.resource_limits,ResourceLimits):
            raise ValueError("scheduler resource limits must be explicit")
        if type(self.max_requests) is not int or not 1 <= self.max_requests <= 100:
            raise ValueError("scheduler request slice must be in [1,100]")
        if type(self.max_batch_seconds) is not int or not 1 <= self.max_batch_seconds <= policy.slot_hours * 3600:
            raise ValueError("scheduler collection deadline must not exceed a slot")
        if type(self.poll_seconds) is not int or not 1 <= self.poll_seconds <= 60 or type(self.include_undated) is not bool:
            raise ValueError("scheduler needs explicit polling and undated-import options")


def _policy_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 80 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError("scheduler policy identity is invalid")
    return value


def _decode(raw, policy_id):
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > STATE_BYTES:
            raise ValueError("scheduler state exceeds its text/byte budget")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate scheduler state key")
                result[key] = value
            return result

        def instant(value):
            if not isinstance(value, str):
                raise ValueError("scheduler timestamps must be strings")
            return _time(value)

        def scheduled_revision(value):
            if not isinstance(value, str) or not re.fullmatch(r"scheduled-[0-9a-f]{32}", value):
                raise ValueError("invalid scheduled revision")
            validate_revision(value)

        def nonnegative(value):
            if type(value) is not int or value < 0:
                raise ValueError("invalid scheduler count")

        def receipt(value, *, published=False):
            if not isinstance(value, dict):
                raise ValueError("invalid scheduler receipt")
            keys = {"slot", "batchId", "revision", "completionReason", "coalescedSlots"}
            is_published = value.get("revision") is not None
            if published and not is_published:
                raise ValueError("last publication must have a revision")
            if is_published:
                keys |= {"publishedAt", "partial"}
                scheduled_revision(value["revision"])
                if instant(value["publishedAt"]) < instant(value["slot"]) or type(value["partial"]) is not bool:
                    raise ValueError("invalid publication receipt")
                if value["completionReason"] not in COMPLETION_REASONS:
                    raise ValueError("invalid publication completion reason")
            elif value.get("completionReason") != "superseded_by_latest_slot":
                raise ValueError("invalid superseded receipt")
            if set(value) != keys:
                raise ValueError("invalid scheduler receipt fields")
            slot = timestamp(instant(value["slot"]))
            if utc_slot(slot, 4) != slot:
                raise ValueError("invalid scheduler slot")
            if value["batchId"] is not None:
                expected = hashlib.sha256(encode_json([policy_id, slot]).encode()).hexdigest()
                if value["batchId"] != expected:
                    raise ValueError("receipt belongs to another batch")
            elif is_published:
                raise ValueError("publication has no batch")
            nonnegative(value["coalescedSlots"])

        state = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite scheduler state")))
        keys = {"schemaVersion", "policyId", "active", "lastPublished", "lastTickAt", "history", "policyHash"}
        if (not isinstance(state, dict) or set(state) != keys or type(state["schemaVersion"]) is not int
                or state["schemaVersion"] != 1 or state["policyId"] != policy_id):
            raise ValueError("invalid scheduler state envelope")
        _sha(state["policyHash"])
        if not isinstance(state["history"], list) or len(state["history"]) > HISTORY_LIMIT:
            raise ValueError("invalid scheduler history")
        if state["lastTickAt"] is not None:
            instant(state["lastTickAt"])
        if state["lastPublished"] is not None:
            receipt(state["lastPublished"], published=True)
        for item in state["history"]:
            receipt(item)
        active = state["active"]
        if active is not None:
            keys = {"phase", "slot", "batchId", "startedAt", "deadlineAt", "revision", "includeUndated",
                    "skippedSlots", "abandonedRevisions", "expectedPointerSha256"}
            if not isinstance(active, dict) or active.get("phase") not in ("planning", "collecting", "publishing"):
                raise ValueError("invalid scheduler phase")
            if active["phase"] == "publishing":
                keys |= {"completionReason", "collectionFinishedAt"}
                if active["completionReason"] not in COMPLETION_REASONS:
                    raise ValueError("invalid collection completion reason")
                if instant(active["collectionFinishedAt"]) < instant(active["startedAt"]):
                    raise ValueError("collection finished before it started")
                if active["expectedPointerSha256"] != "absent":
                    _sha(active["expectedPointerSha256"])
            elif active["expectedPointerSha256"] is not None:
                raise ValueError("unexpected publication pointer")
            if set(active) != keys:
                raise ValueError("invalid active scheduler fields")
            slot = timestamp(instant(active["slot"]))
            started, deadline = instant(active["startedAt"]), instant(active["deadlineAt"])
            if utc_slot(slot, 4) != slot or not instant(slot) <= started < deadline:
                raise ValueError("invalid scheduler time ordering")
            scheduled_revision(active["revision"])
            attempts = active["abandonedRevisions"]
            if not isinstance(attempts, list) or len(attempts) > 5:
                raise ValueError("invalid scheduler attempt history")
            for revision in attempts:
                scheduled_revision(revision)
            if len(set(attempts)) != len(attempts) or active["revision"] in attempts:
                raise ValueError("duplicate scheduler publication attempt")
            if type(active["includeUndated"]) is not bool:
                raise ValueError("invalid scheduler import option")
            nonnegative(active["skippedSlots"])
            expected_batch = hashlib.sha256(encode_json([policy_id, slot]).encode()).hexdigest()
            if active["phase"] == "planning":
                if active["batchId"] is not None:
                    raise ValueError("planning unexpectedly has a batch")
            elif active["batchId"] != expected_batch:
                raise ValueError("active state belongs to another batch")
        return state
    except (ValueError, TypeError, KeyError, RecursionError, ServiceError) as error:
        raise ServiceError("scheduler_state_invalid", "persisted scheduler state is invalid", 503) from error


def _load(connection, policy):
    row = connection.execute("SELECT value FROM metadata WHERE key=?", (PREFIX + policy.policy_id,)).fetchone()
    state = _decode(row[0], policy.policy_id) if row else {"schemaVersion": 1, "policyId": policy.policy_id, "active": None,
        "lastPublished": None, "lastTickAt": None, "history": [], "policyHash": None}
    definition = asdict(policy)
    if policy.recommendations is None:
        definition.pop("recommendations")
    for lane in ("manifests","releases"):
        if getattr(policy,lane) is None:definition.pop(lane)
    expected = hashlib.sha256(encode_json(definition).encode()).hexdigest()
    if state["policyHash"] is not None and state["policyHash"] != expected:
        raise ServiceError("batch_policy_conflict", "scheduler policy ID cannot change its parameters", 409)
    state["policyHash"] = expected
    for item in (state["active"], state["lastPublished"], *state["history"]):
        if item and utc_slot(item["slot"], policy.slot_hours) != item["slot"]:
            raise ServiceError("scheduler_state_invalid", "scheduler slot does not match its policy", 503)
    active = state["active"]
    if active:
        if utc_slot(active["startedAt"], policy.slot_hours) != active["slot"] or (_time(active["deadlineAt"]) - _time(active["startedAt"])).total_seconds() > policy.slot_hours * 3600:
            raise ServiceError("scheduler_state_invalid", "scheduler collection times do not match its slot", 503)
        if active["phase"] != "planning":
            try:
                status = batch_status(connection, active["batchId"])
            except ServiceError as error:
                raise ServiceError("scheduler_state_invalid", "scheduler active batch is unavailable", 503) from error
            if (status["policyId"], status["slot"]) != (policy.policy_id, active["slot"]):
                raise ServiceError("scheduler_state_invalid", "scheduler active batch differs from its policy", 503)
    return state


def _save(connection, state):
    encoded = encode_json(state)
    _decode(encoded, state["policyId"])
    if len(encoded.encode()) > STATE_BYTES:
        raise ServiceError("scheduler_state_invalid", "scheduler state exceeds its byte budget", 503)
    with connection:
        connection.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (PREFIX + state["policyId"], encoded))


def _history(state, receipt):
    state["history"].append(receipt)
    del state["history"][:-HISTORY_LIMIT]


def _new_active(policy, options, now, skipped):
    return {"phase": "planning", "slot": utc_slot(now, policy.slot_hours), "batchId": None,
        "startedAt": timestamp(now), "deadlineAt": timestamp(_time(now) + timedelta(seconds=options.max_batch_seconds)),
        "revision": "scheduled-" + uuid.uuid4().hex, "includeUndated": options.include_undated,
        "skippedSlots": skipped, "abandonedRevisions": [], "expectedPointerSha256": None}


def _next_slot(slot, hours):
    return timestamp(_time(slot) + timedelta(hours=hours))


def _publication_wake(receipt, policy, now):
    if utc_slot(now, policy.slot_hours) > receipt["slot"]:
        return timestamp(now)
    return _next_slot(receipt["slot"], policy.slot_hours)


def _pending(lane):
    return any(lane["jobs"].get(key, 0) for key in ("pending", "retry_wait", "leased"))


def _completion(status, active, now):
    if status["selectedWorkSettled"]:
        return "selected_work_settled"
    if _time(now) >= _time(active["deadlineAt"]):
        return "collection_deadline"
    if not any(_pending(lane) for lane in status["lanes"].values()):
        return "unresolved_or_partial_work"
    if not any(_pending(lane) and lane["charged"] < lane["quota"] for lane in status["lanes"].values()):
        return "request_budgets_exhausted"
    return None


def _collection_batch(connection, policy, active):
    status = batch_status(connection, active["batchId"])
    return {"batchId": status["batchId"], "policyId": status["policyId"], "scheduledSlot": status["slot"],
        "slotHours": policy.slot_hours, "plannedAt": status["plannedAt"], "startedAt": active["startedAt"],
        "collectionFinishedAt": active["collectionFinishedAt"], "completionReason": active["completionReason"],
        "stateAtPublication": status["state"], "selectedWorkSettled": status["selectedWorkSettled"],
        "partial": active["completionReason"] != "selected_work_settled" or not status["selectedWorkSettled"],
        "confirmedRequests": sum(lane["requests"] for lane in status["lanes"].values()),
        "reservedRequests": sum(lane["charged"] for lane in status["lanes"].values()),
        "uncertainAttempts": sum(lane["uncertain"] for lane in status["lanes"].values()), "coalescedSlots": active["skippedSlots"],
        "catalogCoverage": status["catalogCoverage"], "deferredRefreshResources": status["summary"]["refreshDeferred"]}


def validate_collection_batch(value, connection, generated_at):
    """Publication provenance is derived from durable batch accounting, not a quality score."""
    keys = {"batchId", "policyId", "scheduledSlot", "slotHours", "plannedAt", "startedAt", "collectionFinishedAt", "completionReason",
            "stateAtPublication", "selectedWorkSettled", "partial", "confirmedRequests", "reservedRequests", "uncertainAttempts", "coalescedSlots", "catalogCoverage", "deferredRefreshResources"}
    if not isinstance(value, dict) or set(value) != keys or type(value["slotHours"]) is not int or value["slotHours"] not in (4, 8):
        raise ValueError("invalid collection batch provenance")
    if value["completionReason"] not in ("selected_work_settled", "collection_deadline", "unresolved_or_partial_work", "request_budgets_exhausted"):
        raise ValueError("invalid collection completion reason")
    status = batch_status(connection, value["batchId"])
    config = json.loads(connection.execute("SELECT config_json FROM batch_policies WHERE id=?", (status["policyId"],)).fetchone()[0])
    if value["slotHours"] != config["slot_hours"]:
        raise ValueError("collection slot duration differs from durable policy")
    if (value["policyId"], value["scheduledSlot"], value["plannedAt"], value["stateAtPublication"], value["selectedWorkSettled"]) != (status["policyId"], status["slot"], status["plannedAt"], status["state"], status["selectedWorkSettled"]):
        raise ValueError("collection batch differs from durable status")
    if value["catalogCoverage"] != status["catalogCoverage"] or type(value["deferredRefreshResources"]) is not int or value["deferredRefreshResources"] != status["summary"]["refreshDeferred"]:
        raise ValueError("collection coverage differs from durable planning")
    expected = (sum(lane["requests"] for lane in status["lanes"].values()), sum(lane["charged"] for lane in status["lanes"].values()), sum(lane["uncertain"] for lane in status["lanes"].values()))
    if any(type(value[key]) is not int or value[key] < 0 for key in ("confirmedRequests", "reservedRequests", "uncertainAttempts", "coalescedSlots")) or (value["confirmedRequests"], value["reservedRequests"], value["uncertainAttempts"]) != expected:
        raise ValueError("collection request accounting differs from durable attempts")
    if type(value["partial"]) is not bool or type(value["selectedWorkSettled"]) is not bool or value["partial"] != (value["completionReason"] != "selected_work_settled" or not status["selectedWorkSettled"]):
        raise ValueError("invalid collection completeness declaration")
    if any(not isinstance(value[key], str) for key in ("scheduledSlot", "plannedAt", "startedAt", "collectionFinishedAt")):
        raise ValueError("collection times must be strings")
    if not _time(value["scheduledSlot"]) <= _time(value["startedAt"]) <= _time(value["plannedAt"]) <= _time(value["collectionFinishedAt"]) <= _time(generated_at) or utc_slot(value["plannedAt"],value["slotHours"]) != value["scheduledSlot"]:
        raise ValueError("invalid collection/publication time ordering")
    return value


def _publication_time(state, connection, manifest, clock):
    observed = timestamp(clock())
    earliest = max(state["lastTickAt"], timestamp(manifest["generatedAt"]))
    if observed < earliest:
        state["lastTickAt"] = earliest
        _save(connection, state)
        raise ServiceError("scheduler_clock_wait", "wall clock moved backward during publication", 503)
    return observed


def _publish_locked(settings, policy, state, connection, clock, publisher, should_stop=None):
    if should_stop and should_stop():
        return None
    active = state["active"]
    target = settings.snapshots / active["revision"]
    if target.exists() or target.is_symlink():
        _directory(settings.snapshots)
        current_hash = _pointer_hash(settings.snapshots)
        manifest, checksum, _ = _verify_snapshot(target, active["revision"], DEFAULT_MAX_DATABASE_BYTES)
        context = manifest.get("collectionBatch", {})
        if encode_json(context) != encode_json(_collection_batch(connection, policy, active)):
            raise ServiceError("publication_conflict", "existing revision differs from the scheduled batch", 409)
        try:
            is_current = SnapshotStore(settings.snapshots).current()["revision"] == active["revision"]
        except ServiceError:
            is_current = False
        if _pointer_hash(settings.snapshots) != current_hash:
            raise ServiceError("publication_conflict", "current pointer changed during recovery validation", 409)
        _regular(target / "manifest.json")
        _regular(target / "catalog.db")
        if digest_file(target / "manifest.json") != checksum or digest_file(target / "catalog.db") != manifest["database"]["sha256"]:
            raise ServiceError("snapshot_checksum", "scheduled snapshot changed during recovery validation", 409)
        if should_stop and should_stop():
            return None
        if not is_current:
            if current_hash != active["expectedPointerSha256"]:
                raise ServiceError("publication_conflict", "current pointer changed during interrupted publication", 409)
            published_at = _publication_time(state, connection, manifest, clock)
            atomic_json(settings.snapshots / "current.json", {"revision": active["revision"], "manifestSha256": checksum, "publishedAt": published_at})
    else:
        if _pointer_hash(settings.snapshots) != active["expectedPointerSha256"]:
            raise ServiceError("publication_conflict", "current pointer changed before publication retry", 409)
        staging = settings.snapshots / ".staging" / active["revision"]
        if staging.exists() or staging.is_symlink():
            _directory(staging)
            if len(active["abandonedRevisions"]) >= 5:
                raise ServiceError("publication_retries_exhausted", "inspect abandoned publication attempts before retrying", 503)
            active["abandonedRevisions"].append(active["revision"])
            active["revision"] = "scheduled-" + uuid.uuid4().hex
            _save(connection, state)
        context = _collection_batch(connection, policy, active)
        if should_stop and should_stop():
            return None
        # The builder opens its own state connection; the coordinator transaction is already committed.
        manifest = publisher(settings, now=clock(), revision=active["revision"], include_undated=active["includeUndated"],
                             collection_batch=context, expected_pointer_sha256=active["expectedPointerSha256"])
    receipt = {"slot": active["slot"], "batchId": active["batchId"], "revision": active["revision"],
        "publishedAt": _publication_time(state, connection, manifest, clock), "completionReason": active["completionReason"], "partial": manifest["collectionBatch"]["partial"],
        "coalescedSlots": active["skippedSlots"]}
    state["lastTickAt"] = receipt["publishedAt"]
    state["lastPublished"] = receipt
    _history(state, receipt)
    state["active"] = None
    _save(connection, state)
    return receipt


def scheduled_tick(settings, policy: BatchPolicy, options: ScheduleOptions, *, identity=None, owner="scheduled-worker",
                   clock=utc_now, client_factory=None, downloads_factory=None, recommendations_factory=None, anonymous_factory=None, publisher=_build_and_publish_locked, should_stop=None):
    if not isinstance(policy, BatchPolicy) or not isinstance(options, ScheduleOptions):
        raise ValueError("scheduler needs explicit batch policy and options")
    options.validate(policy)
    github_required = any((policy.refresh_budget, policy.bootstrap_budget, policy.discovery_budget, policy.history_budget, policy.recommendations.budget if policy.recommendations else 0))
    if github_required and (not isinstance(identity,str) or not identity or len(identity)>200 or any(ord(c)<33 or ord(c)==127 for c in identity)):
        raise ValueError("GitHub lanes require an explicit non-secret identity")
    if not isinstance(owner,str) or not owner or len(owner)>200 or any(ord(c)<33 or ord(c)==127 for c in owner):
        raise ValueError("scheduler needs a bounded lease owner")
    if should_stop is not None and not callable(should_stop):
        raise ValueError("scheduler cancellation must be callable")
    if should_stop and should_stop():
        return {"phase": "interrupted", "nextWakeAt": timestamp(clock()), "requestsThisTick": 0}
    storage=storage_status(settings.runtime_root,options.resource_limits)
    if not storage["allowed"]:
        return {"phase":"storage_wait","requestsThisTick":0,"storage":storage,
                "nextWakeAt":timestamp(_time(clock())+timedelta(seconds=options.poll_seconds))}
    with writer_lock(settings.runtime_root):
        connection = open_state(settings.state_path,resource_limits=options.resource_limits)
        requests = 0
        try:
            state = _load(connection, policy)
            now = clock(); slot = utc_slot(now, policy.slot_hours)
            if state["lastTickAt"] and _time(now)<_time(state["lastTickAt"]):
                return {"phase":"clock_wait", "nextWakeAt":state["lastTickAt"], "requestsThisTick":0}
            state["lastTickAt"] = timestamp(now)
            active = state["active"]
            if active and active["phase"] == "publishing":
                receipt = _publish_locked(settings, policy, state, connection, clock, publisher, should_stop)
                if receipt is None:
                    return {"phase": "interrupted", "nextWakeAt": timestamp(clock()), "requestsThisTick": 0}
                return {"phase": "published", "publication": receipt,
                        "nextWakeAt": _publication_wake(receipt, policy, clock()), "requestsThisTick": 0}
            skipped = 0
            if active and _time(active["slot"])<_time(slot):
                skipped = int((_time(slot)-_time(active["slot"])).total_seconds()//(policy.slot_hours*3600))
                _history(state,{"slot":active["slot"],"batchId":active["batchId"],"revision":None,"completionReason":"superseded_by_latest_slot","coalescedSlots":skipped})
                state["active"] = None
            if state["lastPublished"] and state["lastPublished"]["slot"]==slot and state["active"] is None:
                _save(connection,state)
                return {"phase":"idle", "publication":state["lastPublished"], "nextWakeAt":_next_slot(slot,policy.slot_hours), "requestsThisTick":0}
            if state["active"] is None:
                last = state["lastPublished"]
                if last:
                    skipped = max(skipped, int((_time(slot)-_time(last["slot"])).total_seconds()//(policy.slot_hours*3600))-1)
                state["active"] = _new_active(policy,options,now,max(0,skipped))
                _save(connection,state)
            active = state["active"]
            if active["phase"]=="planning":
                sync_canonical_mappings(connection,settings.catalog_root,now=now)
                planned = plan_batch(connection,policy,now=now,ranking_store=SnapshotStore(settings.snapshots))
                active.update(batchId=planned["batchId"],phase="collecting")
                _save(connection,state)
            if utc_slot(clock(),policy.slot_hours)!=active["slot"]:
                return {"phase":"slot_advanced", "nextWakeAt":timestamp(_time(clock())+timedelta(seconds=1)), "requestsThisTick":0}
            if should_stop and should_stop():
                return {"phase":"interrupted", "nextWakeAt":timestamp(clock()), "requestsThisTick":0}
            client = (client_factory(connection) if client_factory else GitHubClient(connection,identity=identity,clock=clock)) if github_required else None
            downloads = (downloads_factory(connection) if downloads_factory else DownloadsClient(connection,clock=clock)) if policy.downloads and policy.downloads.budget else None
            recommendations = (recommendations_factory(connection) if recommendations_factory else RecommendationClient(connection,identity=identity,clock=clock)) if policy.recommendations and policy.recommendations.budget else None
            from .public_git import AnonymousGitClient
            anonymous=(anonymous_factory(connection) if anonymous_factory else AnonymousGitClient(connection,clock=clock)) if any(getattr(policy,lane) and getattr(policy,lane).budget for lane in ("manifests","releases")) else None
            status = run_batch(connection,client,active["batchId"],owner=owner,max_requests=options.max_requests,clock=clock,downloads_client=downloads,recommendation_client=recommendations,anonymous_client=anonymous,
                should_stop=lambda: bool(should_stop and should_stop()) or _time(clock()) >= _time(active["deadlineAt"]) or utc_slot(clock(),policy.slot_hours)!=active["slot"])
            finished = clock()
            requests = status["requestsThisInvocation"]
            if _time(finished) < _time(state["lastTickAt"]):
                return {"phase": "clock_wait", "nextWakeAt": state["lastTickAt"],
                        "requestsThisTick": requests, "batch": status}
            state["lastTickAt"] = timestamp(finished)
            _save(connection,state)
            if should_stop and should_stop():
                return {"phase":"interrupted", "nextWakeAt":timestamp(finished), "requestsThisTick":requests, "batch":status}
            if utc_slot(finished,policy.slot_hours)!=active["slot"]:
                return {"phase":"slot_advanced", "nextWakeAt":timestamp(_time(finished)+timedelta(seconds=1)), "requestsThisTick":requests, "batch":status}
            reason = _completion(status,active,finished)
            if reason:
                active.update(phase="publishing",completionReason=reason,collectionFinishedAt=timestamp(finished),expectedPointerSha256=_pointer_hash(settings.snapshots))
                _save(connection,state)
                receipt = _publish_locked(settings, policy, state, connection, clock, publisher, should_stop)
                if receipt is None:
                    return {"phase": "interrupted", "batch": status, "nextWakeAt": timestamp(clock()), "requestsThisTick": requests}
                return {"phase": "published", "publication": receipt, "batch": status,
                        "nextWakeAt": _publication_wake(receipt, policy, clock()), "requestsThisTick": requests}
            next_times = [_time(finished)+timedelta(seconds=options.poll_seconds),_time(active["deadlineAt"]),_time(_next_slot(active["slot"],policy.slot_hours))]
            for name,lane in status["lanes"].items():
                if not _pending(lane) or lane["charged"]>=lane["quota"] or not lane["nextRunAt"]:
                    continue
                resource = "anonymousCore" if name in ("manifests","releases") else "downloads" if name.startswith("download") else "search" if name in ("incremental","history") else "core"
                ready = _time(lane["nextRunAt"])
                if status["nextNetworkAt"].get(resource):
                    ready = max(ready,_time(status["nextNetworkAt"][resource]))
                if ready>_time(finished):
                    next_times.append(ready)
            return {"phase":"collecting", "batch":status, "requestsThisTick":requests,
                    "nextWakeAt":timestamp(max(_time(finished)+timedelta(seconds=1),min(next_times)))}
        except ServiceError as error:
            if error.code != "scheduler_clock_wait":
                raise
            return {"phase": "clock_wait", "nextWakeAt": state["lastTickAt"], "requestsThisTick": requests}
        finally:
            connection.close()


def scheduler_status(path, policy_id):
    _policy_id(policy_id)
    if not path.exists() and not path.is_symlink():
        return {"configured":False,"policyId":policy_id}
    _regular(path)
    connection = sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True,timeout=2)
    try:
        row = connection.execute("SELECT value FROM metadata WHERE key=?",(PREFIX+policy_id,)).fetchone()
        return {"configured":False,"policyId":policy_id} if row is None else {"configured":True,**_decode(row[0],policy_id)}
    except sqlite3.Error as error:
        raise ServiceError("scheduler_state_invalid", "scheduler state cannot be read", 503) from error
    finally:
        connection.close()


def run_schedule(settings, policy, options, *, identity=None, owner="scheduled-worker", stop=None, clock=utc_now, emit=print, tick=scheduled_tick):
    """Foreground driver. Signals stop new requests; the current GET settles first."""
    stop = stop or threading.Event()
    previous = None
    while not stop.is_set():
        try:
            result = tick(settings,policy,options,identity=identity,owner=owner,clock=clock,should_stop=stop.is_set)
            event = {"phase":result["phase"],"requestsThisTick":result["requestsThisTick"],"publication":result.get("publication"),"nextWakeAt":result["nextWakeAt"]}
            if result.get("storage") is not None:event["storage"]=result["storage"]
        except ServiceError as error:
            if error.code in ("batch_policy_conflict","scheduler_state_invalid","recommendation_refresh_state_invalid","publication_conflict","publication_retries_exhausted","snapshot_checksum"):
                raise
            event = {"phase":"retry_wait","error":error.code,"requestsThisTick":0,"nextWakeAt":timestamp(_time(clock())+timedelta(seconds=options.poll_seconds))}
        signature = encode_json({key:value for key,value in event.items() if key!="nextWakeAt"})
        if signature!=previous or event["requestsThisTick"]:
            emit(encode_json(event));previous=signature
        remaining = max(0.1, min(60, (_time(event["nextWakeAt"]) - _time(clock())).total_seconds()))
        # Windows dispatches Python signals only after an Event wait returns.
        while remaining > 0 and not stop.is_set():
            interval = min(1, remaining)
            stop.wait(interval)
            remaining -= interval
    return {"phase":"stopped","newRequestsSuppressed":True}
