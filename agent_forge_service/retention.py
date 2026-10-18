"""Revision retirement, bounded cleanup plans and explicit guarded application."""
from __future__ import annotations

from contextlib import nullcontext
from itertools import islice
from datetime import datetime, timedelta
import hashlib
import math
import json
import os
from pathlib import Path
import re
import uuid

from .bundles import _directory, _json, _pointer_hash, _regular, _sha, _verify_snapshot, DEFAULT_MAX_DATABASE_BYTES
from .errors import ServiceError
from .publishing import SnapshotStore, atomic_json, digest_file, sync_directory, validate_revision
from .reader_leases import exclusive_snapshot
from .schema_contracts import QUERY_CONTRACT, QUERY_SCHEMA_VERSION
from .storage import encode_json, timestamp, utc_now, writer_lock

REGISTRY = ".retention.json"
REGISTRY_KIND = "agent-forge-snapshot-retention"
PLAN_KIND = "agent-forge-snapshot-cleanup-plan"
MAX_REGISTRY_BYTES = 2 * 1024 ** 2
MAX_ENTRIES = 4096
DRAIN_SECONDS = 60
PLAN_SECONDS = 600


def _time(value):
    if not isinstance(value, str):
        raise ValueError("retention times must be explicit timestamps")
    return datetime.fromisoformat(timestamp(value).replace("Z", "+00:00"))


def _read(root):
    path = root / REGISTRY
    if not path.exists() and not path.is_symlink():
        return {"schemaVersion": 1, "kind": REGISTRY_KIND, "entries": {}}
    data, _ = _json(path, MAX_REGISTRY_BYTES)
    if set(data) != {"schemaVersion", "kind", "entries"} or type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1 or data["kind"] != REGISTRY_KIND or not isinstance(data["entries"], dict) or len(data["entries"]) > MAX_ENTRIES:
        raise ValueError("invalid snapshot retention registry")
    for revision, entry in data["entries"].items():
        validate_revision(revision)
        if not isinstance(entry, dict) or set(entry) != {"registeredAt", "lastUsedAt", "protectedUntil", "manifestSha256", "databaseSha256"}:
            raise ValueError("invalid snapshot retention entry")
        for key in ("registeredAt", "lastUsedAt", "protectedUntil"):
            if not isinstance(entry[key], str) or timestamp(entry[key]) != entry[key]:
                raise ValueError("retention timestamps must use canonical UTC form")
            _time(entry[key])
        if _time(entry["protectedUntil"]) < _time(entry["lastUsedAt"]):
            raise ValueError("retention protection precedes snapshot use")
        _sha(entry["manifestSha256"]); _sha(entry["databaseSha256"])
    return data


def _save(root, registry):
    if len(registry["entries"]) > MAX_ENTRIES or len(encode_json(registry).encode("utf-8")) + 1 > MAX_REGISTRY_BYTES:
        raise ValueError("snapshot retention registry exceeds its budget")
    atomic_json(root / REGISTRY, registry)


def protect_snapshot(settings, manifest, *, previous_revision=None, now=None):
    """Caller holds the writer lock. Protection is persisted before pointer swap."""
    root = settings.snapshots
    registry = _read(root)
    instant = now or utc_now()
    when = timestamp(instant)
    deadline = timestamp(instant + timedelta(seconds=settings.snapshot_cursor_ttl_seconds))
    protected = [manifest]
    if previous_revision is not None and previous_revision != manifest["revision"]:
        try:
            previous, _ = _json(root / validate_revision(previous_revision) / "manifest.json", 1024 ** 2)
            if previous.get("storageContract") == QUERY_CONTRACT and previous.get("schemaVersion") == QUERY_SCHEMA_VERSION:
                protected.append(previous)
        except (OSError, ValueError, ServiceError):
            pass  # An uncertain prior directory remains untracked and cannot be cleaned.
    for item in protected:
        revision = validate_revision(item["revision"])
        manifest_hash = digest_file(root / revision / "manifest.json")
        database_hash = _sha(item["database"]["sha256"])
        old = registry["entries"].get(revision)
        if old and (old["manifestSha256"], old["databaseSha256"]) != (manifest_hash, database_hash):
            raise ValueError("tracked immutable snapshot content changed")
        registry["entries"][revision] = {"registeredAt": old["registeredAt"] if old else when,
            "lastUsedAt": max(old["lastUsedAt"], when) if old else when,
            "protectedUntil": max(old["protectedUntil"], deadline) if old else deadline,
            "manifestSha256": manifest_hash, "databaseSha256": database_hash}
    _save(root, registry)


