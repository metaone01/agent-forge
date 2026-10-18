"""Build isolated immutable query snapshots and atomically publish their pointer."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tools.project import iter_records, record_time
from tools.validate import build_validator
from tools.media import media_summary

from .errors import ServiceError
from .settings import Settings
from .storage import encode_json, open_state, read_snapshot, remember_package, remember_removals, timestamp, utc_now, writer_lock

REVISION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
from .schema_contracts import QUERY_SCHEMA_VERSION, QUERY_CONTRACT


def validate_revision(value: str) -> str:
    if not isinstance(value, str) or not REVISION_PATTERN.fullmatch(value):
        raise ServiceError("invalid_revision", "invalid snapshot revision")
    return value


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sync_directory(path: Path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _cleanup_missing_file(function, path, error):
    if not isinstance(error[1], FileNotFoundError):
        raise error[1]


def cleanup_staging(path: Path):
    # Antivirus/file-system filters can finish transient journal cleanup after close on Windows.
    for attempt in range(6):
        try:
            shutil.rmtree(path, onerror=_cleanup_missing_file)
            return
        except OSError as error:
            if (error.errno != errno.ENOTEMPTY and getattr(error, "winerror", None) != 145) or attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_json(path: Path, value: Any):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encode_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _create_schema(connection: sqlite3.Connection) -> bool:
    connection.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE packages (
            id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
            display_name TEXT, summary TEXT NOT NULL DEFAULT '', subtype TEXT,
            canonical_latest TEXT, selection_status TEXT NOT NULL DEFAULT 'unknown',
            canonical_updated_at TEXT, first_seen_at TEXT, bootstrap INTEGER NOT NULL DEFAULT 1,
            keywords_json TEXT NOT NULL DEFAULT '[]', facets_json TEXT NOT NULL DEFAULT '{}',
            custom_facets_json TEXT NOT NULL DEFAULT '{}', targets_json TEXT NOT NULL DEFAULT '[]',
            media_json TEXT NOT NULL DEFAULT '{}', search_text TEXT NOT NULL DEFAULT '',
            repository_id INTEGER, repository_mapping_status TEXT NOT NULL DEFAULT 'unmapped',
            UNIQUE(type,name)
        );
        CREATE TABLE package_versions (
            package_id TEXT NOT NULL REFERENCES packages(id), version TEXT NOT NULL,
            canonical_json TEXT NOT NULL, canonical_time TEXT, source_path TEXT NOT NULL,
            payload_hash TEXT NOT NULL, PRIMARY KEY(package_id,version)
        );
        CREATE TABLE package_agents (
            package_id TEXT NOT NULL REFERENCES packages(id), agent TEXT NOT NULL,
            PRIMARY KEY(package_id,agent)
        );
        CREATE INDEX agents_lookup ON package_agents(agent,package_id);
        CREATE TABLE package_facets (
            package_id TEXT NOT NULL REFERENCES packages(id), kind TEXT NOT NULL,
            value TEXT NOT NULL, custom INTEGER NOT NULL, PRIMARY KEY(package_id,kind,value,custom)
        );
        CREATE INDEX facets_lookup ON package_facets(custom,kind,value,package_id);
        CREATE TABLE change_events (
            seq INTEGER PRIMARY KEY, unit_id TEXT NOT NULL, event_type TEXT NOT NULL,
            observed_at TEXT NOT NULL, evidence_json TEXT NOT NULL
        );
        CREATE TABLE sources (id TEXT PRIMARY KEY, data_json TEXT NOT NULL);
        CREATE INDEX packages_updated ON packages(canonical_updated_at DESC,id);
        CREATE INDEX packages_type_updated ON packages(type,canonical_updated_at DESC,id);
        CREATE INDEX packages_discovered ON packages(first_seen_at DESC,id);
        CREATE INDEX packages_name ON packages(name,id);
        CREATE VIRTUAL TABLE packages_fts USING fts5(
            name,display_name,summary,search_text,content='packages',content_rowid='rowid',tokenize='unicode61'
        );
        PRAGMA user_version=1;
    """)
    from .observations import create_observation_schema
    create_observation_schema(connection)
    try:
        connection.execute("CREATE VIRTUAL TABLE packages_trigram USING fts5(name,display_name,summary,search_text,content='packages',content_rowid='rowid',tokenize='trigram')")
        return True
    except sqlite3.OperationalError as error:
        if "tokenizer" not in str(error).lower():
            raise
        return False


