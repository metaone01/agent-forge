"""Known public repository checks. No plugin execution, canonical writes, or ranking."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from .errors import ServiceError
from .github import GitHubRequest, PUBLIC_FIELDS_VERSION
from .github_client import CollectionError, GitHubClient, _time
from .jobs import JobQueue, save_checkpoint
from .metrics import MetricSample
from .storage import encode_json, timestamp, utc_now


def enqueue_repository(queue: JobQueue, *, repository: str | int, slot: str,
                       bootstrap: bool, now, max_attempts: int = 5) -> str:
    request = GitHubRequest.repository(repository).validate()
    if type(bootstrap) is not bool:
        raise ValueError("bootstrap provenance must be explicit")
    slot = timestamp(slot)
    return queue.enqueue(key=encode_json(["github_repository", request.endpoint, slot]),
                         kind="github_repository", scope=request.endpoint,
                         payload={"repository": repository, "bootstrap": bootstrap, "slot": slot},
                         now=now, due_at=slot, max_attempts=max_attempts)


def normalize_repository(body: dict) -> dict:
    try:
        GitHubRequest.repository(body["full_name"]).validate()
        if type(body.get("id")) is not int or not 0 < body["id"] <= 2**63 - 1 or body.get("private") is not False:
            raise ValueError("invalid repository identity")
        for field in ("stargazers_count", "forks_count", "open_issues_count"):
            if body.get(field) is not None and (type(body[field]) is not int or body[field] < 0):
                raise ValueError("invalid public count")
        for field in ("archived", "disabled"):
            if body.get(field) is not None and type(body[field]) is not bool:
                raise ValueError("invalid public flag")
        topics = body.get("topics")
        if topics is not None and (not isinstance(topics, list) or len(topics) > 100 or any(not isinstance(x, str) or not x or len(x) > 50 for x in topics)):
            raise ValueError("invalid repository topics")
        return {"githubRepoId": body["id"], "fullName": body["full_name"],
                "url": "https://github.com/" + body["full_name"], "topics": topics,
                "archived": body.get("archived"), "disabled": body.get("disabled"),
                "defaultBranch": body.get("default_branch"),
                "createdAt": timestamp(body["created_at"]) if body.get("created_at") else None,
                "updatedAt": timestamp(body["updated_at"]) if body.get("updated_at") else None,
                "pushedAt": timestamp(body["pushed_at"]) if body.get("pushed_at") else None,
                "stars": body.get("stargazers_count"), "forks": body.get("forks_count")}
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        raise ServiceError("github_invalid_payload", "repository metadata is invalid", 502) from error


def store_repository(connection, *, lease, fetch, bootstrap: bool, identity: str):
    metadata = normalize_repository(fetch.body)
    encoded = encode_json(metadata)
    repo_id = metadata["githubRepoId"]
    connection.execute("INSERT OR IGNORE INTO collection_evidence VALUES (?,?,?)",
                       (fetch.body_hash, PUBLIC_FIELDS_VERSION, encode_json(fetch.body)))
    candidate = connection.execute("SELECT first_seen_at,bootstrap FROM discovery_candidates WHERE github_id=?", (repo_id,)).fetchone()
    first_seen = min(fetch.observed_at, candidate["first_seen_at"]) if candidate else fetch.observed_at
    bootstrap = bootstrap or bool(candidate and candidate["bootstrap"])
    connection.execute(
        "INSERT INTO repositories VALUES (?,?,?,?,?,?,?) ON CONFLICT(github_id) DO UPDATE SET "
        "full_name=excluded.full_name,last_seen_at=excluded.last_seen_at,"
        "last_success_at=excluded.last_success_at,metadata_json=excluded.metadata_json",
        (repo_id, metadata["fullName"], first_seen, int(bootstrap), fetch.checked_at, fetch.checked_at, encoded))
    connection.execute("INSERT INTO repository_observations VALUES (?,?,?,?,?,?,?,?)",
                       (lease.id, lease.id, repo_id, fetch.observed_at, fetch.checked_at, fetch.status, fetch.body_hash, encoded))
    from .canonical_mapping import observe_repository_aliases
    observe_repository_aliases(connection, github_id=repo_id, full_name=metadata["fullName"],
                               requested=lease.scope, observed_at=fetch.observed_at,
                               checked_at=fetch.checked_at, observation_id=lease.id)
    sample = MetricSample(source_id="github.com", subject_kind="repository", subject_id=str(repo_id),
                          metric="stars", unit="stars", counter_identity=f"github-repository:{repo_id}:stargazers",
                          distribution_scope="github_repository", observed_at=fetch.observed_at,
                          value=metadata["stars"], availability="available" if metadata["stars"] is not None else "not_reported")
    # A 304 reuses the original numeric observation; it does not manufacture a new sample time.
    sample_id = hashlib.sha256(encode_json([sample.identity, sample.observed_at, sample.value, fetch.body_hash]).encode("utf-8")).hexdigest()
    connection.execute("INSERT OR IGNORE INTO metric_observations VALUES (?,?,?,?,?,?,?)",
                       (sample_id, lease.id, sample.subject_kind, sample.subject_id, sample.metric,
                        sample.observed_at, encode_json(asdict(sample))))
    source_id = "github.com:repository:" + identity
    previous = connection.execute("SELECT watermark FROM checkpoints WHERE source_id=? AND shard_key=?",
                                  (source_id, str(repo_id))).fetchone()
    watermark = max(fetch.observed_at, previous["watermark"] or fetch.observed_at) if previous else fetch.observed_at
    save_checkpoint(connection, source_id=source_id, shard_key=str(repo_id),
                    cursor=None, watermark=watermark, complete=True, now=fetch.checked_at,
                    details={"status": fetch.status, "observedAt": fetch.observed_at, "evidenceHash": fetch.body_hash})
    from .batches import remember_refresh_success
    remember_refresh_success(connection, github_id=repo_id, checked_at=fetch.checked_at)


def run_repository_jobs(connection, client: GitHubClient, *, owner: str, max_requests: int,
                        clock=utc_now, job_ids=None, should_stop=None) -> dict:
    def prepare(lease):
        repository = lease.payload.get("repository")
        bootstrap = lease.payload.get("bootstrap")
        if type(bootstrap) is not bool:
            raise ValueError("invalid bootstrap provenance")
        request = GitHubRequest.repository(repository).validate()
        if request.endpoint != lease.scope:
            raise ValueError("job scope and repository differ")
        return request, lambda c, fetch: store_repository(c, lease=lease, fetch=fetch,
                                                         bootstrap=bootstrap, identity=client.identity)
    return run_collection_jobs(connection, client, owner=owner, max_requests=max_requests,
                               kind="github_repository", prepare=prepare, clock=clock, job_ids=job_ids, should_stop=should_stop)


def run_collection_jobs(connection, client: GitHubClient, *, owner: str, max_requests: int,
                        kind: str, prepare, clock=utc_now, job_ids=None, scopes=None, should_stop=None) -> dict:
    if type(max_requests) is not int or not 1 <= max_requests <= 100:
        raise ValueError("one run needs an explicit request budget between 1 and 100")
    if client.connection is not connection:
        raise ValueError("client and jobs must share one worker state")
    if should_stop is not None and not callable(should_stop):
        raise ValueError("collection cancellation must be callable")
    queue = JobQueue(connection)
    result = {"claimed": 0, "succeeded": 0, "deferred": 0, "dead": 0, "leaseLost": 0, "requests": 0}
    for _ in range(max_requests):
        if should_stop and should_stop():
            break
        lease = queue.claim(owner=owner, now=clock(), kind=kind, job_ids=job_ids, scopes=scopes)
        if lease is None:
            break
        result["claimed"] += 1
        try:
            request, persist = prepare(lease)
            try:
                if should_stop and should_stop():
                    raise CollectionError("collection_stopped", retry_at=timestamp(_time(clock()) + timedelta(seconds=1)), attempted=False)
                fetch = client.fetch(request, attempt=lease.attempts)
                result["requests"] += 1
            except CollectionError as error:
                if error.attempted:
                    result["requests"] += 1
                raise
            queue.finish(lease, now=clock(), apply=lambda c: persist(c, fetch))
            result["succeeded"] += 1
        except CollectionError as error:
            try:
                queue.fail(lease, now=clock(), code=error.code, retry_at=error.retry_at, attempted=error.attempted)
                state = connection.execute("SELECT state FROM jobs WHERE id=?", (lease.id,)).fetchone()[0]
                result["dead" if state == "dead" else "deferred"] += 1
            except ServiceError as lost:
                if lost.code != "lease_lost":
                    raise
                result["leaseLost"] += 1
        except (ValueError, ServiceError) as error:
            if isinstance(error, ServiceError) and error.code == "lease_lost":
                result["leaseLost"] += 1
                continue
            code = error.code if isinstance(error, ServiceError) else "invalid_collection_job"
            retry = timestamp(_time(clock()) + timedelta(seconds=60)) if code == "github_invalid_payload" else None
            queue.fail(lease, now=clock(), code=code, retry_at=retry)
            state = connection.execute("SELECT state FROM jobs WHERE id=?", (lease.id,)).fetchone()[0]
            result["dead" if state == "dead" else "deferred"] += 1
    result["pending"] = connection.execute("SELECT count(*) FROM jobs WHERE state IN ('pending','retry_wait','leased')").fetchone()[0]
    return result


def collection_status(path: Path, limit: int = 20) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100 or not path.is_file():
        raise ValueError("collector state must exist and status limit must be in [1,100]")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        from .discovery import discovery_status
        from .canonical_mapping import mapping_status
        from .batches import recent_batches
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone():
            return {"jobs": {}, "repositories": 0, "metrics": 0, "recentJobs": []}
        return {"jobs": {r[0]: r[1] for r in connection.execute("SELECT state,count(*) FROM jobs GROUP BY state")},
                "repositories": connection.execute("SELECT count(*) FROM repositories").fetchone()[0],
                "metrics": connection.execute("SELECT count(*) FROM metric_observations").fetchone()[0],
                "discovery": discovery_status(connection, limit),
                "canonicalMapping": mapping_status(connection),
                "batches": recent_batches(connection, min(limit,10)),
                "recentJobs": [dict(r) for r in connection.execute(
                    "SELECT id,kind,scope,state,attempts,next_run,last_error FROM jobs ORDER BY created_at DESC,id LIMIT ?", (limit,))]}
    finally:
        connection.close()
