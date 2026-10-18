"""Bounded public repository Release observations, never plugin-version selection."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .collector import run_collection_jobs
from .errors import ServiceError
from .jobs import JobQueue, save_checkpoint
from .public_git import AnonymousGitClient, PublicGitRequest, sanitize_releases
from .storage import encode_json, timestamp, utc_now

RELEASE_FIELDS = "github-public-release-fields-v1"
SOURCE = "github.com:public-releases"


def _enqueue_page(connection, scan, page, now):
    request = PublicGitRequest("releases", scan["github_id"], page=page).validate()
    job_id = JobQueue(connection).enqueue(
        key=encode_json(["github_releases", scan["id"], page]), kind="github_releases", scope=request.endpoint,
        payload={"scanId": scan["id"], "page": page}, now=now, due_at=scan["slot"])
    connection.execute("INSERT OR IGNORE INTO release_tasks VALUES (?,?,?)", (job_id, scan["id"], page))
    return job_id


def enqueue_releases(connection, *, github_id, slot, max_pages, now):
    PublicGitRequest("releases", github_id).validate()
    if type(max_pages) is not int or not 1 <= max_pages <= 10:
        raise ValueError("release traversal requires an explicit page budget in [1,10]")
    if not connection.execute("SELECT 1 FROM repositories WHERE github_id=?", (github_id,)).fetchone():
        raise ValueError("release collection requires a previously observed stable repository ID")
    slot, now = timestamp(slot), timestamp(now)
    scan_id = hashlib.sha256(encode_json([SOURCE, github_id, slot]).encode()).hexdigest()
    previous = connection.execute("SELECT * FROM release_scans WHERE id=?", (scan_id,)).fetchone()
    if previous:
        if previous["max_pages"] != max_pages:
            raise ServiceError("release_scan_conflict", "same slot has different release page budget", 409)
        return scan_id
    connection.execute("INSERT INTO release_scans VALUES (?,?,?,?,1,'running',?,NULL,NULL)",
                       (scan_id, github_id, slot, max_pages, now))
    _enqueue_page(connection, connection.execute("SELECT * FROM release_scans WHERE id=?", (scan_id,)).fetchone(), 1, now)
    return scan_id


def plan_release_jobs(connection, *, slot, limit, max_pages, now):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("release plan limit must be in [1,1000]")
    PublicGitRequest("releases", 1).validate()
    if type(max_pages) is not int or not 1 <= max_pages <= 10:
        raise ValueError("release traversal requires an explicit page budget in [1,10]")
    slot, now = timestamp(slot), timestamp(now)
    # Only explicitly resolved canonical resources. In-flight scans finish before a new slot is opened.
    rows = connection.execute(
        "SELECT r.github_id FROM repositories r WHERE EXISTS (SELECT 1 FROM canonical_repository_links l "
        "WHERE l.github_id=r.github_id AND l.active=1 AND l.resolution='resolved') "
        "AND NOT EXISTS (SELECT 1 FROM release_scans s JOIN release_tasks t ON t.scan_id=s.id "
        "JOIN jobs j ON j.id=t.job_id WHERE s.github_id=r.github_id AND j.state IN ('pending','retry_wait','leased')) "
        "AND NOT EXISTS (SELECT 1 FROM release_scans s WHERE s.github_id=r.github_id AND s.slot>=?) "
        "ORDER BY coalesce((SELECT max(s.slot) FROM release_scans s WHERE s.github_id=r.github_id),''),r.github_id LIMIT ?",
        (slot, limit)).fetchall()
    with connection:
        scan_ids = [enqueue_releases(connection, github_id=row["github_id"], slot=slot, max_pages=max_pages, now=now) for row in rows]
    return {"scanIds": scan_ids, "selected": len(scan_ids), "maxPagesPerScan": max_pages,
            "selectionSemantics": "runtime_priority_not_public_quality", "catalogCoverage": "not_guaranteed"}


def _persist_page(connection, scan, lease, fetch):
    clean = sanitize_releases(fetch.body)
    body_json = encode_json(clean)
    if hashlib.sha256(body_json.encode()).hexdigest() != fetch.body_hash:
        raise ServiceError("github_invalid_payload", "release evidence hash does not match metadata", 502)
    connection.execute("INSERT OR IGNORE INTO collection_evidence VALUES (?,?,?)", (fetch.body_hash, RELEASE_FIELDS, body_json))
    items = clean["releases"]
    connection.execute("INSERT INTO release_pages VALUES (?,?,?,?,?,?)",
                       (lease.id, fetch.body_hash, fetch.observed_at, fetch.checked_at, fetch.status, len(items)))
    overlap = False
    for item in items:
        overlap = overlap or bool(connection.execute(
            "SELECT 1 FROM release_observations o JOIN release_tasks t ON t.job_id=o.job_id "
            "WHERE t.scan_id=? AND o.release_id=? LIMIT 1", (scan["id"], item["id"])).fetchone())
        connection.execute("INSERT INTO release_observations VALUES (?,?,?)", (lease.id, item["id"], encode_json(item)))
    page = lease.payload["page"]
    complete = len(items) < 100 and not overlap
    exhausted = page >= scan["max_pages"]
    stopped = complete or exhausted or overlap
    reason = "pagination_overlap" if overlap else "page_budget_exhausted" if exhausted and not complete else None
    state = "traversal_complete" if complete else "partial" if stopped else "running"
    connection.execute("UPDATE release_scans SET next_page=?,state=?,finished_at=?,reason=? WHERE id=?",
                       (page + 1, state, fetch.checked_at if stopped else None, reason, scan["id"]))
    if not stopped:
        _enqueue_page(connection, scan, page + 1, fetch.checked_at)
    old = connection.execute("SELECT watermark,details_json FROM checkpoints WHERE source_id=? AND shard_key=?", (SOURCE, str(scan["github_id"]))).fetchone()
    prior_slot = json.loads(old["details_json"]).get("slot") if old else None
    if (not old or not old["watermark"] or scan["slot"] >= old["watermark"]) and (not prior_slot or scan["slot"] >= prior_slot):
        save_checkpoint(connection, source_id=SOURCE, shard_key=str(scan["github_id"]),
                        cursor=None if stopped else {"scanId": scan["id"], "page": page + 1},
                        watermark=scan["slot"] if complete else None, complete=complete, now=fetch.checked_at,
                        details={"scanId": scan["id"], "slot": scan["slot"], "state": state, "reason": reason,
                                 "semantics": "mutable_page_traversal_not_snapshot_or_deletion_proof"})


def run_release_jobs(connection, client, *, owner, max_requests, clock=utc_now, job_ids=None, should_stop=None):
    if not isinstance(client, AnonymousGitClient):
        raise ValueError("public releases must use the credential-free anonymous client")

    def prepare(lease):
        row = connection.execute("SELECT s.*,t.page FROM release_tasks t JOIN release_scans s ON s.id=t.scan_id WHERE t.job_id=?", (lease.id,)).fetchone()
        if row is None or lease.payload != {"scanId": row["id"], "page": row["page"]} or row["next_page"] != row["page"] or row["state"] != "running":
            raise ValueError("release task does not match its active scan step")
        request = PublicGitRequest("releases", row["github_id"], page=row["page"]).validate()
        if lease.scope != request.endpoint:
            raise ValueError("release task scope differs from its stable repository")
        return request, lambda c, fetch: _persist_page(c, row, lease, fetch)

    return run_collection_jobs(connection, client, owner=owner, max_requests=max_requests,
                               kind="github_releases", prepare=prepare, clock=clock, job_ids=job_ids, should_stop=should_stop)


def release_status(connection, limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("release status limit must be in [1,100]")
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='release_scans'").fetchone():
        return {"releaseSchema": "upgrade_required", "observedLatest": None}
    rows = connection.execute(
        "SELECT s.*,coalesce((SELECT j.state FROM release_tasks t JOIN jobs j ON j.id=t.job_id "
        "WHERE t.scan_id=s.id ORDER BY t.page DESC LIMIT 1),'missing') AS task_state "
        "FROM release_scans s ORDER BY s.created_at DESC,s.id LIMIT ?", (limit,))
    recent = []
    for row in rows:
        item = dict(row)
        if item["state"] == "running" and item["task_state"] in ("dead", "missing"):
            item["state"] = "failed"
        recent.append(item)
    return {"releaseSchema": "ready", "scans": connection.execute("SELECT count(*) FROM release_scans").fetchone()[0],
            "pages": connection.execute("SELECT count(*) FROM release_pages").fetchone()[0],
            "observations": connection.execute("SELECT count(*) FROM release_observations").fetchone()[0],
            "recentScans": recent, "observedLatest": None, "selectionStatus": "repository_scope_only",
            "deletionInference": "not_supported", "assetDownloadSemantics": "raw_asset_request_count_not_users_or_npm_downloads"}


def read_release_status(path: Path, limit=20):
    if not path.is_file():
        raise ValueError("release state does not exist")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return release_status(connection, limit)
    finally:
        connection.close()
