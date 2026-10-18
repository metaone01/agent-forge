"""Public curated-list observations; listing, package identity and endorsement stay distinct."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .collector import run_collection_jobs
from .declarations import strict_json
from .errors import ServiceError
from .github import API_VERSION, GitHubRequest, _execute, credential_identity, parse_response
from .github_client import GitHubClient
from .jobs import JobQueue
from .public_git import PublicGitRequest, sanitize_object
from .storage import encode_json, timestamp, utc_now

POLICY = "public-curated-lists-v1"
REPRESENTATION = "github-public-curated-git-v1"
MAX_LIST_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 10000
_LINK = re.compile(r"^\s*[-*+]\s+(?:\*\*)?\[([^\]\r\n]{1,200})\]\((https://[^\s)]+)\)")
_BULLET = re.compile(r"^\s*[-*+]\s+")
_NAVIGATION = re.compile(r"\]\(#[A-Za-z0-9_-]+\)\s*$")


def _key(value):
    return hashlib.sha256(encode_json(value).encode("utf-8")).hexdigest()


def _text(value, label, maximum=500):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("invalid " + label)
    return value


def validate_source(value):
    required = {"schemaVersion", "provider", "repositoryId", "path", "label", "selectionStatement", "startHeading", "endHeading", "minEntries", "maxEntries", "emptyStatement"}
    if not isinstance(value, dict) or set(value) != required or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1 or value["provider"] != "github-curated-markdown-v1":
        raise ValueError("unsupported recommendation source definition")
    GitHubRequest.repository(value["repositoryId"]).validate()
    if type(value["repositoryId"]) is not int:
        raise ValueError("source requires a stable public repository ID")
    path = _text(value["path"], "public list path", 1024)
    if not path.endswith(".md") or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in (".", "..", ".git") for part in path.split("/")):
        raise ValueError("source path must be a literal Markdown file")
    _text(value["label"], "source label", 200)
    statement = _text(value["selectionStatement"], "public selection statement")
    if not re.fullmatch(r"(?:A|An|The) curated (?:list|collection) of .+", statement, re.I):
        raise ValueError("source must explicitly describe its curated selection")
    for key in ("startHeading", "endHeading"):
        if not _text(value[key], key).startswith("## "):
            raise ValueError("source boundaries must be explicit second-level headings")
    if value["startHeading"] == value["endHeading"]:
        raise ValueError("source boundaries must differ")
    if any(type(value[key]) is not int for key in ("minEntries", "maxEntries")) or not 1 <= value["minEntries"] <= value["maxEntries"] <= MAX_ENTRIES:
        raise ValueError("source requires explicit entry bounds")
    if value["emptyStatement"] is not None:
        _text(value["emptyStatement"], "explicit empty-list statement")
    return dict(value)


def source_identity(definition):
    return "recommendation-source:" + _key(validate_source(definition))


def load_source(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("source definition must be a bounded regular JSON file")
    return validate_source(strict_json(path.read_bytes(), maximum=16384))


def _target(value):
    _text(value, "public selection link", 2000)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.username or parsed.password:
        raise ValueError("curated adapter requires public HTTPS GitHub links")
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2 or any(not re.fullmatch(r"[A-Za-z0-9_.+-]+", part) or part in (".", "..") for part in parts):
        raise ValueError("unsafe curated GitHub target")
    GitHubRequest.repository("/".join(parts[:2])).validate()
    if len(parts) > 2 and (parts[2] not in ("tree", "blob") or len(parts) < (4 if parts[2] == "tree" else 5)):
        raise ValueError("unsupported curated GitHub path")
    if parsed.query not in ("", "tab=readme-ov-file") or parsed.fragment and not re.fullmatch(r"[A-Za-z0-9_-]+", parsed.fragment):
        raise ValueError("unsupported public selection URL parameters")
    # README navigation does not change the selected repository/path identity; original links remain evidence.
    parts[:2] = [part.lower() for part in parts[:2]]
    return urlunsplit(("https", "github.com", "/" + "/".join(parts), "", ""))


def parse_curated_list(raw, definition):
    definition = validate_source(definition)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_LIST_BYTES:
        raise ValueError("curated list exceeds the byte budget")
    text = raw.decode("utf-8-sig")
    if any(ord(ch) < 32 and ch not in "\r\n\t" or ord(ch) == 127 for ch in text):
        raise ValueError("curated list contains control characters")
    if text.count("<!--") != text.count("-->"):
        raise ValueError("curated list has an unterminated comment")
    text = re.sub(r"<!--.*?-->", lambda match: "\n" * match.group().count("\n"), text, flags=re.S)
    lines = text.splitlines()
    if definition["selectionStatement"] not in [line.strip() for line in lines]:
        raise ValueError("public curated-selection statement is missing")
    boundaries = []
    for key in ("startHeading", "endHeading"):
        hits = [i for i, line in enumerate(lines) if line.strip() == definition[key]]
        if len(hits) != 1:
            raise ValueError("curated section boundary is missing or ambiguous")
        boundaries.append(hits[0])
    start, end = boundaries
    if start >= end:
        raise ValueError("curated section order is invalid")
    members, entries, fence, section = {}, 0, None, definition["startHeading"]
    for index in range(start + 1, end):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        if stripped.startswith("## "):
            raise ValueError("unexpected second-level heading in the curated scope")
        if stripped.startswith("### "):
            section = _text(stripped, "curated section")
        if not _BULLET.match(line):
            continue
        match = _LINK.match(line)
        if not match:
            if _NAVIGATION.search(line):
                continue
            raise ValueError("unrecognized curated-list entry; complete coverage cannot be claimed")
        target = _target(match.group(2))
        entries += 1
        if entries > definition["maxEntries"]:
            raise ValueError("curated list exceeds the entry budget")
        item = members.setdefault(target, {"targetUrl": target, "name": match.group(1), "positions": [], "sourceRank": None})
        if len(item["positions"]) >= 100:
            raise ValueError("curated link occurs too many times")
        item["positions"].append({"line": index + 1, "section": section, "url": match.group(2)})
    if fence is not None:
        raise ValueError("curated list has an unterminated code fence")
    explicit_empty = not entries and definition["emptyStatement"] is not None and definition["emptyStatement"] in [line.strip() for line in lines[start + 1:end]]
    if entries < definition["minEntries"] and not explicit_empty:
        raise ValueError("curated list is unexpectedly empty or below its entry bound")
    return {"members": sorted(members.values(), key=lambda item: item["targetUrl"]), "entryCount": entries, "complete": True, "claimKind": "curated_listing", "sourceRank": None, "scope": {"startLine": start + 1, "endLine": end + 1, "lineCount": len(lines)}}


@dataclass(frozen=True)
class RecommendationGitRequest(PublicGitRequest):
    def cache_key(self, identity):
        self.validate()
        return _key([REPRESENTATION, API_VERSION, self.endpoint, credential_identity(identity), MAX_LIST_BYTES])


def _sanitize(body, request):
    return sanitize_object(body, request, max_blob_bytes=MAX_LIST_BYTES)


class GhRecommendationTransport:
    def __init__(self, *, runner=None):
        self.runner = runner or _execute

    def get(self, request, *, etag=None, modified=None):
        if not isinstance(request, RecommendationGitRequest) or request.kind not in ("repository", "commit", "tree", "blob"):
            raise ValueError("curated transport only accepts allowlisted public Git metadata")
        request.validate()
        argv = ["gh", "api", "--hostname", "github.com", "-X", "GET", "--include", "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: " + API_VERSION]
        for name, value in (("If-None-Match", etag), ("If-Modified-Since", modified)):
            if value is not None:
                argv += ["-H", name + ": " + _text(value, "conditional request header", 1024)]
        argv.append(request.endpoint)
        env = dict(os.environ)
        for key in ("GH_DEBUG", "GH_FORCE_TTY", "GH_HTTP_UNIX_SOCKET"):
            env.pop(key, None)
        env.update(GH_PROMPT_DISABLED="1", GH_PAGER="")
        code, output = self.runner(argv, env, 30, 4 * 1024 * 1024)
        if not isinstance(output, bytes) or len(output) > 4 * 1024 * 1024:
            raise ServiceError("github_response_too_large", "curated metadata response exceeds the byte budget", 502)
        return parse_response(output, code, request, body_sanitizer=_sanitize)


class RecommendationClient(GitHubClient):
    def __init__(self, connection, *, identity, transport=None, **kwargs):
        super().__init__(connection, identity=identity, transport=transport or GhRecommendationTransport(), **kwargs)

    def _validate_body(self, body, request):
        if not isinstance(request, RecommendationGitRequest):
            raise ValueError("curated client requires its isolated request representation")
        try:
            if _sanitize(body, request) != body:
                raise ValueError("noncanonical curated Git evidence")
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ServiceError("github_invalid_payload", "cached curated Git response is invalid", 502) from error

    def fetch(self, request, *, attempt=1):
        if not isinstance(request, RecommendationGitRequest):
            raise ValueError("curated client cannot use other request types")
        return super().fetch(request, attempt=attempt)


def _enqueue_step(connection, scan_id, step, now):
    queue = JobQueue(connection)
    job_id = queue.enqueue(key="recommendation:" + scan_id + ":" + str(step), kind="github_recommendation", scope="recommendation:" + scan_id, payload={"scanId": scan_id, "step": step}, now=now, due_at=now)
    connection.execute("INSERT OR IGNORE INTO recommendation_tasks VALUES (?,?,?)", (job_id, scan_id, step))


def enqueue_recommendation(connection, *, definition, slot, now):
    definition = validate_source(definition)
    source_id = source_identity(definition)
    now, slot = timestamp(now), timestamp(slot)
    connection.execute("INSERT OR IGNORE INTO recommendation_sources VALUES (?,?,?)", (source_id, encode_json(definition), now))
    scan_id = _key([POLICY, source_id, slot])
    connection.execute("INSERT OR IGNORE INTO recommendation_scans VALUES (?,?,?,'queued',0,?,?,NULL,NULL)", (scan_id, source_id, slot, encode_json({"stage": "repository"}), now))
    _enqueue_step(connection, scan_id, 0, max(slot, now))
    return scan_id


def _request(definition, progress):
    stage = progress["stage"]
    if stage in ("repository", "verify"):
        return RecommendationGitRequest("repository", definition["repositoryId"])
    if stage == "commit":
        return RecommendationGitRequest("commit", progress["fullName"], progress["branch"])
    if stage in ("tree", "blob"):
        return RecommendationGitRequest(stage, progress["fullName"], progress["tree" if stage == "tree" else "blob"])
    raise ValueError("invalid persisted curated scan stage")


def _snapshot(connection, scan, progress, now):
    source_id = scan["source_id"]
    previous = connection.execute("SELECT * FROM recommendation_snapshots WHERE source_id=? AND applied=1 ORDER BY selection_at DESC,completed_at DESC,id DESC LIMIT 1", (source_id,)).fetchone()
    if previous and previous["selection_at"] == progress["selectionAt"] and previous["commit_sha"] != progress["commit"]:
        raise ValueError("conflicting curated selection observations at the same time")
    applied = previous is None or progress["selectionAt"] > previous["selection_at"]
    snapshot_id = _key([POLICY, scan["id"], progress["commit"], progress["contentHash"]])
    old_id = previous["id"] if applied and previous else None
    parsed = progress["parsed"]
    connection.execute("INSERT INTO recommendation_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (snapshot_id, scan["id"], source_id, progress["fullName"], progress["commit"], progress["blob"], progress["contentHash"], progress["selectionAt"], progress["blobObservedAt"], progress["blobCheckedAt"], now, len(parsed["members"]), parsed["entryCount"], old_id, int(applied)))
    for member in parsed["members"]:
        connection.execute("INSERT INTO recommendation_members VALUES (?,?,?,?,NULL)", (snapshot_id, member["targetUrl"], member["name"], encode_json(member["positions"])))
    if applied:
        old_targets = {row[0] for row in connection.execute("SELECT target_url FROM recommendation_members WHERE snapshot_id=?", (old_id,))} if old_id else set()
        targets = {member["targetUrl"] for member in parsed["members"]}
        for target in sorted(old_targets | targets):
            status = "active" if target in targets else "withdrawn"
            reason = "complete_snapshot_membership" if status == "active" else "complete_snapshot_absence"
            evidence = {"policy": POLICY, "claimKind": "curated_listing", "snapshotId": snapshot_id, "previousSnapshotId": old_id, "sourceRank": None, "withdrawalMeaning": "source-list-membership-only", "packageEndorsement": "not_inferred"}
            connection.execute("INSERT INTO recommendation_observations VALUES (?,?,?,?,?,?,?,?,?)", (_key([source_id, target, snapshot_id]), source_id, target, status, snapshot_id, old_id, reason, progress["selectionAt"], encode_json(evidence)))
    return snapshot_id


def _persist(connection, scan, definition, progress, lease, fetch):
    now = fetch.checked_at
    evidence_hash = _key([REPRESENTATION, fetch.body_hash])
    connection.execute("INSERT OR IGNORE INTO collection_evidence VALUES (?,?,?)", (evidence_hash, REPRESENTATION, encode_json(fetch.body)))
    connection.execute("INSERT INTO recommendation_fetches VALUES (?,?,?,?,?,?)", (lease.id, evidence_hash, _request(definition, progress).endpoint, fetch.observed_at, now, fetch.status))
    stage, body = progress["stage"], fetch.body
    state = "running"
    if stage == "repository":
        progress.update(stage="commit", fullName=body["full_name"], branch=body["default_branch"])
    elif stage == "commit":
        progress.update(stage="tree", commit=body["sha"], selectionAt=now, tree=body["commit"]["tree"]["sha"], remaining=definition["path"].split("/"))
    elif stage == "tree":
        found = next((item for item in body["tree"] if item["path"] == progress["remaining"][0]), None)
        if found is None:
            raise ValueError("curated path is absent; this is not a withdrawal snapshot")
        if len(progress["remaining"]) > 1:
            if found["mode"] != "040000" or found["type"] != "tree":
                raise ValueError("curated path traverses a non-directory")
            progress.update(tree=found["sha"], remaining=progress["remaining"][1:])
        else:
            if found["mode"] not in ("100644", "100755") or found["type"] != "blob" or "size" not in found or not 0 < found["size"] <= MAX_LIST_BYTES:
                raise ValueError("curated list is not a bounded regular Git file")
            progress.update(stage="blob", blob=found["sha"], size=found["size"])
    elif stage == "blob":
        raw = base64.b64decode(body["content"], validate=True)
        if len(raw) != progress["size"]:
            raise ValueError("curated tree/blob length mismatch")
        parsed = parse_curated_list(raw, definition)
        progress.update(stage="verify", parsed=parsed, contentHash=hashlib.sha256(raw).hexdigest(), blobObservedAt=fetch.observed_at, blobCheckedAt=now)
    elif stage == "verify":
        if body["full_name"].lower() != progress["fullName"].lower():
            raise ValueError("curated repository alias changed during the pinned read")
        progress["snapshotId"] = _snapshot(connection, scan, progress, now)
        # Member rows already persist the parsed metadata; avoid retaining a second large copy in scan status.
        progress.pop("parsed")
        state = "complete"
    step = scan["step"] + 1
    connection.execute("UPDATE recommendation_scans SET state=?,step=?,progress_json=?,finished_at=?,reason=NULL WHERE id=?", (state, step, encode_json(progress), now if state == "complete" else None, scan["id"]))
    if state == "running":
        _enqueue_step(connection, scan["id"], step, now)


def run_recommendation_jobs(connection, client, *, owner, max_requests, clock=utc_now, should_stop=None, scan_ids=None):
    if not isinstance(client, RecommendationClient):
        raise ValueError("curated collection requires the isolated read-only gh client")
    if scan_ids is not None and (not isinstance(scan_ids, tuple) or len(scan_ids) > 1000 or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in scan_ids)):
        raise ValueError("curated scan selection must be a bounded tuple of stable IDs")
    scopes = tuple("recommendation:" + value for value in scan_ids) if scan_ids is not None else None
    def prepare(lease):
        scan = connection.execute("SELECT * FROM recommendation_scans WHERE id=?", (lease.payload.get("scanId"),)).fetchone()
        if not scan or scan["state"] not in ("queued", "running") or lease.payload.get("step") != scan["step"]:
            raise ValueError("curated task is stale or invalid")
        definition = validate_source(json.loads(connection.execute("SELECT definition_json FROM recommendation_sources WHERE id=?", (scan["source_id"],)).fetchone()[0]))
        if source_identity(definition) != scan["source_id"]:
            raise ValueError("curated source definition changed without a new identity")
        progress = json.loads(scan["progress_json"])
        return _request(definition, progress), lambda c, fetch: _persist(c, scan, definition, progress, lease, fetch)
    result = run_collection_jobs(connection, client, owner=owner, max_requests=max_requests, kind="github_recommendation", prepare=prepare, clock=clock, should_stop=should_stop, scopes=scopes)
    with connection:
        connection.execute("UPDATE recommendation_scans SET state='failed',reason='terminal_job_failure',finished_at=? WHERE state IN ('queued','running') AND EXISTS (SELECT 1 FROM recommendation_tasks t JOIN jobs j ON j.id=t.job_id WHERE t.scan_id=recommendation_scans.id AND j.state='dead')", (timestamp(clock()),))
    result.update(policy=POLICY, canonicalWrites=0, nextNetworkAt=client.next_allowed_at("core", now=clock()))
    return result


def recommendation_status(connection, *, limit=20):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("curated status limit must be between 1 and 100")
    return {"policy": POLICY, "canonicalWrites": 0, "sources": [dict(row) for row in connection.execute("SELECT id,definition_json,created_at FROM recommendation_sources ORDER BY id LIMIT ?", (limit,))], "scans": [dict(row) for row in connection.execute("SELECT id,source_id,slot,state,step,created_at,finished_at,reason FROM recommendation_scans ORDER BY created_at DESC,id LIMIT ?", (limit,))], "snapshots": [dict(row) for row in connection.execute("SELECT id,source_id,repository_name,commit_sha,blob_sha,content_hash,selection_at,completed_at,member_count,entry_count,applied FROM recommendation_snapshots ORDER BY selection_at DESC,completed_at DESC,id LIMIT ?", (limit,))], "observations": {row[0]: row[1] for row in connection.execute("SELECT status,count(*) FROM recommendation_observations GROUP BY status")}}


def read_recommendation_status(path, *, limit=20):
    if path.is_symlink() or not path.is_file():
        raise ValueError("curated state must already exist")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='recommendation_sources'").fetchone():
            return {"available": False, "sources": [], "canonicalWrites": 0}
        return recommendation_status(connection, limit=limit)
    finally:
        connection.close()
