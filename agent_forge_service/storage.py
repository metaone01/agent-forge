"""State writes are confined to an OS-locked worker; snapshots use immutable reads."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import ServiceError


def timestamp(value: str | datetime) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError("timestamps require an explicit timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def encode_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


@contextmanager
def writer_lock(runtime_root: Path):
    runtime_root.mkdir(parents=True, exist_ok=True)
    # The file is intentionally retained: its existence is not lock ownership.
    handle = (runtime_root / "worker.lock").open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as error:
            raise ServiceError("worker_busy", "another worker owns this runtime", 409) from error
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def open_state(path: Path, *, resource_limits=None) -> sqlite3.Connection:
    from .resource_budget import WorkerConnection, ResourceLimits, DEFAULT_LIMITS
    limits=DEFAULT_LIMITS if resource_limits is None else resource_limits
    if not isinstance(limits,ResourceLimits):raise ValueError("resource limits must be explicit")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5, factory=WorkerConnection)
    connection.resource_limits=limits
    connection.row_factory = sqlite3.Row
    from .schema_contracts import STATE_SCHEMA_VERSION, STATE_CONTRACT
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        populated = connection.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
        if version == 0 and populated is None:
            connection.execute("PRAGMA foreign_keys=ON")
            from .collection_schema import initialize_state
            initialize_state(connection)
        elif version != STATE_SCHEMA_VERSION:
            raise ServiceError("state_version", "unsupported state format; initialize a fresh runtime from current canonical metadata", 503)
        else:
            try:
                marker = connection.execute("SELECT value FROM metadata WHERE key='storageContract'").fetchone()
                recognized = marker is not None and json.loads(marker[0]) == STATE_CONTRACT
            except (sqlite3.Error, ValueError, TypeError):
                recognized = False
            if not recognized:
                raise ServiceError("state_version", "unrecognized state contract; development databases are not migrated", 503)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
    except Exception:
        connection.close()
        raise
    return connection


def remember_package(connection: sqlite3.Connection, package_id: str, category: str,
                     name: str, digest: str, observed_at: str, bootstrap: bool) -> sqlite3.Row:
    old = connection.execute("SELECT * FROM package_units WHERE id=?", (package_id,)).fetchone()
    if old and old["type"] != category:
        raise ValueError("canonical id was reassigned to another package identity")
    if old is None:
        connection.execute("INSERT INTO package_units VALUES (?,?,?,?,?,1,?)",
                           (package_id, category, name, observed_at, int(bootstrap), digest))
        event_type = "canonical_bootstrap" if bootstrap else "canonical_added"
    elif old["canonical_hash"] != digest or old["name"] != name or not old["active"]:
        connection.execute("UPDATE package_units SET active=1,name=?,canonical_hash=? WHERE id=?", (name, digest, package_id))
        event_type = "canonical_updated" if old["active"] else "canonical_restored"
    else:
        return old
    # A state transition may recur (A -> B -> A); include the previous event sequence.
    sequence = connection.execute("SELECT coalesce(max(seq),0) FROM change_events").fetchone()[0]
    fingerprint = encode_json([package_id, digest, event_type, sequence])
    connection.execute("INSERT INTO change_events(fingerprint,unit_id,event_type,observed_at,evidence_json) VALUES (?,?,?,?,?)",
                       (fingerprint, package_id, event_type, observed_at, encode_json({"canonicalHash": digest})))
    return connection.execute("SELECT * FROM package_units WHERE id=?", (package_id,)).fetchone()


def remember_removals(connection: sqlite3.Connection, canonical_ids: set[str], observed_at: str):
    for old in connection.execute("SELECT * FROM package_units WHERE active=1").fetchall():
        if old["id"] in canonical_ids:
            continue
        connection.execute("UPDATE package_units SET active=0 WHERE id=?", (old["id"],))
        sequence = connection.execute("SELECT coalesce(max(seq),0) FROM change_events").fetchone()[0]
        fingerprint = encode_json([old["id"], "canonical_removed", sequence])
        connection.execute("INSERT INTO change_events(fingerprint,unit_id,event_type,observed_at,evidence_json) VALUES (?,?,?,?,?)",
                           (fingerprint, old["id"], "canonical_removed", observed_at, encode_json({"canonicalHash": old["canonical_hash"]})))


def backup_state(source: Path, destination: Path):
    if destination.exists() or source.resolve() == destination.resolve():
        raise ValueError("backup destination must be a new file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        target = sqlite3.connect(destination)
        try:
            source_connection.backup(target)
        finally:
            target.close()
    finally:
        source_connection.close()


def read_snapshot(path: Path, timeout: float = 2.0) -> sqlite3.Connection:
    from .reader_leases import SnapshotConnection, _lock
    path = path.resolve()
    lease = path.open("rb")
    try:
        _lock(lease, False)
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True,
                                     timeout=timeout, factory=SnapshotConnection)
        connection._snapshot_lease = lease
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except Exception:
        lease.close()
        raise