def protect_before_switch(settings, manifest):
    previous = None
    try:
        pointer, _ = _json(settings.snapshots / "current.json", 4096)
        revision = validate_revision(pointer["revision"])
        if pointer.get("manifestSha256") == digest_file(settings.snapshots / revision / "manifest.json"):
            previous = revision
    except (OSError, ValueError, KeyError, ServiceError):
        pass
    protect_snapshot(settings, manifest, previous_revision=previous)


def register_snapshot(settings, manifest):
    registry = _read(settings.snapshots)
    if manifest["revision"] not in registry["entries"]:
        protect_snapshot(settings, manifest)


def assert_query_available(root, manifest, *, cursor=False):
    """The active revision never expires. Retired revisions use one fixed window."""
    try:
        try:
            pointer, _ = _json(root / "current.json", 4096)
        except FileNotFoundError:
            pointer = {}
        if pointer.get("revision") == manifest["revision"] and pointer.get("manifestSha256") == digest_file(root / manifest["revision"] / "manifest.json"):
            return
        entry = _read(root)["entries"].get(manifest["revision"])
        if entry and utc_now() >= _time(entry["protectedUntil"]):
            raise ServiceError("cursor_expired" if cursor else "revision_unavailable", "retired revision retention window expired", 410)
        return entry["protectedUntil"] if entry else None
    except ServiceError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ServiceError("retention_unavailable", "snapshot retirement metadata is unavailable", 503) from error


def retirement_max_age(deadline):
    return max(0, math.floor((_time(deadline) - utc_now()).total_seconds()))


def _policy(keep_last, max_remove):
    if type(keep_last) is not int or not 1 <= keep_last <= 100 or type(max_remove) is not int or not 1 <= max_remove <= 20:
        raise ValueError("retention keep_last must be [1,100] and max_remove [1,20]")
    return {"keepLast": keep_last, "maxRemove": max_remove, "drainSeconds": DRAIN_SECONDS}


def _record(root, path, revision, location, entry):
    _directory(path)
    names = {p.name for p in path.iterdir()}
    if not names <= {"catalog.db", "manifest.json"} or (location == "snapshot" and names != {"catalog.db", "manifest.json"}):
        raise ValueError("cleanup directory has unknown or missing files")
    hashes = {"catalog.db": entry["databaseSha256"], "manifest.json": entry["manifestSha256"]}
    files = {}
    for name in sorted(names):
        p = path / name
        size = _regular(p).st_size
        if digest_file(p) != hashes[name]:
            raise ValueError("cleanup snapshot checksum mismatch")
        files[name] = {"bytes": size, "sha256": hashes[name]}
    if location == "snapshot":
        _verify_snapshot(path, revision, DEFAULT_MAX_DATABASE_BYTES)
    return {"revision": revision, "path": path.relative_to(root).as_posix(), "location": location,
            "protectedUntil": entry["protectedUntil"], "files": files}


