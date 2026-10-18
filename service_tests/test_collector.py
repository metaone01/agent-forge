import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_forge_service.collector import collection_status, enqueue_repository, run_repository_jobs
from agent_forge_service.github import GhTransport
from agent_forge_service.github_client import GitHubClient, RequestPolicy
from agent_forge_service.jobs import JobQueue
from agent_forge_service.metrics import MetricSample, rate_between
from agent_forge_service.storage import open_state, timestamp
from service_tests.temporary import cleanup_temporary

NOW = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
BODY = {"id": 123, "full_name": "example/public-repo", "private": False, "visibility": "public", "topics": ["mcp-server"],
        "stargazers_count": 10, "forks_count": 3, "created_at": "2020-01-01T00:00:00Z", "pushed_at": "2026-10-10T00:00:00Z"}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(cleanup_temporary, self.temp, Path(self.temp.name))
        self.path = Path(self.temp.name) / "runtime/state.db"
        self.db = open_state(self.path); self.addCleanup(self.db.close)
        self.now = NOW; self.responses = []; self.calls = []
        def runner(*args):
            self.calls.append(args[0]); return self.responses.pop(0)
        self.client = GitHubClient(self.db, identity="test-profile", transport=GhTransport(runner=runner),
                                   policy=RequestPolicy(jitter_fraction=0), clock=lambda: self.now)
        self.queue = JobQueue(self.db)

    def push(self, status=200, body=BODY):
        text = f'HTTP/2.0 {status} status\r\nETag: "unchanged"\r\n\r\n' + (json.dumps(body) if body is not None else "")
        self.responses.append((0 if status < 400 else 1, text.encode()))

    def enqueue(self, *, repository=123, bootstrap=True, slot=None):
        with self.db:
            return enqueue_repository(self.queue, repository=repository, slot=slot or timestamp(self.now), bootstrap=bootstrap, now=self.now)

    def run_jobs(self, budget=1):
        return run_repository_jobs(self.db, self.client, owner="unit-worker", max_requests=budget, clock=lambda: self.now)

    def test_metadata_stars_and_evidence_are_atomic_and_not_canonical(self):
        self.enqueue(); self.push(); result = self.run_jobs()
        self.assertEqual(1, result["succeeded"])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM package_units").fetchone()[0])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM change_events").fetchone()[0])
        row = self.db.execute("SELECT * FROM repositories").fetchone()
        self.assertEqual(123, row["github_id"]); self.assertTrue(row["bootstrap"])
        sample = json.loads(self.db.execute("SELECT sample_json FROM metric_observations").fetchone()[0])
        self.assertEqual("repository", sample["subject_kind"])
        self.assertEqual(10, sample["value"])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM collection_evidence").fetchone()[0])
        self.assertEqual("complete", self.db.execute("SELECT completeness FROM checkpoints").fetchone()[0])
        self.assertEqual({"succeeded": 1}, collection_status(self.path)["jobs"])

    def test_repeated_slot_is_idempotent_and_304_is_not_a_fresh_metric(self):
        job = self.enqueue(); self.push(); self.run_jobs()
        self.assertEqual(job, self.enqueue())
        self.assertEqual(0, self.run_jobs()["requests"])
        self.now += timedelta(hours=8)
        self.enqueue(bootstrap=False); self.push(304, None); self.run_jobs()
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM metric_observations").fetchone()[0])
        self.assertEqual(2, self.db.execute("SELECT count(*) FROM repository_observations").fetchone()[0])
        row = self.db.execute("SELECT * FROM repositories").fetchone()
        self.assertEqual(timestamp(NOW), row["first_seen_at"])
        self.assertEqual(timestamp(self.now), row["last_success_at"])
        self.assertTrue(row["bootstrap"])
        checkpoint = self.db.execute("SELECT * FROM checkpoints").fetchone()
        self.assertEqual(timestamp(NOW), checkpoint["watermark"])
        self.assertEqual(timestamp(self.now), checkpoint["last_success_at"])

    def test_alias_and_numeric_endpoint_caches_do_not_regress_checkpoint(self):
        self.enqueue(repository="example/public-repo"); self.push(); self.run_jobs()
        self.now += timedelta(hours=8)
        self.enqueue(repository=123); self.push(); self.run_jobs()
        self.now += timedelta(hours=8)
        self.enqueue(repository="example/public-repo"); self.push(304, None)
        self.assertEqual(1, self.run_jobs()["succeeded"])
        row = self.db.execute("SELECT * FROM checkpoints").fetchone()
        self.assertEqual(timestamp(NOW + timedelta(hours=8)), row["watermark"])
        self.assertEqual(timestamp(self.now), row["last_success_at"])
        self.assertEqual(2, self.db.execute("SELECT count(*) FROM metric_observations").fetchone()[0])

    def test_rename_and_negative_star_delta_keep_stable_identity(self):
        self.enqueue(); self.push(); self.run_jobs()
        self.now += timedelta(hours=8)
        self.enqueue(); self.push(body={**BODY, "full_name": "transferred/new-name", "stargazers_count": 6}); self.run_jobs()
        row = self.db.execute("SELECT * FROM repositories").fetchone()
        self.assertEqual("transferred/new-name", row["full_name"])
        self.assertEqual(timestamp(NOW), row["first_seen_at"])
        samples = [MetricSample(**json.loads(r[0])) for r in self.db.execute("SELECT sample_json FROM metric_observations ORDER BY observed_at")]
        self.assertEqual(-0.5, rate_between(*samples).value_per_hour)

    def test_missing_stars_is_unknown_not_zero_and_downloads_are_not_invented(self):
        self.enqueue(); self.push(body={k: v for k, v in BODY.items() if k not in ("stargazers_count", "topics")}); self.run_jobs()
        sample = json.loads(self.db.execute("SELECT sample_json FROM metric_observations").fetchone()[0])
        self.assertIsNone(sample["value"])
        self.assertEqual("not_reported", sample["availability"])
        self.assertIsNone(json.loads(self.db.execute("SELECT metadata_json FROM repositories").fetchone()[0])["topics"])
        self.assertFalse(self.db.execute("SELECT 1 FROM metric_observations WHERE metric='downloads'").fetchone())

    def test_private_or_404_does_not_remove_last_good_public_metadata(self):
        self.enqueue(); self.push(); self.run_jobs()
        self.now += timedelta(hours=8)
        self.enqueue(); self.push(body={**BODY, "private": True}); result = self.run_jobs()
        self.assertEqual(1, result["dead"])
        self.now += timedelta(hours=8)
        self.enqueue(); self.push(404, {"message": "Not Found"}); result = self.run_jobs()
        self.assertEqual(1, result["deferred"])
        self.assertEqual(timestamp(NOW), self.db.execute("SELECT last_success_at FROM repositories").fetchone()[0])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM repository_observations").fetchone()[0])

    def test_crash_before_ack_rolls_back_observations_then_recovers_from_cache(self):
        self.enqueue(); self.push()
        with patch("agent_forge_service.collector.save_checkpoint", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.run_jobs()
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM repositories").fetchone()[0])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM collection_evidence").fetchone()[0])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM metric_observations").fetchone()[0])
        self.assertEqual("leased", self.db.execute("SELECT state FROM jobs").fetchone()[0])
        self.now += timedelta(minutes=2); self.push(304, None)
        self.assertEqual(1, self.run_jobs()["succeeded"])
        sample = self.db.execute("SELECT observed_at FROM metric_observations").fetchone()[0]
        self.assertEqual(timestamp(NOW), sample)

    def test_spacing_defer_is_persistent_and_does_not_claim_network_work(self):
        self.enqueue(); self.enqueue(repository="example/public-repo"); self.push()
        result = self.run_jobs(budget=2)
        self.assertEqual(1, result["succeeded"]); self.assertEqual(1, result["deferred"])
        self.assertEqual(1, result["requests"]); self.assertEqual(1, len(self.calls))
        row = self.db.execute("SELECT attempts,state FROM jobs WHERE state='retry_wait'").fetchone()
        self.assertEqual(0, row["attempts"])

    def test_invalid_metadata_is_not_committed_as_success(self):
        self.enqueue(); self.push(body={**BODY, "stargazers_count": "not-a-count"})
        result = self.run_jobs()
        self.assertEqual(0, result["succeeded"]); self.assertEqual(1, result["deferred"])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM repositories").fetchone()[0])

    def test_canonical_publication_preserves_collector_state_without_publishing_unmapped_facts(self):
        from agent_forge_service.publishing import build_and_publish
        from agent_forge_service.query import QueryService
        from agent_forge_service.settings import Settings
        from service_tests.test_publication_query import record
        self.enqueue(); self.push(); self.run_jobs()
        catalog = Path(self.temp.name) / "catalog"
        packages = catalog / "sources/plugin/packages"
        packages.mkdir(parents=True)
        (packages / "alpha.json").write_text(json.dumps(record()), encoding="utf-8")
        settings = Settings(catalog, self.path.parent)
        build_and_publish(settings, now=NOW, revision="collector-and-canonical")
        service = QueryService(settings)
        detail = service.package("plugin.alpha")
        self.assertEqual([], detail["observations"])
        self.assertEqual("upstream_not_collected", service.packages()["items"][0]["freshness"]["reason"])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM repositories").fetchone()[0])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM metric_observations").fetchone()[0])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM package_units").fetchone()[0])
        snapshot = sqlite3.connect((settings.snapshots / "collector-and-canonical/catalog.db").resolve().as_uri() + "?mode=ro", uri=True)
        try:
            self.assertFalse(snapshot.execute("SELECT name FROM sqlite_master WHERE name IN ('jobs','http_cache','rate_limits','collection_evidence')").fetchall())
        finally:
            snapshot.close()

    def test_cli_enqueue_and_status_are_local_only(self):
        runtime = Path(self.temp.name) / "cli-runtime"
        command = [sys.executable, "-m", "agent_forge_service.worker", "collect-enqueue", "--runtime", str(runtime),
                   "--repository", "cli/cli", "--slot", timestamp(NOW), "--bootstrap"]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(result.stdout)["queued"])
        result = subprocess.run([sys.executable, "-m", "agent_forge_service.worker", "collect-status", "--runtime", str(runtime)],
                                capture_output=True, text=True, check=True)
        self.assertEqual({"pending": 1}, json.loads(result.stdout)["jobs"])
