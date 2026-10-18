"""Resumable public declaration checks. Successful candidates still need review."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3

from .collector import run_collection_jobs
from .declarations import MAX_DECLARATION_BYTES, declaration_association, declaration_path, normalize_declaration, parse_declaration
from .errors import ServiceError
from .jobs import JobQueue
from .public_git import AnonymousGitClient, PublicGitRequest, REPRESENTATION
from .storage import encode_json, timestamp, utc_now


def _key(value):
    return hashlib.sha256(encode_json(value).encode("utf8")).hexdigest()


def _enqueue_step(connection, scan_id, step, now):
    job_id = JobQueue(connection).enqueue(key=encode_json(["github_manifest", scan_id, step]),
        kind="github_manifest", scope="manifest:" + scan_id, payload={"scanId": scan_id, "step": step},
        now=now, due_at=now)
    connection.execute("INSERT OR IGNORE INTO manifest_tasks VALUES (?,?,?)", (job_id, scan_id, step))
    return job_id


def enqueue_manifest(connection, *, github_id, path, kind, agent, slot, now):
    PublicGitRequest("repository", github_id).validate()
    if type(github_id) is not int:
        raise ValueError("manifest checks require a stable GitHub repository ID")
    declaration_path(path, kind)
    declaration_association(kind, agent)
    if not connection.execute("SELECT 1 FROM repositories WHERE github_id=? UNION ALL SELECT 1 FROM discovery_candidates WHERE github_id=? LIMIT 1", (github_id, github_id)).fetchone():
        raise ValueError("repository must first be a discovered candidate or a known public seed")
    slot, now = timestamp(slot), timestamp(now)
    scan_id = _key(["manifest-scan-v1", github_id, path, kind, agent, slot])
    connection.execute("INSERT OR IGNORE INTO manifest_scans VALUES (?,?,?,?,?,?,'queued',0,?,?,NULL,NULL)",
                       (scan_id, github_id, path, kind, agent, slot, encode_json({"stage": "repository"}), now))
    _enqueue_step(connection, scan_id, 0, max(now, slot))
    return scan_id



def plan_discovered_manifests(connection, *, path, kind, agent, slot, limit, now):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("manifest planning requires a repository budget between 1 and 100")
    declaration_path(path, kind)
    declaration_association(kind, agent)
    slot = timestamp(slot)
    rows = connection.execute(
        "SELECT d.github_id,max(s.slot) AS last_slot FROM discovery_candidates d "
        "LEFT JOIN manifest_scans s ON s.github_id=d.github_id AND s.path=? AND s.format=? AND s.agent=? "
        "GROUP BY d.github_id HAVING last_slot IS NULL OR last_slot<? "
        "ORDER BY last_slot IS NOT NULL,last_slot,d.github_id LIMIT ?", (path, kind, agent, slot, limit)).fetchall()
    ids = [enqueue_manifest(connection, github_id=row["github_id"], path=path, kind=kind, agent=agent,
                            slot=slot, now=now) for row in rows]
    return {"scanIds": ids, "queuedRepositories": len(ids), "pathCoverage": "explicit-path-only"}


def _request(scan, progress):
    stage = progress["stage"]
    if stage in ("repository", "verify"):
        return PublicGitRequest("repository", scan["github_id"])
    if stage == "commit":
        return PublicGitRequest("commit", progress["fullName"], progress["branch"])
    if stage in ("tree", "blob"):
        return PublicGitRequest(stage, progress["fullName"], progress["tree" if stage == "tree" else "blob"])
    raise ValueError("invalid persisted manifest stage")


def _candidate(connection, scan, progress, now):
    candidate_id = _key(["manifest-candidate-v1", scan["github_id"], scan["path"], scan["format"], scan["agent"], progress["commit"]])
    if connection.execute("SELECT 1 FROM manifest_candidates WHERE id=?", (candidate_id,)).fetchone():
        return candidate_id
    record, issues = progress["record"], progress["issues"]
    conflict = connection.execute("SELECT id FROM package_units WHERE type=? AND name=? AND id<>?", (record["type"], record.get("name", ""), record["id"])).fetchone()
    if conflict:
        issues = [*issues, {"code": "canonical_identity_conflict", "canonicalId": conflict["id"]}]
    state = "needs_metadata" if progress["issues"] else "identity_conflict" if conflict else "review_pending"
    steps = [dict(row) for row in connection.execute(
        "SELECT f.endpoint,f.evidence_hash,f.observed_at,f.checked_at,f.response_status FROM manifest_fetches f "
        "JOIN manifest_tasks t ON t.job_id=f.job_id WHERE t.scan_id=? ORDER BY t.step", (scan["id"],))]
    evidence = {"policy": "finite-declarations-v1", "publicAccess": "anonymous-https", "steps": steps,
                "repositoryNameAtRead": progress["fullName"], "association": "explicit-profile-not-compatibility-verification",
                "declaredFields": progress["fields"], "agentAssociation": scan["agent"]}
    connection.execute("INSERT OR IGNORE INTO manifest_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (candidate_id, scan["github_id"], scan["path"], scan["format"], scan["agent"], progress["commit"],
         progress["blob"], progress["contentHash"], state, encode_json(record), encode_json(issues), encode_json(evidence), now))
    states = ["metadata_found", "normalized"]
    if not progress["issues"]:
        states.append("schema_valid")
    states.append(state)
    for item in states:
        connection.execute("INSERT OR IGNORE INTO manifest_candidate_events VALUES (?,?,?)", (candidate_id, item, now))
    return candidate_id


def _persist(connection, scan, progress, lease, fetch):
    now = fetch.checked_at
    evidence_hash = _key([REPRESENTATION, fetch.body_hash])
    connection.execute("INSERT OR IGNORE INTO collection_evidence VALUES (?,?,?)", (evidence_hash, REPRESENTATION, encode_json(fetch.body)))
    connection.execute("INSERT INTO manifest_fetches VALUES (?,?,?,?,?,?)", (lease.id, evidence_hash, _request(scan, progress).endpoint, fetch.observed_at, now, fetch.status))
    stage, body = progress["stage"], fetch.body
    state, reason = "running", None
    if stage == "repository":
        progress.update(stage="commit", fullName=body["full_name"], branch=body["default_branch"])
    elif stage == "commit":
        progress.update(stage="tree", commit=body["sha"], tree=body["commit"]["tree"]["sha"], remaining=scan["path"].split("/"))
    elif stage == "tree":
        part = progress["remaining"][0]
        found = next((item for item in body["tree"] if item["path"] == part), None)
        if found is None:
            state, reason = "not_found", "declared_path_absent_not_a_classification"
        elif len(progress["remaining"]) > 1:
            if found["mode"] != "040000" or found["type"] != "tree":
                state, reason = "rejected", "non_directory_or_symlink"
            else:
                progress.update(tree=found["sha"], remaining=progress["remaining"][1:])
        elif found["mode"] not in ("100644", "100755") or found["type"] != "blob":
            state, reason = "rejected", "non_file_or_symlink"
        elif "size" not in found or found["size"] > MAX_DECLARATION_BYTES:
            state, reason = "rejected", "manifest_size_unknown_or_exceeded"
        else:
            progress.update(stage="blob", blob=found["sha"], size=found["size"])
    elif stage == "blob":
        raw = base64.b64decode(body["content"], validate=True)
        if len(raw) != progress["size"]:
            state, reason = "rejected", "tree_blob_length_mismatch"
        else:
            try:
                fields = parse_declaration(raw, scan["format"])
                record, issues = normalize_declaration(fields, kind=scan["format"], github_id=scan["github_id"], full_name=progress["fullName"], path=scan["path"], commit=progress["commit"], agent=scan["agent"])
                progress.update(stage="verify", fields=fields, record=record, issues=issues,
                                contentHash=hashlib.sha256(raw).hexdigest())
            except ServiceError as error:
                state, reason = "rejected", error.code
    elif stage == "verify":
        if body["full_name"].lower() != progress["fullName"].lower():
            state, reason = "rejected", "repository_alias_changed_during_read"
        else:
            progress["candidateId"] = _candidate(connection, scan, progress, now)
            state = "complete"
    step = scan["step"] + 1
    connection.execute("UPDATE manifest_scans SET state=?,step=?,progress_json=?,finished_at=?,reason=? WHERE id=?",
                       (state, step, encode_json(progress), now if state != "running" else None, reason, scan["id"]))
    if state == "running":
        _enqueue_step(connection, scan["id"], step, now)


def run_manifest_jobs(connection, client, *, owner, max_requests, clock=utc_now, job_ids=None, should_stop=None):
    if not isinstance(client, AnonymousGitClient):
        raise ValueError("declarations must use the isolated anonymous client")
    def prepare(lease):
        scan = connection.execute("SELECT * FROM manifest_scans WHERE id=?", (lease.payload.get("scanId"),)).fetchone()
        if not scan or scan["state"] not in ("queued", "running") or lease.payload.get("step") != scan["step"]:
            raise ValueError("manifest task is stale or invalid")
        progress = json.loads(scan["progress_json"])
        request = _request(scan, progress)
        return request, lambda c, fetch: _persist(c, scan, progress, lease, fetch)
    result = run_collection_jobs(connection, client, owner=owner, max_requests=max_requests,
                                 kind="github_manifest", prepare=prepare, clock=clock, job_ids=job_ids, should_stop=should_stop)
    # A crash after a terminal failure is reconciled on the next bounded invocation.
    with connection:
        connection.execute("UPDATE manifest_scans SET state='failed',reason='terminal_job_failure',finished_at=? "
            "WHERE state IN ('queued','running') AND EXISTS (SELECT 1 FROM manifest_tasks t JOIN jobs j ON j.id=t.job_id "
            "WHERE t.scan_id=manifest_scans.id AND j.state='dead')", (timestamp(clock()),))
    result["candidateStates"] = {row[0]: row[1] for row in connection.execute("SELECT state,count(*) FROM manifest_candidates GROUP BY state")}
    result["nextNetworkAt"] = client.next_allowed_at("core", now=clock())
    return result


def manifest_status(connection, *, limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("manifest status limit must be between 1 and 100")
    scans = [dict(row) for row in connection.execute("SELECT id,github_id,path,format,agent,slot,state,step,created_at,finished_at,reason FROM manifest_scans ORDER BY created_at DESC,id LIMIT ?", (limit,))]
    candidates = []
    for row in connection.execute("SELECT * FROM manifest_candidates ORDER BY created_at DESC,id LIMIT ?", (limit,)):
        candidates.append({"id": row["id"], "githubRepoId": row["github_id"], "path": row["path"], "format": row["format"], "state": row["state"], "commit": row["commit_sha"], "blob": row["blob_sha"], "record": json.loads(row["record_json"]), "issues": json.loads(row["issues_json"])})
    return {"scans": scans, "candidates": candidates, "canonicalWrites": 0, "policy": "finite-declarations-v1"}


def read_manifest_status(path, limit=20):
    if not path.is_file():
        raise ValueError("manifest state must exist")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("manifest status limit must be between 1 and 100")
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='manifest_scans'").fetchone():
            return {"scans": [], "candidates": [], "available": False, "canonicalWrites": 0}
        return manifest_status(connection, limit=limit)
    finally:
        connection.close()