def plan_cleanup(settings, *, keep_last=3, max_remove=3, now=None):
    policy = _policy(keep_last, max_remove)
    instant = now or utc_now()
    root = settings.snapshots
    _directory(root)
    current = SnapshotStore(root).current()["revision"]
    pointer_hash = _pointer_hash(root)
    registry = _read(root)
    registry_path = root / REGISTRY
    registry_hash = digest_file(registry_path) if registry_path.exists() else "absent"
    inventory, kept = [], []
    children = list(islice(root.iterdir(), MAX_ENTRIES * 2 + 1))
    if len(children) > MAX_ENTRIES * 2:
        raise ValueError("snapshot inventory exceeds its inspection budget")
    for p in sorted(children):
        if p.name.startswith(".") or not p.is_dir():
            continue
        try:
            revision = validate_revision(p.name)
            _directory(p)
            inventory.append((p, revision, "snapshot"))
        except (OSError, ValueError, ServiceError):
            kept.append({"path": p.name, "reason": "unsafe_or_unknown"})
    trash = root / ".trash"
    if trash.exists() or trash.is_symlink():
        _directory(trash)
        children = list(islice(trash.iterdir(), MAX_ENTRIES * 2 + 1))
        if len(children) > MAX_ENTRIES * 2:
            raise ValueError("quarantine inventory exceeds its inspection budget")
        for p in sorted(children):
            match = re.fullmatch(r"(.+)--[0-9a-f]{32}", p.name)
            if match is None:
                kept.append({"path": p.relative_to(root).as_posix(), "reason": "unknown_quarantine"})
                continue
            try:
                revision = validate_revision(match[1]); _directory(p)
                inventory.append((p, revision, "quarantine"))
            except (OSError, ValueError, ServiceError):
                kept.append({"path": p.relative_to(root).as_posix(), "reason": "unsafe_or_unknown"})
    recent = sorted({r for _, r, where in inventory if where == "snapshot" and r in registry["entries"]},
                    key=lambda r: (registry["entries"][r]["lastUsedAt"], r), reverse=True)
    recent = [current, *(r for r in recent if r != current)]
    pinned = set(recent[:keep_last])
    due = []
    for path, revision, location in inventory:
        entry = registry["entries"].get(revision)
        reason = "current" if revision == current else "untracked" if entry is None else "recent" if location == "snapshot" and revision in pinned else None
        if reason is None and instant < _time(entry["protectedUntil"]) + timedelta(seconds=DRAIN_SECONDS):
            reason = "cursor_or_reader_grace"
        if reason is not None:
            kept.append({"path": path.relative_to(root).as_posix(), "reason": reason})
        else:
            due.append((path, revision, location, entry))
    due.sort(key=lambda r: (r[2] != "quarantine", r[3]["protectedUntil"], r[0].relative_to(root).as_posix()))
    eligible, inspected = [], 0
    inspection_budget = max_remove * 4
    for path, revision, location, entry in due:
        if inspected >= inspection_budget or len(eligible) >= max_remove:
            kept.append({"path": path.relative_to(root).as_posix(), "reason": "inspection_budget"})
            continue
        inspected += 1
        try:
            eligible.append(_record(root, path, revision, location, entry))
        except (OSError, ValueError, ServiceError) as error:
            kept.append({"path": path.relative_to(root).as_posix(), "reason": "unverified", "error": type(error).__name__})
    plan = {"schemaVersion": 1, "kind": PLAN_KIND, "runtimeRoot": str(settings.runtime_root),
            "generatedAt": timestamp(instant), "validUntil": timestamp(instant + timedelta(seconds=PLAN_SECONDS)),
            "pointerSha256": pointer_hash, "registrySha256": registry_hash, "policy": policy,
            "candidates": eligible, "dueCount": len(due), "inspectedCount": inspected, "kept": kept}
    return {"plan": plan, "planSha256": hashlib.sha256(encode_json(plan).encode("utf-8")).hexdigest(), "deleted": False}


def _owned(root, relative):
    p = root / relative
    if p.absolute() != p.resolve() or not p.resolve().is_relative_to(root.resolve()) or p == root:
        raise ValueError("cleanup target leaves the snapshot workspace")
    _directory(p)
    return p


def _remove_files(root, path, expected):
    path = _owned(root, path.relative_to(root))
    if {p.name for p in path.iterdir()} != set(expected):
        raise ValueError("quarantine contents changed")
    for name, item in expected.items():
        p = path / name
        if p.resolve().parent != path.resolve() or _regular(p).st_size != item["bytes"] or digest_file(p) != item["sha256"]:
            raise ValueError("quarantine file changed")
    # Only the two verified files are unlinked. Unexpected children are never
    # recursively deleted, including during recovery from a partial cleanup.
    for name in ("catalog.db", "manifest.json"):
        if name in expected:
            p = path / name
            if p.resolve().parent != path.resolve():
                raise ValueError("quarantine path changed during removal")
            p.unlink()
    path.rmdir()


