"""Persistent conditional requests and conservative, separate rate-limit buckets."""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .errors import ServiceError
from .github import GhTransport, GitHubRequest, credential_identity, _public
from .storage import encode_json, timestamp, utc_now
from .resource_budget import cached_response, store_cached_response, require_collection_room


def _time(value):
    return datetime.fromisoformat(timestamp(value).replace("Z", "+00:00"))


class CollectionError(ServiceError):
    def __init__(self, code: str, *, retry_at: str | None, attempted: bool = True):
        super().__init__(code, "public metadata collection did not complete", 503)
        self.retry_at = retry_at
        self.attempted = attempted


@dataclass(frozen=True)
class RequestPolicy:
    core_spacing_seconds: float = 1
    search_spacing_seconds: float = 6
    reserve_fraction: float = 0.2
    retry_base_seconds: float = 60
    retry_max_seconds: float = 3600
    jitter_fraction: float = 0.2

    def __post_init__(self):
        values = (self.core_spacing_seconds, self.search_spacing_seconds, self.reserve_fraction,
                  self.retry_base_seconds, self.retry_max_seconds, self.jitter_fraction)
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
            raise ValueError("request policy must use finite numbers")
        if not 0.1 <= self.core_spacing_seconds <= 3600 or not 1 <= self.search_spacing_seconds <= 3600:
            raise ValueError("request spacing is outside the conservative bounds")
        if not 0 <= self.reserve_fraction < 1 or not 1 <= self.retry_base_seconds <= self.retry_max_seconds <= 86400 or not 0 <= self.jitter_fraction <= 1:
            raise ValueError("invalid retry or reserve policy")


@dataclass(frozen=True)
class FetchResult:
    body: dict
    observed_at: str
    checked_at: str
    status: int
    body_hash: str


