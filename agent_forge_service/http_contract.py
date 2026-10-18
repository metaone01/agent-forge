"""Framework-independent boundary for the public, read-only HTTP API."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import queue
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Iterable

from .errors import ServiceError
from .query import Query, QueryService
from .settings import Settings
from .operational_log import error_kind
from .storage import encode_json

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpLimits:
    max_in_flight: int = 4
    max_query_bytes: int = 16384
    max_response_bytes: int = 2 * 1024 * 1024
    max_cache_entries: int = 128
    max_cache_bytes: int = 8 * 1024 * 1024
    cache_ttl_seconds: float = 5.0
    rate_per_second: float = 20.0
    rate_burst: int = 80

    def __post_init__(self):
        ranges = {"max_in_flight": (1, 32), "max_query_bytes": (1024, 131072),
                  "max_response_bytes": (1024, 16 * 1024 * 1024),
                  "max_cache_entries": (1, 4096), "max_cache_bytes": (1024, 128 * 1024 * 1024),
                  "rate_burst": (1, 10000)}
        for name, (minimum, maximum) in ranges.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("invalid HTTP limit: " + name)
        for name, maximum in (("cache_ttl_seconds", 30), ("rate_per_second", 10000)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError("invalid HTTP limit: " + name)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]


@dataclass(frozen=True)
class _Cached:
    body: bytes
    etag: str
    expires: float


@dataclass(frozen=True)
class _Request:
    endpoint: str
    package_id: str | None
    parameters: dict
    binding: dict
    query: Query | None = None


def _text(value: str, label: str, maximum: int) -> str:
    if not value or len(value) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ServiceError("invalid_query", "invalid " + label)
    return value


def _integer(value: str, label: str, maximum: int, minimum: int = 0) -> int:
    if not re.fullmatch(r"[0-9]{1,19}", value) or not minimum <= int(value) <= maximum:
        raise ServiceError("invalid_query", "invalid " + label)
    return int(value)


def _facet_json(value: str) -> dict:
    def object_pairs(pairs):
        result = {}
        for key, entry in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = entry
        return result

    def nonfinite(_):
        raise ValueError("nonfinite JSON")

    try:
        result = json.loads(value, object_pairs_hook=object_pairs, parse_constant=nonfinite)
        if not isinstance(result, dict):
            raise ValueError("expected object")
        return result
    except (ValueError, TypeError, RecursionError) as error:
        raise ServiceError("invalid_query", "invalid facet JSON") from error


def parse_request(path: str, pairs: Iterable[tuple[str, str]], settings: Settings, limits: HttpLimits) -> _Request:
    if not isinstance(path, str) or len(path.encode("utf-8")) > 16384 or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise ServiceError("invalid_request", "invalid request path")
    fixed = {"/health/live": "live", "/health/ready": "ready", "/api/v1/packages": "packages",
             "/api/v1/changes": "changes", "/api/v1/facets": "facets",
             "/api/v1/recommendations": "curated", "/api/v1/recommendation-sources": "curated_sources", "/api/v1/sources": "sources", "/api/v1/source-controls": "source_controls", "/api/v1/releases": "release_list", "/api/v1/manifest": "manifest", "/api/v1/dashboard": "dashboard"}
    endpoint, package_id = fixed.get(path), None
    if endpoint is None and path.startswith("/api/v1/packages/"):
        package_id = path[len("/api/v1/packages/"):]
        endpoint = "detail"
        # Explicit /detail also makes IDs ending in /metrics or /versions addressable.
        for suffix in ("detail", "versions", "metrics", "recommendations", "releases"):
            if package_id.endswith("/" + suffix):
                endpoint = suffix
                package_id = package_id[:-(len(suffix) + 1)]
                break
        _text(package_id, "package id", 4096)
        if any(c.isspace() or c == "\\" for c in package_id):
            raise ServiceError("invalid_query", "invalid package id")
    elif endpoint is None and path.startswith("/api/v1/discoveries/"):
        endpoint, package_id = "discovery", path[len("/api/v1/discoveries/"):]
        _text(package_id, "discovery id", 4096)
    if endpoint is None:
        raise ServiceError("not_found", "route not found", 404)
    allowed = {"packages": {"q", "agent", "type", "subtype", "scope", "sort", "limit", "facets", "customFacets", "metricSource", "recommendationSource", "reviewDisposition", "cursor", "revision"},
               "detail": {"revision", "version"}, "versions": {"revision", "limit", "cursor"},
               "metrics": {"revision", "limit", "cursor", "source"}, "changes": {"revision", "limit", "cursor", "since"},
               "facets": {"revision", "scope"}, "sources": {"revision"}, "manifest": {"revision"}, "dashboard": {"revision", "agent"},
               "live": set(), "ready": set(), "recommendations": {"revision", "limit", "cursor", "source", "status", "version"}, "curated": {"revision", "limit", "cursor", "source", "status"}, "curated_sources": {"revision", "limit", "cursor"}, "source_controls": {"revision", "limit", "cursor"}, "release_list":{"revision","limit","cursor","repositoryId"},"releases":{"revision","limit","cursor","repositoryId","version","association"}, "discovery": {"revision"}}[endpoint]
    parameters = {}
    size = 0
    for key, value in pairs:
        if not isinstance(key, str) or not isinstance(value, str):
            raise ServiceError("invalid_query", "query parameters must be strings")
        size += len(key.encode("utf-8")) + len(value.encode("utf-8")) + 2
        if len(parameters) >= 32 or size > limits.max_query_bytes:
            raise ServiceError("request_too_large", "query is too large", 414)
        if key not in allowed or key in parameters:
            raise ServiceError("invalid_query", "unknown or duplicate query parameter")
        parameters[key] = _text(value, "query parameter", 65536 if key == "cursor" else limits.max_query_bytes) if key != "q" else value
    if "limit" in parameters or endpoint in ("packages", "versions", "metrics", "changes", "recommendations", "curated", "curated_sources", "source_controls", "release_list", "releases"):
        parameters["limit"] = _integer(parameters.get("limit", "20"), "limit", settings.max_page_size, 1)
    if endpoint == "changes":
        parameters["since"] = _integer(parameters.get("since", "0"), "since", 9223372036854775807)
    query = None
    if endpoint == "packages":
        query = Query(q=parameters.get("q"), agent=parameters.get("agent"), type=parameters.get("type"),
                      subtype=parameters.get("subtype"), scope=parameters.get("scope", "catalog"),
                      sort=parameters.get("sort"), limit=parameters["limit"],
                      facets=_facet_json(parameters.get("facets", "{}")),
                      custom_facets=_facet_json(parameters.get("customFacets", "{}")),
                      metric_source=parameters.get("metricSource"), recommendation_source=parameters.get("recommendationSource"), review_disposition=parameters.get("reviewDisposition"))
        binding = query.normalized(settings)
        binding.update({key: parameters[key] for key in ("revision", "cursor") if key in parameters})
    else:
        binding = dict(parameters)
    if endpoint == "metrics":
        source = parameters.setdefault("source", "github.com")
        if source not in ("github.com", "npmjs.org"):
            raise ServiceError("invalid_query", "unsupported metric source")
        binding["source"] = source
    if "version" in parameters:
        _text(parameters["version"], "version", 1024)
    return _Request(endpoint, package_id, parameters, binding, query)


class HttpReadService:
    """Bounded queries and a short-lived byte cache; never opens private state."""

    def __init__(self, settings: Settings, limits: HttpLimits | None = None, *, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.limits = limits or HttpLimits()
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens = float(self.limits.rate_burst)
        self._last_refill = clock()
        self._cache: OrderedDict[str, _Cached] = OrderedDict()
        self._cache_bytes = 0
        self._services: queue.LifoQueue = queue.LifoQueue(self.limits.max_in_flight)
        for _ in range(self.limits.max_in_flight):
            self._services.put_nowait(QueryService(settings))

    def error_response(self, code: str, status: int, *, request_id: str | None = None) -> HttpResponse:
        request_id = request_id or uuid.uuid4().hex
        messages = {"agent_not_found": "Agent is not present in this revision.", "not_found": "Resource not found.", "version_not_found": "Canonical version not found.",
                    "invalid_query": "Invalid query parameters.", "invalid_cursor": "Invalid pagination cursor.",
                    "invalid_revision": "Invalid revision.", "cursor_mismatch": "Cursor does not match this query or revision.",
                    "cursor_expired": "Cursor revision has been retired.", "revision_unavailable": "Revision is unavailable.",
                    "query_timeout": "Query exceeded its execution budget.", "snapshot_unavailable": "Published snapshot is unavailable.",
                    "search_unavailable": "Search index is unavailable.",
                    "retention_unavailable": "Snapshot retirement metadata is unavailable.",
                    "snapshot_busy": "Snapshot is temporarily busy.",
                    "scope_unavailable": "Requested scope is unavailable.", "sort_unavailable": "Requested sort is unavailable.",
                    "reviews_unavailable": "Metadata review is unavailable in this revision.",
                    "releases_unavailable": "Release observations are unavailable in this revision.",
                    "source_controls_unavailable": "Collection controls are unavailable in this revision.",
                    "recommendations_unavailable": "Recommendation projection is unavailable.",
                    "recommendation_source_not_found": "Recommendation source was not found in this revision.",
                    "discovery_unavailable": "Discovery projection is unavailable.",
                    "discovery_not_found": "Discovery candidate was not found.",
                    "rate_limited": "Request rate exceeded.", "service_busy": "Query concurrency exceeded.",
                    "request_too_large": "Request is too large.", "response_too_large": "Response exceeds the service limit.",
                    "invalid_request": "Invalid request.", "method_not_allowed": "Only GET is supported.",
                    "internal_error": "The request could not be completed."}
        if code not in messages:
            code, status = "internal_error", 500
        body = encode_json({"error": {"code": code, "message": messages[code]}, "requestId": request_id}).encode("utf-8")
        headers = self._headers(request_id)
        headers["Cache-Control"] = "no-store"
        if code in ("service_busy", "rate_limited", "query_timeout", "snapshot_unavailable", "search_unavailable", "retention_unavailable", "snapshot_busy"):
            headers["Retry-After"] = str(max(1, math.ceil(1 / self.limits.rate_per_second))) if code == "rate_limited" else "1"
        if status == 405:
            headers["Allow"] = "GET"
        return HttpResponse(status, body, headers)

    @staticmethod
    def _headers(request_id):
        return {"Content-Type": "application/json; charset=utf-8", "X-Content-Type-Options": "nosniff", "X-Request-ID": request_id}

    def _rate_limit(self):
        with self._lock:
            now = self._clock()
            elapsed = max(0, now - self._last_refill)
            self._last_refill = now
            self._tokens = min(float(self.limits.rate_burst), self._tokens + elapsed * self.limits.rate_per_second)
            if self._tokens < 1:
                raise ServiceError("rate_limited", "request rate exceeded", 429)
            self._tokens -= 1

    def _cached(self, key):
        with self._lock:
            entry = self._cache.get(key)
            if entry and entry.expires <= self._clock():
                self._cache_bytes -= len(entry.body)
                del self._cache[key]
                return None
            if entry:
                self._cache.move_to_end(key)
            return entry

    def _remember(self, key, entry):
        if len(entry.body) > self.limits.max_cache_bytes:
            return
        with self._lock:
            previous = self._cache.pop(key, None)
            if previous:
                self._cache_bytes -= len(previous.body)
            self._cache[key] = entry
            self._cache_bytes += len(entry.body)
            while len(self._cache) > self.limits.max_cache_entries or self._cache_bytes > self.limits.max_cache_bytes:
                _, removed = self._cache.popitem(last=False)
                self._cache_bytes -= len(removed.body)

    @staticmethod
    def _execute(service, request, revision):
        params = request.parameters
        endpoint = request.endpoint
        if endpoint == "packages":
            return service.packages(request.query, cursor=params.get("cursor"), revision=revision)
        if endpoint == "detail":
            return service.package(request.package_id, revision=revision, version=params.get("version"))
        if endpoint == "versions":
            return service.versions(request.package_id, limit=params["limit"], cursor=params.get("cursor"), revision=revision)
        if endpoint == "metrics":
            return service.metrics(request.package_id, limit=params["limit"], cursor=params.get("cursor"), source=params["source"], revision=revision)
        if endpoint == "changes":
            return service.changes(since=params["since"], limit=params["limit"], cursor=params.get("cursor"), revision=revision)
        if endpoint == "dashboard":
            return service.dashboard(agent=params.get("agent"), revision=revision)
        if endpoint == "facets":
            return service.facets(revision=revision, scope=params.get("scope", "catalog"))
        if endpoint == "discovery":
            return service.discovery(request.package_id, revision=revision)
        if endpoint == "curated_sources":
            return service.recommendation_sources(limit=params["limit"], cursor=params.get("cursor"), revision=revision)
        if endpoint in ("recommendations", "curated"):
            return service.recommendations(request.package_id, limit=params["limit"], cursor=params.get("cursor"), source=params.get("source"), status=params.get("status", "all"), version=params.get("version"), revision=revision)
        if endpoint in ("releases","release_list"):
            return service.releases(request.package_id,repository_id=params.get("repositoryId"),version=params.get("version"),association=params.get("association","all"),limit=params["limit"],cursor=params.get("cursor"),revision=revision)
        if endpoint == "source_controls":
            return service.source_controls(limit=params["limit"],cursor=params.get("cursor"),revision=revision)
        if endpoint == "sources":
            return service.sources(revision=revision)
        if endpoint == "manifest":
            manifest = service.store.load(revision)
            return {**manifest, **service._envelope(manifest)}
        raise AssertionError("unhandled read endpoint")

    def handle(self, method: str, path: str, pairs: Iterable[tuple[str, str]] = (), *, if_none_match: str | None = None) -> HttpResponse:
        request_id = uuid.uuid4().hex
        service = None
        try:
            if method != "GET":
                raise ServiceError("method_not_allowed", "only GET is supported", 405)
            if if_none_match is not None and (not isinstance(if_none_match, str) or len(if_none_match) > 4096 or any(ord(c) < 32 for c in if_none_match)):
                raise ServiceError("invalid_request", "invalid conditional request header")
            if path != "/health/live":
                self._rate_limit()
            request = parse_request(path, pairs, self.settings, self.limits)
            if request.endpoint == "live":
                return HttpResponse(200, b'{"status":"ok"}', {**self._headers(request_id), "Cache-Control": "no-store"})
            try:
                service = self._services.get_nowait()
            except queue.Empty as error:
                raise ServiceError("service_busy", "query concurrency exceeded", 503) from error
            # A service belongs to one admitted request; its verified-revision cache is never used concurrently.
            manifest = service.resolve_snapshot(revision=request.parameters.get("revision"), cursor=request.parameters.get("cursor"))
            revision = manifest["revision"]
            if request.endpoint == "ready":
                body = encode_json({"status": "ready", "revision": revision}).encode("utf-8")
                return HttpResponse(200, body, {**self._headers(request_id), "Cache-Control": "no-store"})
            key = hashlib.sha256(encode_json({"contract": "http-read-v1", "endpoint": request.endpoint,
                "id": request.package_id, "revision": revision, "query": request.binding, "manifest": {name: value for name, value in manifest.items() if not name.startswith("_")}}).encode("utf-8")).hexdigest()
            entry = self._cached(key)
            if entry is None:
                started = self._clock()
                body = encode_json(self._execute(service, request, revision)).encode("utf-8")
                if len(body) > self.limits.max_response_bytes:
                    raise ServiceError("response_too_large", "response exceeds the service limit", 503)
                etag = '"' + hashlib.sha256(key.encode("ascii") + body).hexdigest() + '"'
                entry = _Cached(body, etag, started + self.limits.cache_ttl_seconds)
                self._remember(key, entry)
            remaining = max(0, math.floor(entry.expires - self._clock()))
            if manifest.get("_cursorDeadline") is not None:
                from .retention import retirement_max_age
                remaining = min(remaining, retirement_max_age(manifest["_cursorDeadline"]))
            headers = {**self._headers(request_id), "ETag": entry.etag,
                       "Cache-Control": f"public, max-age={remaining}, must-revalidate"}
            matches = if_none_match is not None and any(part.strip() in ("*", entry.etag, "W/" + entry.etag) for part in if_none_match.split(","))
            return HttpResponse(304 if matches else 200, b"" if matches else entry.body, headers)
        except ServiceError as error:
            return self.error_response(error.code, error.status, request_id=request_id)
        except Exception as error:
            LOGGER.error("read API request failed requestId=%s errorKind=%s", request_id, error_kind(error))
            return self.error_response("internal_error", 500, request_id=request_id)
        finally:
            if service is not None:
                # Old revisions can be reverified when revisited, rather than growing process memory forever.
                if len(service.store._verified) > 8:
                    service.store._verified.clear()
                self._services.put_nowait(service)