def apply_cleanup(settings, supplied, *, expected_plan_sha256, now=None):
    _sha(expected_plan_sha256)
    if not isinstance(supplied, dict) or hashlib.sha256(encode_json(supplied).encode("utf-8")).hexdigest() != expected_plan_sha256:
        raise ValueError("cleanup plan checksum mismatch")
    keys = {"schemaVersion", "kind", "runtimeRoot", "generatedAt", "validUntil", "pointerSha256", "registrySha256", "policy", "candidates", "dueCount", "inspectedCount", "kept"}
    if set(supplied) != keys or type(supplied["schemaVersion"]) is not int or supplied["schemaVersion"] != 1 or supplied["kind"] != PLAN_KIND or supplied["runtimeRoot"] != str(settings.runtime_root):
        raise ValueError("unsupported cleanup plan or runtime")
    instant = now or utc_now()
    if not _time(supplied["generatedAt"]) <= instant < _time(supplied["validUntil"]) or (_time(supplied["validUntil"]) - _time(supplied["generatedAt"])).total_seconds() != PLAN_SECONDS:
        raise ValueError("cleanup plan is expired or from the future")
    policy = supplied["policy"]
    if not isinstance(policy, dict) or policy != _policy(policy.get("keepLast"), policy.get("maxRemove")):
        raise ValueError("invalid cleanup policy")
    with writer_lock(settings.runtime_root):
        fresh = plan_cleanup(settings, keep_last=policy["keepLast"], max_remove=policy["maxRemove"], now=instant)["plan"]
        if (fresh["pointerSha256"], fresh["registrySha256"]) != (supplied["pointerSha256"], supplied["registrySha256"]):
            raise ServiceError("retention_conflict", "publication or retirement state changed; make a new plan", 409)
        expected = {r["path"]: r for r in fresh["candidates"]}
        candidates = supplied["candidates"]
        if not isinstance(candidates, list) or len(candidates) > policy["maxRemove"] or any(not isinstance(r, dict) or not isinstance(r.get("path"), str) for r in candidates):
            raise ValueError("invalid cleanup candidate list")
        if len({r["path"] for r in candidates}) != len(candidates) or any(expected.get(r["path"]) != r for r in candidates):
            raise ValueError("cleanup targets are no longer verified eligible snapshots")
        root = settings.snapshots
        deleted, pending = [], []
        for item in candidates:
            path = _owned(root, item["path"])
            try:
                lease = exclusive_snapshot(path / "catalog.db") if "catalog.db" in item["files"] else nullcontext()
                with lease:
                    if item["location"] == "snapshot":
                        trash = root / ".trash"; trash.mkdir(exist_ok=True); _directory(trash)
                        target = trash / (item["revision"] + "--" + uuid.uuid4().hex)
                        if target.absolute() != target.resolve() or not target.resolve().is_relative_to(root.resolve()) or target.exists():
                            raise ValueError("quarantine output is not a new owned directory")
                        # Existing Windows reader handles make this rename fail.
                        os.rename(path, target); path = target
                        sync_directory(root)
                    _remove_files(root, path, item["files"])
                    deleted.append(item["path"])
            except (OSError, ServiceError, ValueError) as error:
                pending.append({"path": path.relative_to(root).as_posix(), "error": getattr(error, "code", type(error).__name__)})
        registry = _read(root)
        for item in candidates:
            revision = item["revision"]
            remains = (root / revision).exists()
            if (root / ".trash").exists():
                remains = remains or any(p.name.startswith(revision + "--") for p in (root / ".trash").iterdir())
            if not remains:
                registry["entries"].pop(revision, None)
        if deleted or pending:
            _save(root, registry)
            sync_directory(root)
        return {"complete": not pending, "deleted": deleted, "pending": pending,
                "pointerSha256": _pointer_hash(root), "currentPreserved": _pointer_hash(root) == supplied["pointerSha256"],
                "replanRequired": bool(pending), "externalDeployment": False}


def run_cleanup(settings, *, keep_last=3, max_remove=3, apply=False):
    """Opt-in unattended entry point. A plain invocation only returns a plan."""
    if type(apply) is not bool:
        raise ValueError("cleanup apply must be explicit")
    receipt = plan_cleanup(settings, keep_last=keep_last, max_remove=max_remove)
    if not apply:
        return {**receipt, "applied": False}
    result = apply_cleanup(settings, receipt["plan"], expected_plan_sha256=receipt["planSha256"])
    return {"planSha256": receipt["planSha256"], "policy": receipt["plan"]["policy"],
            "candidateCount": len(receipt["plan"]["candidates"]), "applied": True, **result}
