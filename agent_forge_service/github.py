"""Bounded gh transport: github.com, public repository metadata/search, GET only."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode

from .errors import ServiceError
from .storage import encode_json, timestamp

ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"
PUBLIC_FIELDS_VERSION = "github-repository-public-fields-v1"
PUBLIC_FIELDS = {"id", "full_name", "private", "visibility", "description", "topics", "created_at", "updated_at", "pushed_at", "archived", "disabled", "default_branch", "stargazers_count", "forks_count", "open_issues_count", "license"}
_NAME = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}"
_TOPIC = r"[a-z0-9][a-z0-9-]{0,49}"
_DATE = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
_SEARCH = re.compile(rf"topic:({_TOPIC}) is:public (created|pushed):({_DATE})\.\.({_DATE})")


def credential_identity(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) or value.startswith(("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")):
        raise ValueError("credential identity must be an explicit opaque label, not a token")
    return value


def _second(value: str | datetime) -> str:
    normalized = timestamp(value)
    if not normalized.endswith(".000000Z"):
        raise ValueError("search windows require whole-second timestamps")
    return normalized.replace(".000000Z", "Z")


@dataclass(frozen=True)
class GitHubRequest:
    endpoint: str
    params: tuple[tuple[str, str], ...] = ()

    @classmethod
    def repository(cls, identity: int | str):
        if type(identity) is int and 0 < identity <= 2**63 - 1:
            return cls(f"repositories/{identity}")
        if isinstance(identity, str) and re.fullmatch(_NAME, identity) and identity.split("/")[1] not in (".", ".."):
            return cls("repos/" + identity)
        raise ValueError("repository must be a positive numeric ID or owner/name")

    @classmethod
    def search(cls, *, topic: str, field: str, start: str | datetime,
               end: str | datetime, page: int = 1):
        if not isinstance(topic, str) or not re.fullmatch(_TOPIC, topic) or field not in ("created", "pushed"):
            raise ValueError("invalid discovery topic or time field")
        start, end = _second(start), _second(end)
        if start >= end or type(page) is not int or not 1 <= page <= 10:
            raise ValueError("invalid search window or page")
        return cls("search/repositories", tuple(sorted({
            "q": f"topic:{topic} is:public {field}:{start}..{end}",
            "page": str(page), "per_page": "100", "sort": "updated", "order": "asc",
        }.items())))

    def validate(self):
        if not isinstance(self.endpoint, str) or not isinstance(self.params, tuple):
            raise ValueError("invalid GitHub request")
        if self.endpoint == "search/repositories":
            try:
                params = dict(self.params)
            except (TypeError, ValueError) as error:
                raise ValueError("invalid search parameters") from error
            if len(params) != len(self.params) or set(params) != {"q", "page", "per_page", "sort", "order"}:
                raise ValueError("unsupported search parameters")
            if not all(isinstance(v, str) for v in params.values()):
                raise ValueError("search parameters must be strings")
            query = _SEARCH.fullmatch(params["q"])
            if not query or params["per_page"] != "100" or params["sort"] != "updated" or params["order"] != "asc":
                raise ValueError("only structured public repository searches are allowed")
            if params["page"] not in {str(x) for x in range(1, 11)}:
                raise ValueError("search page outside the GitHub result bound")
            self.search(topic=query[1], field=query[2], start=query[3], end=query[4], page=int(params["page"]))
        elif re.fullmatch(r"repositories/[1-9][0-9]{0,18}", self.endpoint):
            self.repository(int(self.endpoint.split("/")[1]))
            if self.params:
                raise ValueError("repository metadata does not take query parameters")
        elif self.endpoint.startswith("repos/"):
            self.repository(self.endpoint.removeprefix("repos/"))
            if self.params:
                raise ValueError("repository metadata does not take query parameters")
        else:
            raise ValueError("GitHub endpoint is not on the read-only allowlist")
        return self

    @property
    def resource(self) -> str:
        return "search" if self.endpoint == "search/repositories" else "core"

    def cache_key(self, identity: str) -> str:
        self.validate()
        data = ["github.com", self.endpoint, sorted(self.params), ACCEPT, API_VERSION, PUBLIC_FIELDS_VERSION, credential_identity(identity)]
        return hashlib.sha256(encode_json(data).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GitHubResponse:
    status: int
    headers: dict[str, str]
    body: dict | None


def _public(body: dict, request: GitHubRequest):
    items = body.get("items") if request.resource == "search" else [body]
    if not isinstance(items, list) or len(items) > 100:
        raise ServiceError("github_invalid_payload", "repository response has an invalid shape", 502)
    for item in items:
        if not isinstance(item, dict) or item.get("private") is not False or item.get("visibility", "public") != "public":
            raise ServiceError("github_not_public", "non-public repository response was rejected", 403)
        if type(item.get("id")) is not int or not 0 < item["id"] <= 2**63 - 1:
            raise ServiceError("github_invalid_payload", "repository identity is missing", 502)
    if request.endpoint.startswith("repositories/") and body["id"] != int(request.endpoint.split("/")[1]):
        raise ServiceError("github_identity_mismatch", "repository response has a different stable ID", 502)


def _reject_constant(value):
    raise ValueError("non-finite JSON")


def parse_response(output: bytes, returncode: int, request: GitHubRequest, *, body_sanitizer=None) -> GitHubResponse:
    rest = output
    headers = {}
    for _ in range(8):
        separator = b"\r\n\r\n" if b"\r\n\r\n" in rest else b"\n\n"
        prefix, found, body = rest.partition(separator)
        if not found or len(prefix) > 16384:
            raise ServiceError("github_transport", "gh did not return valid HTTP headers", 502)
        lines = prefix.decode("utf-8", "replace").splitlines()
        match = re.fullmatch(r"HTTP/\S+ ([1-5][0-9]{2})(?: .*|)", lines[0]) if lines else None
        if not match:
            raise ServiceError("github_transport", "gh did not return a valid HTTP status", 502)
        status = int(match[1])
        headers = {}
        for line in lines[1:]:
            key, colon, value = line.partition(":")
            if colon:
                headers[key.strip().lower()] = value.strip()
        if (100 <= status < 200 or 300 <= status < 400 and status != 304) and body.startswith(b"HTTP/"):
            rest = body
            continue
        break
    else:
        raise ServiceError("github_transport", "too many response header blocks", 502)
    # gh exits 1 for a valid HTTP 304; its exit status is not the HTTP status.
    if returncode and status < 400 and status != 304:
        raise ServiceError("github_transport", "gh failed before completing the response", 502)
    if status == 304:
        return GitHubResponse(status, headers, None)
    try:
        data = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError):
        if status >= 400:
            return GitHubResponse(status, headers, None)
        raise ServiceError("github_invalid_payload", "GitHub returned invalid JSON", 502)
    if not isinstance(data, dict):
        raise ServiceError("github_invalid_payload", "GitHub returned a non-object response", 502)
    if status == 200 and body_sanitizer is not None:
        try:
            return GitHubResponse(status, headers, body_sanitizer(data, request))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ServiceError("github_invalid_payload", "public Git object response is invalid", 502) from error
    if status == 200:
        _public(data, request)
        if request.resource == "search":
            if type(data.get("total_count")) is not int or not 0 <= data["total_count"] <= 2**63 - 1 or type(data.get("incomplete_results")) is not bool:
                raise ServiceError("github_invalid_payload", "search coverage fields are missing", 502)
            data = {"total_count": data["total_count"], "incomplete_results": data["incomplete_results"],
                    "items": [{k: v for k, v in item.items() if k in PUBLIC_FIELDS} for item in data["items"]]}
        else:
            data = {k: v for k, v in data.items() if k in PUBLIC_FIELDS}
    return GitHubResponse(status, headers, data)


def _execute(argv: list[str], env: dict, timeout: float, maximum: int) -> tuple[int, bytes]:
    with tempfile.TemporaryFile() as output:
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                                     env=env, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            raise ServiceError("gh_unavailable", "the configured gh executable is unavailable", 503) from error
        deadline = time.monotonic() + timeout
        try:
            while child.poll() is None:
                if os.fstat(output.fileno()).st_size > maximum:
                    raise ServiceError("github_response_too_large", "GitHub response exceeds the byte budget", 502)
                if time.monotonic() >= deadline:
                    raise ServiceError("github_timeout", "GitHub query exceeded its time budget", 504)
                time.sleep(0.02)
            output.seek(0)
            body = output.read(maximum + 1)
            if len(body) > maximum:
                raise ServiceError("github_response_too_large", "GitHub response exceeds the byte budget", 502)
            return child.returncode, body
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


class GhTransport:
    def __init__(self, *, timeout: float = 30, max_response_bytes: int = 4 * 1024 * 1024, runner=None):
        if not 0 < timeout <= 60 or type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= 16 * 1024 * 1024:
            raise ValueError("invalid GitHub request budget")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.runner = runner or _execute

    def get(self, request: GitHubRequest, *, etag: str | None = None, modified: str | None = None) -> GitHubResponse:
        if not isinstance(request, GitHubRequest):
            raise ValueError("gh transport only accepts public repository metadata requests")
        request.validate()
        endpoint = request.endpoint + ("?" + urlencode(request.params) if request.params else "")
        argv = ["gh", "api", "--hostname", "github.com", "-X", "GET", "--include",
                "-H", "Accept: " + ACCEPT, "-H", "X-GitHub-Api-Version: " + API_VERSION]
        for name, value in (("If-None-Match", etag), ("If-Modified-Since", modified)):
            if value is not None:
                if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                    raise ValueError("unsafe conditional request header")
                argv += ["-H", name + ": " + value]
        argv.append(endpoint)
        env = dict(os.environ)
        for key in ("GH_DEBUG", "GH_FORCE_TTY", "GH_HTTP_UNIX_SOCKET"):
            env.pop(key, None)
        env.update({"GH_PROMPT_DISABLED": "1", "GH_PAGER": ""})
        code, output = self.runner(argv, env, self.timeout, self.max_response_bytes)
        if not isinstance(output, bytes) or len(output) > self.max_response_bytes:
            raise ServiceError("github_response_too_large", "GitHub response exceeds the byte budget", 502)
        return parse_response(output, code, request)
