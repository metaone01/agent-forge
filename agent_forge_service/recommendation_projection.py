"""Cutoff-bound public curated facts; no articles, credentials or inferred endorsements."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from urllib.parse import quote

from .public_git import sanitize_object
from .recommendations import (POLICY as COLLECTION_POLICY, REPRESENTATION, RecommendationGitRequest,
                              _target, parse_curated_list, source_identity, validate_source)
from .storage import encode_json, timestamp

POLICY = "public-curated-projection-v1"
STALE_SECONDS = 86400
MAX_JSON_BYTES = 256 * 1024
COLUMNS = {
    "curated_sources": {"id", "data_json"},
    "curated_snapshots": {"id", "source_id", "selection_at", "completed_at", "data_json"},
    "curated_members": {"snapshot_id", "target_url", "data_json"},
    "curated_claims": {"source_id", "target_url", "status", "snapshot_id", "previous_id", "data_json"},
    "curated_package_links": {"package_id", "version", "source_id", "target_url", "data_json"},
}


def create_schema(connection):
    connection.executescript("""
        CREATE TABLE curated_sources (id TEXT PRIMARY KEY,data_json TEXT NOT NULL);
        CREATE TABLE curated_snapshots (
            id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES curated_sources(id),
            selection_at TEXT NOT NULL,completed_at TEXT NOT NULL,data_json TEXT NOT NULL
        );
        CREATE INDEX curated_snapshot_history ON curated_snapshots(source_id,selection_at,completed_at,id);
        CREATE TABLE curated_members (
            snapshot_id TEXT NOT NULL REFERENCES curated_snapshots(id),target_url TEXT NOT NULL,data_json TEXT NOT NULL,
            PRIMARY KEY(snapshot_id,target_url)
        );
        CREATE TABLE curated_claims (
            source_id TEXT NOT NULL REFERENCES curated_sources(id),target_url TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','withdrawn')),
            snapshot_id TEXT NOT NULL REFERENCES curated_snapshots(id),previous_id TEXT REFERENCES curated_snapshots(id),
            data_json TEXT NOT NULL,PRIMARY KEY(source_id,target_url)
        );
        CREATE INDEX curated_claim_order ON curated_claims(source_id,target_url);
        CREATE TABLE curated_package_links (
            package_id TEXT NOT NULL,version TEXT NOT NULL,source_id TEXT NOT NULL,target_url TEXT NOT NULL,data_json TEXT NOT NULL,
            PRIMARY KEY(package_id,version,source_id,target_url),
            FOREIGN KEY(package_id,version) REFERENCES package_versions(package_id,version),
            FOREIGN KEY(source_id,target_url) REFERENCES curated_claims(source_id,target_url)
        );
        CREATE INDEX curated_package_order ON curated_package_links(package_id,version,source_id,target_url);
    """)


def _hash(value):
    return hashlib.sha256(encode_json(value).encode("utf-8")).hexdigest()


def public_source_identity(definition):
    definition = validate_source(definition)
    # Labels and parser budgets are configuration aliases, not independent recommending lists.
    identity = {key: definition[key] for key in ("provider", "repositoryId", "path", "selectionStatement", "startHeading", "endHeading", "emptyStatement")}
    return "curated-source:" + _hash(identity)


def _url(name, commit, path):
    return "https://github.com/" + name + "/blob/" + commit + "/" + quote(path, safe="/")


def _date(value, as_of):
    if not isinstance(value, str) or timestamp(value) != value or value > as_of:
        raise ValueError("curated evidence timestamp is invalid or beyond cutoff")
    return value


def _sha(value, length=64):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{" + str(length) + "}", value):
        raise ValueError("invalid curated evidence hash")
    return value


def _object(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("public curated metadata exceeds its byte budget")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate public curated JSON key")
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite public metadata")))
    if not isinstance(value, dict) or encode_json(value) != text:
        raise ValueError("public curated JSON is not a canonical object")
    return value


def _private_snapshot(state, row, definition, as_of):
    validate_source(definition)
    if row["source_id"] != source_identity(definition):
        raise ValueError("private curated source identity differs from its definition")
    fetches = state.execute(
        "SELECT f.*,e.representation,e.body_json,t.step,j.state AS job_state FROM recommendation_fetches f "
        "JOIN recommendation_tasks t ON t.job_id=f.job_id JOIN collection_evidence e ON e.hash=f.evidence_hash "
        "JOIN jobs j ON j.id=f.job_id WHERE t.scan_id=? ORDER BY t.step", (row["scan_id"],)).fetchall()
    stage, remaining, steps, raw = "repository", definition["path"].split("/"), [], None
    name = branch = commit = tree = selected = None
    parsed = None
    for index, fetch in enumerate(fetches):
        if fetch["step"] != index or fetch["job_state"] != "succeeded" or fetch["representation"] != REPRESENTATION:
            raise ValueError("curated evidence chain is incomplete")
        observed, checked = _date(fetch["observed_at"], as_of), _date(fetch["checked_at"], as_of)
        if observed > checked or checked > row["completed_at"] or steps and checked < steps[-1]["checkedAt"]:
            raise ValueError("curated observation clocks are inconsistent")
        request = RecommendationGitRequest("repository", definition["repositoryId"]) if stage in ("repository", "verify") else RecommendationGitRequest("commit", name, branch) if stage == "commit" else RecommendationGitRequest(stage, name, tree if stage == "tree" else selected["sha"])
        body = json.loads(fetch["body_json"])
        if sanitize_object(body, request, max_blob_bytes=2 * 1024 * 1024) != body:
            raise ValueError("noncanonical private curated Git evidence")
        response_hash = _hash(body)
        if fetch["endpoint"] != request.endpoint or fetch["evidence_hash"] != _hash([REPRESENTATION, response_hash]) or fetch["response_status"] not in (200, 304):
            raise ValueError("curated fetch identity/hash differs")
        # Public proof omits the blob body, tree entries and repository fields unrelated to this list.
        step = {"kind": stage, "url": "https://api.github.com/" + request.endpoint,
                "responseHash": response_hash, "status": fetch["response_status"], "observedAt": observed, "checkedAt": checked}
        steps.append(step)
        if stage == "repository":
            name, branch, stage = body["full_name"], body["default_branch"], "commit"
        elif stage == "commit":
            commit, tree, stage = body["sha"], body["commit"]["tree"]["sha"], "tree"
            selection_at = checked
        elif stage == "tree":
            selected = next((entry for entry in body["tree"] if entry["path"] == remaining[0]), None)
            if selected is None:
                raise ValueError("curated path lacks complete Git evidence")
            if len(remaining) > 1:
                if selected["type"] != "tree" or selected["mode"] != "040000":
                    raise ValueError("curated path traverses a non-directory")
                tree, remaining = selected["sha"], remaining[1:]
            else:
                if selected["type"] != "blob" or selected["mode"] not in ("100644", "100755") or not 0 < selected.get("size", 0) <= 2 * 1024 * 1024:
                    raise ValueError("curated file is not a bounded regular blob")
                stage = "blob"
        elif stage == "blob":
            raw = base64.b64decode(body["content"], validate=True)
            if len(raw) != selected["size"]:
                raise ValueError("curated tree/blob length differs")
            parsed, stage = parse_curated_list(raw, definition), "verify"
            blob_observed, blob_checked = observed, checked
        elif stage == "verify":
            if body["full_name"].lower() != name.lower():
                raise ValueError("curated repository alias changed during read")
            stage = "complete"
        else:
            raise ValueError("unexpected step after curated evidence completion")
    if stage != "complete" or parsed is None:
        raise ValueError("curated snapshot has no complete Git chain")
    expected = {"repository_name": name, "commit_sha": commit, "blob_sha": selected["sha"],
                "content_hash": hashlib.sha256(raw).hexdigest(), "selection_at": selection_at,
                "observed_at": blob_observed, "checked_at": blob_checked,
                "member_count": len(parsed["members"]), "entry_count": parsed["entryCount"]}
    if any(row[key] != value for key, value in expected.items()):
        raise ValueError("curated snapshot differs from complete Git evidence")
    stored = state.execute("SELECT * FROM recommendation_members WHERE snapshot_id=? ORDER BY target_url", (row["id"],)).fetchall()
    if len(stored) != len(parsed["members"]):
        raise ValueError("private curated member count differs")
    for member, item in zip(stored, parsed["members"]):
        if member["target_url"] != item["targetUrl"] or member["name"] != item["name"] or member["positions_json"] != encode_json(item["positions"]) or member["source_rank"] is not None:
            raise ValueError("private curated members differ from parsed evidence")
    source_id = public_source_identity(definition)
    identity = _hash([POLICY, source_id, commit, selected["sha"], selection_at, row["completed_at"], _hash(definition)])
    public = {"id": identity, "sourceId": source_id, "repositoryName": name, "commitSha": commit,
              "blobSha": selected["sha"], "contentSha256": expected["content_hash"], "contentBytes": len(raw),
              "selectionAt": selection_at, "observedAt": blob_observed, "checkedAt": blob_checked,
              "completedAt": row["completed_at"], "entryCount": parsed["entryCount"], "memberCount": len(stored),
              "scope": parsed["scope"], "definition": definition, "steps": steps,
              "url": _url(name, commit, definition["path"]), "complete": True, "claimKind": "curated_listing", "sourceRank": None}
    return public, parsed["members"]


def _claims(snapshots, members):
    # Replay completion order, ignoring an old pointer check which finishes after a newer complete scan.
    current, claims = {}, {}
    for snapshot in sorted(snapshots.values(), key=lambda value: (value["completedAt"], value["selectionAt"], value["id"])):
        source = snapshot["sourceId"]
        old = current.get(source)
        if old and snapshot["selectionAt"] == old["selectionAt"] and (snapshot["commitSha"], snapshot["contentSha256"]) != (old["commitSha"], old["contentSha256"]):
            raise ValueError("conflicting public curated pointer observations at the same time")
        if old and (snapshot["selectionAt"], snapshot["id"]) <= (old["selectionAt"], old["id"]):
            continue
        targets = members[snapshot["id"]]
        previous = members[old["id"]] if old else {}
        for target in sorted(set(targets) | set(previous)):
            active = target in targets
            member = targets[target] if active else previous[target]
            claims[source, target] = {"sourceId": source, "targetUrl": target,
                "status": "active" if active else "withdrawn", "snapshotId": snapshot["id"],
                "previousSnapshotId": old["id"] if old else None, "observedAt": snapshot["selectionAt"],
                "completedAt": snapshot["completedAt"], "reason": "complete_snapshot_membership" if active else "complete_snapshot_absence",
                "name": member["name"], "sourceRank": None, "claimKind": "curated_listing",
                "endorsement": "source-listing-not-package-endorsement"}
        current[source] = snapshot
    return current, claims


def _package_links(connection, claims):
    by_target = {}
    for source, target in claims:
        by_target.setdefault(target, []).append(source)
    links = {}
    for row in connection.execute("SELECT package_id,version,payload_hash,canonical_json FROM package_versions ORDER BY package_id,version"):
        record = json.loads(row["canonical_json"])
        assertions = [("/links/repository", record.get("links", {}).get("repository"), "repository_context")]
        assertions += [("/distributions/" + str(index) + "/url", value.get("url"), "distribution_declaration") for index, value in enumerate(record.get("distributions", [])) if value.get("type") in ("git", "github-repo")]
        grouped = {}
        for field, url, kind in assertions:
            if not url:
                continue
            try:
                target = _target(url)
            except ValueError:
                continue
            if target not in by_target:
                continue
            grouped.setdefault(target, []).append({"field": field, "url": url, "kind": kind})
        for target, assertions in grouped.items():
            for source in by_target[target]:
                key = (row["package_id"], row["version"], source, target)
                links[key] = {"packageId": row["package_id"], "version": row["version"], "sourceId": source,
                              "targetUrl": target, "canonicalHash": row["payload_hash"], "assertions": assertions,
                              "association": "exact-declared-link", "endorsement": "not-inferred"}
    return links


def coverage(connection, as_of):
    return {"policyVersion": POLICY, "asOf": as_of, "sources": connection.execute("SELECT count(*) FROM curated_sources").fetchone()[0],
            "snapshots": connection.execute("SELECT count(*) FROM curated_snapshots").fetchone()[0],
            "members": connection.execute("SELECT count(*) FROM curated_members").fetchone()[0],
            "active": connection.execute("SELECT count(*) FROM curated_claims WHERE status='active'").fetchone()[0],
            "withdrawn": connection.execute("SELECT count(*) FROM curated_claims WHERE status='withdrawn'").fetchone()[0],
            "packageLinks": connection.execute("SELECT count(*) FROM curated_package_links").fetchone()[0],
            "coverage": "configured-scopes-only", "rankPolicy": "upstream-rank-unknown", "endorsement": "not-inferred"}


def project_recommendations(state, connection, *, as_of):
    as_of = timestamp(as_of)
    create_schema(connection)
    sources, snapshots, members, labels = {}, {}, {}, {}
    definitions = {row["id"]: validate_source(json.loads(row["definition_json"])) for row in state.execute("SELECT * FROM recommendation_sources WHERE created_at<=?", (as_of,))}
    for row in state.execute("SELECT s.* FROM recommendation_snapshots s JOIN recommendation_scans r ON r.id=s.scan_id WHERE s.completed_at<=? AND s.selection_at<=? AND r.finished_at<=? AND r.state='complete' ORDER BY s.completed_at,s.id", (as_of, as_of, as_of)):
        definition = definitions.get(row["source_id"])
        if definition is None:
            raise ValueError("curated snapshot source is missing before cutoff")
        snapshot, parsed = _private_snapshot(state, row, definition, as_of)
        source = snapshot["sourceId"]
        if snapshot["id"] in snapshots and snapshots[snapshot["id"]] != snapshot:
            raise ValueError("conflicting curated snapshots for public identity")
        snapshots[snapshot["id"]] = snapshot
        members[snapshot["id"]] = {member["targetUrl"]: member for member in parsed}
        labels.setdefault(source, set()).add(definition["label"])
    current, claims = _claims(snapshots, members)
    for source, latest in current.items():
        data = {"id": source, "labels": sorted(labels[source]), "provider": "github-curated-markdown-v1",
                "repositoryId": latest["definition"]["repositoryId"], "path": latest["definition"]["path"],
                "selectionStatement": latest["definition"]["selectionStatement"],
                "startHeading": latest["definition"]["startHeading"], "endHeading": latest["definition"]["endHeading"],
                "emptyStatement": latest["definition"]["emptyStatement"], "latestSnapshotId": latest["id"],
                "lastSuccessAt": latest["completedAt"], "lastObservationAt": latest["selectionAt"],
                "staleAfterSeconds": STALE_SECONDS, "coverage": "configured-scopes-only", "claimKind": "curated_listing", "sourceRank": None}
        sources[source] = data
        connection.execute("INSERT INTO curated_sources VALUES (?,?)", (source, encode_json(data)))
    for snapshot in snapshots.values():
        connection.execute("INSERT INTO curated_snapshots VALUES (?,?,?,?,?)", (snapshot["id"], snapshot["sourceId"], snapshot["selectionAt"], snapshot["completedAt"], encode_json(snapshot)))
        for member in members[snapshot["id"]].values():
            connection.execute("INSERT INTO curated_members VALUES (?,?,?)", (snapshot["id"], member["targetUrl"], encode_json(member)))
    for claim in claims.values():
        connection.execute("INSERT INTO curated_claims VALUES (?,?,?,?,?,?)", (claim["sourceId"], claim["targetUrl"], claim["status"], claim["snapshotId"], claim["previousSnapshotId"], encode_json(claim)))
    for key, data in _package_links(connection, claims).items():
        connection.execute("INSERT INTO curated_package_links VALUES (?,?,?,?,?)", (*key, encode_json(data)))
    return coverage(connection, as_of)


def _validate_snapshot(value, as_of):
    required = {"id", "sourceId", "repositoryName", "commitSha", "blobSha", "contentSha256", "contentBytes", "selectionAt", "observedAt", "checkedAt", "completedAt", "entryCount", "memberCount", "scope", "definition", "steps", "url", "complete", "claimKind", "sourceRank"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid public curated snapshot fields")
    definition = validate_source(value["definition"])
    for key in ("selectionAt", "observedAt", "checkedAt", "completedAt"):
        _date(value[key], as_of)
    if not value["observedAt"] <= value["checkedAt"] <= value["completedAt"] or value["selectionAt"] > value["checkedAt"]:
        raise ValueError("public curated snapshot clocks differ")
    _sha(value["commitSha"], 40); _sha(value["blobSha"], 40); _sha(value["contentSha256"])
    name = value["repositoryName"]
    RecommendationGitRequest("commit", name, value["commitSha"]).validate()
    identity = _hash([POLICY, public_source_identity(definition), value["commitSha"], value["blobSha"], value["selectionAt"], value["completedAt"], _hash(definition)])
    if value["id"] != identity or value["sourceId"] != public_source_identity(definition) or value["url"] != _url(name, value["commitSha"], definition["path"]) or value["complete"] is not True or value["claimKind"] != "curated_listing" or value["sourceRank"] is not None:
        raise ValueError("public curated snapshot identity/meaning differs")
    for key in ("contentBytes", "entryCount", "memberCount"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError("public curated counts are invalid")
    if not 0 < value["contentBytes"] <= 2 * 1024 * 1024 or not value["memberCount"] <= value["entryCount"] <= definition["maxEntries"] or value["entryCount"] < definition["minEntries"] and not (value["entryCount"] == 0 and definition["emptyStatement"]):
        raise ValueError("public curated counts exceed parser scope")
    scope = value["scope"]
    if not isinstance(scope, dict) or set(scope) != {"startLine", "endLine", "lineCount"} or any(type(number) is not int for number in scope.values()) or not 1 <= scope["startLine"] < scope["endLine"] <= scope["lineCount"] <= value["contentBytes"]:
        raise ValueError("public curated line scope is invalid")
    kinds = ["repository", "commit"] + ["tree"] * len(definition["path"].split("/")) + ["blob", "verify"]
    steps = value["steps"]
    if not isinstance(steps, list) or len(steps) != len(kinds):
        raise ValueError("public curated Git chain is incomplete")
    for index, (step, kind) in enumerate(zip(steps, kinds)):
        if not isinstance(step, dict) or set(step) != {"kind", "url", "responseHash", "status", "observedAt", "checkedAt"} or step["kind"] != kind or type(step["status"]) is not int or step["status"] not in (200, 304):
            raise ValueError("invalid public curated Git step")
        _sha(step["responseHash"]); _date(step["observedAt"], as_of); _date(step["checkedAt"], as_of)
        if step["observedAt"] > step["checkedAt"] or step["checkedAt"] > value["completedAt"] or index and step["checkedAt"] < steps[index-1]["checkedAt"]:
            raise ValueError("public curated Git step clocks differ")
        prefix = "https://api.github.com/repos/" + name + "/"
        if kind in ("repository", "verify"):
            expected = "https://api.github.com/repositories/" + str(definition["repositoryId"])
            if step["url"] != expected:
                raise ValueError("public curated repository identity differs")
        elif kind == "commit":
            if not step["url"].startswith(prefix + "commits/"):
                raise ValueError("public curated commit endpoint differs")
            from urllib.parse import unquote
            RecommendationGitRequest("commit", name, unquote(step["url"][len(prefix + "commits/"):])).validate()
        elif kind == "tree":
            if not step["url"].startswith(prefix + "git/trees/"):
                raise ValueError("public curated tree endpoint differs")
            _sha(step["url"][len(prefix + "git/trees/"):], 40)
        elif step["url"] != prefix + "git/blobs/" + value["blobSha"]:
            raise ValueError("public curated blob endpoint differs")
    if steps[1]["checkedAt"] != value["selectionAt"] or steps[-2]["observedAt"] != value["observedAt"] or steps[-2]["checkedAt"] != value["checkedAt"]:
        raise ValueError("public curated evidence clocks differ from snapshot")


def validate_projection(connection, manifest):
    reference = sqlite3.connect(":memory:")
    try:
        create_schema(reference)
        expected = {row[0]: row[1] for row in reference.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
        actual = {row[0]: row[1] for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
        if any(actual.get(name) != sql for name, sql in expected.items()):
            raise ValueError("public curated SQL schema differs")
    finally:
        reference.close()
    as_of = timestamp(manifest["generatedAt"])
    snapshots, members = {}, {}
    for row in connection.execute("SELECT * FROM curated_snapshots"):
        item = _object(row["data_json"]); _validate_snapshot(item, as_of)
        if (row["id"], row["source_id"], row["selection_at"], row["completed_at"]) != (item["id"], item["sourceId"], item["selectionAt"], item["completedAt"]):
            raise ValueError("public curated snapshot columns differ")
        snapshots[row["id"]], members[row["id"]] = item, {}
    for row in connection.execute("SELECT * FROM curated_members"):
        item = _object(row["data_json"])
        snapshot = snapshots.get(row["snapshot_id"])
        if snapshot is None or not isinstance(item, dict) or set(item) != {"targetUrl", "name", "positions", "sourceRank"} or item["targetUrl"] != row["target_url"] or _target(item["targetUrl"]) != item["targetUrl"] or item["sourceRank"] is not None:
            raise ValueError("invalid public curated member")
        if not isinstance(item["name"], str) or not 1 <= len(item["name"]) <= 200 or any(ord(char) < 32 for char in item["name"]):
            raise ValueError("invalid public curated member name")
        positions = item["positions"]
        if not isinstance(positions, list) or not 1 <= len(positions) <= 100:
            raise ValueError("invalid public curated member citations")
        for position in positions:
            if not isinstance(position, dict) or set(position) != {"line", "section", "url"} or type(position["line"]) is not int or not snapshot["scope"]["startLine"] < position["line"] < snapshot["scope"]["endLine"] or _target(position["url"]) != item["targetUrl"] or not isinstance(position["section"], str) or not position["section"].startswith(("## ", "### ")) or len(position["section"]) > 500 or any(ord(char) < 32 for char in position["section"]):
                raise ValueError("public curated citation is outside its complete scope")
        if len({position["line"] for position in positions}) != len(positions):
            raise ValueError("duplicate public curated citation")
        members[row["snapshot_id"]][row["target_url"]] = item
    for identity, snapshot in snapshots.items():
        if snapshot["memberCount"] != len(members[identity]) or snapshot["entryCount"] != sum(len(item["positions"]) for item in members[identity].values()):
            raise ValueError("public curated snapshot member count differs")
        lines = [position["line"] for item in members[identity].values() for position in item["positions"]]
        if len(set(lines)) != len(lines):
            raise ValueError("public curated entries share conflicting citation lines")
    current, claims = _claims(snapshots, members)
    actual_claims = {}
    for row in connection.execute("SELECT * FROM curated_claims"):
        item = _object(row["data_json"])
        if (row["source_id"], row["target_url"], row["status"], row["snapshot_id"], row["previous_id"]) != (item.get("sourceId"), item.get("targetUrl"), item.get("status"), item.get("snapshotId"), item.get("previousSnapshotId")):
            raise ValueError("public curated claim columns differ")
        actual_claims[row["source_id"], row["target_url"]] = item
    if actual_claims != claims:
        raise ValueError("public curated claims differ from complete-snapshot replay")
    sources = {}
    for row in connection.execute("SELECT * FROM curated_sources"):
        item = _object(row["data_json"])
        if not isinstance(item, dict) or set(item) != {"id", "labels", "provider", "repositoryId", "path", "selectionStatement", "startHeading", "endHeading", "emptyStatement", "latestSnapshotId", "lastSuccessAt", "lastObservationAt", "staleAfterSeconds", "coverage", "claimKind", "sourceRank"} or item["id"] != row["id"] or row["id"] not in current:
            raise ValueError("invalid public curated source fields/identity")
        latest = current[row["id"]]
        expected_labels = sorted({value["definition"]["label"] for value in snapshots.values() if value["sourceId"] == row["id"]})
        for key in ("provider", "repositoryId", "path", "selectionStatement", "startHeading", "endHeading", "emptyStatement"):
            if item[key] != latest["definition"][key]:
                raise ValueError("public curated source scope differs")
        if item["labels"] != expected_labels or item["latestSnapshotId"] != latest["id"] or item["lastSuccessAt"] != latest["completedAt"] or item["lastObservationAt"] != latest["selectionAt"] or type(item["staleAfterSeconds"]) is not int or item["staleAfterSeconds"] != STALE_SECONDS or item["coverage"] != "configured-scopes-only" or item["claimKind"] != "curated_listing" or item["sourceRank"] is not None:
            raise ValueError("public curated source status differs from evidence")
        sources[row["id"]] = item
    if set(sources) != set(current):
        raise ValueError("public curated source completeness differs")
    links = {}
    for row in connection.execute("SELECT * FROM curated_package_links"):
        links[row["package_id"], row["version"], row["source_id"], row["target_url"]] = _object(row["data_json"])
    if links != _package_links(connection, claims):
        raise ValueError("public curated package links differ from exact canonical declarations")
    if manifest.get("recommendationCoverage") != coverage(connection, as_of):
        raise ValueError("public curated coverage differs")
