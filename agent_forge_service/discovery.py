"""Resumable public Topic searches. A repository hit is a candidate, not a plugin."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import timedelta

from .collector import normalize_repository, run_collection_jobs
from .errors import ServiceError
from .github import GitHubRequest, PUBLIC_FIELDS_VERSION
from .github_client import _time
from .jobs import JobQueue, save_checkpoint
from .storage import encode_json, timestamp, utc_now


def _hash(value):
    return hashlib.sha256(encode_json(value).encode("utf-8")).hexdigest()


def _label(value):
    if not isinstance(value, str) or not value or len(value) > 160 or any(ord(c) < 32 for c in value):
        raise ValueError("discovery generation/cycle must be an explicit nonempty label")
    return value


def _source(topic, field):
    GitHubRequest.search(topic=topic, field=field, start="2000-01-01T00:00:00Z", end="2000-01-01T00:00:01Z")
    return "github.com:topic:" + topic + ":" + field


@dataclass(frozen=True)
class DiscoveryPolicy:
    min_shard_seconds: int = 60
    max_shards: int = 255

    def __post_init__(self):
        if type(self.min_shard_seconds) is not int or not 1 <= self.min_shard_seconds <= 86400:
            raise ValueError("minimum search shard must be between one second and one day")
        if type(self.max_shards) is not int or not 1 <= self.max_shards <= 1023:
            raise ValueError("search shard budget must be between 1 and 1023")


def _page_job(connection, shard, page, now):
    job_id = JobQueue(connection).enqueue(
        key="github-discovery:" + shard["id"] + ":" + str(page), kind="github_discovery",
        scope=shard["run_id"], payload={"runId": shard["run_id"], "shardId": shard["id"], "page": page},
        now=now, due_at=now)
    connection.execute("INSERT OR IGNORE INTO discovery_tasks VALUES (?,?,?,?)",
                       (job_id, shard["run_id"], shard["id"], page))
    return job_id


def enqueue_window(connection, *, topic: str, field: str, start: str, end: str,
                   generation: str, now, policy: DiscoveryPolicy | None = None,
                   stream_id: str | None = None) -> str:
    # No commit: creating runs, child shards, and page tasks is part of their caller's transaction.
    GitHubRequest.search(topic=topic, field=field, start=start, end=end)
    start, end = timestamp(start), timestamp(end)
    if end > timestamp(now):
        raise ValueError("discovery windows cannot claim future coverage")
    generation = _label(generation)
    policy = policy or DiscoveryPolicy()
    source = _source(topic, field)
    run_id = _hash([source, start, end, generation, stream_id])
    old = connection.execute("SELECT * FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
    encoded_policy = encode_json(asdict(policy))
    if old:
        if old["policy_json"] != encoded_policy:
            raise ServiceError("discovery_conflict", "a run generation has different shard policy", 409)
        return run_id
    if stream_id:
        stream = connection.execute("SELECT * FROM discovery_streams WHERE id=?", (stream_id,)).fetchone()
        if not stream or stream["source_id"] != source:
            raise ValueError("run does not match its discovery stream")
    connection.execute("INSERT INTO discovery_runs VALUES (?,?,?,?,?,?,?,?,?,'pending',?,NULL)",
                       (run_id, source, topic, field, start, end, generation, stream_id, encoded_policy, timestamp(now)))
    shard_id = _hash([run_id, start, end])
    connection.execute("INSERT INTO discovery_shards(id,run_id,window_start,window_end,state) VALUES (?,?,?,?,'pending')",
                       (shard_id, run_id, start, end))
    shard = connection.execute("SELECT * FROM discovery_shards WHERE id=?", (shard_id,)).fetchone()
    _page_job(connection, shard, 1, now)
    if stream_id:
        connection.execute("UPDATE discovery_streams SET active_run_id=? WHERE id=?", (run_id, stream_id))
    return run_id


def _stream(connection, *, mode, topic, field, start, end, config, now):
    source = _source(topic, field)
    start = timestamp(start)
    end = timestamp(end) if end else None
    identity = _hash([source, mode] + ([start, end, config["cycle"]] if mode == "historical" else []))
    encoded = encode_json(config)
    old = connection.execute("SELECT * FROM discovery_streams WHERE id=?", (identity,)).fetchone()
    if old:
        if (old["range_start"], old["range_end"], old["config_json"]) != (start, end, encoded):
            raise ServiceError("discovery_stream_conflict", "stream coverage/configuration changed", 409)
    else:
        connection.execute("INSERT INTO discovery_streams VALUES (?,?,?,?,?,?,?,NULL,NULL,?,NULL)",
                           (identity, source, mode, topic, field, start, end, encoded))
    return connection.execute("SELECT * FROM discovery_streams WHERE id=?", (identity,)).fetchone()


def _active(connection, stream):
    if stream["active_run_id"] and connection.execute(
            "SELECT 1 FROM discovery_tasks t JOIN jobs j ON j.id=t.job_id WHERE t.run_id=? "
            "AND j.state IN ('pending','retry_wait','leased') LIMIT 1", (stream["active_run_id"],)).fetchone():
        return stream["active_run_id"]
    return None


def enqueue_incremental(connection, *, topic: str, field: str, bootstrap_start: str,
                        cutoff: str, generation: str, overlap_seconds: int, now,
                        policy: DiscoveryPolicy | None = None) -> str:
    if type(overlap_seconds) is not int or not 0 <= overlap_seconds <= 7 * 86400:
        raise ValueError("incremental overlap must be explicitly between zero and seven days")
    GitHubRequest.search(topic=topic, field=field, start=bootstrap_start, end=cutoff)
    if timestamp(cutoff) > timestamp(now):
        raise ValueError("incremental cutoff is in the future")
    stream = _stream(connection, mode="incremental", topic=topic, field=field, start=bootstrap_start,
                     end=None, config={"overlapSeconds": overlap_seconds}, now=now)
    old = connection.execute("SELECT * FROM discovery_runs WHERE stream_id=? AND generation=?",
                             (stream["id"], _label(generation))).fetchone()
    if old:
        if old["policy_json"] != encode_json(asdict(policy or DiscoveryPolicy())):
            raise ServiceError("discovery_conflict", "generation has a different shard policy", 409)
        return old["id"]
    active = _active(connection, stream)
    if active:
        return active
    start = max(_time(stream["range_start"]), _time(stream["watermark"]) - timedelta(seconds=overlap_seconds)) if stream["watermark"] else _time(stream["range_start"])
    if timestamp(cutoff) <= (stream["watermark"] or timestamp(start)):
        raise ValueError("incremental cutoff must advance successful coverage")
    return enqueue_window(connection, topic=topic, field=field, start=timestamp(start), end=cutoff,
                          generation=generation, now=now, policy=policy, stream_id=stream["id"])


def _history_next(connection, stream, now, policy):
    start = stream["watermark"] or stream["range_start"]
    if start >= stream["range_end"]:
        return None
    config = json.loads(stream["config_json"])
    end = min(_time(stream["range_end"]), _time(start) + timedelta(seconds=config["chunkSeconds"]))
    generation = "history-" + _hash([config["cycle"], start])
    return enqueue_window(connection, topic=stream["topic"], field=stream["time_field"], start=start,
                          end=timestamp(end), generation=generation, now=now, policy=policy, stream_id=stream["id"])


def enqueue_history(connection, *, topic: str, start: str, end: str, cycle: str,
                    chunk_seconds: int, now, policy: DiscoveryPolicy | None = None) -> str | None:
    if type(chunk_seconds) is not int or not 1 <= chunk_seconds <= 365 * 86400:
        raise ValueError("history chunk must be explicitly between one second and one year")
    GitHubRequest.search(topic=topic, field="created", start=start, end=end)
    if timestamp(end) > timestamp(now):
        raise ValueError("historical coverage cannot extend into the future")
    stream = _stream(connection, mode="historical", topic=topic, field="created", start=start, end=end,
                     config={"cycle": _label(cycle), "chunkSeconds": chunk_seconds}, now=now)
    active = _active(connection, stream)
    if active:
        return active
    return _history_next(connection, stream, now, policy or DiscoveryPolicy())


def _split(connection, shard, run, policy, reason, now):
    start, end = _time(shard["window_start"]), _time(shard["window_end"])
    middle = start + timedelta(seconds=int((end - start).total_seconds()) // 2)
    count = connection.execute("SELECT count(*) FROM discovery_shards WHERE run_id=?", (run["id"],)).fetchone()[0]
    blocked = "shard_budget:" + reason if count + 2 > policy.max_shards else None
    if min((middle - start).total_seconds(), (end - middle).total_seconds()) < policy.min_shard_seconds:
        blocked = "unsplittable:" + reason
    if blocked:
        connection.execute("UPDATE discovery_shards SET state='partial',reason=? WHERE id=?", (blocked, shard["id"]))
        return
    connection.execute("UPDATE discovery_shards SET state='split',reason=? WHERE id=?", (reason, shard["id"]))
    # Search boundaries are inclusive. Children intentionally overlap at the midpoint.
    for a, b in ((start, middle), (middle, end)):
        a, b = timestamp(a), timestamp(b)
        child_id = _hash([run["id"], a, b])
        connection.execute("INSERT INTO discovery_shards(id,run_id,parent_id,window_start,window_end,state) VALUES (?,?,?,?,?,'pending')",
                           (child_id, run["id"], shard["id"], a, b))
        child = connection.execute("SELECT * FROM discovery_shards WHERE id=?", (child_id,)).fetchone()
        _page_job(connection, child, 1, now)


def _refresh_run(connection, run_id, now):
    run = connection.execute("SELECT * FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
    for _ in range(64):
        changed = connection.execute(
            "UPDATE discovery_shards SET state='complete' WHERE run_id=? AND state='split' "
            "AND NOT EXISTS (SELECT 1 FROM discovery_shards child WHERE child.parent_id=discovery_shards.id AND child.state!='complete')",
            (run_id,)).rowcount
        if not changed:
            break
    else:
        raise RuntimeError("discovery split tree exceeds its depth bound")
    leaves = connection.execute(
        "SELECT s.* FROM discovery_shards s WHERE s.run_id=? AND NOT EXISTS "
        "(SELECT 1 FROM discovery_shards child WHERE child.parent_id=s.id)", (run_id,)).fetchall()
    pending = [r for r in leaves if r["state"] in ("pending", "paging")]
    blocked = [r for r in leaves if r["state"] == "partial"]
    complete = bool(leaves) and all(r["state"] == "complete" for r in leaves)
    state = "complete" if complete else "partial" if blocked else "active"
    connection.execute("UPDATE discovery_runs SET state=?,finished_at=? WHERE id=?",
                       (state, timestamp(now) if not pending else None, run_id))
    pages = connection.execute("SELECT count(*) FROM discovery_pages p JOIN discovery_tasks t ON t.job_id=p.job_id WHERE t.run_id=?", (run_id,)).fetchone()[0]
    save_checkpoint(connection, source_id=run["source_id"], shard_key=run_id,
                    cursor=None if complete else {"pending": [{"shardId": r["id"], "page": r["next_page"]} for r in pending]},
                    watermark=run["window_end"] if complete else None, complete=complete, now=now,
                    details={"coverageSemantics": "search_pages_traversed_not_snapshot_guarantee", "pages": pages,
                             "blocked": [{"shardId": r["id"], "reason": r["reason"]} for r in blocked]})
    if complete and run["stream_id"]:
        stream = connection.execute("SELECT * FROM discovery_streams WHERE id=?", (run["stream_id"],)).fetchone()
        if stream["active_run_id"] == run_id:
            previous = stream["watermark"] or stream["range_start"]
            if run["window_start"] > previous or run["window_end"] < previous:
                raise ServiceError("discovery_coverage_gap", "stream completion would skip or regress coverage", 409)
            connection.execute("UPDATE discovery_streams SET watermark=?,last_success_at=?,active_run_id=NULL WHERE id=?",
                               (run["window_end"], timestamp(now), stream["id"]))
            if stream["mode"] == "historical":
                stream = connection.execute("SELECT * FROM discovery_streams WHERE id=?", (stream["id"],)).fetchone()
                _history_next(connection, stream, now, DiscoveryPolicy(**json.loads(run["policy_json"])))


def _candidate(connection, *, item, run, fetch, job_id):
    metadata = normalize_repository(item)
    repo_id = metadata["githubRepoId"]
    known = connection.execute("SELECT first_seen_at,bootstrap FROM repositories WHERE github_id=?", (repo_id,)).fetchone()
    first_seen = known["first_seen_at"] if known else fetch.observed_at
    bootstrap = known["bootstrap"] if known else 0
    from .canonical_mapping import seed_provenance
    seed_seen = seed_provenance(connection, github_id=repo_id, full_name=metadata["fullName"])
    if seed_seen:
        first_seen, bootstrap = min(first_seen, seed_seen), 1
    connection.execute(
        "INSERT INTO discovery_candidates VALUES (?,?,?,?,?,'discovered',?) ON CONFLICT(github_id) DO UPDATE SET "
        "full_name=CASE WHEN excluded.last_seen_at>=last_seen_at THEN excluded.full_name ELSE full_name END,"
        "metadata_json=CASE WHEN excluded.last_seen_at>=last_seen_at THEN excluded.metadata_json ELSE metadata_json END,"
        "last_seen_at=max(last_seen_at,excluded.last_seen_at),"
        "first_seen_at=min(first_seen_at,excluded.first_seen_at),bootstrap=max(bootstrap,excluded.bootstrap)",
        (repo_id, metadata["fullName"], first_seen, fetch.checked_at, bootstrap, encode_json(metadata)))
    connection.execute(
        "INSERT INTO discovery_hits VALUES (?,?,?,?) ON CONFLICT(github_id,source_id) DO UPDATE SET "
        "last_seen_at=max(last_seen_at,excluded.last_seen_at)",
        (repo_id, run["source_id"], fetch.observed_at, fetch.checked_at))
    connection.execute("INSERT OR IGNORE INTO discovery_page_hits VALUES (?,?)", (job_id, repo_id))


def store_page(connection, *, lease, fetch):
    task = connection.execute("SELECT * FROM discovery_tasks WHERE job_id=?", (lease.id,)).fetchone()
    run = connection.execute("SELECT * FROM discovery_runs WHERE id=?", (task["run_id"],)).fetchone()
    shard = connection.execute("SELECT * FROM discovery_shards WHERE id=?", (task["shard_id"],)).fetchone()
    page = task["page"]
    if shard["state"] not in ("pending", "paging") or page != shard["next_page"]:
        raise ServiceError("discovery_page_order", "page is no longer the shard's next page", 409)
    body = fetch.body
    total, incomplete, items = body.get("total_count"), body.get("incomplete_results"), body.get("items")
    if type(total) is not int or not 0 <= total <= 2**63 - 1 or type(incomplete) is not bool or not isinstance(items, list) or len(items) > 100:
        raise ServiceError("github_invalid_payload", "invalid search page coverage", 502)
    if _hash(body) != fetch.body_hash:
        raise ServiceError("github_cache_corrupt", "search evidence hash differs", 502)
    connection.execute("INSERT OR IGNORE INTO collection_evidence VALUES (?,?,?)",
                       (fetch.body_hash, PUBLIC_FIELDS_VERSION + ":search", encode_json(body)))
    connection.execute("INSERT INTO discovery_pages VALUES (?,?,?,?,?,?,?,?)",
                       (lease.id, fetch.body_hash, fetch.observed_at, fetch.checked_at, fetch.status,
                        total, int(incomplete), len(items)))
    for item in items:
        _candidate(connection, item=item, run=run, fetch=fetch, job_id=lease.id)
    seen = connection.execute(
        "SELECT count(DISTINCT h.github_id) FROM discovery_page_hits h JOIN discovery_tasks t ON t.job_id=h.job_id WHERE t.shard_id=?",
        (shard["id"],)).fetchone()[0]
    expected_size = min(100, max(0, total - (page - 1) * 100))
    reason = ("search_incomplete" if incomplete else "result_limit" if total > 1000 else
              "result_set_changed" if shard["expected_total"] is not None and total != shard["expected_total"] else
              "short_or_inconsistent_page" if len(items) != expected_size else
              "duplicate_results" if seen != min(total, page * 100) else None)
    connection.execute("UPDATE discovery_shards SET expected_total=coalesce(expected_total,?) WHERE id=?", (total, shard["id"]))
    if reason:
        _split(connection, shard, run, DiscoveryPolicy(**json.loads(run["policy_json"])), reason, fetch.checked_at)
    elif seen == total:
        connection.execute("UPDATE discovery_shards SET state='complete',reason=NULL WHERE id=?", (shard["id"],))
    else:
        connection.execute("UPDATE discovery_shards SET state='paging',next_page=? WHERE id=?", (page + 1, shard["id"]))
        _page_job(connection, shard, page + 1, fetch.checked_at)
    _refresh_run(connection, run["id"], fetch.checked_at)


def reconcile_dead_tasks(connection, now):
    affected = set()
    with connection:
        for row in connection.execute(
                "SELECT t.run_id,t.shard_id,j.last_error FROM discovery_tasks t JOIN jobs j ON j.id=t.job_id "
                "JOIN discovery_shards s ON s.id=t.shard_id WHERE j.state='dead' AND s.state IN ('pending','paging')").fetchall():
            connection.execute("UPDATE discovery_shards SET state='partial',reason=? WHERE id=?",
                               ("job_dead:" + (row["last_error"] or "unknown"), row["shard_id"]))
            affected.add(row["run_id"])
        for run_id in affected:
            _refresh_run(connection, run_id, now)


def run_discovery_jobs(connection, client, *, owner, max_requests, clock=utc_now, scopes=None, should_stop=None):
    reconcile_dead_tasks(connection, clock())
    def prepare(lease):
        task = connection.execute("SELECT * FROM discovery_tasks WHERE job_id=?", (lease.id,)).fetchone()
        if not task or lease.scope != task["run_id"] or lease.payload != {"runId": task["run_id"], "shardId": task["shard_id"], "page": task["page"]}:
            raise ValueError("discovery job is not bound to its stored task")
        run = connection.execute("SELECT * FROM discovery_runs WHERE id=?", (task["run_id"],)).fetchone()
        shard = connection.execute("SELECT * FROM discovery_shards WHERE id=?", (task["shard_id"],)).fetchone()
        request = GitHubRequest.search(topic=run["topic"], field=run["time_field"], start=shard["window_start"],
                                       end=shard["window_end"], page=task["page"])
        return request, lambda c, fetch: store_page(c, lease=lease, fetch=fetch)
    try:
        return run_collection_jobs(connection, client, owner=owner, max_requests=max_requests,
                                   kind="github_discovery", prepare=prepare, clock=clock, scopes=scopes, should_stop=should_stop)
    finally:
        reconcile_dead_tasks(connection, clock())


def discovery_status(connection, limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("discovery status limit must be in [1,100]")
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_runs'").fetchone():
        return {"candidates": 0, "runs": {}, "recentRuns": [], "streams": []}
    return {"candidates": connection.execute("SELECT count(*) FROM discovery_candidates").fetchone()[0],
            "runs": {r[0]: r[1] for r in connection.execute("SELECT state,count(*) FROM discovery_runs GROUP BY state")},
            "recentRuns": [dict(r) for r in connection.execute("SELECT * FROM discovery_runs ORDER BY created_at DESC,id LIMIT ?", (limit,))],
            "streams": [dict(r) for r in connection.execute("SELECT * FROM discovery_streams ORDER BY id LIMIT ?", (limit,))],
            "coverageSemantics": "search_pages_traversed_not_snapshot_guarantee"}
