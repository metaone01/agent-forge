"""Public npm daily download intervals. Never infer installs, users or cumulative counts."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlsplit

from .errors import ServiceError
from .collector import run_collection_jobs
from .github_client import CollectionError, FetchResult, _time
from .jobs import JobQueue, save_checkpoint
from .storage import encode_json, read_snapshot, timestamp, utc_now
from .resource_budget import cached_response, store_cached_response, require_collection_room

SOURCE = "npmjs.org"
IDENTITY = "npm-public-downloads-v1"
REPRESENTATION = "npm-download-range-v1"
MAX_BYTES = 128 * 1024
_NAME = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")


def package_name(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 214 or not _NAME.fullmatch(value):
        raise ValueError("an explicit supported npm package name is required")
    return value


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("a UTC calendar date is required")
    return date.fromisoformat(value)


@dataclass(frozen=True)
class DownloadsRequest:
    package: str
    start: str
    end: str

    def validate(self):
        package_name(self.package)
        days = (_day(self.end) - _day(self.start)).days + 1
        if days != 30:
            raise ValueError("this policy requires exactly 30 complete UTC days")
        return self

    @property
    def endpoint(self):
        self.validate()
        return "/downloads/range/" + self.start + ":" + self.end + "/" + quote(self.package, safe="@")

    def cache_key(self):
        return hashlib.sha256(encode_json(["api.npmjs.org", self.endpoint, IDENTITY, REPRESENTATION]).encode()).hexdigest()


def request_for_day(package, *, as_of):
    end = _time(as_of).date() - timedelta(days=1)
    return DownloadsRequest(package, (end - timedelta(days=29)).isoformat(), end.isoformat()).validate()


def normalize_downloads(body, request):
    request.validate()
    if not isinstance(body, dict) or body.get("package") != request.package or body.get("start") != request.start or body.get("end") != request.end:
        raise ValueError("download response identity or interval mismatch")
    entries = body.get("downloads")
    if not isinstance(entries, list) or len(entries) != 30:
        raise ValueError("download range is incomplete")
    days = {}
    for row in entries:
        if not isinstance(row, dict):
            raise ValueError("invalid daily downloads")
        day, count = row.get("day"), row.get("downloads")
        _day(day)
        if day in days or type(count) is not int or not 0 <= count <= 2**63 - 1:
            raise ValueError("duplicate day or invalid count")
        days[day] = count
    expected = [(_day(request.start) + timedelta(days=i)).isoformat() for i in range(30)]
    if set(days) != set(expected) or sum(days.values()) > 2**63 - 1:
        raise ValueError("download range has gaps, extra dates or an overflowing total")
    return {"package": request.package, "start": request.start, "end": request.end,
            "downloads": [{"day": day, "downloads": days[day]} for day in expected]}


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class DownloadsResponse:
    status: int
    headers: dict
    body: dict | None


class DownloadsTransport:
    def __init__(self, *, timeout=20):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            raise ValueError("invalid public downloads timeout")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_NoRedirect())

    def get(self, request, *, etag=None, modified=None):
        url = "https://api.npmjs.org" + request.endpoint
        headers = {"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "AgentForge-PublicMetadata/1"}
        for key, value in (("If-None-Match", etag), ("If-Modified-Since", modified)):
            if value:
                if not isinstance(value, str) or len(value) > 1024 or any(ord(c) < 32 for c in value):
                    raise ValueError("invalid cached response header")
                headers[key] = value
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            try:
                response = self.opener.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                if response.geturl() != url:
                    raise ValueError("public download redirects are forbidden")
                status = response.code
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES or response_headers.get("content-encoding", "identity") != "identity":
                    raise ValueError("public download response exceeds representation limits")
                body = json.loads(raw, object_pairs_hook=_json_object) if status == 200 else None
                return DownloadsResponse(status, response_headers, body)
        except (OSError, ValueError, RecursionError) as error:
            raise CollectionError("npm_transport_error", retry_at=None) from error


class DownloadsClient:
    def __init__(self, connection, *, transport=None, clock=utc_now):
        self.connection = connection
        self.transport = transport or DownloadsTransport()
        self.clock = clock

    def next_allowed_at(self, resource="downloads", *, now=None):
        if resource != "downloads":
            raise ValueError("unsupported npm request bucket")
        now = _time(self.clock() if now is None else now)
        row = self.connection.execute("SELECT * FROM rate_limits WHERE credential_identity=? AND resource='downloads'", (IDENTITY,)).fetchone()
        waits = [row[key] for key in ("next_request_at", "blocked_until") if row and row[key] and _time(row[key]) > now]
        return max(waits) if waits else None

    def fetch(self, request, *, attempt=1):
        request.validate()
        now = _time(self.clock())
        if _day(request.end) >= now.date():
            raise ValueError("only complete past UTC days may be requested")
        if self.connection.in_transaction:
            raise ValueError("network reads cannot run in a state transaction")
        try:require_collection_room(self.connection)
        except ServiceError as error:
            raise CollectionError("storage_budget_wait",retry_at=timestamp(now+timedelta(seconds=60)),attempted=False) from error
        wait = self.next_allowed_at("downloads", now=now)
        if wait:
            raise CollectionError("npm_budget_wait", retry_at=wait, attempted=False)
        with self.connection:
            self.connection.execute("INSERT INTO rate_limits(credential_identity,resource,next_request_at) VALUES (?,'downloads',?) "
                "ON CONFLICT(credential_identity,resource) DO UPDATE SET next_request_at=excluded.next_request_at",
                (IDENTITY, timestamp(now + timedelta(seconds=1))))
        key = request.cache_key()
        cache = cached_response(self.connection,key,IDENTITY,now=now)
        retry = lambda: timestamp(_time(self.clock()) + timedelta(seconds=min(3600, 60 * 2**min(max(attempt - 1, 0), 6))))
        try:
            response = self.transport.get(request, etag=cache["etag"] if cache else None, modified=cache["last_modified"] if cache else None)
        except CollectionError as error:
            raise CollectionError(error.code, retry_at=retry()) from error
        now = _time(self.clock())
        if response.status == 429:
            until = _time(retry())
            value = response.headers.get("retry-after", "")
            try:
                candidate = now + timedelta(seconds=int(value)) if value.isdigit() and len(value) <= 7 else parsedate_to_datetime(value)
                if candidate.tzinfo is not None:
                    until = max(until, candidate.astimezone(timezone.utc))
            except (ValueError, TypeError, OverflowError):
                pass
            with self.connection:
                self.connection.execute("UPDATE rate_limits SET blocked_until=? WHERE credential_identity=? AND resource='downloads'", (timestamp(until), IDENTITY))
            raise CollectionError("npm_rate_limit", retry_at=timestamp(until))
        if response.status not in (200, 304):
            retryable = response.status >= 500 or response.status in (404, 408)
            raise CollectionError("npm_not_found" if response.status == 404 else "npm_http_error", retry_at=retry() if retryable else None)
        if response.status == 304 and cache is None:
            raise CollectionError("npm_cache_miss", retry_at=retry())
        try:
            body = normalize_downloads(json.loads(cache["body_json"]) if response.status == 304 else response.body, request)
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise CollectionError("npm_invalid_payload", retry_at=retry()) from error
        encoded = encode_json(body)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        if response.status == 304 and digest != cache["body_hash"]:
            raise CollectionError("npm_cache_corrupt", retry_at=None)
        observed = cache["observed_at"] if response.status == 304 else timestamp(now)
        with self.connection:
            store_cached_response(self.connection,(key,IDENTITY,
                response.headers.get("etag") or (cache["etag"] if response.status==304 and cache else None),
                response.headers.get("last-modified") or (cache["last_modified"] if response.status==304 and cache else None),
                encoded,digest,observed,timestamp(now)),now=now)
        return FetchResult(body, observed, timestamp(now), response.status, digest)


def enqueue_downloads(connection, *, package, slot, now):
    slot = timestamp(slot)
    request = request_for_day(package, as_of=slot)
    return JobQueue(connection).enqueue(key=encode_json(["npm_downloads", request.endpoint, slot]),
        kind="npm_downloads", scope=package, payload={"package": package, "start": request.start, "end": request.end},
        now=now, due_at=slot, max_attempts=5)


def run_download_jobs(connection, client, *, owner, max_requests, clock=utc_now, job_ids=None, should_stop=None):
    def prepare(lease):
        request = DownloadsRequest(**lease.payload).validate()
        if lease.scope != request.package:
            raise ValueError("npm job scope differs from its explicit package")
        def persist(db, fetch):
            body = normalize_downloads(fetch.body, request)
            db.execute("INSERT OR IGNORE INTO collection_evidence VALUES (?,?,?)", (fetch.body_hash, REPRESENTATION, encode_json(body)))
            db.execute("INSERT INTO npm_download_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
                (lease.id, lease.id, request.package, request.start, request.end, fetch.observed_at, fetch.checked_at,
                 fetch.status, fetch.body_hash, encode_json(body)))
            watermark = timestamp(datetime.combine(_day(request.end) + timedelta(days=1), datetime.min.time(), timezone.utc))
            previous = db.execute("SELECT watermark FROM checkpoints WHERE source_id=? AND shard_key=?", (SOURCE, request.package)).fetchone()
            # A delayed historical retry is valid evidence, not a rollback of current coverage.
            if previous is None or previous["watermark"] is None or watermark >= previous["watermark"]:
                save_checkpoint(db, source_id=SOURCE, shard_key=request.package, cursor=None, watermark=watermark,
                    complete=True, now=fetch.checked_at, details={"startDay": request.start, "endDay": request.end, "observedAt": fetch.observed_at})
        return request, persist
    return run_collection_jobs(connection, client, owner=owner, max_requests=max_requests,
        kind="npm_downloads", prepare=prepare, clock=clock, job_ids=job_ids, should_stop=should_stop)


def declared_packages(record):
    """Only explicit npm distribution identities; dependencies and commands are not ownership."""
    found = {}
    for index, item in enumerate(record.get("distributions", [])):
        if item.get("type") != "npm-pkg":
            continue
        registry = item.get("registry")
        if registry and registry.rstrip("/") not in ("https://registry.npmjs.org", "https://registry.npmjs.com", "npm"):
            continue
        try:
            url = urlsplit(item.get("url", ""))
            if url.scheme != "https" or url.username or url.password or url.port not in (None, 443) or url.query or url.fragment:
                continue
            path = unquote(url.path).rstrip("/")
            if url.hostname in ("www.npmjs.com", "npmjs.com") and path.startswith("/package/"):
                name = package_name(path[len("/package/"):])
            elif url.hostname == "registry.npmjs.org":
                name = package_name(path[1:])
            else:
                continue
            found.setdefault(name, []).append({"field": f"distributions[{index}].url", "url": item["url"], "distributionId": item["id"]})
        except (ValueError, KeyError, TypeError):
            continue
    details = record.get("mcpDetails", {})
    if details.get("registryType") == "npm" and details.get("registryBaseUrl", "https://registry.npmjs.org").rstrip("/") == "https://registry.npmjs.org":
        try:
            name = package_name(details.get("identifier"))
            found.setdefault(name, []).append({"field": "mcpDetails.identifier", "identifier": name, "registry": "https://registry.npmjs.org"})
        except ValueError:
            pass
    return found


def plan_download_jobs(connection, store, *, slot, limit, now):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("npm planning requires a limit between 1 and 1000")
    manifest = store.current()
    snapshot = read_snapshot(store.root / manifest["revision"] / "catalog.db")
    names = set()
    try:
        for row in snapshot.execute("SELECT v.canonical_json FROM packages p JOIN package_versions v ON v.package_id=p.id AND v.version=p.canonical_latest"):
            names.update(declared_packages(json.loads(row[0])))
    finally:
        snapshot.close()
    pending = {row[0] for row in connection.execute("SELECT scope FROM jobs WHERE kind='npm_downloads' AND state IN ('pending','leased','retry_wait')")}
    checked = {row[0]: row[1] for row in connection.execute("SELECT package,max(checked_at) FROM npm_download_observations GROUP BY package")}
    target = request_for_day("placeholder", as_of=slot)
    collected = {row[0] for row in connection.execute("SELECT DISTINCT package FROM npm_download_observations WHERE start_day=? AND end_day=?", (target.start, target.end))}
    selected = sorted(names - pending - collected, key=lambda name: (checked.get(name, ""), name))[:limit]
    with connection:
        jobs = [enqueue_downloads(connection, package=name, slot=slot, now=now) for name in selected]
    return {"revision": manifest["revision"], "declaredNpmPackages": len(names), "pendingSkipped": len(names & pending), "alreadyCollected": len(names & collected), "jobIds": jobs, "queued": len(jobs)}


def read_download_status(path, limit=20):
    if type(limit) is not int or not 1 <= limit <= 100 or not path.is_file():
        raise ValueError("an existing state database and bounded status limit are required")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        available = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='npm_download_observations'").fetchone() is not None
        observations = [dict(row) for row in db.execute("SELECT package,start_day,end_day,observed_at,checked_at,response_status FROM npm_download_observations ORDER BY checked_at DESC,id LIMIT ?", (limit,))] if available else []
        jobs = [dict(row) for row in db.execute("SELECT id,scope,state,attempts,next_run,last_error FROM jobs WHERE kind='npm_downloads' ORDER BY created_at DESC,id LIMIT ?", (limit,))]
        return {"sourceId": SOURCE, "schemaAvailable": available, "observations": observations, "jobs": jobs}
    finally:
        db.close()
