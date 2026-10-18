"""Private Worker state backups and recovery into a fresh runtime only."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from functools import lru_cache
from pathlib import Path

from .bundles import _budget, _cleanup, _copy, _directory, _json, _regular, _sha
from .errors import ServiceError
from .publishing import atomic_json, digest_file, sync_directory
from .storage import encode_json, open_state, timestamp, utc_now, writer_lock

from .schema_contracts import STATE_SCHEMA_VERSION as STATE_VERSION, STATE_CONTRACT
DEFAULT_MAX_STATE_BYTES = 1024**3
FILES = {"backup.json", "state.db"}


def _schema(connection):
    rows = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    result = []
    for row in rows:
        if row[1] in ("sqlite_stat1", "sqlite_stat4"):
            continue
        sql = re.sub(r"\s+", " ", (row[3] or "").strip())
        sql = re.sub(r"\bIF NOT EXISTS\s+", "", sql, flags=re.IGNORECASE)
        result.append([row[0], row[1], row[2], sql])
    return result


@lru_cache(maxsize=1)
def _reference_schema():
    # Build only an empty fixture of the installed Worker contract, never migrate a backup to validate it.
    with tempfile.TemporaryDirectory(prefix="forge-state-schema-") as temporary:
        connection = open_state(Path(temporary) / "reference.db")
        try:
            if connection.execute("PRAGMA user_version").fetchone()[0] != STATE_VERSION:
                raise ValueError("state recovery contract needs an explicit schema-version update")
            return _schema(connection)
        finally:
            connection.close()


def _validate_schema(schema):
    expected = _reference_schema()
    if schema != expected:
        raise ValueError("private state schema differs from the installed Worker contract")


def verify_state(path: Path, *, max_bytes=DEFAULT_MAX_STATE_BYTES):
    size = _regular(path).st_size
    if not 0 < size <= _budget(max_bytes):
        raise ValueError("private state exceeds the file byte budget")
    for suffix in ("-wal", "-shm", "-journal"):
        if path.with_name(path.name + suffix).exists():
            raise ValueError("backup must be a settled standalone SQLite database")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    deadline = time.monotonic() + 60
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != STATE_VERSION:
            raise ValueError("managed recovery requires the initial state contract; development databases are not migrated")
        schema = _schema(connection)
        _validate_schema(schema)
        marker = connection.execute("SELECT value FROM metadata WHERE key='storageContract'").fetchone()
        if marker is None or json.loads(marker[0]) != STATE_CONTRACT:
            raise ValueError("unrecognized private state contract")
        # Schema is compared before any application query; no views/triggers/extra private extensions execute.
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("private state integrity check failed")
        tables = [row[1] for row in schema if row[0] == "table"]
        counts = {name: connection.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0] for name in tables}
        job_states = {row[0]: row[1] for row in connection.execute("SELECT state,count(*) FROM jobs GROUP BY state")}
        schema_hash = hashlib.sha256(encode_json(schema).encode()).hexdigest()
        return {"stateSchemaVersion": version, "schemaSha256": schema_hash, "tableCounts": counts, "jobStates": job_states}
    except sqlite3.Error as error:
        raise ServiceError("state_backup_invalid", "private state SQLite validation failed", 503) from error
    finally:
        connection.close()


def _standalone_backup(source: Path, target: Path, maximum):
    # SQLite backup includes committed WAL contents; copying just source state.db would omit those.
    _regular(source)
    origin = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    destination = sqlite3.connect(target)
    deadline = time.monotonic() + 60
    try:
        origin.execute("PRAGMA query_only=ON")
        origin.execute("PRAGMA trusted_schema=OFF")
        origin.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
        page_size = origin.execute("PRAGMA page_size").fetchone()[0]
        if origin.execute("PRAGMA user_version").fetchone()[0] != STATE_VERSION:
            raise ValueError("backup source must match the installed initial state contract")
        _validate_schema(_schema(origin))
        if origin.execute("PRAGMA page_count").fetchone()[0] * page_size > maximum:
            raise ValueError("private state exceeds the backup byte budget")

        def progress(status, remaining, total):
            if time.monotonic() >= deadline:
                raise ServiceError("state_backup_timeout", "SQLite backup exceeded its time budget", 503)
            if total * page_size > maximum:
                raise ValueError("private state grew beyond the backup byte budget")

        origin.backup(destination, pages=256, progress=progress, sleep=0.01)
        destination.execute("PRAGMA journal_mode=DELETE")
        destination.commit()
    except sqlite3.Error as error:
        raise ServiceError("state_backup_invalid", "source SQLite backup did not complete", 503) from error
    finally:
        destination.close()
        origin.close()
    with target.open("r+b") as stream:
        os.fsync(stream.fileno())


def _publish_backup_directory(staging: Path, output: Path):
    if staging.absolute() != staging.resolve() or output.absolute() != output.resolve() or staging.parent != output.parent:
        raise ValueError("backup publication paths changed")
    for attempt in range(6):
        if output.exists() or output.is_symlink():
            raise FileExistsError("backup output appeared during publication")
        try:
            os.rename(staging, output)
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33) or attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


def create_state_backup(settings, output: Path, *, max_state_bytes=DEFAULT_MAX_STATE_BYTES):
    output = output.absolute()
    if output != output.resolve() or output.exists() or output.is_symlink():
        raise ValueError("state backup output must be a new unlinked directory")
    for protected in (settings.runtime_root, settings.catalog_root / "sources"):
        if output.is_relative_to(protected) or protected.is_relative_to(output):
            raise ValueError("state backup must not overlap runtime or canonical sources")
    _budget(max_state_bytes)
    with writer_lock(settings.runtime_root):
        _directory(settings.runtime_root)
        if not settings.state_path.is_file():
            raise ValueError("no Worker state to back up")
        output.parent.mkdir(parents=True, exist_ok=True)
        _directory(output.parent)
        staging = output.parent / (".state-backup-" + uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        try:
            _standalone_backup(settings.state_path, staging / "state.db", max_state_bytes)
            facts = verify_state(staging / "state.db", max_bytes=max_state_bytes)
            database = {"path": "state.db", "sha256": digest_file(staging / "state.db"), "bytes": (staging / "state.db").stat().st_size}
            descriptor = {"schemaVersion": 1, "kind": "agent-forge-private-worker-state", "createdAt": timestamp(utc_now()),
                          "database": database, **facts}
            atomic_json(staging / "backup.json", descriptor)
            os.chmod(staging / "state.db", 0o600)
            os.chmod(staging / "backup.json", 0o600)
            checksum = digest_file(staging / "backup.json")
            _publish_backup_directory(staging, output)
            sync_directory(output.parent)
            return {"backupPath": str(output), "backupSha256": checksum, "databaseSha256": database["sha256"],
                    "databaseBytes": database["bytes"], "stateSchemaVersion": STATE_VERSION, "jobStates": facts["jobStates"],
                    "private": True, "scope": "Worker state only; config/canonical/query snapshots/secrets managed separately"}
        finally:
            _cleanup(staging, output.parent)


def inspect_state_backup(source: Path, *, expected_backup_sha256, max_state_bytes=DEFAULT_MAX_STATE_BYTES):
    source = source.absolute()
    if source != source.resolve():
        raise ValueError("private backup source must not be a link")
    _directory(source)
    if {p.name for p in source.iterdir()} != FILES:
        raise ValueError("private backup must contain exactly backup.json and state.db")
    descriptor, checksum = _json(source / "backup.json", 1024 * 1024)
    if checksum != _sha(expected_backup_sha256):
        raise ValueError("private backup differs from trusted expected checksum")
    required = {"schemaVersion", "kind", "createdAt", "database", "stateSchemaVersion", "schemaSha256", "tableCounts", "jobStates"}
    if set(descriptor) != required or type(descriptor["schemaVersion"]) is not int or descriptor["schemaVersion"] != 1 or descriptor["kind"] != "agent-forge-private-worker-state":
        raise ValueError("unsupported private backup representation")
    if not isinstance(descriptor["createdAt"], str):
        raise ValueError("backup timestamp must be an explicit timestamp string")
    timestamp(descriptor["createdAt"])
    db = descriptor["database"]
    if not isinstance(db, dict) or set(db) != {"path", "sha256", "bytes"} or db["path"] != "state.db" or type(db["bytes"]) is not int or not 0 < db["bytes"] <= _budget(max_state_bytes):
        raise ValueError("private backup database descriptor is invalid")
    _sha(db["sha256"])
    if _regular(source / "state.db").st_size != db["bytes"] or digest_file(source / "state.db") != db["sha256"]:
        raise ValueError("private backup database length/checksum mismatch")
    facts = verify_state(source / "state.db", max_bytes=max_state_bytes)
    if encode_json(facts) != encode_json({key: descriptor[key] for key in facts}):
        raise ValueError("private backup descriptor differs from SQLite facts")
    return {"descriptor": descriptor, "backupSha256": checksum, "private": True, "verified": True}


def restore_state_backup(settings, source: Path, *, expected_backup_sha256, max_state_bytes=DEFAULT_MAX_STATE_BYTES):
    # Recovery always installs into a fresh directory, preserving the damaged/old runtime for inspection.
    source = source.absolute()
    if source.is_relative_to(settings.runtime_root) or settings.runtime_root.is_relative_to(source):
        raise ValueError("private backup must not overlap recovery runtime")
    _budget(max_state_bytes)
    _sha(expected_backup_sha256)
    if settings.runtime_root != settings.runtime_root.resolve():
        raise ValueError("recovery runtime changed to a link")
    with writer_lock(settings.runtime_root):
        _directory(settings.runtime_root)
        if {p.name for p in settings.runtime_root.iterdir()} != {"worker.lock"}:
            raise ServiceError("recovery_target_not_empty", "restore requires a fresh runtime; preserve and inspect any existing runtime", 409)
        _regular(settings.runtime_root / "worker.lock")
        _directory(source)
        if {p.name for p in source.iterdir()} != FILES:
            raise ValueError("private backup must contain exactly backup.json and state.db")
        descriptor, checksum = _json(source / "backup.json", 1024 * 1024)
        if checksum != expected_backup_sha256:
            raise ValueError("private backup differs from trusted expected checksum")
        # Work from a private, owned copy before semantic inspection, not from a changing transfer directory.
        if descriptor.get("kind") != "agent-forge-private-worker-state" or descriptor.get("schemaVersion") != 1:
            raise ValueError("unsupported private backup representation")
        item = descriptor.get("database")
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"} or item["path"] != "state.db" or type(item["bytes"]) is not int or not 0 < item["bytes"] <= max_state_bytes:
            raise ValueError("invalid recovery database descriptor")
        _sha(item["sha256"])
        staging = settings.runtime_root / (".state-recovery-" + uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        installed = False
        try:
            _copy(source / "backup.json", staging / "backup.json", checksum, (source / "backup.json").stat().st_size)
            _copy(source / "state.db", staging / "state.db", item["sha256"], item["bytes"])
            os.chmod(staging / "state.db", 0o600)
            os.chmod(staging / "backup.json", 0o600)
            verification = inspect_state_backup(staging, expected_backup_sha256=checksum, max_state_bytes=max_state_bytes)
            if {p.name for p in settings.runtime_root.iterdir()} != {"worker.lock", staging.name}:
                raise ServiceError("recovery_target_not_empty", "recovery target changed during validation", 409)
            # Hard-link creation is atomic and fails if another process already created state.db.
            # Remove the staging name immediately, leaving a normal standalone file with one link.
            os.link(staging / "state.db", settings.state_path)
            installed = True
            (staging / "state.db").unlink()
            sync_directory(settings.runtime_root)
            atomic_json(settings.runtime_root / "state-recovery.json", {
                "schemaVersion": 1, "backupSha256": checksum, "databaseSha256": item["sha256"],
                "restoredAt": timestamp(utc_now()), "backupCreatedAt": descriptor["createdAt"],
                "semantics": "no lease/attempt/limit reset; elapsed time and normal fencing decide resumption"})
            return {"restored": True, "databaseSha256": item["sha256"], "backupSha256": checksum,
                    "stateSchemaVersion": STATE_VERSION, "private": True, "networkRequests": 0,
                    "jobStates": verification["descriptor"]["jobStates"], "preservedTaskAttemptsLeasesBudgetsAndFirstSeen": True,
                    "querySnapshotInstalled": False, "configAndCredentialIdentityRevalidationRequired": True}
        finally:
            # Post-install failures retain valid restored state; inspect status, never overwrite it on retry.
            if installed and (staging / "state.db").exists() and settings.state_path.exists():
                if os.path.samefile(staging / "state.db", settings.state_path):
                    (staging / "state.db").unlink()
            _cleanup(staging, settings.runtime_root)


def recovery_status(settings, *, max_state_bytes=DEFAULT_MAX_STATE_BYTES):
    root = settings.runtime_root
    if not root.exists():
        return {"statePresent": False, "scope": "private Worker state only"}
    _directory(root)
    if not settings.state_path.exists():
        return {"statePresent": False, "scope": "private Worker state only"}
    _regular(settings.state_path)
    _budget(max_state_bytes)
    with writer_lock(root):
        connection = sqlite3.connect(settings.state_path.resolve().as_uri() + "?mode=ro", uri=True)
        deadline = time.monotonic() + 60
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            if connection.execute("PRAGMA page_count").fetchone()[0] * connection.execute("PRAGMA page_size").fetchone()[0] > max_state_bytes:
                raise ValueError("private state exceeds status byte budget")
            if connection.execute("PRAGMA user_version").fetchone()[0] != STATE_VERSION:
                raise ValueError("recovery status requires the installed initial state schema")
            _validate_schema(_schema(connection))
            jobs = {row[0]: row[1] for row in connection.execute("SELECT state,count(*) FROM jobs GROUP BY state")}
            return {"statePresent": True, "stateSchemaVersion": STATE_VERSION, "jobStates": jobs,
                    "recoveryReceiptPresent": (root / "state-recovery.json").is_file(),
                    "automaticResumption": False, "scope": "private Worker state only; snapshots/config/secrets separate"}
        except sqlite3.Error as error:
            raise ServiceError("state_status_invalid", "private state status could not be read", 503) from error
        finally:
            connection.close()
