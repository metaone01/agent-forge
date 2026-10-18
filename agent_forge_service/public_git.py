"""Anonymous HTTPS GETs for public Git metadata; never uses gh credentials."""
from __future__ import annotations

import base64
import hashlib
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote

from .declarations import MAX_DECLARATION_BYTES, strict_json
from .errors import ServiceError
from .github import API_VERSION, GitHubRequest, GitHubResponse, _public
from .github_client import GitHubClient
from .storage import encode_json, timestamp

ANONYMOUS_IDENTITY = "github-public-declarations-anonymous-v1"
REPRESENTATION = "github-public-git-objects-v1"
_SHA = re.compile(r"[0-9a-f]{40}")


def git_sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("Git object identity must be a full SHA-1")
    return value


@dataclass(frozen=True)
class PublicGitRequest:
    kind: str
    repo: int | str
    ref: str | None = None
    page: int = 1

    def validate(self):
        GitHubRequest.repository(self.repo).validate()
        if type(self.page) is not int or not 1 <= self.page <= 10 or self.kind != "releases" and self.page != 1:
            raise ValueError("invalid public metadata page")
        if self.kind == "releases":
            if type(self.repo) is not int or self.ref is not None:
                raise ValueError("release requests require a stable numeric repository ID and no ref")
        elif self.kind == "repository":
            if self.ref is not None:
                raise ValueError("repository request has no ref")
        elif self.kind in ("tree", "blob"):
            if not isinstance(self.repo, str):
                raise ValueError("Git objects require a repository name")
            git_sha(self.ref)
        elif self.kind == "commit":
            if not isinstance(self.repo, str) or not isinstance(self.ref, str) or not 1 <= len(self.ref) <= 200 or not re.fullmatch(r"[A-Za-z0-9_./-]+", self.ref) or any(p in ("", ".", "..") or p.endswith(".") for p in self.ref.split("/")) or ".." in self.ref:
                raise ValueError("invalid literal branch or commit ref")
        else:
            raise ValueError("public Git endpoint is not allowed")
        return self

    @property
    def endpoint(self):
        self.validate()
        if self.kind == "releases":
            return f"repositories/{self.repo}/releases?per_page=100&page={self.page}"
        if self.kind == "repository":
            return GitHubRequest.repository(self.repo).endpoint
        segment = {"commit": "commits", "tree": "git/trees", "blob": "git/blobs"}[self.kind]
        return "repos/" + self.repo + "/" + segment + "/" + quote(self.ref, safe="")

    @property
    def resource(self):
        return "core"

    def cache_key(self, identity):
        if identity != ANONYMOUS_IDENTITY:
            raise ValueError("anonymous object cache cannot share an authenticated identity")
        return hashlib.sha256(encode_json(["api.github.com", self.endpoint, REPRESENTATION, API_VERSION, identity]).encode()).hexdigest()


def sanitize_object(body, request, *, max_blob_bytes=MAX_DECLARATION_BYTES):
    request.validate()
    if type(max_blob_bytes) is not int or not 1 <= max_blob_bytes <= 2 * 1024 * 1024:
        raise ValueError("invalid public blob byte budget")
    if request.kind == "releases":
        return sanitize_releases(body)
    if not isinstance(body, dict):
        raise ValueError("public Git response must be an object")
    if request.kind == "repository":
        _public(body, GitHubRequest.repository(request.repo))
        full_name = body.get("full_name")
        GitHubRequest.repository(full_name).validate()
        branch = body.get("default_branch")
        PublicGitRequest("commit", full_name, branch).validate()
        return {"id": body["id"], "full_name": full_name, "private": False, "default_branch": branch}
    sha = git_sha(body.get("sha"))
    if request.kind in ("tree", "blob") and sha != request.ref:
        raise ValueError("Git object identity mismatch")
    if request.kind == "commit":
        if _SHA.fullmatch(request.ref) and sha != request.ref:
            raise ValueError("pinned commit identity mismatch")
        tree = body.get("commit", {}).get("tree", {})
        return {"sha": sha, "commit": {"tree": {"sha": git_sha(tree.get("sha"))}}}
    if request.kind == "tree":
        if body.get("truncated") is not False or not isinstance(body.get("tree"), list) or len(body["tree"]) > 10000:
            raise ValueError("Git tree is truncated or exceeds the entry budget")
        items, seen = [], set()
        for item in body["tree"]:
            if not isinstance(item, dict):
                raise ValueError("invalid tree entry")
            path = item.get("path")
            mode, category = item.get("mode"), item.get("type")
            if not isinstance(path, str) or not path or "/" in path or path in seen or path in (".", "..") or len(path) > 1024:
                raise ValueError("invalid or duplicate tree path")
            if (mode, category) not in (("040000", "tree"), ("100644", "blob"), ("100755", "blob"), ("120000", "blob"), ("160000", "commit")):
                raise ValueError("unsupported Git entry mode")
            seen.add(path)
            row = {"path": path, "mode": mode, "type": category, "sha": git_sha(item.get("sha"))}
            if "size" in item:
                if type(item["size"]) is not int or not 0 <= item["size"] <= 2**63 - 1:
                    raise ValueError("invalid blob size")
                row["size"] = item["size"]
            items.append(row)
        return {"sha": sha, "truncated": False, "tree": items}
    if body.get("encoding") != "base64" or type(body.get("size")) is not int or not 0 <= body["size"] <= max_blob_bytes or not isinstance(body.get("content"), str) or len(body["content"]) > 2 * max_blob_bytes:
        raise ValueError("unsupported blob encoding or byte budget")
    raw = base64.b64decode(body["content"].replace("\n", ""), validate=True)
    if len(raw) != body["size"] or hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() != sha:
        raise ValueError("blob length or Git hash mismatch")
    return {"sha": sha, "encoding": "base64", "size": len(raw), "content": base64.b64encode(raw).decode("ascii")}


