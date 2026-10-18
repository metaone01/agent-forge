"""Tests for resumable public repository discovery."""
import json
import errno
import time
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from agent_forge_service.collector import collection_status
from agent_forge_service.discovery import (DiscoveryPolicy, discovery_status, enqueue_window,
    enqueue_incremental, enqueue_history, run_discovery_jobs)
from agent_forge_service.errors import ServiceError
from agent_forge_service.github import GhTransport
from agent_forge_service.github_client import GitHubClient, RequestPolicy
from agent_forge_service.storage import open_state, timestamp

NOW = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
START = NOW - timedelta(hours=8)


def item(identity, **extra):
    return {"id": identity, "full_name": f"example/repo-{identity}", "private": False,
            "visibility": "public", "topics": ["mcp-server"], **extra}


def result(total=0, identities=(), incomplete=False):
    return {"total_count": total, "incomplete_results": incomplete, "items": [item(i) for i in identities]}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="forge-discovery-test-"); self.addCleanup(self.cleanup_temporary)
        self.path = Path(self.temp.name) / "runtime/state.db"
        self.db = open_state(self.path); self.addCleanup(self.db.close)
        self.now = NOW; self.responses = []; self.calls = []; self.response_for = None
        def runner(*args):
            self.calls.append(args[0])
            status, body = self.response_for(args[0][-1]) if self.response_for else self.responses.pop(0)
            headers = f'HTTP/2.0 {status} status\r\nETag: "page"\r\n\r\n'
            return (0 if status == 200 else 1), (headers + (json.dumps(body) if body is not None else "")).encode()
        self.client = GitHubClient(self.db, identity="discovery-test", transport=GhTransport(runner=runner),
                                   policy=RequestPolicy(jitter_fraction=0), clock=lambda: self.now)

    def cleanup_temporary(self):
        target = Path(self.temp.name).resolve()
        if target.parent != Path(tempfile.gettempdir()).resolve() or not target.name.startswith("forge-discovery-test-"):
            raise RuntimeError("temporary cleanup target escaped its owned directory")
        for attempt in range(6):
            try:
                self.temp.cleanup()
                return
            except OSError as error:
                if (error.errno != errno.ENOTEMPTY and getattr(error, "winerror", None) != 145) or attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def enqueue(self, *, start=START, end=NOW, generation="run-1", policy=None, topic="mcp-server", field="created"):
        with self.db:
            return enqueue_window(self.db, topic=topic, field=field, start=timestamp(start), end=timestamp(end),
                                  generation=generation, policy=policy, now=self.now)

    def push(self, total=0, identities=(), incomplete=False, status=200):
        body = result(total, identities, incomplete) if status == 200 else None if status == 304 else {"message": "temporary"}
        self.responses.append((status, body))

    def tick(self, budget=1):
        report = run_discovery_jobs(self.db, self.client, owner="discovery-worker", max_requests=budget, clock=lambda: self.now)
        self.now += timedelta(seconds=7)
        return report

    def run_row(self, identity):
        return self.db.execute("SELECT * FROM discovery_runs WHERE id=?", (identity,)).fetchone()

    def checkpoint(self, identity):
        return self.db.execute("SELECT * FROM checkpoints WHERE shard_key=?", (identity,)).fetchone()

    def test_complete_page_records_candidates_not_canonical_or_stars(self):
        run_id = self.enqueue(); self.push(2, [1, 2]); report = self.tick()
        self.assertEqual(1, report["succeeded"])
        self.assertEqual("complete", self.run_row(run_id)["state"])
        self.assertEqual(timestamp(NOW), self.checkpoint(run_id)["watermark"])
        self.assertEqual(2, discovery_status(self.db)["candidates"])
        for table in ("package_units", "metric_observations", "repositories"):
            self.assertEqual(0, self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        self.assertEqual("discovered", self.db.execute("SELECT classification FROM discovery_candidates LIMIT 1").fetchone()[0])
        self.assertEqual("search_pages_traversed_not_snapshot_guarantee", discovery_status(self.db)["coverageSemantics"])

    def test_next_page_and_checkpoint_are_persisted_before_requesting_it(self):
        run_id = self.enqueue(); self.push(150, range(1, 101)); self.tick()
        checkpoint = self.checkpoint(run_id)
        self.assertIsNone(checkpoint["watermark"])
        self.assertEqual(2, json.loads(checkpoint["cursor_json"])["pending"][0]["page"])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0])
        self.push(150, range(101, 151)); self.tick()
        self.assertEqual("complete", self.run_row(run_id)["state"])
        self.assertEqual(150, discovery_status(self.db)["candidates"])
        self.assertEqual(2, self.db.execute("SELECT count(*) FROM discovery_pages").fetchone()[0])

    def test_failed_page_keeps_cursor_and_retries_same_page(self):
        run_id = self.enqueue(); self.push(101, range(1, 101)); self.tick()
        self.push(status=500); report = self.tick()
        self.assertEqual(1, report["deferred"])
        self.assertIsNone(self.checkpoint(run_id)["watermark"])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM discovery_pages").fetchone()[0])
        self.now += timedelta(minutes=1)
        self.push(101, [101]); self.tick()
        self.assertEqual("complete", self.run_row(run_id)["state"])
        pages = [parse_qs(urlsplit("https://github.com/" + argv[-1]).query)["page"][0] for argv in self.calls]
        self.assertEqual(["1", "2", "2"], pages)

    def test_crash_before_ack_rolls_back_candidates_page_cursor_and_next_task(self):
        run_id = self.enqueue(); self.push(101, range(1, 101))
        with patch("agent_forge_service.discovery._refresh_run", side_effect=RuntimeError("crash before ack")):
            with self.assertRaises(RuntimeError):
                self.tick()
        for table in ("discovery_pages", "discovery_candidates", "discovery_hits", "collection_evidence", "checkpoints"):
            self.assertEqual(0, self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], table)
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM jobs").fetchone()[0])
        self.assertEqual("leased", self.db.execute("SELECT state FROM jobs").fetchone()[0])
        self.now += timedelta(seconds=91); self.push(status=304); self.tick()
        self.assertEqual(100, discovery_status(self.db)["candidates"])
        self.assertEqual(timestamp(NOW), self.db.execute("SELECT first_seen_at FROM discovery_candidates LIMIT 1").fetchone()[0])
        self.push(101, [101]); self.tick()
        self.assertEqual("complete", self.run_row(run_id)["state"])

    def test_overflow_splits_inclusive_time_ranges_and_waits_for_all_children(self):
        run_id = self.enqueue()
        midpoint = timestamp(START + timedelta(hours=4)).replace(".000000Z", "Z")
        def response_for(endpoint):
            params = parse_qs(urlsplit("https://github.com/" + endpoint).query)
            query, page = params["q"][0], int(params["page"][0])
            if query.endswith(midpoint):
                identities = list(range(1, 501))
            elif ("created:" + midpoint + "..") in query:
                identities = list(range(500, 1002))
            else:
                return 200, result(1001, range(1, 101))
            return 200, result(len(identities), identities[(page - 1) * 100:page * 100])
        self.response_for = response_for
        self.tick()
        self.assertEqual(3, self.db.execute("SELECT count(*) FROM discovery_shards").fetchone()[0])
        self.assertIsNone(self.checkpoint(run_id)["watermark"])
        for _ in range(11):
            self.tick()
        self.assertEqual("complete", self.run_row(run_id)["state"])
        self.assertEqual(1001, discovery_status(self.db)["candidates"])
        self.assertEqual(12, self.db.execute("SELECT count(*) FROM discovery_pages").fetchone()[0])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM discovery_candidates WHERE github_id=500").fetchone()[0])

    def test_incomplete_zero_count_is_not_a_complete_empty_search(self):
        run_id = self.enqueue(start=NOW - timedelta(seconds=60), policy=DiscoveryPolicy(min_shard_seconds=60))
        self.push(incomplete=True); self.tick()
        self.assertEqual("partial", self.run_row(run_id)["state"])
        self.assertIsNone(self.checkpoint(run_id)["watermark"])
        self.assertIn("unsplittable", self.db.execute("SELECT reason FROM discovery_shards").fetchone()[0])

    def test_shard_budget_exhaustion_is_visible_without_false_watermark(self):
        run_id = self.enqueue(policy=DiscoveryPolicy(max_shards=1)); self.push(1001, range(1, 101)); self.tick()
        self.assertEqual("partial", self.run_row(run_id)["state"])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM discovery_shards").fetchone()[0])
        self.assertIn("shard_budget", self.db.execute("SELECT reason FROM discovery_shards").fetchone()[0])
        self.assertIsNone(self.checkpoint(run_id)["watermark"])

    def test_duplicate_or_short_pages_never_advance_coverage(self):
        for total, identities in [(2, [1, 1]), (200, range(1, 51))]:
            run_id = self.enqueue(generation="bad-" + str(total), policy=DiscoveryPolicy(max_shards=1))
            self.push(total, identities); self.tick()
            self.assertEqual("partial", self.run_row(run_id)["state"])
            self.assertIsNone(self.checkpoint(run_id)["watermark"])

    def test_cross_page_duplicates_or_changed_total_trigger_partial_not_success(self):
        run_id = self.enqueue(policy=DiscoveryPolicy(max_shards=1)); self.push(101, range(1, 101)); self.tick()
        self.push(101, [100]); self.tick()
        self.assertEqual("partial", self.run_row(run_id)["state"])
        run_id = self.enqueue(generation="changed", policy=DiscoveryPolicy(max_shards=1))
        self.push(101, range(1, 101)); self.tick(); self.push(102, [101, 102]); self.tick()
        self.assertEqual("partial", self.run_row(run_id)["state"])
        self.assertIsNone(self.checkpoint(run_id)["watermark"])

    def test_exactly_1000_results_stops_at_page_ten(self):
        run_id = self.enqueue()
        for page in range(10):
            self.push(1000, range(page * 100 + 1, page * 100 + 101)); self.tick()
        self.assertEqual("complete", self.run_row(run_id)["state"])
        self.assertEqual(10, len(self.calls))
        self.assertEqual(1000, discovery_status(self.db)["candidates"])

    def test_private_response_or_dead_task_blocks_coverage_without_publishing(self):
        run_id = self.enqueue()
        self.responses.append((200, {"total_count": 1, "incomplete_results": False, "items": [item(1, private=True)]}))
        self.assertEqual(1, self.tick()["dead"])
        self.assertEqual("partial", self.run_row(run_id)["state"])
        self.assertEqual(0, discovery_status(self.db)["candidates"])
        self.assertIsNone(self.checkpoint(run_id)["watermark"])

    def test_repeated_window_is_idempotent_but_changed_policy_is_rejected(self):
        first = self.enqueue(); self.assertEqual(first, self.enqueue())
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM jobs").fetchone()[0])
        with self.assertRaises(ServiceError):
            self.enqueue(policy=DiscoveryPolicy(max_shards=1))

    def test_rename_topics_and_generations_do_not_reset_candidate_first_seen(self):
        self.enqueue(); self.push(1, [1]); self.tick()
        self.enqueue(topic="claude-code-plugin", generation="second")
        self.responses.append((200, {"total_count": 1, "incomplete_results": False,
                                     "items": [item(1, full_name="transferred/new-name")]})); self.tick()
        candidate = self.db.execute("SELECT * FROM discovery_candidates").fetchone()
        self.assertEqual(timestamp(NOW), candidate["first_seen_at"])
        self.assertEqual("transferred/new-name", candidate["full_name"])
        self.assertEqual(2, self.db.execute("SELECT count(*) FROM discovery_hits").fetchone()[0])

    def test_known_bootstrap_repository_is_not_rediscovered_as_new(self):
        with self.db:
            self.db.execute("INSERT INTO repositories VALUES (?,?,?,?,?,?,?)",
                (1, "example/repo-1", timestamp(NOW - timedelta(days=10)), 1, timestamp(NOW), timestamp(NOW), "{}"))
        self.enqueue(); self.push(1, [1]); self.tick()
        candidate = self.db.execute("SELECT * FROM discovery_candidates").fetchone()
        self.assertTrue(candidate["bootstrap"])
        self.assertEqual(timestamp(NOW - timedelta(days=10)), candidate["first_seen_at"])

    def test_incremental_overlap_keeps_last_successful_watermark_on_partial(self):
        bootstrap = timestamp(NOW - timedelta(days=2))
        with self.db:
            first = enqueue_incremental(self.db, topic="mcp-server", field="created", bootstrap_start=bootstrap,
                cutoff=timestamp(NOW), generation="g1", overlap_seconds=4 * 3600, now=self.now)
        self.push(); self.tick()
        self.assertEqual("complete", self.run_row(first)["state"])
        self.now = NOW + timedelta(hours=8)
        with self.db:
            second = enqueue_incremental(self.db, topic="mcp-server", field="created", bootstrap_start=bootstrap,
                cutoff=timestamp(self.now), generation="g2", overlap_seconds=4 * 3600, now=self.now,
                policy=DiscoveryPolicy(max_shards=1))
        self.assertEqual(timestamp(NOW - timedelta(hours=4)), self.run_row(second)["window_start"])
        self.push(incomplete=True); self.tick()
        self.assertEqual(timestamp(NOW), self.db.execute("SELECT watermark FROM discovery_streams").fetchone()[0])
        self.now = NOW + timedelta(hours=16)
        with self.db:
            third = enqueue_incremental(self.db, topic="mcp-server", field="created", bootstrap_start=bootstrap,
                cutoff=timestamp(self.now), generation="g3", overlap_seconds=4 * 3600, now=self.now)
        self.assertEqual(timestamp(NOW - timedelta(hours=4)), self.run_row(third)["window_start"])
        self.assertEqual(timestamp(self.now), self.run_row(third)["window_end"])

    def test_incremental_active_run_resumes_and_generation_cutoff_is_frozen(self):
        def enqueue(generation, cutoff):
            return enqueue_incremental(self.db, topic="mcp-server", field="pushed", bootstrap_start=timestamp(START),
                cutoff=timestamp(cutoff), generation=generation, overlap_seconds=86400, now=self.now)
        with self.db:
            first = enqueue("g1", NOW)
        self.now += timedelta(hours=8)
        with self.db:
            self.assertEqual(first, enqueue("g2", self.now))
        self.push(); self.tick()
        with self.db:
            self.assertEqual(first, enqueue("g1", self.now.replace(microsecond=0)))
        self.assertEqual(timestamp(NOW), self.run_row(first)["window_end"])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM jobs").fetchone()[0])

    def test_stream_configuration_conflicts_and_future_coverage_are_rejected(self):
        with self.db:
            enqueue_incremental(self.db, topic="mcp-server", field="created", bootstrap_start=timestamp(START),
                cutoff=timestamp(NOW), generation="g1", overlap_seconds=86400, now=self.now)
        with self.assertRaises(ServiceError), self.db:
            enqueue_incremental(self.db, topic="mcp-server", field="created", bootstrap_start=timestamp(START),
                cutoff=timestamp(NOW), generation="g2", overlap_seconds=0, now=self.now)
        with self.assertRaises(ValueError):
            self.enqueue(end=NOW + timedelta(seconds=1))
        with self.assertRaises(ValueError):
            self.enqueue(start=NOW, end=NOW)
        with self.assertRaises(ValueError):
            self.enqueue(generation="bad\nlabel")
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM discovery_runs").fetchone()[0])

    def test_history_advances_contiguously_and_new_cycle_restarts_rescan(self):
        def enqueue(cycle):
            return enqueue_history(self.db, topic="mcp-server", start=timestamp(NOW - timedelta(hours=16)),
                end=timestamp(NOW), cycle=cycle, chunk_seconds=8 * 3600, now=self.now)
        with self.db:
            first = enqueue("cycle-1")
        self.push(); self.tick()
        stream = self.db.execute("SELECT * FROM discovery_streams").fetchone()
        self.assertEqual(timestamp(START), stream["watermark"])
        self.assertNotEqual(first, stream["active_run_id"])
        self.assertEqual(timestamp(START), self.run_row(stream["active_run_id"])["window_start"])
        self.push(); self.tick()
        stream = self.db.execute("SELECT * FROM discovery_streams").fetchone()
        self.assertEqual(timestamp(NOW), stream["watermark"])
        self.assertIsNone(stream["active_run_id"])
        with self.db:
            self.assertIsNone(enqueue("cycle-1"))
            new_run = enqueue("cycle-2")
        self.assertEqual(timestamp(NOW - timedelta(hours=16)), self.run_row(new_run)["window_start"])
        self.assertEqual(2, self.db.execute("SELECT count(*) FROM discovery_streams").fetchone()[0])

    def test_partial_history_stops_before_next_chunk_instead_of_skipping_hole(self):
        with self.db:
            run_id = enqueue_history(self.db, topic="mcp-server", start=timestamp(NOW - timedelta(hours=16)),
                end=timestamp(NOW), cycle="blocked", chunk_seconds=8 * 3600, now=self.now,
                policy=DiscoveryPolicy(max_shards=1))
        self.push(incomplete=True); self.tick()
        self.assertEqual("partial", self.run_row(run_id)["state"])
        self.assertIsNone(self.db.execute("SELECT watermark FROM discovery_streams").fetchone()[0])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM discovery_runs").fetchone()[0])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0])

    def test_old_repository_new_topic_hit_keeps_creation_and_discovery_separate(self):
        with self.db:
            enqueue_history(self.db, topic="mcp-server", start="2020-01-01T00:00:00Z", end="2021-01-01T00:00:00Z",
                cycle="old-repositories", chunk_seconds=365 * 86400, now=self.now)
        self.responses.append((200, {"total_count": 1, "incomplete_results": False,
            "items": [item(1, created_at="2020-06-01T00:00:00Z")]})); self.tick()
        candidate = self.db.execute("SELECT * FROM discovery_candidates").fetchone()
        self.assertEqual(timestamp(NOW), candidate["first_seen_at"])
        self.assertEqual("2020-06-01T00:00:00.000000Z", json.loads(candidate["metadata_json"])["createdAt"])

    def test_incomplete_parent_is_only_completed_after_both_child_queries(self):
        run_id = self.enqueue(); self.push(incomplete=True); self.tick()
        self.assertIsNone(self.checkpoint(run_id)["watermark"])
        self.push(); self.tick()
        self.assertIsNone(self.checkpoint(run_id)["watermark"])
        self.push(); self.tick()
        self.assertEqual("complete", self.run_row(run_id)["state"])

    def test_exhausted_expired_lease_is_reconciled_as_partial_without_new_request(self):
        run_id = self.enqueue()
        with self.db:
            self.db.execute("UPDATE jobs SET max_attempts=1")
        from agent_forge_service.jobs import JobQueue
        JobQueue(self.db).claim(owner="interrupted", now=self.now, kind="github_discovery", lease_seconds=1)
        self.now += timedelta(seconds=2)
        self.assertEqual(0, self.tick()["requests"])
        self.assertEqual("partial", self.run_row(run_id)["state"])
        self.assertIsNone(self.checkpoint(run_id)["watermark"])


    def test_cli_enqueue_and_status_are_local_and_idempotent(self):
        runtime = Path(self.temp.name) / "cli"
        command = [sys.executable, "-m", "agent_forge_service.worker", "discover-enqueue", "--runtime", str(runtime),
            "--topic", "mcp-server", "--field", "created", "--start", timestamp(START), "--end", timestamp(NOW),
            "--generation", "cli-smoke"]
        first = subprocess.run(command, capture_output=True, text=True, check=True)
        second = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(first.stdout)["runId"], json.loads(second.stdout)["runId"])
        report = collection_status(runtime / "state.db")
        self.assertEqual({"pending": 1}, report["jobs"])
        self.assertEqual({"pending": 1}, report["discovery"]["runs"])

    def test_candidates_stay_out_of_public_canonical_snapshot(self):
        from agent_forge_service.publishing import build_and_publish
        from agent_forge_service.query import Query, QueryService
        from agent_forge_service.settings import Settings
        from service_tests.test_publication_query import record
        self.enqueue(); self.push(1, [1]); self.tick()
        catalog = Path(self.temp.name) / "catalog"
        packages = catalog / "sources/plugin/packages"; packages.mkdir(parents=True)
        (packages / "alpha.json").write_text(json.dumps(record()), encoding="utf-8")
        settings = Settings(catalog, self.path.parent)
        build_and_publish(settings, now=self.now, revision="candidate-isolation")
        self.assertEqual(["plugin.alpha"], [r["id"] for r in QueryService(settings).packages()["items"]])
        self.assertEqual(1, discovery_status(self.db)["candidates"])
        snapshot = sqlite3.connect((settings.snapshots / "candidate-isolation/catalog.db").resolve().as_uri() + "?mode=ro", uri=True)
        try:
            self.assertFalse(snapshot.execute("SELECT name FROM sqlite_master WHERE name IN ('discovery_candidates','discovery_hits','discovery_page_hits','discovery_pages','discovery_runs','discovery_tasks')").fetchall())
            self.assertEqual(["github-repository:1"], [item["id"] for item in QueryService(settings).packages(Query(scope="discovery"))["items"]])
        finally:
            snapshot.close()

    def test_run_scoped_queries_use_persistent_indexes(self):
        run_id = self.enqueue()
        shards = self.db.execute("EXPLAIN QUERY PLAN SELECT * FROM discovery_shards WHERE run_id=? AND state='paging'", (run_id,)).fetchall()
        tasks = self.db.execute("EXPLAIN QUERY PLAN SELECT job_id FROM discovery_tasks WHERE run_id=?", (run_id,)).fetchall()
        self.assertTrue(any("discovery_shards_run_state" in r[3] for r in shards))
        self.assertTrue(any("discovery_tasks_run" in r[3] for r in tasks))

    def test_out_of_range_total_is_rejected_before_sqlite_storage(self):
        self.enqueue(); self.push(total=2**63)
        report = self.tick()
        self.assertEqual(1, report["deferred"])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM discovery_pages").fetchone()[0])
        self.assertEqual("github_invalid_payload", self.db.execute("SELECT last_error FROM jobs").fetchone()[0])
