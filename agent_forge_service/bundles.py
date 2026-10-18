"""Public query-snapshot bundles; no state, credentials, archives or network writes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from pathlib import Path

from .errors import ServiceError
from .schema_contracts import QUERY_SCHEMA_VERSION, QUERY_CONTRACT
from .discovery_projection import COLUMNS as DISCOVERY_COLUMNS
from .recommendation_projection import COLUMNS as RECOMMENDATION_COLUMNS
from .candidate_review_projection import COLUMNS as REVIEW_COLUMNS
from .source_control_projection import COLUMNS as CONTROL_COLUMNS
from .release_projection import COLUMNS as RELEASE_COLUMNS
from .release_sort import COLUMNS as RELEASE_SORT_COLUMNS
from .publishing import (SnapshotStore, atomic_json, cleanup_staging, digest_file,
                         sync_directory, validate_revision)
from .storage import encode_json, read_snapshot, timestamp, utc_now, writer_lock

BUNDLE_VERSION = 1
MAX_METADATA_BYTES = 1024 * 1024
DEFAULT_MAX_DATABASE_BYTES = 1024**3
FILES = frozenset(("bundle.json", "manifest.json", "catalog.db"))
_SHA = re.compile(r"[0-9a-f]{64}\Z")
BASE_COLUMNS = {
    "metadata": {"key", "value"}, "packages": {"id", "type", "name", "display_name", "summary", "subtype", "canonical_latest", "selection_status", "canonical_updated_at", "first_seen_at", "bootstrap", "keywords_json", "facets_json", "custom_facets_json", "targets_json", "media_json", "search_text"},
    "package_versions": {"package_id", "version", "canonical_json", "canonical_time", "source_path", "payload_hash"},
    "package_agents": {"package_id", "agent"}, "package_facets": {"package_id", "kind", "value", "custom"},
    "change_events": {"seq", "unit_id", "event_type", "observed_at", "evidence_json"}, "sources": {"id", "data_json"},
    "packages_fts": {"name", "display_name", "summary", "search_text"},
}
REPOSITORY_COLUMNS = {
    "package_repository_links": {"package_id", "github_id", "evidence_json"},
    "repository_observations": {"github_id", "observation_id", "observed_at", "checked_at", "metadata_json", "evidence_hash"},
    "repository_metric_samples": {"id", "github_id", "observed_at", "value", "sample_json", "evidence_hash"},
    "repository_signals": {"github_id", "stars_current", "stars_rank_value", "growth_per_hour", "pushed_at", "checked_at", "star_observed_at", "freshness_json", "growth_json"},
    "package_sort_keys": {"package_id", "stars_value", "growth_value", "pushed_at"},
}
DOWNLOAD_COLUMNS = {
    "package_npm_links": {"package_id", "npm_package", "evidence_json"},
    "npm_download_observations": {"package", "observed_at", "checked_at", "evidence_hash", "body_json"},
    "npm_download_signals": {"package", "signal_json"},
    "package_download_sort_keys": {"package_id", "mapping_status", "npm_package", "downloads_value", "growth_value"},
}
PUBLIC_TABLES = set(RELEASE_SORT_COLUMNS) | set(RELEASE_COLUMNS) | set(CONTROL_COLUMNS) | set(REVIEW_COLUMNS) | set(RECOMMENDATION_COLUMNS) | set(DISCOVERY_COLUMNS) | {"discoveries_trigram"} | set(BASE_COLUMNS) | set(REPOSITORY_COLUMNS) | set(DOWNLOAD_COLUMNS) | {"sqlite_stat1", "sqlite_stat4", "packages_trigram", "dashboard_summaries"}
PUBLIC_TABLES |= {name + suffix for name in ("packages_fts", "packages_trigram", "discoveries_fts", "discoveries_trigram") for suffix in ("_data", "_idx", "_docsize", "_config")}



def _sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("expected checksum must be a full lowercase SHA-256")
    return value


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("bundle and snapshot files must be unlinked regular files")
    return info


def _directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("bundle and snapshot directories must not be links or reparse points")


def _json(path, maximum):
    _regular(path)
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("snapshot metadata exceeds the byte budget")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate metadata key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite metadata number")

    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except RecursionError as error:
        raise ValueError("metadata nesting exceeds the parser budget") from error
    if not isinstance(result, dict):
        raise ValueError("snapshot metadata must be an object")
    return result, hashlib.sha256(raw).hexdigest()


def _budget(value):
    if type(value) is not int or not 4096 <= value <= 32 * 1024**3:
        raise ValueError("database byte budget must be in [4096,32 GiB]")
    return value


def _verify_snapshot(directory, revision, max_bytes):
    _directory(directory)
    if {p.name for p in directory.iterdir()} != {"catalog.db", "manifest.json"}:
        raise ValueError("snapshot contains unexpected files, journals or private state")
    database = directory / "catalog.db"
    size = _regular(database).st_size
    if not 0 < size <= _budget(max_bytes):
        raise ValueError("snapshot database exceeds the byte budget")
    manifest, manifest_hash = _json(directory / "manifest.json", MAX_METADATA_BYTES)
    if manifest.get("revision") != revision or type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != QUERY_SCHEMA_VERSION or manifest.get("storageContract") != QUERY_CONTRACT:
        raise ValueError("unsupported or inconsistent query-snapshot revision/schema")
    declaration = manifest.get("database")
    if not isinstance(declaration, dict) or set(declaration) != {"path", "bytes", "sha256"} or declaration["path"] != "catalog.db" or type(declaration["bytes"]) is not int or declaration["bytes"] != size:
        raise ValueError("snapshot database descriptor is invalid")
    database_hash = digest_file(database)
    if _sha(declaration["sha256"]) != database_hash:
        raise ValueError("snapshot database checksum mismatch")
    connection = read_snapshot(database)
    deadline = time.monotonic() + 60
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        if connection.execute("PRAGMA user_version").fetchone()[0] != manifest["schemaVersion"]:
            raise ValueError("snapshot SQLite schema version differs from manifest")
        schema = connection.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','trigger','view')").fetchall()
        if any(row["type"] != "table" or row["name"] not in PUBLIC_TABLES for row in schema):
            raise ValueError("bundle database has private/unsupported tables or executable schema objects")
        required = dict(BASE_COLUMNS)
        required.update(REPOSITORY_COLUMNS)
        required["packages"] = required["packages"] | {"repository_id", "repository_mapping_status"}
        required.update(DOWNLOAD_COLUMNS)
        features = manifest.get("features")
        if not isinstance(features, dict) or features.get("unicode61") is not True or type(features.get("trigram")) is not bool:
            raise ValueError("snapshot search features are invalid")
        if "dashboard" in features and type(features["dashboard"]) is not bool:
            raise ValueError("snapshot dashboard feature is invalid")
        if features.get("dashboard"):
            required["dashboard_summaries"] = {"scope", "value"}
        elif any(row["name"] == "dashboard_summaries" for row in schema):
            raise ValueError("snapshot dashboard table is not declared")
        if "discovery" in features and type(features["discovery"]) is not bool:
            raise ValueError("snapshot discovery feature is invalid")
        if features.get("discovery"):
            required.update(DISCOVERY_COLUMNS)
            if features["trigram"]:
                required["discoveries_trigram"] = DISCOVERY_COLUMNS["discoveries_fts"]
        elif any(row["name"].startswith(("discoveries", "discovery_agents", "discovery_facets")) for row in schema) or "discoveryCoverage" in manifest:
            raise ValueError("snapshot discovery projection is not declared")
        if "recommendations" in features and type(features["recommendations"]) is not bool:
            raise ValueError("snapshot recommendations feature is invalid")
        if features.get("recommendations"):
            required.update(RECOMMENDATION_COLUMNS)
        elif any(row["name"].startswith("curated_") for row in schema) or "recommendationCoverage" in manifest:
            raise ValueError("snapshot recommendation projection is not declared")
        if "discoveryReviews" in features and type(features["discoveryReviews"]) is not bool:
            raise ValueError("snapshot review feature must be boolean")
        if features.get("discoveryReviews"):
            if not features.get("discovery"):
                raise ValueError("metadata reviews require discovery")
            required.update(REVIEW_COLUMNS)
        elif any(row["name"].startswith("discovery_review_") for row in schema) or "discoveryReviewCoverage" in manifest:
            raise ValueError("review projection is not declared")
        if "sourceControls" in features and type(features["sourceControls"]) is not bool:
            raise ValueError("source controls feature must be boolean")
        if features.get("sourceControls"):
            required.update(CONTROL_COLUMNS)
        elif any(row["name"] in CONTROL_COLUMNS for row in schema) or "sourceControlCoverage" in manifest:
            raise ValueError("source control projection is not declared")
        if "releases" in features and type(features["releases"]) is not bool:
            raise ValueError("Release feature must be boolean")
        if features.get("releases"):
            required.update(RELEASE_COLUMNS)
        elif any(row["name"] in RELEASE_COLUMNS for row in schema) or "releaseCoverage" in manifest:
            raise ValueError("Release projection is not declared")
        if "releaseSort" in features and type(features["releaseSort"]) is not bool:
            raise ValueError("release sort feature must be boolean")
        if features.get("releaseSort"):
            if not features.get("releases"):
                raise ValueError("release sorting requires public Release evidence")
            required.update(RELEASE_SORT_COLUMNS)
        elif any(row["name"] in RELEASE_SORT_COLUMNS for row in schema) or any(key in manifest for key in ("releaseSortCoverage", "releaseSortPolicyVersion")) or "latest_release" in manifest.get("availableSorts", []):
            raise ValueError("release sort projection is not declared")
        if features["trigram"]:
            required["packages_trigram"] = BASE_COLUMNS["packages_fts"]
        for name, columns in required.items():
            present = {row["name"] for row in connection.execute('PRAGMA table_info("' + name + '")')}
            if not columns <= present or (name in {"dashboard_summaries", *DISCOVERY_COLUMNS, *RECOMMENDATION_COLUMNS, *REVIEW_COLUMNS, *CONTROL_COLUMNS, *RELEASE_COLUMNS, *RELEASE_SORT_COLUMNS, "discoveries_trigram"} and present != columns):
                raise ValueError("snapshot table contract is incomplete: " + name)
        for row in schema:
            if row["name"] in ("packages_fts", "packages_trigram", "discoveries_fts", "discoveries_trigram") and "using fts5" not in (row["sql"] or "").lower():
                raise ValueError("snapshot search table is not FTS5")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("snapshot SQLite integrity check failed")
        stored = {row["key"]: json.loads(row["value"]) for row in connection.execute("SELECT key,value FROM metadata")}
        if encode_json(stored) != encode_json({key: value for key, value in manifest.items() if key != "database"}):
            raise ValueError("snapshot manifest differs from stored metadata")
        counts = manifest.get("counts")
        if not isinstance(counts, dict) or set(counts) != {"packages", "versions"} or any(type(value) is not int or value < 0 for value in counts.values()):
            raise ValueError("snapshot counts are invalid")
        if counts != {"packages": connection.execute("SELECT count(*) FROM packages").fetchone()[0],
                      "versions": connection.execute("SELECT count(*) FROM package_versions").fetchone()[0]}:
            raise ValueError("snapshot counts differ from database")
        if features.get("discovery"):
            from .discovery_projection import validate_projection
            validate_projection(connection, manifest)
        if features.get("discoveryReviews"):
            from .candidate_review_projection import validate_projection as validate_reviews
            validate_reviews(connection,manifest)
        if features.get("sourceControls"):
            from .source_control_projection import validate_projection as validate_controls
            validate_controls(connection,manifest)
        if features.get("releases"):
            from .release_projection import validate_projection as validate_releases
            validate_releases(connection,manifest)
        if features.get("releaseSort"):
            from .release_sort import validate_projection as validate_release_sort
            validate_release_sort(connection,manifest)
        if features.get("recommendations"):
            from .recommendation_projection import validate_projection as validate_recommendations
            validate_recommendations(connection, manifest)
        if features.get("dashboard"):
            from .dashboard import MAX_SUMMARY_BYTES, aggregate_dashboard, dashboard_key
            if connection.execute("SELECT coalesce(max(length(CAST(value AS BLOB))),0) FROM dashboard_summaries").fetchone()[0] > MAX_SUMMARY_BYTES:
                raise ValueError("dashboard summary exceeds its byte budget")
            agents = [row[0] for row in connection.execute("SELECT DISTINCT agent FROM package_agents ORDER BY agent")]
            if agents != manifest.get("agents"):
                raise ValueError("dashboard Agent scope differs from canonical associations")
            summaries = {row[0]: row[1] for row in connection.execute("SELECT scope,value FROM dashboard_summaries")}
            if set(summaries) != {dashboard_key(agent) for agent in (None, *agents)}:
                raise ValueError("dashboard scopes are missing or unexpected")
            for agent in (None, *agents):
                expected = aggregate_dashboard(connection, agent, deadline=deadline)
                if summaries[dashboard_key(agent)] != encode_json(expected):
                    raise ValueError("dashboard summary differs from canonical snapshot")
    except sqlite3.Error as error:
        raise ServiceError("snapshot_validation", "snapshot SQLite validation failed", 503) from error
    finally:
        connection.close()
    return manifest, manifest_hash, database_hash


def _copy(source, target, expected_hash, expected_bytes):
    info = _regular(source)
    if info.st_size != expected_bytes:
        raise ValueError("bundle file length changed before copy")
    checksum, written = hashlib.sha256(), 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    with os.fdopen(descriptor, "rb") as origin, target.open("xb") as destination:
        actual = os.fstat(origin.fileno())
        if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino, actual.st_size) != (info.st_dev, info.st_ino, info.st_size):
            raise ValueError("bundle source changed during open")
        for block in iter(lambda: origin.read(1024 * 1024), b""):
            written += len(block)
            if written > expected_bytes:
                raise ValueError("bundle source grew during copy")
            checksum.update(block)
            destination.write(block)
        destination.flush()
        os.fsync(destination.fileno())
    if written != expected_bytes or checksum.hexdigest() != expected_hash:
        raise ValueError("bundle source changed or checksum mismatched during copy")


def _paths(settings):
    root = settings.snapshots
    root.mkdir(parents=True, exist_ok=True)
    _directory(root)
    if root.resolve() != root:
        raise ValueError("snapshot root changed to a link")
    staging = root / ".staging"
    staging.mkdir(exist_ok=True)
    _directory(staging)
    return root, staging


def _cleanup(path, parent):
    # Only a directory created by this invocation, still resolved under its staging parent.
    if path.exists():
        if path.parent != parent or path.resolve() != path or not path.is_relative_to(parent.resolve()):
            raise ValueError("refusing cleanup of a changed staging path")
        cleanup_staging(path)


def export_bundle(settings, output: Path, *, revision=None, max_database_bytes=DEFAULT_MAX_DATABASE_BYTES):
    output = output.absolute()
    if output != output.resolve() or output.exists() or output.is_symlink():
        raise ValueError("bundle output must be a new unlinked directory")
    for protected in (settings.snapshots, settings.catalog_root / "sources"):
        if output.is_relative_to(protected) or protected.is_relative_to(output):
            raise ValueError("bundle output must not overlap snapshots or canonical sources")
    _budget(max_database_bytes)
    with writer_lock(settings.runtime_root):
        store = SnapshotStore(settings.snapshots)
        revision = validate_revision(revision) if revision is not None else store.current()["revision"]
        source = settings.snapshots / revision
        manifest, manifest_hash, database_hash = _verify_snapshot(source, revision, max_database_bytes)
        output.parent.mkdir(parents=True, exist_ok=True)
        _directory(output.parent)
        staging = output.parent / (".bundle-staging-" + uuid.uuid4().hex)
        staging.mkdir()
        try:
            manifest_size = (source / "manifest.json").stat().st_size
            _copy(source / "manifest.json", staging / "manifest.json", manifest_hash, manifest_size)
            _copy(source / "catalog.db", staging / "catalog.db", database_hash, manifest["database"]["bytes"])
            descriptor = {"schemaVersion": BUNDLE_VERSION, "kind": "agent-forge-query-snapshot", "revision": revision,
                          "manifest": {"path": "manifest.json", "sha256": manifest_hash, "bytes": manifest_size},
                          "database": manifest["database"]}
            atomic_json(staging / "bundle.json", descriptor)
            bundle_hash = digest_file(staging / "bundle.json")
            # No state.db, local credentials, jobs, cache or collection logs are part of this directory.
            os.rename(staging, output)
            sync_directory(output.parent)
            return {"revision": revision, "bundlePath": str(output), "bundleSha256": bundle_hash,
                    "manifestSha256": manifest_hash, "databaseSha256": database_hash, "databaseBytes": manifest["database"]["bytes"]}
        finally:
            _cleanup(staging, output.parent)


def _descriptor(source, expected_hash, max_bytes):
    _directory(source)
    if {p.name for p in source.iterdir()} != FILES:
        raise ValueError("bundle must contain exactly bundle.json, manifest.json and catalog.db")
    descriptor, actual_hash = _json(source / "bundle.json", 8192)
    if actual_hash != _sha(expected_hash):
        raise ValueError("bundle checksum differs from trusted expected checksum")
    if set(descriptor) != {"schemaVersion", "kind", "revision", "manifest", "database"} or type(descriptor["schemaVersion"]) is not int or descriptor["schemaVersion"] != BUNDLE_VERSION or descriptor["kind"] != "agent-forge-query-snapshot":
        raise ValueError("unsupported bundle representation")
    validate_revision(descriptor["revision"])
    for key, name, maximum in (("manifest", "manifest.json", MAX_METADATA_BYTES), ("database", "catalog.db", _budget(max_bytes))):
        item = descriptor[key]
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"} or item["path"] != name or type(item["bytes"]) is not int or not 0 < item["bytes"] <= maximum:
            raise ValueError("invalid bundle file descriptor")
        _sha(item["sha256"])
        if _regular(source / name).st_size != item["bytes"]:
            raise ValueError("bundle file length differs from descriptor")
    return descriptor


def import_bundle(settings, source: Path, *, expected_bundle_sha256, max_database_bytes=DEFAULT_MAX_DATABASE_BYTES):
    source = source.absolute()
    if source != source.resolve():
        raise ValueError("bundle source directory must not be a link")
    descriptor = _descriptor(source, expected_bundle_sha256, max_database_bytes)
    revision = descriptor["revision"]
    with writer_lock(settings.runtime_root):
        root, staging_parent = _paths(settings)
        if source.is_relative_to(root) or root.is_relative_to(source):
            raise ValueError("bundle source must not overlap the receiving snapshot store")
        destination = root / revision
        staging = staging_parent / ("import-" + uuid.uuid4().hex)
        staging.mkdir()
        try:
            for key, name in (("manifest", "manifest.json"), ("database", "catalog.db")):
                item = descriptor[key]
                _copy(source / name, staging / name, item["sha256"], item["bytes"])
            manifest, manifest_hash, database_hash = _verify_snapshot(staging, revision, max_database_bytes)
            if manifest_hash != descriptor["manifest"]["sha256"] or manifest["database"] != descriptor["database"]:
                raise ValueError("bundle manifest and descriptor disagree")
            installed = not (destination.exists() or destination.is_symlink())
            if not installed:
                _, old_manifest_hash, old_database_hash = _verify_snapshot(destination, revision, max_database_bytes)
                if (old_manifest_hash, old_database_hash) != (manifest_hash, database_hash):
                    raise ServiceError("revision_conflict", "same revision has different immutable snapshot content", 409)
            else:
                os.rename(staging, destination)
                sync_directory(staging_parent)
                sync_directory(root)
            from .retention import register_snapshot
            register_snapshot(settings, manifest)
            return {"revision": revision, "installed": installed, "activated": False,
                    "manifestSha256": manifest_hash, "databaseSha256": database_hash, "databaseBytes": manifest["database"]["bytes"]}
        finally:
            _cleanup(staging, staging_parent)


def _pointer_hash(root):
    path = root / "current.json"
    if not path.exists() and not path.is_symlink():
        return "absent"
    _regular(path)
    if path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError("current pointer exceeds the recovery checksum byte budget")
    return digest_file(path)


def activate_snapshot(settings, revision, *, expected_manifest_sha256, expected_pointer_sha256,
                      max_database_bytes=DEFAULT_MAX_DATABASE_BYTES):
    revision = validate_revision(revision)
    _sha(expected_manifest_sha256)
    if expected_pointer_sha256 != "absent":
        _sha(expected_pointer_sha256)
    with writer_lock(settings.runtime_root):
        root, _ = _paths(settings)
        if _pointer_hash(root) != expected_pointer_sha256:
            raise ServiceError("publication_conflict", "current snapshot pointer changed; inspect it before switching", 409)
        manifest, manifest_hash, _ = _verify_snapshot(root / revision, revision, max_database_bytes)
        if manifest_hash != expected_manifest_sha256:
            raise ServiceError("snapshot_checksum", "target manifest differs from expected immutable content", 409)
        old_revision = None
        healthy = False
        try:
            current = SnapshotStore(root).current()
            old_revision, healthy = current["revision"], True
        except ServiceError:
            pass
        changed = not healthy or old_revision != revision
        # Validation can take time; a non-cooperating writer must not bypass the operator's guard.
        if _pointer_hash(root) != expected_pointer_sha256:
            raise ServiceError("publication_conflict", "current snapshot pointer changed during validation", 409)
        if digest_file(root / revision / "manifest.json") != manifest_hash or digest_file(root / revision / "catalog.db") != manifest["database"]["sha256"]:
            raise ServiceError("snapshot_checksum", "target snapshot changed during activation validation", 409)
        if changed:
            from .retention import protect_snapshot
            protect_snapshot(settings, manifest, previous_revision=old_revision)
            if _pointer_hash(root) != expected_pointer_sha256:
                raise ServiceError("publication_conflict", "current pointer changed during retirement protection", 409)
            atomic_json(root / "current.json", {"revision": revision, "manifestSha256": manifest_hash,
                                               "publishedAt": timestamp(utc_now())})
        return {"revision": revision, "previousRevision": old_revision, "previousPointerHealthy": healthy,
                "changed": changed, "pointerSha256": _pointer_hash(root), "manifestSha256": manifest_hash,
                "generatedAt": manifest["generatedAt"], "preservedOtherRevisions": True}


def snapshot_status(settings, limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("snapshot status limit must be in [1,100]")
    root = settings.snapshots
    if not root.exists():
        return {"pointerSha256": "absent", "currentHealthy": False, "currentRevision": None, "revisions": []}
    _directory(root)
    pointer = _pointer_hash(root)
    error, revision = None, None
    try:
        revision = SnapshotStore(root).current()["revision"]
    except ServiceError as caught:
        error = caught.code
    entries = sorted((p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", p.name)), reverse=True)
    return {"pointerSha256": pointer, "currentHealthy": revision is not None, "currentRevision": revision,
            "currentError": error, "revisions": entries[:limit], "revisionCount": len(entries),
            "revisionListingSemantics": "directory_presence_not_individual_integrity_validation"}