def _preferred_versions(catalog_root: Path) -> dict[tuple[str, str], tuple[str, str | None]]:
    preferred = {}
    for category in ("mcp", "plugin", "skill", "general", "bundle"):
        index = catalog_root / "sources" / category / "index.json"
        if not index.exists():
            continue
        data = json.loads(index.read_text(encoding="utf-8"))
        for name, entry in data.get("packages", {}).items():
            preferred[(category, name)] = (entry["latest"], entry.get("id"))
    return preferred


def _canonical_timestamp(record: dict) -> str | None:
    value = record_time(record)
    return timestamp(value) if value else None


def build_and_publish(settings: Settings, *, now: datetime | None = None,
                      revision: str | None = None, include_undated: bool = False) -> dict:
    with writer_lock(settings.runtime_root):
        return _build_and_publish_locked(settings, now=now, revision=revision, include_undated=include_undated)


def _build_and_publish_locked(settings: Settings, *, now: datetime | None = None,
                              revision: str | None = None, include_undated: bool = False,
                              collection_batch: dict | None = None, expected_pointer_sha256: str | None = None) -> dict:
    """Internal publication path; caller holds the runtime writer lock throughout."""
    now = now or utc_now()
    generated_at = timestamp(now)
    cutoff = now - timedelta(seconds=settings.publication_delay_seconds)
    cutoff_at = timestamp(cutoff)
    revision = validate_revision(revision or "rev-" + uuid.uuid4().hex)
    if not (settings.catalog_root / "sources").is_dir():
        raise ValueError("catalog root must contain a sources directory")
    snapshots = settings.snapshots
    snapshots.mkdir(parents=True, exist_ok=True)
    if expected_pointer_sha256 is not None:
        from .bundles import _pointer_hash, _sha
        if expected_pointer_sha256 != "absent":
            _sha(expected_pointer_sha256)
        if _pointer_hash(snapshots) != expected_pointer_sha256:
            raise ServiceError("publication_conflict", "current pointer changed before scheduled publication", 409)
    destination = snapshots / revision
    if destination.exists():
        raise ServiceError("revision_exists", "immutable revision already exists", 409)
    staging_parent = snapshots / ".staging"
    staging_parent.mkdir(exist_ok=True)
    staging = staging_parent / revision
    staging.mkdir()
    state = None
    connection = None
    try:
        state = open_state(settings.state_path)
        bootstrap = state.execute("SELECT 1 FROM metadata WHERE key='bootstrap_completed'").fetchone() is None
        connection = sqlite3.connect(staging / "catalog.db")
        connection.row_factory = sqlite3.Row
        trigram = _create_schema(connection)
        preferred = _preferred_versions(settings.catalog_root)
        validator = build_validator("package.schema.json")
        input_digest = hashlib.sha256(encode_json([[*key, *value] for key, value in sorted(preferred.items())]).encode("utf-8"))
        canonical_ids = set()
        skipped = Counter()
        with state:
            for source_path, record in iter_records(settings.catalog_root):
                if not source_path.resolve().is_relative_to(settings.catalog_root):
                    raise ValueError("canonical path escapes catalog root")
                error = next(validator.iter_errors(record), None)
                if error:
                    raise ValueError(f"invalid canonical record {source_path}: {error.message}")
                encoded = encode_json(record)
                canonical_ids.add(record["id"])
                input_digest.update(encode_json([str(source_path.relative_to(settings.catalog_root)).replace('\\', '/'), encoded]).encode("utf-8"))
                canonical_time = _canonical_timestamp(record)
                if canonical_time is None and not include_undated:
                    skipped["undated"] += 1
                    continue
                if canonical_time and canonical_time > cutoff_at:
                    skipped["publication_cutoff"] += 1
                    continue
                identity = connection.execute("SELECT type,name FROM packages WHERE id=?", (record["id"],)).fetchone()
                if identity and (identity["type"], identity["name"]) != (record["type"], record["name"]):
                    raise ValueError("global canonical id collision")
                connection.execute("INSERT OR IGNORE INTO packages(id,type,name) VALUES (?,?,?)", (record["id"], record["type"], record["name"]))
                # INSERT OR IGNORE must not mask a source-local name collision.
                if connection.execute("SELECT 1 FROM packages WHERE id=?", (record["id"],)).fetchone() is None:
                    raise ValueError("source-local name has multiple canonical ids")
                connection.execute("INSERT INTO package_versions VALUES (?,?,?,?,?,?)", (
                    record["id"], record["version"], encoded, canonical_time,
                    source_path.relative_to(settings.catalog_root).as_posix(), hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                ))
            for package in connection.execute("SELECT id,type,name FROM packages ORDER BY id"):
                versions = connection.execute("SELECT * FROM package_versions WHERE package_id=? ORDER BY version", (package["id"],)).fetchall()
                declared = preferred.get((package["type"], package["name"]))
                if declared and declared[1] is not None and declared[1] != package["id"]:
                    raise ValueError("canonical index id does not match package id")
                chosen = next((item for item in versions if declared and item["version"] == declared[0]), None)
                selection = "canonical-index" if chosen else "unknown"
                if declared is None and len(versions) == 1:
                    chosen, selection = versions[0], "single-version"
                record = json.loads(chosen["canonical_json"]) if chosen else {}
                package_digest = hashlib.sha256(encode_json({"versions": [item["payload_hash"] for item in versions], "selected": chosen["version"] if chosen else None}).encode("utf-8")).hexdigest()
                remembered = remember_package(state, package["id"], package["type"], package["name"], package_digest, generated_at, bootstrap)
                targets = record.get("targets", [])
                # If latest selection is unknown, retain only explicit version-level Agent associations.
                agents = {target["agentId"] for target in targets} if chosen else {
                    target["agentId"] for item in versions for target in json.loads(item["canonical_json"])["targets"]
                }
                connection.executemany("INSERT INTO package_agents VALUES (?,?)", ((package["id"], agent) for agent in sorted(agents)))
                for custom, field in ((0, "facets"), (1, "customFacets")):
                    for kind, values in record.get(field, {}).items():
                        connection.executemany("INSERT INTO package_facets VALUES (?,?,?,?)", ((package["id"], kind, value, custom) for value in values))
                updated_at = chosen["canonical_time"] if chosen else max((item["canonical_time"] for item in versions if item["canonical_time"]), default=None)
                search_text = " ".join(encode_json(record.get(field, "")) for field in ("keywords", "facets", "customFacets"))
                connection.execute("""UPDATE packages SET display_name=?,summary=?,subtype=?,canonical_latest=?,selection_status=?,
                    canonical_updated_at=?,first_seen_at=?,bootstrap=?,keywords_json=?,facets_json=?,custom_facets_json=?,
                    targets_json=?,media_json=?,search_text=? WHERE id=?""", (
                    record.get("displayName"), record.get("description", ""), record.get("subtype"), chosen["version"] if chosen else None, selection,
                    updated_at, remembered["first_seen_at"], remembered["bootstrap"], encode_json(record.get("keywords", [])),
                    encode_json(record.get("facets", {})), encode_json(record.get("customFacets", {})), encode_json(targets),
                    encode_json(media_summary(record) if record else {}), search_text, package["id"],
                ))
            # Canonical import state may run ahead of publication; readers only see the copied event cutoff.
            remember_removals(state, canonical_ids, generated_at)
            state.execute("INSERT OR REPLACE INTO metadata VALUES ('bootstrap_completed',?)", (generated_at,))
            for event in state.execute("SELECT seq,unit_id,event_type,observed_at,evidence_json FROM change_events ORDER BY seq"):
                connection.execute("INSERT INTO change_events VALUES (?,?,?,?,?)", tuple(event))
            for category in ("mcp", "plugin", "skill", "general", "bundle"):
                source_id = "canonical:" + category
                connection.execute("INSERT INTO sources VALUES (?,?)", (source_id, encode_json({
                    "id": source_id, "kind": "canonical", "type": category,
                    "importedAt": generated_at, "upstreamLastSuccessAt": None,
                    "status": "canonical-only", "disclaimer": "canonical 导入时间不是上游采集时间",
                })))
            from .observations import project_observations
            observations = project_observations(state, connection, as_of=generated_at, policy=settings.star_window)
            from .download_projection import create_download_schema, project_downloads
            create_download_schema(connection)
            downloads = project_downloads(state, connection, as_of=generated_at, policy=settings.download_window)
            from .discovery_projection import project_discoveries
            discovery = project_discoveries(state, connection, as_of=generated_at, trigram=trigram)
            from .recommendation_projection import project_recommendations
            recommendations = project_recommendations(state, connection, as_of=generated_at)
            from .source_control_projection import project_controls
            source_controls = project_controls(state,connection,as_of=generated_at)
            from .release_projection import project_releases
            release_coverage=project_releases(state,connection,as_of=generated_at)
            release_sort_coverage = None
            if release_coverage is not None:
                from .release_sort import project as project_release_sort
                release_sort_coverage = project_release_sort(connection,generated_at)
            connection.execute("INSERT INTO packages_fts(packages_fts) VALUES ('rebuild')")
            if trigram:
                connection.execute("INSERT INTO packages_trigram(packages_trigram) VALUES ('rebuild')")
            counts = {key: connection.execute("SELECT count(*) FROM " + table).fetchone()[0] for key, table in (("packages", "packages"), ("versions", "package_versions"))}
            agents = [row[0] for row in connection.execute("SELECT DISTINCT agent FROM package_agents ORDER BY agent")]
            from .dashboard import store_dashboards
            store_dashboards(connection, agents)
            event_cutoff = connection.execute("SELECT coalesce(max(seq),0) FROM change_events").fetchone()[0]
            metadata = {"revision": revision, "generatedAt": generated_at, "canonicalCutoff": cutoff_at,
                        "inputDigest": input_digest.hexdigest(), "schemaVersion": QUERY_SCHEMA_VERSION, "storageContract": QUERY_CONTRACT,
                        "counts": counts, "agents": agents, "eventCutoff": event_cutoff,
                        "features": {"recommendations": True, "discovery": True, "dashboard": True, "unicode61": True, "trigram": trigram, "observations": observations["sharedRepositories"] > 0, "downloadObservations": downloads["sharedNpmPackages"] > 0},
                         "observationCoverage": observations, "downloadCoverage": downloads, "discoveryCoverage": discovery, "recommendationCoverage": recommendations,
                        "skipped": dict(skipped), "includeUndated": include_undated,
                        "sortPolicyVersion": "canonical-v1",
                         "availableSorts": ["canonical_updated", "first_seen", "name", "relevance", "stars", "star_growth", "repo_pushed", "downloads", "download_growth"],
                         "repositorySortPolicyVersion": "repository-signals-v1", "downloadSortPolicyVersion": "npm-download-signals-v1"}
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_review_events'").fetchone():
                from .candidate_review_projection import review_coverage
                metadata["features"]["discoveryReviews"] = True
                metadata["discoveryReviewCoverage"] = review_coverage(connection,generated_at)
            if source_controls is not None:
                metadata["features"]["sourceControls"] = True
                metadata["sourceControlCoverage"] = source_controls
            if release_coverage is not None:
                metadata["features"]["releases"]=True
                metadata["releaseCoverage"]=release_coverage
                from .release_sort import POLICY as RELEASE_SORT_POLICY
                metadata["features"]["releaseSort"] = True
                metadata["releaseSortCoverage"] = release_sort_coverage
                metadata["releaseSortPolicyVersion"] = RELEASE_SORT_POLICY
                metadata["availableSorts"].append("latest_release")
            if collection_batch is not None:
                from .scheduler import validate_collection_batch
                metadata["collectionBatch"] = validate_collection_batch(collection_batch, state, generated_at)
            connection.executemany("INSERT INTO metadata VALUES (?,?)", ((key, encode_json(value)) for key, value in metadata.items()))
            connection.execute("ANALYZE")
            connection.commit()
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("snapshot integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("snapshot foreign key check failed")
            connection.close()
            connection = None
        manifest = {**metadata, "database": {"path": "catalog.db", "sha256": digest_file(staging / "catalog.db"), "bytes": (staging / "catalog.db").stat().st_size}}
        atomic_json(staging / "manifest.json", manifest)
        from .bundles import _verify_snapshot
        _verify_snapshot(staging, revision, manifest["database"]["bytes"])
        os.rename(staging, destination)
        sync_directory(staging_parent)
        sync_directory(snapshots)
        # Validate through the same reader used by the API before making it current.
        SnapshotStore(snapshots).load(revision)
        if expected_pointer_sha256 is not None and _pointer_hash(snapshots) != expected_pointer_sha256:
            raise ServiceError("publication_conflict", "current pointer changed during scheduled publication", 409)
        from .retention import protect_before_switch
        protect_before_switch(settings, manifest)
        if expected_pointer_sha256 is not None and _pointer_hash(snapshots) != expected_pointer_sha256:
            raise ServiceError("publication_conflict", "current pointer changed during retirement protection", 409)
        atomic_json(snapshots / "current.json", {"revision": revision, "manifestSha256": digest_file(destination / "manifest.json"), "publishedAt": timestamp(utc_now())})
        return manifest
    finally:
        if connection is not None:
            connection.close()
        if state is not None:
            state.close()
        if staging.exists() and staging.resolve().is_relative_to(staging_parent.resolve()):
            cleanup_staging(staging)


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._verified: dict[str, dict] = {}

    def _safe_file(self, relative: str, max_bytes: int | None = None) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ServiceError("snapshot_unavailable", "snapshot file is unavailable", 503)
        if max_bytes is not None and path.stat().st_size > max_bytes:
            raise ServiceError("snapshot_unavailable", "snapshot metadata is too large", 503)
        return path

    def load(self, revision: str) -> dict:
        return self._load(revision)[0]

    def _load(self, revision: str) -> tuple[dict, Path]:
        validate_revision(revision)
        try:
            manifest_path = self._safe_file(revision + "/manifest.json", 1024 * 1024)
            database_path = self._safe_file(revision + "/catalog.db")
            if revision in self._verified:
                return self._verified[revision], database_path
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest["revision"] != revision or type(manifest["schemaVersion"]) is not int or manifest["schemaVersion"] != QUERY_SCHEMA_VERSION or manifest.get("storageContract") != QUERY_CONTRACT:
                raise ValueError("unsupported snapshot metadata")
            database = manifest["database"]
            if database["path"] != "catalog.db" or database["bytes"] != database_path.stat().st_size or database["sha256"] != digest_file(database_path):
                raise ValueError("snapshot database checksum mismatch")
            connection = read_snapshot(database_path)
            try:
                stored_revision = json.loads(connection.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0])
                stored_contract = connection.execute("SELECT value FROM metadata WHERE key='storageContract'").fetchone()
                if stored_contract is None or json.loads(stored_contract[0]) != QUERY_CONTRACT:
                    raise ValueError("unrecognized query storage contract")
                if stored_revision != revision or connection.execute("PRAGMA user_version").fetchone()[0] != manifest["schemaVersion"]:
                    raise ValueError("snapshot database revision mismatch")
            finally:
                connection.close()
            self._verified[revision] = manifest
            return manifest, database_path
        except ServiceError:
            raise
        except (OSError, ValueError, KeyError, TypeError, RecursionError, sqlite3.Error) as error:
            raise ServiceError("snapshot_unavailable", "snapshot validation failed", 503) from error

    def current(self) -> dict:
        try:
            pointer_path = self._safe_file("current.json", 4096)
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            revision = validate_revision(pointer["revision"])
            manifest_path = self._safe_file(revision + "/manifest.json", 1024 * 1024)
            # Pointer binding is checked even for an already verified database.
            if pointer["manifestSha256"] != digest_file(manifest_path):
                raise ValueError("current pointer checksum mismatch")
            return self.load(revision)
        except ServiceError as error:
            if error.code == "invalid_revision":
                raise ServiceError("snapshot_unavailable", "invalid published snapshot pointer", 503) from error
            raise
        except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
            raise ServiceError("snapshot_unavailable", "no valid published snapshot", 503) from error

    def database(self, revision: str) -> Path:
        return self._load(revision)[1]
