"""Offline npm identity, interval, queue, publication and query contracts."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from agent_forge_service.downloads import (DownloadsClient, DownloadsRequest, DownloadsResponse, DownloadsTransport,
    MAX_BYTES, declared_packages, enqueue_downloads, normalize_downloads, package_name,
    plan_download_jobs, read_download_status, request_for_day, run_download_jobs)
from agent_forge_service.download_projection import DOWNLOAD_POLICY, download_signal
from agent_forge_service.errors import ServiceError
from agent_forge_service.github_client import CollectionError
from agent_forge_service.publishing import SnapshotStore, build_and_publish
from agent_forge_service.query import Query, QueryService
from agent_forge_service.settings import Settings
from agent_forge_service.storage import open_state, read_snapshot, timestamp
from service_tests.temporary import cleanup_temporary
from service_tests.test_publication_query import NOW, record


def body_for(request, *, daily=24):
    from datetime import date
    start = date.fromisoformat(request.start)
    return {"package": request.package, "start": request.start, "end": request.end,
        "downloads": [{"day": (start + timedelta(days=i)).isoformat(), "downloads": daily} for i in range(30)]}


class DownloadsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="forge-download-test-")
        self.root = Path(self.temp.name)
        self.addCleanup(cleanup_temporary, self.temp, self.root.resolve())
        self.catalog = self.root / "catalog"
        self.packages = self.catalog / "sources/plugin/packages"
        self.packages.mkdir(parents=True)
        self.settings = Settings(self.catalog, self.root / "runtime")
        self.db = open_state(self.settings.state_path)
        self.addCleanup(self.db.close)
        self.now = NOW
        self.request = request_for_day("express", as_of=NOW)
        self.status = 200
        self.body = body_for(self.request)
        self.headers = {"etag": '"npm-fixture"'}
        self.calls = []
        def get(request, **kwargs):
            self.calls.append((request, kwargs))
            return DownloadsResponse(self.status, self.headers, self.body)
        self.transport = Mock(get=get)
        self.client = DownloadsClient(self.db, transport=self.transport, clock=lambda: self.now)

    def observe(self, name="express", daily=24):
        self.request = request_for_day(name, as_of=self.now)
        self.body = body_for(self.request, daily=daily) if self.status != 304 else None
        with self.db:
            job = enqueue_downloads(self.db, package=name, slot=timestamp(self.now), now=self.now)
        return job, run_download_jobs(self.db, self.client, owner="fixture", max_requests=1, clock=lambda: self.now, job_ids=(job,))

    def write(self, name="alpha", npm="express", *, version="1.0.0", extra=None, agents=("dsh",)):
        value = record(name, version=version, agents=agents)
        value["distributions"] = [{"id": "npm", "type": "npm-pkg", "url": "https://www.npmjs.com/package/" + npm}]
        if extra:
            value["distributions"].append({"id": "other", "type": "npm-pkg", "url": "https://www.npmjs.com/package/" + extra})
        self.packages.joinpath(name + "-" + version + ".json").write_text(json.dumps(value), encoding="utf-8")
        return value

    def build(self, revision="downloads-one"):
        return build_and_publish(self.settings, now=self.now, revision=revision)

    def service(self):
        return QueryService(self.settings)

    def test_requests_use_past_utc_days_and_encoded_scopes(self):
        request = request_for_day("@scope/pkg", as_of="2026-10-11T00:30:00+08:00")
        self.assertEqual(("2026-09-10", "2026-10-09"), (request.start, request.end))
        self.assertTrue(request.endpoint.endswith("/@scope%2Fpkg"))
        self.assertNotEqual(request.cache_key(), replace(request, package="pkg").cache_key())

    def test_invalid_package_names_cannot_inject_paths(self):
        for name in ("", "../a", "@scope/pkg/extra", "https://evil.test", "a?x=y", "a,b", "A", "_private", "a%2fb", "a\nb", "a" * 215):
            with self.subTest(name=name), self.assertRaises(ValueError): package_name(name)

    def test_wrong_dates_or_window_are_rejected(self):
        for start, end in (("2026-9-11", "2026-10-10"), ("2026-09-12", "2026-10-10"), ("2026-10-11", "2026-10-10")):
            with self.assertRaises(ValueError): DownloadsRequest("express", start, end).validate()

    def test_normalization_sorts_days_and_strips_unknown_fields(self):
        self.body["downloads"].reverse(); self.body["permissions"] = {"admin": True}
        body = normalize_downloads(self.body, self.request)
        self.assertEqual(self.request.start, body["downloads"][0]["day"])
        self.assertNotIn("permissions", body)

    def test_missing_duplicate_wrong_days_and_identity_are_rejected(self):
        mutations = (lambda b: b["downloads"].pop(), lambda b: b["downloads"].__setitem__(1, b["downloads"][0]),
            lambda b: b["downloads"][0].update(day="2026-10-11"), lambda b: b.update(package="other"), lambda b: b.update(end="2026-10-11"))
        for mutate in mutations:
            body = body_for(self.request); mutate(body)
            with self.assertRaises(ValueError): normalize_downloads(body, self.request)

    def test_counts_are_bounded_nonnegative_integers_including_zero(self):
        for count in (None, True, -1, 1.1, "1", 2**63 - 1):
            with self.subTest(count=count), self.assertRaises(ValueError): normalize_downloads(body_for(self.request, daily=count), self.request)
        self.assertEqual(0, normalize_downloads(body_for(self.request, daily=0), self.request)["downloads"][0]["downloads"])

    def test_observation_ack_and_checkpoint_commit_together(self):
        job, result = self.observe()
        self.assertEqual((1, 1), (result["requests"], result["succeeded"]))
        self.assertEqual(job, self.db.execute("SELECT job_id FROM npm_download_observations").fetchone()[0])
        self.assertEqual("succeeded", self.db.execute("SELECT state FROM jobs").fetchone()[0])
        self.assertEqual("complete", self.db.execute("SELECT completeness FROM checkpoints").fetchone()[0])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM metric_observations").fetchone()[0])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM package_units").fetchone()[0])

    def test_duplicate_enqueue_does_not_duplicate_fetch(self):
        job, _ = self.observe()
        with self.db: again = enqueue_downloads(self.db, package="express", slot=timestamp(NOW), now=NOW)
        self.assertEqual(job, again)
        report = run_download_jobs(self.db, self.client, owner="fixture", max_requests=1, clock=lambda: self.now)
        self.assertEqual(0, report["requests"])

    def test_cache_304_keeps_observation_time_and_source_interval(self):
        self.observe(); self.now += timedelta(hours=8); self.status = 304
        _, result = self.observe()
        self.assertEqual(1, result["succeeded"])
        row = self.db.execute("SELECT * FROM npm_download_observations ORDER BY checked_at DESC").fetchone()
        self.assertEqual(timestamp(NOW), row["observed_at"])
        self.assertEqual(timestamp(self.now), row["checked_at"])
        self.assertEqual('"npm-fixture"', self.calls[-1][1]["etag"])
        signal = download_signal(json.loads(row["body_json"]), observed_at=row["observed_at"], checked_at=row["checked_at"], as_of=self.now, policy=DOWNLOAD_POLICY)
        self.assertEqual(1, signal["downloadGrowth"]["value_per_hour"])

    def test_missing_304_cache_is_not_a_zero_observation(self):
        self.status = 304; _, result = self.observe()
        self.assertEqual(1, result["deferred"])
        self.assertEqual("npm_cache_miss", self.db.execute("SELECT last_error FROM jobs").fetchone()[0])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM npm_download_observations").fetchone()[0])

    def test_corrupt_304_cache_is_terminal(self):
        self.observe()
        with self.db: self.db.execute("UPDATE http_cache SET body_hash='bad'")
        self.now += timedelta(hours=8); self.status = 304
        _, result = self.observe()
        self.assertEqual(1, result["dead"])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM npm_download_observations").fetchone()[0])

    def test_rate_limit_is_persistent_and_wait_does_not_consume_attempt(self):
        self.status = 429; self.headers = {"retry-after": "300"}
        job, result = self.observe()
        self.assertEqual((1, 1), (result["requests"], result["deferred"]))
        self.now += timedelta(seconds=2)
        with self.db: other = enqueue_downloads(self.db, package="other", slot=timestamp(self.now), now=self.now)
        result = run_download_jobs(self.db, self.client, owner="fixture", max_requests=1, clock=lambda: self.now)
        self.assertEqual(0, result["requests"])
        self.assertEqual(0, self.db.execute("SELECT attempts FROM jobs WHERE id=?", (other,)).fetchone()[0])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(timestamp(NOW + timedelta(seconds=300)), self.db.execute("SELECT next_run FROM jobs WHERE id=?", (job,)).fetchone()[0])

    def test_invalid_response_is_retryable_without_sample(self):
        self.body = {"error": "not ready"}
        with self.db: enqueue_downloads(self.db, package="express", slot=timestamp(self.now), now=self.now)
        result = run_download_jobs(self.db, self.client, owner="fixture", max_requests=1, clock=lambda: self.now)
        self.assertEqual(1, result["deferred"])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM npm_download_observations").fetchone()[0])

    def test_crash_before_ack_rolls_back_sample_and_watermark(self):
        with self.db: enqueue_downloads(self.db, package="express", slot=timestamp(self.now), now=self.now)
        with patch("agent_forge_service.downloads.save_checkpoint", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError): run_download_jobs(self.db, self.client, owner="fixture", max_requests=1, clock=lambda: self.now)
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM npm_download_observations").fetchone()[0])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM checkpoints").fetchone()[0])
        self.assertEqual("leased", self.db.execute("SELECT state FROM jobs").fetchone()[0])

    def test_future_days_and_network_in_transaction_are_rejected(self):
        with self.assertRaises(ValueError): self.client.fetch(request_for_day("express", as_of=NOW + timedelta(days=2)))
        with self.db:
            self.db.execute("INSERT INTO metadata VALUES ('test','value')")
            with self.assertRaises(ValueError): self.client.fetch(self.request)
        self.assertEqual([], self.calls)

    def test_daily_rate_is_not_rolling_window_difference(self):
        signal = download_signal(self.body, observed_at=timestamp(NOW), checked_at=timestamp(NOW), as_of=NOW, policy=DOWNLOAD_POLICY)
        self.assertEqual(1, signal["downloadGrowth"]["value_per_hour"])
        self.assertEqual(24, signal["downloadGrowth"]["elapsed_hours"])
        self.assertEqual("source_interval", signal["downloadGrowth"]["time_basis"])
        self.assertEqual(720, signal["downloads30d"]["value"])
        self.assertTrue(signal["downloadGrowth"]["qualification"]["eligible"])

    def test_stale_observation_is_retained_but_not_eligible(self):
        signal = download_signal(self.body, observed_at=timestamp(NOW), checked_at=timestamp(NOW), as_of=NOW + timedelta(hours=27), policy=DOWNLOAD_POLICY)
        self.assertFalse(signal["downloadGrowth"]["qualification"]["eligible"])
        self.assertEqual(1, signal["downloadGrowth"]["value_per_hour"])

    def test_explicit_declarations_reject_private_registries_and_dependencies(self):
        value = self.write(npm="@scope/pkg", extra="other")
        self.assertEqual({"@scope/pkg", "other"}, set(declared_packages(value)))
        value["distributions"][0]["registry"] = "https://private.test"
        self.assertEqual({"other"}, set(declared_packages(value)))
        self.assertEqual({}, declared_packages({"dependencies": [{"id": "express"}]}))
        self.assertEqual({"@scope/pkg"}, set(declared_packages({"mcpDetails": {"registryType": "npm", "identifier": "@scope/pkg"}})))
        self.assertEqual({}, declared_packages({"mcpDetails": {"registryType": "npm", "identifier": "express", "registryBaseUrl": "https://private.test"}}))

    def test_shared_npm_identity_is_not_duplicated_in_public_storage(self):
        self.write("alpha"); self.write("beta"); self.observe(); manifest = self.build()
        self.assertEqual(2, manifest["downloadCoverage"]["mappedPackages"])
        self.assertEqual(1, manifest["downloadCoverage"]["sharedNpmPackages"])
        page = self.service().packages(Query(sort="downloads", metric_source="npmjs.org"))
        self.assertEqual(["alpha", "beta"], [item["name"] for item in page["items"]])
        self.assertEqual([720, 720], [item["downloadMetrics"]["downloads30d"]["value"] for item in page["items"]])
        db = read_snapshot(self.settings.snapshots / "downloads-one/catalog.db")
        try:
            self.assertEqual(1, db.execute("SELECT count(*) FROM npm_download_observations").fetchone()[0])
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"jobs", "http_cache", "rate_limits", "manifest_candidates"}.isdisjoint(tables))
        finally: db.close()

    def test_missing_and_ambiguous_identity_are_unknown_not_summed(self):
        self.write("ambiguous", extra="other"); self.write("pending", npm="uncollected")
        self.packages.joinpath("unmapped.json").write_text(json.dumps(record("unmapped")), encoding="utf-8")
        self.observe(); self.build()
        items = {item["name"]: item for item in self.service().packages(Query(sort="download_growth", metric_source="npmjs.org"))["items"]}
        for name, status in (("ambiguous", "ambiguous"), ("pending", "pending"), ("unmapped", "unmapped")):
            self.assertEqual(status, items[name]["downloadMappingStatus"])
            self.assertIsNone(items[name]["downloadMetrics"])

    def test_zero_is_available_before_unknown_and_source_is_mandatory(self):
        self.write("zero"); self.write("unknown", npm="uncollected"); self.observe(daily=0); self.build()
        page = self.service().packages(Query(sort="downloads", metric_source="npmjs.org"))
        self.assertEqual(["zero", "unknown"], [item["name"] for item in page["items"]])
        self.assertEqual(0, page["items"][0]["downloadMetrics"]["downloads30d"]["value"])
        for source in (None, "pypi.org"):
            with self.assertRaises(ServiceError): self.service().packages(Query(sort="downloads", metric_source=source))
        with self.assertRaises(ServiceError): self.service().packages(Query(sort="stars", metric_source="npmjs.org"))

    def test_global_order_null_boundary_and_filter_bound_cursor(self):
        self.write("high", npm="high"); self.write("low", npm="low", agents=("other",)); self.write("unknown", npm="unknown")
        self.observe("high", daily=48); self.now += timedelta(seconds=2); self.observe("low"); self.build()
        service = self.service(); query = Query(sort="download_growth", metric_source="npmjs.org", limit=1)
        first = service.packages(query); second = service.packages(query, cursor=first["nextCursor"]); third = service.packages(query, cursor=second["nextCursor"])
        self.assertEqual(["high", "low", "unknown"], [p["items"][0]["name"] for p in (first, second, third)])
        self.assertIsNone(third["nextCursor"])
        self.assertEqual(["high", "unknown"], [i["name"] for i in service.packages(replace(query, agent="dsh", limit=20))["items"]])
        with self.assertRaises(ServiceError): service.packages(replace(query, sort="downloads"), cursor=first["nextCursor"])

    def test_actual_download_sql_uses_rank_indexes_and_fts_search(self):
        self.write(); self.observe(); self.build(); service = self.service(); actual = []; original = service._connection
        @contextlib.contextmanager
        def traced(revision):
            with original(revision) as db:
                db.set_trace_callback(lambda sql: actual.append(sql) if sql.startswith("SELECT p.*") else None)
                yield db
        with patch.object(service, "_connection", traced):
            for sort in ("downloads", "download_growth"): service.packages(Query(sort=sort, metric_source="npmjs.org"))
        with original("downloads-one") as db:
            for sql, index in zip(actual, ("package_downloads_rank", "package_download_growth_rank")):
                plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql))
                self.assertIn(index, plan); self.assertNotIn("TEMP B-TREE", plan)
        page = service.packages(Query(q="terminal", agent="dsh", sort="download_growth", metric_source="npmjs.org"))
        self.assertEqual(["alpha"], [item["name"] for item in page["items"]])

    def test_selected_version_only_and_unknown_selection_does_not_guess(self):
        self.write(version="1.0.0"); self.write(npm="new-package", version="2.0.0"); self.observe(); self.build()
        self.assertEqual("unmapped", self.service().package("plugin.alpha")["downloadMappingStatus"])
        self.catalog.joinpath("sources/plugin/index.json").write_text(json.dumps({"packages": {"alpha": {"latest": "2.0.0"}}}), encoding="utf-8")
        self.build("downloads-two")
        self.assertEqual("pending", self.service().package("plugin.alpha")["downloadMappingStatus"])

    def test_daily_history_is_revision_bound_and_source_specific(self):
        self.write(); self.observe(); self.build(); service = self.service()
        first = service.metrics("plugin.alpha", source="npmjs.org", limit=20)
        self.assertEqual(20, len(first["items"]))
        self.now += timedelta(days=1); self.observe(daily=48); self.build("downloads-two")
        second = service.metrics("plugin.alpha", source="npmjs.org", limit=20, cursor=first["nextCursor"])
        self.assertEqual("downloads-one", second["revision"]); self.assertEqual(10, len(second["items"]))
        self.assertEqual(24, second["items"][-1]["downloads"]); self.assertIsNone(second["nextCursor"])
        with self.assertRaises(ServiceError): service.metrics("plugin.alpha", source="github.com", limit=20, cursor=first["nextCursor"])
        with self.assertRaises(ServiceError): service.metrics("plugin.alpha", source="pypi.org")

    def test_planner_deduplicates_names_and_skips_pending(self):
        self.write("alpha"); self.write("beta"); self.write("gamma", npm="other"); self.build(); store = SnapshotStore(self.settings.snapshots)
        first = plan_download_jobs(self.db, store, slot=timestamp(NOW), limit=1, now=NOW)
        second = plan_download_jobs(self.db, store, slot=timestamp(NOW), limit=10, now=NOW)
        self.assertEqual((2, 1, 1), (first["declaredNpmPackages"], first["queued"], second["queued"]))
        self.assertEqual(1, second["pendingSkipped"])
        with self.assertRaises(ValueError): plan_download_jobs(self.db, store, slot=timestamp(NOW), limit=0, now=NOW)

    def test_status_is_readonly_and_does_not_upgrade_old_schema(self):
        self.observe(); self.assertEqual("express", read_download_status(self.settings.state_path)["observations"][0]["package"])
        old = self.root / "old.db"; db = sqlite3.connect(old)
        db.executescript("CREATE TABLE jobs(id TEXT,scope TEXT,state TEXT,attempts INTEGER,next_run TEXT,last_error TEXT,kind TEXT,created_at TEXT); PRAGMA user_version=6;"); db.close()
        self.assertFalse(read_download_status(old)["schemaAvailable"])
        db = sqlite3.connect(old)
        try: self.assertEqual(6, db.execute("PRAGMA user_version").fetchone()[0])
        finally: db.close()


    def test_cli_query_and_daily_history(self):
        from agent_forge_service.worker import main
        self.write(); self.observe(); self.build(); runtime = str(self.settings.runtime_root)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, main(["query", "--runtime", runtime, "--sort", "downloads", "--metric-source", "npmjs.org"]))
        self.assertEqual("npmjs.org", json.loads(output.getvalue())["metricSource"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, main(["metrics", "--runtime", runtime, "plugin.alpha", "--source", "npmjs.org", "--limit", "2"]))
        self.assertEqual(2, len(json.loads(output.getvalue())["items"]))

    def test_planner_does_not_recollect_same_utc_interval_every_eight_hours(self):
        self.write(); self.observe(); self.build()
        report = plan_download_jobs(self.db, SnapshotStore(self.settings.snapshots), slot=timestamp(NOW + timedelta(hours=8)), limit=10, now=NOW)
        self.assertEqual(0, report["queued"])
        self.assertEqual(1, report["alreadyCollected"])
        later = plan_download_jobs(self.db, SnapshotStore(self.settings.snapshots), slot=timestamp(NOW + timedelta(days=1)), limit=10, now=NOW)
        self.assertEqual(1, later["queued"])

    def test_200_without_validator_does_not_reuse_an_obsolete_etag(self):
        self.observe(); self.now += timedelta(hours=8); self.headers = {}
        _, result = self.observe(daily=48)
        self.assertEqual(1, result["succeeded"])
        self.assertIsNone(self.db.execute("SELECT etag FROM http_cache").fetchone()[0])

    def test_existing_canonical_cursor_shape_is_not_changed_by_new_metric_fields(self):
        from agent_forge_service.query import _query_hash, _encode_cursor
        self.write("alpha"); self.write("beta"); self.build()
        query = Query(sort="name", limit=1)
        old_parameters = {"q": None, "agent": None, "type": None, "subtype": None, "scope": "catalog", "sort": "name", "limit": 1,
            "facets": {}, "customFacets": {}, "policyVersion": "canonical-v1"}
        self.assertEqual(old_parameters, query.normalized(self.settings))
        cursor = _encode_cursor("downloads-one", _query_hash(old_parameters), [0, "alpha", "plugin.alpha"])
        self.assertEqual("beta", self.service().packages(query, cursor=cursor)["items"][0]["name"])


    def test_source_listing_and_dynamic_download_freshness_disclose_partial_coverage(self):
        self.write(); self.write("unknown", npm="uncollected"); self.observe(); self.build()
        service = self.service()
        sources = service.sources()
        self.assertIn("npmjs.org:downloads", [row["id"] for row in sources["items"]])
        with patch("agent_forge_service.query.utc_now", return_value=NOW + timedelta(hours=27)):
            result = service.packages(Query(sort="downloads", metric_source="npmjs.org"))
        self.assertTrue(result["downloadFreshnessSummary"]["stale"])
        self.assertEqual("snapshot_stale", result["downloadFreshnessSummary"]["reason"])
        self.assertTrue(result["downloadFreshnessSummary"]["partialCoverage"])


    def test_late_historical_retry_does_not_replace_a_newer_download_window(self):
        self.write(); self.observe(daily=48)
        self.now += timedelta(seconds=2)
        old_slot = NOW - timedelta(days=2)
        self.body = body_for(request_for_day("express", as_of=old_slot), daily=1)
        with self.db:
            enqueue_downloads(self.db, package="express", slot=timestamp(old_slot), now=self.now)
        report = run_download_jobs(self.db, self.client, owner="late-fixture", max_requests=1, clock=lambda: self.now)
        self.assertEqual(1, report["succeeded"])
        self.build()
        signal = self.service().package("plugin.alpha")["downloadMetrics"]
        self.assertEqual(48, signal["dailyDownloads"]["value"])
        self.assertEqual(2, signal["downloadGrowth"]["value_per_hour"])

    def test_recent_historical_fetch_does_not_hide_stale_measurement_time(self):
        self.write()
        old_slot = NOW - timedelta(days=3)
        self.body = body_for(request_for_day("express", as_of=old_slot))
        with self.db:
            enqueue_downloads(self.db, package="express", slot=timestamp(old_slot), now=NOW)
        run_download_jobs(self.db, self.client, owner="historical-fixture", max_requests=1, clock=lambda: self.now)
        self.build()
        with patch("agent_forge_service.query.utc_now", return_value=NOW):
            result = self.service().packages(Query(sort="downloads", metric_source="npmjs.org"))
        self.assertTrue(result["downloadFreshnessSummary"]["stale"])
        self.assertEqual(0, result["downloadCoverage"]["eligiblePackages"])



class TransportTests(unittest.TestCase):
    def response(self, body, *, status=200, headers=None, url=None):
        value = io.BytesIO(body); value.code = status; value.headers = headers or {}
        value.geturl = lambda: url or "https://api.npmjs.org" + request_for_day("express", as_of=NOW).endpoint
        return value

    def test_fixed_host_get_has_no_credential_headers(self):
        request = request_for_day("express", as_of=NOW); transport = DownloadsTransport(); transport.opener = Mock()
        transport.opener.open.return_value = self.response(json.dumps(body_for(request)).encode())
        self.assertEqual(200, transport.get(request, etag='"cache"').status)
        sent = transport.opener.open.call_args.args[0]
        self.assertEqual("GET", sent.get_method()); self.assertTrue(sent.full_url.startswith("https://api.npmjs.org/downloads/range/"))
        self.assertNotIn("Authorization", sent.headers); self.assertNotIn("Cookie", sent.headers)
        self.assertEqual('"cache"', sent.headers["If-none-match"])

    def test_redirected_oversized_encoded_and_duplicate_json_are_rejected(self):
        for raw, headers, url in ((b"{}", {}, "https://evil.test"), (b"x" * (MAX_BYTES + 1), {}, None),
            (b"{}", {"Content-Encoding": "gzip"}, None), (b'{"package":"express","package":"evil"}', {}, None)):
            transport = DownloadsTransport(); transport.opener = Mock()
            transport.opener.open.return_value = self.response(raw, headers=headers, url=url)
            with self.assertRaises(CollectionError): transport.get(request_for_day("express", as_of=NOW))

    def test_cached_headers_cannot_inject_headers(self):
        transport = DownloadsTransport(); transport.opener = Mock()
        with self.assertRaises(ValueError): transport.get(request_for_day("express", as_of=NOW), etag="x\r\nAuthorization: secret")
        transport.opener.open.assert_not_called()