class GitHubClient:
    def __init__(self, connection, *, identity: str, transport: GhTransport | None = None,
                 policy: RequestPolicy | None = None, clock=utc_now, random_value=random.random):
        self.connection = connection
        self.identity = credential_identity(identity)
        self.transport = transport or GhTransport()
        self.policy = policy or RequestPolicy()
        self.clock = clock
        self.random_value = random_value

    def _retry(self, now, attempt):
        delay = min(self.policy.retry_max_seconds, self.policy.retry_base_seconds * 2 ** min(max(0, attempt - 1), 16))
        return now + timedelta(seconds=delay * (1 + self.policy.jitter_fraction * self.random_value()))

    def next_allowed_at(self, resource, *, now=None):
        if resource not in ('core','search'):
            raise ValueError("unknown GitHub resource")
        now = _time(now if now is not None else self.clock())
        waits = []
        for row in self.connection.execute("SELECT * FROM rate_limits WHERE credential_identity=? AND resource IN (?, '*')",
                                           (self.identity, resource)):
            for field in ("blocked_until", "next_request_at"):
                if row[field] and _time(row[field]) > now:
                    waits.append(_time(row[field]))
            if row["remaining"] is not None and row["quota_limit"] is not None and row["reset_at"]:
                reserve = math.ceil(row["quota_limit"] * self.policy.reserve_fraction)
                if row["remaining"] <= reserve and _time(row["reset_at"]) > now:
                    waits.append(_time(row["reset_at"]) + timedelta(seconds=1))
        if waits:
            return timestamp(max(waits))
        return None

    def _before(self, request, now):
        if self.connection.in_transaction:
            raise ValueError("network queries cannot run inside a state transaction")
        wait = self.next_allowed_at(request.resource, now=now)
        if wait:
            raise CollectionError("github_budget_wait", retry_at=wait, attempted=False)
        spacing = self.policy.search_spacing_seconds if request.resource == "search" else self.policy.core_spacing_seconds
        with self.connection:
            self.connection.execute(
                "INSERT INTO rate_limits(credential_identity,resource,next_request_at) VALUES (?,?,?) "
                "ON CONFLICT(credential_identity,resource) DO UPDATE SET next_request_at=excluded.next_request_at",
                (self.identity, request.resource, timestamp(now + timedelta(seconds=spacing))))

    def _headers(self, request, headers):
        # Unknown/missing header values never become a fabricated zero quota.
        def integer(key):
            value = headers.get(key, "")
            return int(value) if len(value) <= 12 and value.isascii() and value.isdigit() else None
        remaining, limit, reset = (integer(x) for x in ("x-ratelimit-remaining", "x-ratelimit-limit", "x-ratelimit-reset"))
        try:
            reset_at = timestamp(datetime.fromtimestamp(reset, timezone.utc)) if reset is not None else None
        except (OverflowError, OSError, ValueError):
            reset_at = None
        with self.connection:
            self.connection.execute(
                "UPDATE rate_limits SET remaining=?,quota_limit=?,reset_at=? WHERE credential_identity=? AND resource=?",
                (remaining, limit, reset_at, self.identity, request.resource))
        return remaining, reset_at

    def _validate_body(self, body, request):
        _public(body, request)

    def fetch(self, request: GitHubRequest, *, attempt: int = 1) -> FetchResult:
        request.validate()
        key = request.cache_key(self.identity)
        now = _time(self.clock())
        if self.connection.in_transaction:
            raise ValueError("network queries cannot run inside a state transaction")
        try:require_collection_room(self.connection)
        except ServiceError as error:
            raise CollectionError("storage_budget_wait", retry_at=timestamp(now+timedelta(seconds=60)),attempted=False) from error
        self._before(request, now)
        cache = cached_response(self.connection,key,self.identity,now=now)
        try:
            response = self.transport.get(request, etag=cache["etag"] if cache else None,
                                          modified=cache["last_modified"] if cache else None)
        except ServiceError as error:
            permanent = error.code in ("github_not_public", "github_identity_mismatch", "gh_unavailable")
            raise CollectionError(error.code, retry_at=None if permanent else timestamp(self._retry(_time(self.clock()), attempt))) from error
        now = _time(self.clock())
        remaining, reset = self._headers(request, response.headers)
        if response.status in (200, 304):
            if response.status == 304 and cache is None:
                raise CollectionError("github_cache_miss", retry_at=timestamp(self._retry(now, attempt)))
            body = json.loads(cache["body_json"]) if response.status == 304 else response.body
            self._validate_body(body, request)
            encoded = encode_json(body)
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            if response.status == 304 and digest != cache["body_hash"]:
                raise CollectionError("github_cache_corrupt", retry_at=None)
            observed = cache["observed_at"] if response.status == 304 else timestamp(now)
            etag = response.headers.get("etag") or (cache["etag"] if response.status == 304 else None)
            modified = response.headers.get("last-modified") or (cache["last_modified"] if response.status == 304 else None)
            with self.connection:
                store_cached_response(self.connection,(key,self.identity,etag,modified,encoded,digest,observed,timestamp(now)),now=now)
            return FetchResult(body, observed, timestamp(now), response.status, digest)
        message = str((response.body or {}).get("message", "")).lower()
        throttled = response.status == 429 or response.status == 403 and (
            remaining == 0 or "retry-after" in response.headers or "secondary rate" in message or "abuse" in message)
        if throttled:
            secondary_hint = "secondary rate" in message or "abuse" in message
            primary = remaining == 0 and reset is not None and not secondary_hint
            retry = self._retry(now, attempt)
            if primary:
                retry = max(retry, _time(reset) + timedelta(seconds=1))
            value = response.headers.get("retry-after", "")
            try:
                candidate = now + timedelta(seconds=float(value)) if value.strip().replace(".", "", 1).isdigit() else parsedate_to_datetime(value)
                if candidate.tzinfo is not None:
                    retry = max(retry, candidate.astimezone(timezone.utc))
            except (ValueError, TypeError, OverflowError):
                pass
            resource = request.resource if primary and "retry-after" not in response.headers else "*"
            with self.connection:
                self.connection.execute(
                    "INSERT INTO rate_limits(credential_identity,resource,blocked_until) VALUES (?,?,?) "
                    "ON CONFLICT(credential_identity,resource) DO UPDATE SET blocked_until=excluded.blocked_until",
                    (self.identity, resource, timestamp(retry)))
            raise CollectionError("github_primary_limit" if primary else "github_secondary_limit", retry_at=timestamp(retry))
        retryable = response.status >= 500 or response.status in (404, 408)
        code = "github_not_found" if response.status == 404 else "github_permission" if response.status in (401, 403) else "github_http_error"
        raise CollectionError(code, retry_at=timestamp(self._retry(now, attempt)) if retryable else None)
