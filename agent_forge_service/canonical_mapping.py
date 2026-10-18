"""Explicit canonical URL assertions, separate from repository identity and ranking."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

from tools.project import iter_records
from tools.validate import build_validator

from .github import GitHubRequest
from .jobs import JobQueue
from .storage import encode_json, timestamp


def parse_repository_url(url: str, *, ref: str | None = None, context: bool = False) -> dict | None:
    """Never infer a branch/path boundary or a package directory from its name."""
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url:
        raise ValueError("invalid_url")
    parsed = urlsplit(url.removeprefix("git+"))
    if parsed.hostname != "github.com":
        return None
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        raise ValueError("unsupported_github_url")
    parts = parsed.path.rstrip("/").split("/")[1:]
    if len(parts) < 2 or any("%" in x for x in parts[:2]):
        raise ValueError("invalid_repository_path")
    repository = "/".join(parts[:2]).removesuffix(".git").lower()
    GitHubRequest.repository(repository).validate()
    if ref is not None and (
        not isinstance(ref, str) or not ref or len(ref) > 1024 or "\\" in ref
        or any(ord(c) < 33 or ord(c) == 127 for c in ref)
        or any(x in ("", ".", "..") for x in ref.split("/"))
    ):
        raise ValueError("invalid_ref")
    result = {"repository": repository, "ref": ref, "subpath": None if context else "",
              "pathStatus": "context" if context else "root"}
    if len(parts) == 2:
        return result
    if len(parts) < 4 or parts[2] != "tree":
        raise ValueError("unsupported_repository_path")
    tail = unquote("/".join(parts[3:]), errors="strict")
    if any(x in ("", ".", "..") for x in tail.split("/")) or "\\" in tail or any(ord(c) < 32 or ord(c) == 127 for c in tail):
        raise ValueError("unsafe_subpath")
    if ref is None:
        first = tail.split("/", 1)[0]
        if first == "HEAD" or re.fullmatch(r"[0-9a-fA-F]{40}", first):
            ref = first
        else:
            return {**result, "ref": None, "subpath": None, "pathStatus": "ambiguous"}
    if tail != ref and not tail.startswith(ref + "/"):
        raise ValueError("ref_mismatch")
    result.update(ref=ref, subpath=tail[len(ref):].lstrip("/"), pathStatus="explicit")
    return result


def _assertions(record):
    values = []
    if record.get("links", {}).get("repository"):
        values.append(("/links/repository", record["links"]["repository"], None, True))
    for i, distribution in enumerate(record.get("distributions", [])):
        if distribution["type"] in ("github-repo", "git"):
            values.append((f"/distributions/{i}/url", distribution["url"], distribution.get("ref"), False))
    assertions, diagnostics = [], []
    for field, url, ref, context in values:
        try:
            value = parse_repository_url(url, ref=ref, context=context)
            if value:
                assertions.append((field, url, value))
                if value["pathStatus"] == "ambiguous":
                    diagnostics.append({"field": field, "reason": "ambiguous_ref_boundary"})
        except (ValueError, UnicodeError):
            # Do not persist rejected credential-bearing URLs in diagnostics.
            diagnostics.append({"field": field, "reason": "invalid_repository_url"})
    return assertions, diagnostics


def _apply_seed_provenance(connection, repo_id):
    row = connection.execute("SELECT min(first_seen_at) FROM canonical_repository_links WHERE active=1 AND resolution='resolved' AND github_id=?", (repo_id,)).fetchone()
    if row[0] is None:
        return
    for table in ("repositories", "discovery_candidates"):
        connection.execute(f"UPDATE {table} SET bootstrap=1,first_seen_at=min(first_seen_at,?) WHERE github_id=?", (row[0], repo_id))


def _resolve_alias(connection, alias, now):
    identities = [r[0] for r in connection.execute("SELECT github_id FROM repository_aliases WHERE alias=?", (alias,))]
    if not identities:
        return
    links = connection.execute("SELECT id,github_id,resolution FROM canonical_repository_links WHERE active=1 AND repository=?", (alias,)).fetchall()
    for link in links:
        conflict = len(identities) != 1 or (link["github_id"] is not None and link["github_id"] != identities[0])
        if conflict:
            connection.execute("UPDATE canonical_repository_links SET resolution='conflict',conflict_reason='repository_name_reused',resolved_at=? WHERE id=?", (timestamp(now), link["id"]))
        elif link["resolution"] != "conflict":
            connection.execute("UPDATE canonical_repository_links SET github_id=?,resolution='resolved',resolved_at=?,conflict_reason=NULL WHERE id=?", (identities[0], timestamp(now), link["id"]))
    for repo_id in identities:
        _apply_seed_provenance(connection, repo_id)


def observe_repository_aliases(connection, *, github_id: int, full_name: str,
                               requested: str, observed_at: str, checked_at: str, observation_id: str):
    aliases = {full_name.lower()}
    if requested.startswith("repos/"):
        aliases.add(requested.removeprefix("repos/").lower())
    for alias in aliases:
        GitHubRequest.repository(alias).validate()
        connection.execute(
            "INSERT INTO repository_aliases VALUES (?,?,?,?,?) ON CONFLICT(alias,github_id) DO UPDATE SET "
            "last_checked_at=max(last_checked_at,excluded.last_checked_at),"
            "observation_id=CASE WHEN excluded.last_checked_at>=last_checked_at THEN excluded.observation_id ELSE observation_id END",
            (alias, github_id, timestamp(observed_at), timestamp(checked_at), observation_id))
        _resolve_alias(connection, alias, checked_at)


def seed_provenance(connection, *, github_id: int, full_name: str) -> str | None:
    row = connection.execute(
        "SELECT min(first_seen_at) FROM canonical_repository_links WHERE active=1 AND "
        "((github_id=? AND resolution='resolved') OR (repository=? AND resolution='pending' AND github_id IS NULL))",
        (github_id, full_name.lower())).fetchone()
    return row[0]


def sync_canonical_mappings(connection, catalog_root: Path, *, now) -> dict:
    """Full validated scan: mapping changes/removals commit together, never canonical."""
    if connection.in_transaction:
        raise ValueError("mapping sync requires its own transaction")
    catalog_root = catalog_root.resolve()
    if not (catalog_root / "sources").is_dir():
        raise ValueError("catalog root must contain canonical sources")
    now = timestamp(now)
    validator = build_validator("package.schema.json")
    seen_versions, identities, names = set(), {}, {}
    digest, counts = hashlib.sha256(), Counter()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE canonical_mapping_records SET active=0")
        connection.execute("DELETE FROM canonical_mapping_targets")
        connection.execute("UPDATE canonical_repository_links SET active=0")
        for path, record in iter_records(catalog_root):
            if not path.resolve().is_relative_to(catalog_root):
                raise ValueError("canonical record escapes catalog root")
            error = next(validator.iter_errors(record), None)
            if error:
                raise ValueError(f"invalid canonical record {path}: {error.message}")
            package_id, version = record["id"], record["version"]
            identity = (record["type"], record["name"])
            if (package_id, version) in seen_versions:
                raise ValueError("duplicate canonical package version")
            if package_id in identities and identities[package_id] != identity:
                raise ValueError("canonical package id collision")
            if identity in names and names[identity] != package_id:
                raise ValueError("canonical package name collision")
            identities[package_id], names[identity] = identity, package_id
            seen_versions.add((package_id, version))
            encoded = encode_json(record)
            canonical_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            relative = path.relative_to(catalog_root).as_posix()
            digest.update(encode_json([relative, canonical_hash]).encode("utf-8"))
            assertions, diagnostics = _assertions(record)
            known = connection.execute("SELECT type,first_seen_at FROM package_units WHERE id=?", (package_id,)).fetchone()
            history = connection.execute("SELECT package_type,first_seen_at FROM canonical_mapping_records WHERE package_id=?", (package_id,)).fetchall()
            if (known and known["type"] != record["type"]) or any(r["package_type"] != record["type"] for r in history):
                raise ValueError("historical canonical package id collision")
            first_seen = min([now] + ([known["first_seen_at"]] if known else []) + [r["first_seen_at"] for r in history])
            connection.execute(
                "INSERT INTO canonical_mapping_records VALUES (?,?,?,?,?,?,?,?,1,?) "
                "ON CONFLICT(package_id,version) DO UPDATE SET package_type=excluded.package_type,"
                "package_name=excluded.package_name,record_path=excluded.record_path,canonical_hash=excluded.canonical_hash,"
                "first_seen_at=min(first_seen_at,excluded.first_seen_at),last_seen_at=excluded.last_seen_at,active=1,diagnostics_json=excluded.diagnostics_json",
                (package_id, version, *identity, relative, canonical_hash, first_seen, now, encode_json(diagnostics)))
            for target in record.get("targets", []):
                connection.execute("INSERT OR IGNORE INTO canonical_mapping_targets VALUES (?,?,?)", (package_id, version, target["agentId"]))
            record_first = connection.execute("SELECT first_seen_at FROM canonical_mapping_records WHERE package_id=? AND version=?", (package_id, version)).fetchone()[0]
            for field, url, value in assertions:
                key = hashlib.sha256(encode_json([package_id, version, relative, field, url, value]).encode("utf-8")).hexdigest()
                evidence = {"recordPath": relative, "canonicalHash": canonical_hash, "field": field, "url": url}
                connection.execute(
                    "INSERT INTO canonical_repository_links(id,package_id,version,repository,ref,subpath,path_status,"
                    "first_seen_at,last_seen_at,active,resolution,evidence_json) VALUES (?,?,?,?,?,?,?,?,?,1,'pending',?) "
                    "ON CONFLICT(id) DO UPDATE SET first_seen_at=min(first_seen_at,excluded.first_seen_at),last_seen_at=excluded.last_seen_at,active=1,evidence_json=excluded.evidence_json",
                    (key, package_id, version, value["repository"], value["ref"], value["subpath"], value["pathStatus"], record_first, now, encode_json(evidence)))
            counts["records"] += 1
            counts["links"] += len(assertions)
            counts["recordsWithoutGitHub"] += int(not assertions)
            counts["diagnostics"] += len(diagnostics)
        if not counts["records"]:
            raise ValueError("refuse empty canonical mapping import")
        # Resolve once per alias, not once per package/version in a monorepo.
        for row in connection.execute("SELECT DISTINCT repository FROM canonical_repository_links WHERE active=1").fetchall():
            _resolve_alias(connection, row[0], now)
        report = {**counts, "packages": len(identities), "inputHash": digest.hexdigest(), "importedAt": now}
        connection.execute("INSERT INTO metadata VALUES ('canonical_targets_sync',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))
        connection.execute("INSERT INTO metadata VALUES ('canonical_mapping_sync',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (encode_json(report),))
    return {**report, **mapping_status(connection)}


def enqueue_mapping_repositories(connection, *, slot, limit: int, now) -> dict:
    """Bounded bootstrap checks share the durable repository queue and its budgets."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("mapping enqueue limit must be in [1,1000]")
    timestamp(slot)
    if connection.in_transaction:
        raise ValueError("mapping enqueue requires its own transaction")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        queued = enqueue_pending_mapping_jobs(connection, slot=slot, limit=limit, now=now)
    return {"jobIds": queued, "selected": len(queued), **mapping_status(connection)}


