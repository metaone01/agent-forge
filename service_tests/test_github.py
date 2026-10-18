import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_forge_service.errors import ServiceError
from agent_forge_service.github import GhTransport, GitHubRequest, GitHubResponse, _execute, credential_identity
from agent_forge_service.github_client import CollectionError, GitHubClient, RequestPolicy
from agent_forge_service.storage import open_state, timestamp

NOW = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
PUBLIC = {"id": 123, "full_name": "example/public-repo", "private": False, "visibility": "public", "stargazers_count": 10}


def wire(status=200, body=PUBLIC, headers=None, newline="\r\n"):
    lines = [f"HTTP/2.0 {status} status"] + [k + ": " + str(v) for k, v in (headers or {}).items()]
    return (newline.join(lines) + newline * 2 + (json.dumps(body) if body is not None else "")).encode()


class TransportTests(unittest.TestCase):
    def test_token_like_identity_and_oversized_numeric_ids_are_rejected(self):
        for value in ["ghp_secret", "github_pat_secret", "token with spaces"]:
            with self.assertRaises(ValueError):
                credential_identity(value)
        with self.assertRaises(ValueError):
            GitHubRequest.repository(2**63)

    def test_fixed_get_argv_host_and_noninteractive_environment(self):
        captured = []
        def runner(*args):
            captured.append(args)
            return 0, wire()
        with patch.dict(os.environ, {"GH_DEBUG": "api", "GH_HOST": "evil.invalid", "HTTP_PROXY": "http://127.0.0.1:7897"}):
            response = GhTransport(runner=runner).get(GitHubRequest.repository(123), etag='"tag"')
        argv, env, timeout, size = captured[0]
        self.assertEqual(["gh", "api", "--hostname", "github.com", "-X", "GET", "--include"], argv[:7])
        self.assertEqual("repositories/123", argv[-1])
        self.assertIn('If-None-Match: "tag"', argv)
        self.assertNotIn("GH_DEBUG", env)
        self.assertEqual("1", env["GH_PROMPT_DISABLED"])
        self.assertEqual("http://127.0.0.1:7897", env["HTTP_PROXY"])
        self.assertEqual(123, response.body["id"])

    def test_endpoint_query_host_and_option_injection_are_rejected_before_execution(self):
        def runner(*args):
            self.fail("invalid request reached gh")
        transport = GhTransport(runner=runner)
        for endpoint in ["graphql", "user", "https://evil.invalid/repos/x/y", "repos/x/y/releases", "repos/x/..", "repos/{owner}/{repo}", "--method=POST", "repos/x/y?z=1", "repositories/9999999999999999999"]:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                transport.get(GitHubRequest(endpoint))
        search = GitHubRequest.search(topic="mcp-server", field="created", start=NOW, end=NOW + timedelta(hours=8))
        params = dict(search.params); params["q"] += " OR is:private"
        with self.assertRaises(ValueError):
            transport.get(GitHubRequest(search.endpoint, tuple(params.items())))
        with self.assertRaises(ValueError):
            transport.get(GitHubRequest.repository(123), etag="safe\r\nAuthorization: secret")

    def test_search_public_filter_bounds_and_cache_identity(self):
        request = GitHubRequest.search(topic="mcp-server", field="pushed", start=NOW, end=NOW + timedelta(hours=8), page=10)
        self.assertIn("is:public", dict(request.params)["q"])
        self.assertNotEqual(request.cache_key("alice"), request.cache_key("bob"))
        self.assertNotEqual(request.cache_key("alice"), GitHubRequest.search(topic="mcp-server", field="pushed", start=NOW, end=NOW + timedelta(hours=8), page=1).cache_key("alice"))
        for page in [0, 11, True]:
            with self.assertRaises(ValueError):
                GitHubRequest.search(topic="mcp-server", field="created", start=NOW, end=NOW + timedelta(hours=1), page=page)
        with self.assertRaises(ValueError):
            GitHubRequest.search(topic="mcp-server is:private", field="created", start=NOW, end=NOW + timedelta(hours=1))

    def test_private_results_and_mismatched_numeric_identity_are_rejected(self):
        for body, endpoint, code in [({**PUBLIC, "private": True}, GitHubRequest.repository(123), "github_not_public"),
                                     ({**PUBLIC, "id": 456}, GitHubRequest.repository(123), "github_identity_mismatch")]:
            with self.assertRaises(ServiceError) as error:
                GhTransport(runner=lambda *args: (0, wire(body=body))).get(endpoint)
            self.assertEqual(code, error.exception.code)
        request = GitHubRequest.search(topic="mcp-server", field="created", start=NOW, end=NOW + timedelta(hours=1))
        with self.assertRaises(ServiceError):
            GhTransport(runner=lambda *args: (0, wire(body={"total_count": 1, "incomplete_results": False, "items": [{**PUBLIC, "private": True}]}))).get(request)

    def test_authenticated_view_fields_are_not_kept_as_public_evidence(self):
        response = GhTransport(runner=lambda *args: (0, wire(body={**PUBLIC, "permissions": {"admin": True}, "temp_clone_token": "secret"}))).get(GitHubRequest.repository(123))
        self.assertNotIn("permissions", response.body)
        self.assertNotIn("temp_clone_token", response.body)

    def test_header_variants_errors_and_304(self):
        for newline in ["\n", "\r\n"]:
            response = GhTransport(runner=lambda *args: (1, wire(403, {"message": "secondary rate limit"}, {"Retry-After": "120"}, newline))).get(GitHubRequest.repository(123))
            self.assertEqual(403, response.status)
            self.assertEqual("120", response.headers["retry-after"])
        response = GhTransport(runner=lambda *args: (1, wire(304, None))).get(GitHubRequest.repository(123))
        self.assertIsNone(response.body)
        response = GhTransport(runner=lambda *args: (0, b"HTTP/1.1 100 Continue\r\n\r\n" + wire())).get(GitHubRequest.repository(123))
        self.assertEqual(200, response.status)

    def test_malformed_and_oversized_responses_are_not_accepted(self):
        for output in [b"not HTTP", wire(body=[]), b"HTTP/2.0 200 OK\n\n{", wire(body={**PUBLIC, "id": True})]:
            with self.assertRaises(ServiceError):
                GhTransport(runner=lambda *args: (0, output)).get(GitHubRequest.repository(123))
        with self.assertRaises(ServiceError) as error:
            GhTransport(max_response_bytes=1024, runner=lambda *args: (0, b"x" * 1025)).get(GitHubRequest.repository(123))
        self.assertEqual("github_response_too_large", error.exception.code)

    def test_local_child_timeout_and_byte_limit_settle_process(self):
        with self.assertRaises(ServiceError) as error:
            _execute([sys.executable, "-c", "import time;time.sleep(5)"], dict(os.environ), 0.1, 1024)
        self.assertEqual("github_timeout", error.exception.code)
        with self.assertRaises(ServiceError) as error:
            _execute([sys.executable, "-c", "import sys;sys.stdout.write('x'*10000)"], dict(os.environ), 3, 1024)
        self.assertEqual("github_response_too_large", error.exception.code)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db = open_state(Path(self.temp.name) / "state.db"); self.addCleanup(self.db.close)
        self.now = NOW
        self.responses = []
        self.calls = []
        def runner(*args):
            self.calls.append(args[0])
            return self.responses.pop(0)
        self.transport = GhTransport(runner=runner)
        self.client = GitHubClient(self.db, identity="local-profile", transport=self.transport,
                                   policy=RequestPolicy(jitter_fraction=0), clock=lambda: self.now)
        self.request = GitHubRequest.repository(123)

    def push(self, status=200, body=PUBLIC, headers=None):
        self.responses.append((0 if status < 400 else 1, wire(status, body, headers)))

    def test_conditional_cache_keeps_original_observation_time(self):
        self.push(headers={"ETag": '"r1"'})
        first = self.client.fetch(self.request)
        self.now += timedelta(hours=8)
        self.push(304, None, {"ETag": '"r1"'})
        second = self.client.fetch(self.request)
        self.assertEqual(first.observed_at, second.observed_at)
        self.assertNotEqual(first.checked_at, second.checked_at)
        self.assertEqual(first.body_hash, second.body_hash)
        self.assertIn('If-None-Match: "r1"', self.calls[-1])
        self.assertEqual(304, second.status)

    def test_304_without_cache_and_private_payload_never_publish(self):
        self.push(304, None)
        with self.assertRaises(CollectionError) as error:
            self.client.fetch(self.request)
        self.assertEqual("github_cache_miss", error.exception.code)
        self.now += timedelta(seconds=2)
        self.push(body={**PUBLIC, "private": True})
        with self.assertRaises(CollectionError) as error:
            self.client.fetch(self.request)
        self.assertIsNone(error.exception.retry_at)
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM http_cache").fetchone()[0])

    def test_primary_reserve_and_search_core_buckets_are_separate(self):
        reset = int((NOW + timedelta(minutes=10)).timestamp())
        search = GitHubRequest.search(topic="mcp-server", field="created", start=NOW, end=NOW + timedelta(hours=1))
        self.push(403, {"message": "API rate limit exceeded"}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "30", "X-RateLimit-Reset": str(reset)})
        with self.assertRaises(CollectionError) as error:
            self.client.fetch(search)
        self.assertEqual("github_primary_limit", error.exception.code)
        self.push(headers={"X-RateLimit-Remaining": "19", "X-RateLimit-Limit": "100", "X-RateLimit-Reset": str(reset)})
        self.client.fetch(self.request)
        self.now += timedelta(seconds=2)
        with self.assertRaises(CollectionError) as error:
            self.client.fetch(self.request)
        self.assertFalse(error.exception.attempted)
        self.assertEqual(2, len(self.calls))

    def test_secondary_limit_blocks_other_resource_and_honors_retry_after(self):
        self.push(429, {"message": "slow down"}, {"Retry-After": "7200"})
        with self.assertRaises(CollectionError) as error:
            self.client.fetch(self.request)
        self.assertEqual(timestamp(NOW + timedelta(hours=2)), error.exception.retry_at)
        search = GitHubRequest.search(topic="mcp-server", field="created", start=NOW, end=NOW + timedelta(hours=1))
        self.now += timedelta(minutes=1)
        with self.assertRaises(CollectionError) as error:
            self.client.fetch(search)
        self.assertFalse(error.exception.attempted)
        self.assertEqual(1, len(self.calls))

    def test_explicit_secondary_limit_with_empty_primary_bucket_is_still_global(self):
        reset = int((NOW + timedelta(minutes=10)).timestamp())
        self.push(403, {"message": "You have exceeded a secondary rate limit"},
                  {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset), "Retry-After": "120"})
        with self.assertRaises(CollectionError) as error:
            self.client.fetch(self.request)
        self.assertEqual("github_secondary_limit", error.exception.code)
        self.assertTrue(self.db.execute("SELECT 1 FROM rate_limits WHERE resource='*'").fetchone())

    def test_permissions_are_not_misclassified_as_secondary_throttling(self):
        self.push(403, {"message": "Resource not accessible by integration"})
        with self.assertRaises(CollectionError) as error:
            self.client.fetch(self.request)
        self.assertEqual("github_permission", error.exception.code)
        self.assertIsNone(error.exception.retry_at)
        self.assertFalse(self.db.execute("SELECT 1 FROM rate_limits WHERE resource='*'").fetchone())

    def test_missing_headers_do_not_become_zero_and_error_body_is_not_persisted(self):
        self.push(500, {"message": "secret error text must not be persisted"})
        with self.assertRaises(CollectionError):
            self.client.fetch(self.request)
        row = self.db.execute("SELECT * FROM rate_limits").fetchone()
        self.assertIsNone(row["remaining"])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM http_cache").fetchone()[0])

    def test_credential_identities_have_separate_conditional_caches(self):
        self.push(headers={"ETag": '"first"'}); self.client.fetch(self.request)
        other = GitHubClient(self.db, identity="different-profile", transport=self.transport, clock=lambda: self.now)
        self.push(); other.fetch(self.request)
        self.assertNotIn('If-None-Match: "first"', self.calls[-1])
        self.assertEqual(2, self.db.execute("SELECT count(*) FROM http_cache").fetchone()[0])