def sanitize_releases(body):
    """Only public release/asset facts; no bodies, author data, archive or upload URLs."""
    if isinstance(body, dict) and set(body) == {"releases"}:
        body = body["releases"]
    if not isinstance(body, list) or len(body) > 100:
        raise ValueError("release page is not a bounded array")

    def number(value, *, positive=False):
        if type(value) is not int or not (1 if positive else 0) <= value <= 2**63 - 1:
            raise ValueError("invalid public release number")
        return value

    def text(value, maximum, *, optional=False):
        if optional and value in (None, ""):
            return value
        if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("invalid public release text")
        return value

    items, seen, assets_seen = [], set(), set()
    for item in body:
        if not isinstance(item, dict) or item.get("draft") is not False or type(item.get("prerelease")) is not bool:
            raise ValueError("release is malformed or not publicly published")
        release_id = number(item.get("id"), positive=True)
        if release_id in seen:
            raise ValueError("duplicate release identity in one page")
        seen.add(release_id)
        assets = item.get("assets")
        if not isinstance(assets, list) or len(assets) > 1000:
            raise ValueError("release assets exceed the inline metadata budget")
        clean = {"id": release_id, "tag_name": text(item.get("tag_name"), 512),
                 "name": text(item.get("name"), 1000, optional=True), "draft": False,
                 "prerelease": item["prerelease"], "target_commitish": text(item.get("target_commitish"), 512),
                 "created_at": timestamp(item["created_at"]), "published_at": timestamp(item["published_at"]), "assets": []}
        for asset in assets:
            if not isinstance(asset, dict):
                raise ValueError("invalid release asset")
            asset_id = number(asset.get("id"), positive=True)
            if asset_id in assets_seen:
                raise ValueError("duplicate release asset identity in one page")
            assets_seen.add(asset_id)
            clean["assets"].append({"id": asset_id, "name": text(asset.get("name"), 1000),
                "state": text(asset.get("state"), 100), "size": number(asset.get("size")),
                "download_count": number(asset.get("download_count")),
                "created_at": timestamp(asset["created_at"]), "updated_at": timestamp(asset["updated_at"])})
        items.append(clean)
    return {"releases": items}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AnonymousGitTransport:
    def __init__(self, *, timeout=15, max_response_bytes=2 * 1024 * 1024, opener=None):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 20 or type(max_response_bytes) is not int or not 1024 <= max_response_bytes <= 4 * 1024 * 1024:
            raise ValueError("invalid anonymous request budget")
        self.timeout, self.maximum = timeout, max_response_bytes
        # No cookies, HTTP auth handlers, gh configuration, token environment or redirect following.
        self.opener = opener or urllib.request.build_opener(_NoRedirect())

    def get(self, request, *, etag=None, modified=None):
        if not isinstance(request, PublicGitRequest):
            raise ValueError("anonymous transport only accepts public Git requests")
        request.validate()
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
                   "User-Agent": "agent-forge-public-metadata/1", "Accept-Encoding": "identity"}
        for name, value in (("If-None-Match", etag), ("If-Modified-Since", modified)):
            if value is not None:
                if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                    raise ValueError("unsafe conditional header")
                headers[name] = value
        req = urllib.request.Request("https://api.github.com/" + request.endpoint, headers=headers, method="GET")
        deadline = time.monotonic() + self.timeout
        try:
            try:
                response = self.opener.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                if response.geturl() != req.full_url:
                    raise ServiceError("github_redirect_rejected", "public declaration redirect was rejected", 502)
                status = response.code
                result_headers = {key.lower(): value for key, value in response.headers.items()}
                chunks, size = [], 0
                while status != 304:
                    if time.monotonic() >= deadline:
                        raise ServiceError("github_timeout", "public declaration request timed out", 502)
                    chunk = response.read1(min(65536, self.maximum + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk); size += len(chunk)
                    if size > self.maximum:
                        raise ServiceError("github_response_too_large", "public declaration response exceeds the budget", 502)
            if status == 304:
                return GitHubResponse(status, result_headers, None)
            if 300 <= status < 400:
                raise ServiceError("github_redirect_rejected", "public declaration redirect was rejected", 502)
            try:
                body = strict_json(b"".join(chunks), maximum=self.maximum)
                if not isinstance(body, dict) and not (status == 200 and request.kind == "releases" and isinstance(body, list)):
                    raise ValueError("unexpected GitHub response shape")
                body = sanitize_object(body, request) if status == 200 else {"message": str(body.get("message", ""))[:500]}
            except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
                raise ServiceError("github_invalid_payload", "public declaration response is invalid", 502) from error
            return GitHubResponse(status, result_headers, body)
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise ServiceError("github_transport", "anonymous public request did not complete", 502) from error


class AnonymousGitClient(GitHubClient):
    def __init__(self, connection, *, transport=None, **kwargs):
        super().__init__(connection, identity=ANONYMOUS_IDENTITY, transport=transport or AnonymousGitTransport(), **kwargs)

    def _validate_body(self, body, request):
        if not isinstance(request, PublicGitRequest):
            raise ValueError("public declaration client cannot use authenticated request types")
        try:
            clean = sanitize_object(body, request)
            if clean != body:
                raise ValueError("cached representation is not canonical")
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ServiceError("github_invalid_payload", "cached public declaration response is invalid", 502) from error

    def fetch(self, request, *, attempt=1):
        if not isinstance(request, PublicGitRequest):
            raise ValueError("public declaration client only accepts public Git requests")
        return super().fetch(request, attempt=attempt)
