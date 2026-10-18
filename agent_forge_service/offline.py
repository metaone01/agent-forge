"""Agent/type metadata exports from one immutable public query revision."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from tools.project import TYPES
from .bundles import _directory, _json, _regular, _sha
from .publishing import SnapshotStore, atomic_json, digest_file
from .settings import Settings
from .schema_contracts import QUERY_SCHEMA_VERSION, QUERY_CONTRACT
from .storage import encode_json, read_snapshot, timestamp

KIND = "agent-forge-offline-canonical"
MAX_DATABASE_BYTES = 2 * 1024 ** 3
POLICY = "version-target-membership-v1"
SCHEMA = """
PRAGMA journal_mode=DELETE;
PRAGMA foreign_keys=ON;
PRAGMA user_version=1;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE packages (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
    canonical_latest TEXT, selection_status TEXT NOT NULL,
    selected_version TEXT, selected_status TEXT NOT NULL,
    first_seen_at TEXT, bootstrap INTEGER NOT NULL,
    UNIQUE(type,name)
);
CREATE TABLE package_versions (
    package_id TEXT NOT NULL REFERENCES packages(id), version TEXT NOT NULL,
    canonical_json TEXT NOT NULL, canonical_time TEXT, source_path TEXT NOT NULL,
    payload_hash TEXT NOT NULL, PRIMARY KEY(package_id,version)
);
CREATE INDEX packages_name ON packages(name,id);
CREATE VIRTUAL TABLE packages_fts USING fts5(package_id UNINDEXED,name,description,tokenize='unicode61');
"""


def _selection(latest, versions):
    if latest is None:
        return None, "unknown"
    return (latest, "available") if latest in versions else (None, "not_in_scope")


def _schema(db):
    return [tuple(r) for r in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name")]


def _scope(agent, package_type):
    if not isinstance(agent, str) or not 1 <= len(agent) <= 200 or any(ord(c) < 32 for c in agent):
        raise ValueError("an explicit Agent ID is required")
    if package_type not in TYPES:
        raise ValueError("an explicit supported package type is required")
    return {"agent": agent, "type": package_type, "policy": POLICY}


def _counts(db):
    return {"packages": db.execute("SELECT count(*) FROM packages").fetchone()[0],
            "versions": db.execute("SELECT count(*) FROM package_versions").fetchone()[0]}


def export_offline(settings: Settings, output: Path, *, agent: str, package_type: str, revision=None):
    scope = _scope(agent, package_type)
    store = SnapshotStore(settings.snapshots)
    manifest = store.current() if revision is None else store.load(revision)
    revision = manifest["revision"]
    database = store.database(revision)
    output = Path(output).absolute()
    if output != output.resolve() or output.exists() or output.is_symlink():
        raise ValueError("offline output must be a new unlinked directory")
    for protected in (settings.catalog_root, settings.runtime_root):
        if output.is_relative_to(protected) or protected.is_relative_to(output):
            raise ValueError("offline output must not overlap catalog or runtime")
    provenance = {"revision": revision, "schemaVersion": QUERY_SCHEMA_VERSION, "storageContract": QUERY_CONTRACT,
                  "manifestSha256": digest_file(settings.snapshots / revision / "manifest.json"),
                  "databaseSha256": manifest["database"]["sha256"],
                  "generatedAt": manifest["generatedAt"],
                  "canonicalCutoff": manifest["canonicalCutoff"]}
    output.mkdir(parents=True, exist_ok=False)
    target_path = output / "catalog.db"
    # An interrupted export has no descriptor and is never accepted as complete.
    source = read_snapshot(database)
    target = sqlite3.connect(target_path)
    try:
        target.executescript(SCHEMA)
        target.execute("PRAGMA max_page_count=" + str(MAX_DATABASE_BYTES // 4096))
        target.executemany("INSERT INTO metadata VALUES (?,?)", [
            ("scope", encode_json(scope)), ("source", encode_json(provenance))])
        for package in source.execute("SELECT * FROM packages WHERE type=? ORDER BY id", (package_type,)):
            versions = []
            for row in source.execute("SELECT * FROM package_versions WHERE package_id=? ORDER BY version", (package["id"],)):
                record = json.loads(row["canonical_json"])
                if any(t["agentId"] == agent for t in record["targets"]):
                    versions.append((row, record))
            if not versions:
                continue
            selected, status = _selection(package["canonical_latest"], {r["version"] for r, _ in versions})
            target.execute("INSERT INTO packages VALUES (?,?,?,?,?,?,?,?,?)", (
                package["id"], package_type, package["name"], package["canonical_latest"],
                package["selection_status"], selected, status, package["first_seen_at"], package["bootstrap"]))
            for row, _ in versions:
                target.execute("INSERT INTO package_versions VALUES (?,?,?,?,?,?)", tuple(row[key] for key in ("package_id", "version", "canonical_json", "canonical_time", "source_path", "payload_hash")))
            description = next((r.get("description", "") for row, r in versions if row["version"] == selected), "")
            target.execute("INSERT INTO packages_fts VALUES (?,?,?)", (package["id"], package["name"], description))
        target.commit()
        if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("offline database integrity failed")
        counts = _counts(target)
    finally:
        target.close()
        source.close()
    if target_path.stat().st_size > MAX_DATABASE_BYTES:
        raise ValueError("offline database exceeds supported byte limit")
    descriptor = {"schemaVersion": 1, "kind": KIND, "scope": scope, "source": provenance,
                  "counts": counts, "content": "canonical_metadata_only",
                  "database": {"path": "catalog.db", "bytes": target_path.stat().st_size,
                               "sha256": digest_file(target_path)}}
    atomic_json(output / "offline.json", descriptor)
    return {"directory": str(output), "exportSha256": digest_file(output / "offline.json"),
            **descriptor, "onlineSnapshot": False}


def verify_offline(directory: Path, *, expected_export_sha256: str):
    """Verify the exact two files and offline schema without opening private state."""
    _sha(expected_export_sha256)
    directory = Path(directory).absolute()
    if directory != directory.resolve():
        raise ValueError("offline directory must not contain links or traversal")
    _directory(directory)
    if {p.name for p in directory.iterdir()} != {"offline.json", "catalog.db"}:
        raise ValueError("offline export must contain exactly its two declared files")
    descriptor_path = directory / "offline.json"
    descriptor, descriptor_hash = _json(descriptor_path, 64 * 1024)
    if descriptor_hash != expected_export_sha256:
        raise ValueError("offline descriptor checksum mismatch")
    keys = {"schemaVersion", "kind", "scope", "source", "counts", "content", "database"}
    if set(descriptor) != keys or type(descriptor["schemaVersion"]) is not int or descriptor["schemaVersion"] != 1 or descriptor["kind"] != KIND or descriptor["content"] != "canonical_metadata_only":
        raise ValueError("unsupported offline descriptor")
    scope = descriptor["scope"]
    if not isinstance(scope, dict) or scope != _scope(scope.get("agent"), scope.get("type")):
        raise ValueError("offline scope policy mismatch")
    provenance = descriptor["source"]
    if not isinstance(provenance, dict) or set(provenance) != {"revision", "schemaVersion", "storageContract", "manifestSha256", "databaseSha256", "generatedAt", "canonicalCutoff"}:
        raise ValueError("invalid source provenance")
    if type(provenance["schemaVersion"]) is not int or provenance["schemaVersion"] != QUERY_SCHEMA_VERSION or provenance["storageContract"] != QUERY_CONTRACT:
        raise ValueError("unsupported offline source contract")
    from .publishing import validate_revision
    validate_revision(provenance["revision"])
    _sha(provenance["manifestSha256"]); _sha(provenance["databaseSha256"])
    for key in ("generatedAt", "canonicalCutoff"):
        if not isinstance(provenance[key], str):
            raise ValueError("source times must be explicit timestamps")
        timestamp(provenance[key])
    counts = descriptor["counts"]
    if not isinstance(counts, dict) or set(counts) != {"packages", "versions"} or any(type(v) is not int or v < 0 for v in counts.values()):
        raise ValueError("invalid offline counts")
    database = descriptor["database"]
    if not isinstance(database, dict) or set(database) != {"path", "bytes", "sha256"} or database["path"] != "catalog.db" or type(database["bytes"]) is not int or not 0 < database["bytes"] <= MAX_DATABASE_BYTES:
        raise ValueError("invalid offline database descriptor")
    _sha(database["sha256"])
    path = directory / "catalog.db"
    if _regular(path).st_size != database["bytes"] or digest_file(path) != database["sha256"]:
        raise ValueError("offline database checksum mismatch")
    db = read_snapshot(path)
    model = sqlite3.connect(":memory:")
    try:
        model.executescript(SCHEMA)
        if _schema(db) != _schema(model) or db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("offline schema mismatch")
        if [tuple(r) for r in db.execute("PRAGMA integrity_check")] != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("offline database integrity failed")
        if {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM metadata")} != {"scope": scope, "source": provenance} or _counts(db) != counts:
            raise ValueError("offline metadata/count mismatch")
        from tools.validate import build_validator
        validator = build_validator("package.schema.json")
        search = {}
        for p in db.execute("SELECT * FROM packages ORDER BY id"):
            if p["type"] != scope["type"] or p["bootstrap"] not in (0, 1) or p["selection_status"] not in ("unknown", "canonical-index", "single-version"):
                raise ValueError("invalid offline package")
            if p["first_seen_at"] is not None:
                if not isinstance(p["first_seen_at"], str):
                    raise ValueError("first_seen requires an explicit timestamp")
                timestamp(p["first_seen_at"])
            rows = list(db.execute("SELECT * FROM package_versions WHERE package_id=? ORDER BY version", (p["id"],)))
            if not rows:
                raise ValueError("offline package has no versions")
            descriptions = {}
            for row in rows:
                record = json.loads(row["canonical_json"])
                if next(validator.iter_errors(record), None):
                    raise ValueError("offline canonical record schema mismatch")
                if (record["id"], record["type"], record["name"], record["version"]) != (p["id"], p["type"], p["name"], row["version"]) or not any(t["agentId"] == scope["agent"] for t in record["targets"]):
                    raise ValueError("offline version identity/target mismatch")
                if hashlib.sha256(encode_json(record).encode("utf-8")).hexdigest() != row["payload_hash"]:
                    raise ValueError("offline canonical payload hash mismatch")
                descriptions[row["version"]] = record.get("description", "")
            if (p["selected_version"], p["selected_status"]) != _selection(p["canonical_latest"], descriptions):
                raise ValueError("offline latest selection mismatch")
            search[p["id"]] = (p["name"], descriptions.get(p["selected_version"], ""))
        actual_search = {r["package_id"]: (r["name"], r["description"]) for r in db.execute("SELECT * FROM packages_fts")}
        if actual_search != search or db.execute("SELECT count(*) FROM packages_fts").fetchone()[0] != len(search):
            raise ValueError("offline search content mismatch")
    except (sqlite3.Error, KeyError, TypeError) as error:
        raise ValueError("invalid offline database") from error
    finally:
        db.close(); model.close()
    return {"valid": True, "exportSha256": expected_export_sha256, **descriptor, "onlineSnapshot": False}