def enqueue_pending_mapping_jobs(connection, *, slot, limit, now):
    if not connection.in_transaction or type(limit) is not int or not 0 <= limit <= 1000:
        raise ValueError("mapping job creation requires a transaction and bounded limit")
    from .collector import enqueue_repository
    aliases = connection.execute(
        "SELECT l.repository FROM canonical_repository_links l LEFT JOIN jobs j "
        "ON j.kind='github_repository' AND j.scope='repos/'||l.repository "
        "WHERE l.active=1 AND l.resolution='pending' GROUP BY l.repository "
        "HAVING sum(CASE WHEN j.state IN ('pending','leased','retry_wait') THEN 1 ELSE 0 END)=0 "
        "ORDER BY coalesce(max(j.created_at),''),l.repository LIMIT ?", (limit,)).fetchall()
    return [enqueue_repository(JobQueue(connection), repository=row[0], slot=slot, bootstrap=True, now=now) for row in aliases]


def mapping_status(connection) -> dict:
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='canonical_mapping_records'").fetchone():
        return {"mappingSchema": "upgrade_required", "mappingRecords": None,
                "mappedRepositories": None, "mappingLinks": None}
    return {"mappingSchema": "ready","mappingRecords": connection.execute("SELECT count(*) FROM canonical_mapping_records WHERE active=1").fetchone()[0],
            "mappedRepositories": connection.execute("SELECT count(DISTINCT github_id) FROM canonical_repository_links WHERE active=1 AND resolution='resolved'").fetchone()[0],
            "mappingLinks": {r[0]: r[1] for r in connection.execute("SELECT resolution,count(*) FROM canonical_repository_links WHERE active=1 GROUP BY resolution")}}
