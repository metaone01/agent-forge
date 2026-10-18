import json
import shutil
import sqlite3
import base64
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_forge_service.app import _origins
from agent_forge_service.errors import ServiceError
from agent_forge_service.http_contract import HttpLimits, HttpReadService
from agent_forge_service.publishing import atomic_json, build_and_publish
from agent_forge_service.query import Query, QueryService
from agent_forge_service.settings import Settings
from service_tests.test_publication_query import record

NOW = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)


class HttpContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup)
        self.root = Path(self.temporary.name)
        self.catalog = self.root / "catalog"
        self.packages = self.catalog / "sources/plugin/packages"
        self.packages.mkdir(parents=True)
        self.settings = Settings(self.catalog, self.root / "runtime")
        self.write(record("alpha", version="1.0.0", identity="dsh/plugin/数据库"))
        self.write(record("alpha", version="2.0.0", identity="dsh/plugin/数据库"))
        self.write(record("beta"))
        (self.catalog / "sources/plugin/index.json").write_text(json.dumps({"packages": {"alpha": {"latest": "2.0.0"}, "beta": {"latest": "1.0.0"}}}), encoding="utf-8")
        self.build("one")
        self.ticks = [100.0]
        self.reader = HttpReadService(self.settings, clock=lambda: self.ticks[0])

    def cleanup(self):
        for attempt in range(6):
            try:
                self.temporary.cleanup()
                return
            except OSError as error:
                if getattr(error, "winerror", None) != 145 or attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def write(self, value):
        (self.packages / (value["name"] + "-" + value["version"] + ".json")).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def build(self, revision):
        return build_and_publish(self.settings, revision=revision, now=NOW)

    def get(self, path="/api/v1/packages", **parameters):
        return self.reader.handle("GET", path, list(parameters.items()))

    def data(self, response):
        return json.loads(response.body)

    def code(self, response, code, status=400):
        self.assertEqual(status, response.status, response.body)
        self.assertEqual(code, self.data(response)["error"]["code"])
        self.assertEqual(self.data(response)["requestId"], response.headers["X-Request-ID"])
        self.assertEqual("no-store", response.headers["Cache-Control"])

    def test_list_envelope_and_unknown_observations(self):
        response = self.get(limit="1")
        self.assertEqual(200, response.status)
        data = self.data(response)
        self.assertEqual("one", data["revision"])
        self.assertEqual(1, len(data["items"]))
        self.assertTrue(data["nextCursor"])
        self.assertTrue(data["freshnessSummary"]["stale"])
        self.assertIsNone(data["items"][0]["observedLatest"])

    def test_slash_unicode_id_and_exact_old_version(self):
        result = self.get("/api/v1/packages/dsh/plugin/数据库/detail", version="1.0.0")
        self.assertEqual(200, result.status)
        data = self.data(result)
        self.assertEqual("1.0.0", data["canonicalRecord"]["version"])
        self.assertEqual("2.0.0", data["canonicalLatest"])
        self.assertEqual("1.0.0", data["requestedVersion"])
        self.code(self.get("/api/v1/packages/dsh/plugin/数据库/detail", version="does-not-exist"), "version_not_found", 404)

    def test_basic_detail_alias(self):
        self.assertEqual(200, self.get("/api/v1/packages/dsh/plugin/数据库").status)

    def test_suffix_like_ids_remain_addressable(self):
        self.write(record("gamma", identity="dsh/plugin/example/metrics"))
        self.build("two")
        response = self.get("/api/v1/packages/dsh/plugin/example/metrics/detail")
        self.assertEqual(200, response.status)
        self.assertEqual("dsh/plugin/example/metrics", self.data(response)["id"])

    def test_all_versions_with_stable_pagination(self):
        first = self.get("/api/v1/packages/dsh/plugin/数据库/versions", limit="1")
        second = self.get("/api/v1/packages/dsh/plugin/数据库/versions", limit="1", cursor=self.data(first)["nextCursor"])
        self.assertEqual(["1.0.0", "2.0.0"], [self.data(item)["items"][0]["version"] for item in (first, second)])

    def test_unknown_metrics_are_empty_not_zero(self):
        for source in ("github.com", "npmjs.org"):
            with self.subTest(source=source):
                response = self.get("/api/v1/packages/dsh/plugin/数据库/metrics", source=source)
                self.assertEqual(200, response.status)
                self.assertEqual([], self.data(response)["items"])
                self.assertEqual("unknown", self.data(response)["availability"])

    def test_metric_sources_have_different_etags(self):
        a = self.get("/api/v1/packages/dsh/plugin/数据库/metrics", source="github.com")
        b = self.get("/api/v1/packages/dsh/plugin/数据库/metrics", source="npmjs.org")
        self.assertNotEqual(a.headers["ETag"], b.headers["ETag"])

    def test_download_sort_requires_explicit_source(self):
        self.code(self.get(sort="downloads"), "invalid_query")
        response = self.get(sort="downloads", metricSource="npmjs.org")
        self.assertEqual(200, response.status)
        self.assertEqual("one", self.data(response)["revision"])

    def test_auxiliary_endpoints_are_bound_to_revision(self):
        for endpoint in ("facets", "sources", "changes", "manifest"):
            with self.subTest(endpoint=endpoint):
                response = self.get("/api/v1/" + endpoint, revision="one")
                self.assertEqual(200, response.status)
                self.assertEqual("one", self.data(response)["revision"])
        self.assertIn("customFacets", self.data(self.get("/api/v1/facets")))
        self.assertIn("database", self.data(self.get("/api/v1/manifest")))

    def test_subtype_choices_cover_selected_versions_across_the_full_snapshot(self):
        old = record("alpha", version="1.0.0", identity="dsh/plugin/数据库")
        old["subtype"] = "skin"
        current = record("alpha", version="2.0.0", identity="dsh/plugin/数据库")
        current["subtype"] = "theme"
        self.write(old); self.write(current); self.build("two")
        response = self.get("/api/v1/facets", revision="two")
        self.assertEqual(200, response.status)
        self.assertEqual([{"value": "theme", "count": 1}], self.data(response)["subtypes"])
        self.assertEqual(["dsh"], self.data(response)["agents"])
        self.assertEqual([], self.data(self.get("/api/v1/facets", revision="one"))["subtypes"])

    def test_health_does_not_read_private_state_or_use_gh(self):
        before = self.settings.state_path.read_bytes()
        with patch("subprocess.run", side_effect=AssertionError("network must not run")):
            self.assertEqual(200, self.get("/health/live").status)
            ready = self.get("/health/ready")
            self.assertEqual({"revision": "one", "status": "ready"}, self.data(ready))
            self.assertEqual(200, self.get().status)
        self.assertEqual(before, self.settings.state_path.read_bytes())
        self.assertNotIn(str(self.root), ready.body.decode())

    def test_empty_runtime_is_live_but_not_ready(self):
        settings = replace(self.settings, runtime_root=self.root / "empty")
        reader = HttpReadService(settings)
        self.assertEqual(200, reader.handle("GET", "/health/live").status)
        self.code(reader.handle("GET", "/health/ready"), "snapshot_unavailable", 503)
        self.assertFalse(settings.runtime_root.exists())

    def test_unavailable_projections_are_explicit(self):
        response = self.get("/api/v1/packages/dsh/plugin/数据库/recommendations")
        self.assertEqual(200, response.status)
        self.assertEqual([], self.data(response)["items"])
        self.assertEqual("unknown", self.data(response)["availability"])
        self.code(self.get("/api/v1/discoveries/github-repository:1"), "discovery_not_found", 404)
        self.assertEqual([], self.data(self.get(scope="discovery"))["items"])
        self.code(self.get(scope="unknown"), "scope_unavailable")

    def test_invalid_and_ambiguous_parameters_are_rejected(self):
        cases = [(("limit", "0"),), (("limit", "101"),), (("limit", "1e2"),), (("limit", "99999999999999999999999"),),
                 (("page", "1"),), (("refresh", "true"),), (("sort", "most_used"),), (("sort", "relevance"),),
                 (("limit", "1"), ("limit", "2")), (("type", "wrong"),), (("agent", ""),), (("cursor", ""),),
                 (("q", "a" * 201),), (("q", "x\x00y"),), (("facets", '[]'),), (("facets", '{"other":["x"]}'),),
                 (("facets", '{"runtime":["a"],"runtime":["b"]}'),), (("facets", '{"runtime":[NaN]}'),),
                 (("customFacets", '{"other":[null]}'),)]
        for pairs in cases:
            with self.subTest(pairs=pairs):
                response = self.reader.handle("GET", "/api/v1/packages", pairs)
                self.assertEqual(400, response.status, response.body)
                self.assertEqual("no-store", response.headers["Cache-Control"])

    def test_json_recursion_is_bounded(self):
        response = self.get(facets='{"runtime":' + '[' * 1200 + '"x"' + ']' * 1200 + '}')
        self.code(response, "invalid_query")

    def test_request_size_is_bounded(self):
        self.code(self.get(q="a" * 17000), "request_too_large", 414)

    def test_missing_resources_and_unknown_metric_source(self):
        self.code(self.get("/api/v1/packages/missing"), "not_found", 404)
        self.code(self.get("/api/v1/packages/dsh/plugin/数据库/metrics", source="private"), "invalid_query")
        self.code(self.get("/unknown"), "not_found", 404)

    def test_unsafe_methods_do_not_run_queries(self):
        with patch.object(self.reader, "_execute", side_effect=AssertionError("must not execute")):
            for method in ("HEAD", "POST", "PUT", "DELETE", "OPTIONS"):
                result = self.reader.handle(method, "/api/v1/packages")
                self.code(result, "method_not_allowed", 405)
                self.assertEqual("GET", result.headers["Allow"])

    def test_errors_do_not_expose_internal_exception_text(self):
        with self.assertLogs("agent_forge_service.http_contract", level="ERROR"):
            with patch.object(self.reader, "_execute", side_effect=sqlite3.OperationalError("private-path-secret-token")):
                response = self.get()
        self.code(response, "internal_error", 500)
        self.assertNotIn(b"private-path-secret-token", response.body)
        self.assertNotIn(str(self.root).encode(), response.body)

    def test_timeout_is_structured_and_releases_slot(self):
        with patch.object(self.reader, "_execute", side_effect=ServiceError("query_timeout", "private", 503)):
            self.code(self.get(), "query_timeout", 503)
        self.assertEqual(200, self.get().status)
        self.assertEqual(self.reader.limits.max_in_flight, self.reader._services.qsize())

    def test_etag_hit_and_request_ids_are_independent(self):
        with patch.object(self.reader, "_execute", wraps=self.reader._execute) as execute:
            first = self.get()
            second = self.reader.handle("GET", "/api/v1/packages", if_none_match='"other", W/' + first.headers["ETag"])
            self.assertEqual(304, second.status)
            self.assertEqual(b"", second.body)
            self.assertEqual(1, execute.call_count)
            self.assertNotEqual(first.headers["X-Request-ID"], second.headers["X-Request-ID"])
            self.assertEqual(first.headers["ETag"], second.headers["ETag"])
            self.assertEqual(304, self.reader.handle("GET", "/api/v1/packages", if_none_match="*").status)

    def test_normalized_query_has_same_cache_entry(self):
        with patch.object(self.reader, "_execute", wraps=self.reader._execute) as execute:
            a = self.get(q=" terminal ", facets='{"capabilities":["gui.observe","gui.observe"]}')
            b = self.get(q="terminal", facets='{"capabilities":["gui.observe"]}')
            self.assertEqual(200, a.status)
            self.assertEqual(a.headers["ETag"], b.headers["ETag"])
            self.assertEqual(1, execute.call_count)

    def test_conditional_headers_are_bounded(self):
        self.code(self.reader.handle("GET", "/api/v1/packages", if_none_match="x" * 4097), "invalid_request")

    def test_cache_expiry_updates_age_and_cannot_extend_ttl(self):
        with patch("agent_forge_service.query.utc_now", return_value=NOW):
            first = self.get()
        self.ticks[0] += 4
        intermediate = self.reader.handle("GET", "/api/v1/packages", if_none_match=first.headers["ETag"])
        self.assertEqual(304, intermediate.status)
        self.assertEqual("public, max-age=1, must-revalidate", intermediate.headers["Cache-Control"])
        self.ticks[0] += 2
        with patch("agent_forge_service.query.utc_now", return_value=NOW + timedelta(seconds=6)):
            second = self.reader.handle("GET", "/api/v1/packages", if_none_match=first.headers["ETag"])
        self.assertEqual(200, second.status)
        self.assertGreater(self.data(second)["snapshotAgeSeconds"], self.data(first)["snapshotAgeSeconds"])
        self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])

    def test_expired_cache_recomputes_actual_observation_freshness(self):
        from service_tests.test_observations import ObservationTests
        fixture = ObservationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.write()
        fixture.mapping()
        fixture.observe(1, 100, NOW - timedelta(minutes=1))
        manifest = fixture.build()
        maximum = manifest["observationCoverage"]["starWindowPolicy"]["maximum_age_hours"]
        boundary = NOW + timedelta(hours=maximum, minutes=-1)
        self.reader = HttpReadService(fixture.settings, clock=lambda: self.ticks[0])
        with patch("agent_forge_service.query.utc_now", return_value=boundary - timedelta(seconds=1)):
            first = self.get()
        self.assertFalse(self.data(first)["freshnessSummary"]["stale"])
        self.ticks[0] += 6
        with patch("agent_forge_service.query.utc_now", return_value=boundary + timedelta(seconds=1)):
            second = self.reader.handle("GET", "/api/v1/packages", if_none_match=first.headers["ETag"])
        self.assertEqual(200, second.status)
        self.assertTrue(self.data(second)["freshnessSummary"]["stale"])
        self.assertEqual("snapshot_stale", self.data(second)["freshnessSummary"]["reason"])

    def test_missing_search_index_preserves_service_error(self):
        with patch.object(self.reader, "_execute", side_effect=ServiceError("search_unavailable", "private", 503)):
            self.code(self.get(q="数据库"), "search_unavailable", 503)

    def test_malformed_cursor_payload_has_stable_error(self):
        first = self.data(self.get(limit="1"))
        original = first["nextCursor"]
        decoded = json.loads(base64.urlsafe_b64decode(original + "=" * (-len(original) % 4)))
        for cursor in ("not+a+cursor", "!!!!", "a"):
            self.code(self.get(limit="1", cursor=cursor), "invalid_cursor")
        decoded["last"] = {"unexpected": "shape"}
        cursor = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
        self.code(self.get(limit="1", cursor=cursor), "invalid_cursor")

    def test_revision_switch_during_query_does_not_mix_metadata(self):
        current = self.settings.snapshots / "current.json"
        old = json.loads(current.read_text(encoding="utf-8"))
        self.write(record("gamma"))
        self.build("two")
        new = json.loads(current.read_text(encoding="utf-8"))
        atomic_json(current, old)
        execute = self.reader._execute
        def switch(service, request, revision):
            atomic_json(current, new)
            return execute(service, request, revision)
        with patch.object(self.reader, "_execute", side_effect=switch):
            first = self.get()
        self.assertEqual("one", self.data(first)["revision"])
        self.assertEqual(2, len(self.data(first)["items"]))
        second = self.get()
        self.assertEqual("two", self.data(second)["revision"])
        self.assertEqual(3, len(self.data(second)["items"]))
        self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])

    def test_cursor_continues_old_revision_after_publish(self):
        first = self.get(limit="1", sort="name")
        cursor = self.data(first)["nextCursor"]
        self.write(record("gamma"))
        self.build("two")
        second = self.get(limit="1", sort="name", cursor=cursor)
        self.assertEqual("one", self.data(second)["revision"])
        self.code(self.get(limit="1", sort="name", cursor=cursor, revision="two"), "cursor_mismatch")
        self.code(self.get(limit="1", sort="canonical_updated", cursor=cursor), "cursor_mismatch")

    def test_retired_cursor_cannot_be_served_from_cache(self):
        first = self.get(limit="1", sort="name")
        cursor = self.data(first)["nextCursor"]
        self.assertEqual(200, self.get(limit="1", sort="name", cursor=cursor).status)
        self.build("two")
        target = (self.settings.snapshots / "one").resolve()
        self.assertTrue(target.is_relative_to(self.root.resolve()))
        shutil.rmtree(target)
        self.code(self.get(limit="1", sort="name", cursor=cursor), "cursor_expired", 410)

    def test_missing_or_invalid_explicit_revision(self):
        self.code(self.get(revision="retired"), "revision_unavailable", 410)
        self.code(self.get(revision="../private"), "invalid_revision")

    def test_corrupt_current_pointer_is_not_hidden_by_cache(self):
        self.assertEqual(200, self.get().status)
        (self.settings.snapshots / "current.json").write_text('{}', encoding="utf-8")
        self.code(self.get(), "snapshot_unavailable", 503)
        self.assertEqual(200, self.get(revision="one").status)

    def test_cache_entry_and_byte_limits(self):
        self.reader = HttpReadService(self.settings, HttpLimits(max_cache_entries=2, max_cache_bytes=16384))
        for sort in ("name", "canonical_updated", "first_seen"):
            self.assertEqual(200, self.get(sort=sort).status)
        self.assertLessEqual(len(self.reader._cache), 2)
        self.assertLessEqual(self.reader._cache_bytes, 16384)
        self.reader = HttpReadService(self.settings, HttpLimits(max_cache_bytes=1024))
        self.assertEqual(200, self.get().status)
        self.assertEqual(0, self.reader._cache_bytes)

    def test_response_limit_is_structured_and_not_cached(self):
        self.reader = HttpReadService(self.settings, HttpLimits(max_response_bytes=1024))
        self.code(self.get(), "response_too_large", 503)
        self.assertEqual(0, len(self.reader._cache))

    def test_rate_limit_recovers_without_affecting_liveness(self):
        self.reader = HttpReadService(self.settings, HttpLimits(rate_burst=1, rate_per_second=1), clock=lambda: self.ticks[0])
        self.assertEqual(200, self.get().status)
        self.code(self.get(), "rate_limited", 429)
        self.assertEqual(200, self.get("/health/live").status)
        self.ticks[0] += 1
        self.assertEqual(200, self.get().status)

    def test_concurrent_overload_has_no_waiting_query(self):
        self.reader = HttpReadService(self.settings, HttpLimits(max_in_flight=1))
        entered, release = threading.Event(), threading.Event()
        completed = []
        execute = self.reader._execute
        def wait(service, request, revision):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release admitted request")
            return execute(service, request, revision)
        with patch.object(self.reader, "_execute", side_effect=wait):
            thread = threading.Thread(target=lambda: completed.append(self.get()))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.code(self.get(), "service_busy", 503)
                self.assertEqual(200, self.get("/health/live").status)
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(200, completed[0].status)
        self.assertEqual(1, self.reader._services.qsize())

    def test_direct_query_pinning_and_missing_versions(self):
        self.build("two")
        service = QueryService(self.settings)
        self.assertEqual("one", service.packages(Query(), revision="one")["revision"])
        self.assertEqual("one", service.facets(revision="one")["revision"])
        self.assertEqual("one", service.sources(revision="one")["revision"])
        self.assertEqual("one", service.changes(revision="one")["revision"])
        with self.assertRaises(ServiceError) as error:
            service.package("dsh/plugin/数据库", version="unknown")
        self.assertEqual("version_not_found", error.exception.code)


class HttpConfigurationTests(unittest.TestCase):
    def test_limits_reject_nonfinite_boolean_and_unbounded_values(self):
        for changes in ({"max_in_flight": True}, {"max_in_flight": 0}, {"rate_burst": 0},
                        {"cache_ttl_seconds": float("nan")}, {"rate_per_second": float("inf")},
                        {"cache_ttl_seconds": 31}, {"max_response_bytes": 1023}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    HttpLimits(**changes)

    def test_cors_requires_explicit_origins(self):
        self.assertEqual(["https://example.test"], _origins(["https://example.test", "https://example.test"]))
        for value in ("*", "https://example.test/path", "https://user:secret@example.test", "https://example.test:0", "https://example.test:bad", "https://example.test?x=1", " https://example.test", "https://*.example.test", "https://example.test#"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _origins([value])


if __name__ == "__main__":
    unittest.main()
