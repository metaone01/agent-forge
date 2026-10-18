"""Unified npm batches: published inputs, source budgets, daily windows and recovery."""
import contextlib
import io
import json
import sqlite3
import unittest
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from agent_forge_service.batches import batch_status, load_policy, plan_batch, run_batch
from agent_forge_service.canonical_mapping import sync_canonical_mappings
from agent_forge_service.collector import enqueue_repository, run_repository_jobs
from agent_forge_service.download_batches import DownloadsBatchPolicy, published_download_inputs
from agent_forge_service.downloads import DownloadsResponse, IDENTITY, enqueue_downloads, run_download_jobs
from agent_forge_service.errors import ServiceError
from agent_forge_service.github import GhTransport
from agent_forge_service.github_client import GitHubClient, RequestPolicy
from agent_forge_service.jobs import JobQueue
from agent_forge_service.publishing import SnapshotStore
from agent_forge_service.storage import encode_json, open_state, timestamp
from service_tests import test_downloads as fixtures
from service_tests.test_batches import POLICY
from service_tests.test_collector import BODY

NOW = fixtures.NOW
POLICY_NPM = replace(POLICY, refresh_budget=0, downloads=DownloadsBatchPolicy(4, 1, 168, .25))


class DownloadBatchTests(unittest.TestCase):
    setUp = fixtures.DownloadsTests.setUp
    observe = fixtures.DownloadsTests.observe
    build = fixtures.DownloadsTests.build
    service = fixtures.DownloadsTests.service

    def write(self, name="alpha", npm="express", *, version="1.0.0", extra=None, agents=("dsh",)):
        value = fixtures.DownloadsTests.write(self, name, npm, version=version, extra=extra, agents=agents)
        value["updatedAt"] = timestamp(NOW - timedelta(days=10))
        self.packages.joinpath(name + "-" + version + ".json").write_text(json.dumps(value), encoding="utf-8")
        return value

    def seed(self, names=("hot", "ordinary-a", "ordinary-b", "ordinary-c", "ordinary-d"), *, hot=True):
        for name in names: self.write(name, npm=name)
        self.now = NOW - timedelta(days=2)
        self.build("seed")
        if hot:
            self.now = NOW - timedelta(days=1)
            self.observe(names[0], daily=48)
        self.now = NOW
        self.build("published")
        self.calls.clear()
        self.status = 200
        def get(request, **kwargs):
            self.calls.append((request, kwargs)); self.now += timedelta(seconds=2)
            return DownloadsResponse(self.status, self.headers, fixtures.body_for(request))
        self.client.transport.get = get

    def plan(self, policy=POLICY_NPM):
        return plan_batch(self.db, policy, now=self.now, ranking_store=SnapshotStore(self.settings.snapshots))

    def execute(self, batch, budget=100, github=None):
        return run_batch(self.db, github, batch["batchId"], owner="npm-batch-fixture", max_requests=budget,
            clock=lambda: self.now, downloads_client=self.client)

    def test_published_top_n_and_longtail_have_independent_actual_quotas(self):
        self.seed(); batch = self.plan()
        self.assertEqual(1, batch["summary"]["downloads"]["fastDue"])
        self.assertEqual(4, batch["summary"]["downloads"]["selected"])
        self.assertEqual(1, batch["lanes"]["downloads_fast"]["quota"])
        self.assertGreaterEqual(batch["lanes"]["downloads_ordinary"]["quota"], 1)
        result = self.execute(batch)
        self.assertEqual(4, result["requestsThisInvocation"])
        self.assertEqual(4, len(self.calls))
        self.assertEqual(1, result["lanes"]["downloads_fast"]["requests"])
        self.assertGreaterEqual(result["lanes"]["downloads_ordinary"]["requests"], 1)

    def test_top_n_package_ranks_are_not_refilled_after_npm_deduplication(self):
        self.write("alpha", npm="shared"); self.write("beta", npm="shared"); self.write("gamma", npm="other")
        self.now = NOW - timedelta(days=2); self.build("seed")
        self.now = NOW - timedelta(days=1); self.observe("shared", daily=48)
        self.now += timedelta(seconds=2); self.observe("other", daily=24)
        self.now = NOW; self.build("published")
        resources, rankings, revision, status = published_download_inputs(SnapshotStore(self.settings.snapshots), now=NOW)
        self.assertEqual(2, len(resources)); self.assertEqual("published_snapshot", status)
        self.assertEqual({"npm-package:shared": 1, "npm-package:other": 3}, {row.identity: row.rank for row in rankings})
        batch = self.plan(replace(POLICY_NPM, downloads=DownloadsBatchPolicy(4, 2, 168, .25)))
        self.assertEqual(1, batch["summary"]["downloads"]["fastDue"])

    def test_same_slot_is_idempotent_and_requests_are_capped_across_restarts(self):
        self.seed(); batch = self.plan(); again = self.plan()
        self.assertEqual(batch["batchId"], again["batchId"])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM download_refresh_cohorts").fetchone()[0])
        first = self.execute(batch, budget=1); second = self.execute(batch, budget=1)
        self.assertEqual((1, 1), (first["requestsThisInvocation"], second["requestsThisInvocation"]))
        self.execute(batch)
        settled = self.execute(batch)
        self.assertEqual(0, settled["requestsThisInvocation"])
        self.assertEqual(4, len(self.calls))

    def test_successful_window_is_not_requested_again_at_next_eight_hour_slot(self):
        self.seed(names=("hot",)); batch = self.plan(); self.execute(batch)
        self.now = NOW + timedelta(hours=8)
        later = self.plan()
        self.assertEqual(0, later["summary"]["downloads"]["selected"])
        self.assertEqual(1, later["summary"]["downloads"]["alreadyCollected"])
        self.assertEqual(0, self.execute(later)["requestsThisInvocation"])
        self.assertEqual(1, len(self.calls))

    def test_next_utc_day_uses_a_new_window_not_last_poll_time_plus_24h(self):
        self.seed(names=("hot",)); self.execute(self.plan())
        self.now = NOW + timedelta(days=1)
        later = self.plan()
        self.assertEqual(1, later["summary"]["downloads"]["selected"])
        self.assertEqual(0, later["summary"]["downloads"]["alreadyCollected"])
        self.execute(later)
        self.assertNotEqual(self.calls[0][0].end, self.calls[1][0].end)

    def test_pending_same_window_is_reused_but_old_window_is_not(self):
        self.seed(names=("hot",), hot=False)
        with self.db:
            old = enqueue_downloads(self.db, package="hot", slot=timestamp(NOW - timedelta(days=1)), now=NOW)
            current = enqueue_downloads(self.db, package="hot", slot=timestamp(NOW), now=NOW)
        batch = self.plan()
        job_ids = {row[0] for row in self.db.execute("SELECT job_id FROM batch_jobs WHERE batch_id=?", (batch["batchId"],))}
        self.assertIn(current, job_ids); self.assertNotIn(old, job_ids)
        self.execute(batch)
        self.assertEqual("pending", self.db.execute("SELECT state FROM jobs WHERE id=?", (old,)).fetchone()[0])

    def test_shared_peer_observation_satisfies_attached_task_without_get(self):
        self.seed(names=("hot",)); batch = self.plan()
        self.now += timedelta(seconds=2)
        self.observe("hot")
        calls = len(self.calls)
        result = self.execute(batch)
        self.assertEqual(0, result["requestsThisInvocation"])
        self.assertEqual(calls, len(self.calls))
        self.assertEqual("settled", result["state"])
        self.assertEqual(1, result["lanes"]["downloads_fast"]["jobs"]["succeeded"])

    def test_rate_wait_does_not_charge_budget_and_resume_uses_same_task(self):
        self.seed(names=("hot",)); batch = self.plan()
        self.status = 429; self.headers = {"retry-after": "300"}
        first = self.execute(batch)
        self.assertEqual(1, first["requestsThisInvocation"])
        self.assertIsNotNone(first["nextNetworkAt"]["downloads"])
        self.assertEqual(0, self.execute(batch)["requestsThisInvocation"])
        self.now += timedelta(minutes=6); self.status = 200; self.headers = {}
        # The lane's persisted quota was spent; a later slot may adopt the same retry task, never refund it.
        self.now = NOW + timedelta(hours=8)
        later = self.plan(); self.execute(later)
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM jobs WHERE kind='npm_downloads' AND json_extract(payload_json,'$.end')='2026-10-10'").fetchone()[0])
        self.assertEqual(2, len(self.calls))

    def test_uncertain_attempt_remains_charged_after_crash(self):
        self.seed(names=("hot",)); batch = self.plan()
        with patch("agent_forge_service.batches.run_download_jobs", side_effect=RuntimeError("crash after reservation")):
            with self.assertRaises(RuntimeError): self.execute(batch)
        status = batch_status(self.db, batch["batchId"])
        self.assertEqual("uncertain", status["state"])
        self.assertEqual(1, status["lanes"]["downloads_fast"]["charged"])
        self.assertEqual(0, self.execute(batch)["requestsThisInvocation"])
        self.assertEqual([], self.calls)

    def test_dead_window_is_not_given_new_retry_budget_at_next_slot(self):
        self.seed(names=("hot",)); batch = self.plan()
        self.status = 403; self.execute(batch)
        self.now = NOW + timedelta(hours=8)
        later = self.plan()
        self.assertEqual(0, later["summary"]["downloads"]["selected"])
        self.assertEqual(1, later["summary"]["downloads"]["deadWindowResources"])
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM jobs WHERE kind='npm_downloads' AND state='dead'").fetchone()[0])

    def test_daily_cohort_is_frozen_and_current_mapping_changes_are_disclosed(self):
        self.seed(); batch = self.plan()
        cohort = batch["summary"]["downloads"]["cohortId"]
        self.now += timedelta(hours=1)
        value = fixtures.record("hot"); self.packages.joinpath("hot-1.0.0.json").write_text(json.dumps(value), encoding="utf-8")
        self.build("changed")
        self.now = NOW + timedelta(hours=8)
        later = self.plan()
        self.assertEqual(cohort, later["summary"]["downloads"]["cohortId"])
        self.assertEqual("published", later["summary"]["downloads"]["rankingRevision"])
        self.assertEqual("changed", later["summary"]["downloads"]["revision"])
        self.assertGreater(later["summary"]["downloads"]["ignoredCohortEntries"], 0)

    def test_missing_or_stale_published_metrics_do_not_fall_back_to_total_downloads(self):
        self.seed(hot=False); batch = self.plan()
        self.assertEqual("published_snapshot_no_observations", batch["summary"]["downloads"]["rankingStatus"])
        self.assertEqual(0, batch["summary"]["downloads"]["fastDue"])
        _, ranks, _, _ = published_download_inputs(SnapshotStore(self.settings.snapshots), now=NOW + timedelta(days=3))
        self.assertEqual((), ranks)

    def test_new_canonical_identity_gets_protection_not_bootstrap_old_entries(self):
        self.seed(names=("old",), hot=False)
        self.now += timedelta(hours=1); self.write("new", npm="new"); self.build("with-new")
        batch = self.plan()
        self.assertEqual(1, batch["summary"]["downloads"]["fastDue"])
        self.assertEqual(1, batch["lanes"]["downloads_fast"]["quota"])

    def test_budget_shortfall_is_reported_and_longtail_rotates(self):
        self.seed(hot=False)
        policy = replace(POLICY_NPM, downloads=DownloadsBatchPolicy(1, 1, 168, .25))
        batch = self.plan(policy)
        self.assertIn("npm_daily_target_exceeds_download_budget", batch["summary"]["downloads"]["capacityWarnings"])
        self.execute(batch)
        self.now = NOW + timedelta(hours=8); later = self.plan(policy); self.execute(later)
        self.assertEqual(2, len({call[0].package for call in self.calls}))

    def test_policy_change_requires_new_identity_and_partial_stored_config_is_rejected(self):
        self.seed(); self.plan()
        with self.assertRaises(ServiceError) as error:
            self.plan(replace(POLICY_NPM, downloads=DownloadsBatchPolicy(3, 1, 168, .25)))
        self.assertEqual("batch_policy_conflict", error.exception.code)
        legacy = replace(POLICY, policy_id="legacy", refresh=replace(POLICY.refresh, policy_id="legacy"))
        old = asdict(legacy)
        for optional in ("downloads","recommendations","manifests","releases"):old.pop(optional)
        self.assertNotIn("manifests",old);self.assertNotIn("releases",old)
        with self.db: self.db.execute("INSERT INTO batch_policies VALUES (?,?)", ("legacy", encode_json(old)))
        with self.assertRaises(ServiceError) as rejected:
            plan_batch(self.db, legacy, now=NOW)
        self.assertEqual("batch_policy_conflict", rejected.exception.code)

    def test_clients_must_share_state_and_github_identity_is_not_needed_for_npm_only(self):
        self.seed(); batch = self.plan()
        self.assertGreater(self.execute(batch)["requestsThisInvocation"], 0)
        other = open_state(self.root / "other.db")
        try:
            wrong = fixtures.DownloadsClient(other)
            with self.assertRaises(ValueError):
                run_batch(self.db, None, batch["batchId"], owner="bad", max_requests=1, downloads_client=wrong)
        finally: other.close()


    def test_initial_download_policy_parsing_and_invalid_budgets(self):
        value = json.loads(Path("examples/batch-policy.pilot.json").read_text(encoding="utf-8"))
        value.update(schemaVersion=1, policyId="npm-v2", downloads={"budget": 10, "topN": 2, "protectionHours": 168, "longtailFraction": .25})
        path = self.root / "policy.json"; path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(10, load_policy(path).downloads.budget)
        for budget in (True, -1, 1001, 1.1):
            value["downloads"]["budget"] = budget; path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ValueError): load_policy(path)

    def test_cli_download_only_batch_does_not_require_a_fake_gh_identity(self):
        from agent_forge_service.worker import main
        self.seed(names=("hot",)); batch = self.plan()
        # Block the source before the CLI invocation so this contract test never touches the network.
        with self.db:
            self.db.execute("INSERT INTO rate_limits(credential_identity,resource,blocked_until) VALUES (?,'downloads',?) ON CONFLICT(credential_identity,resource) DO UPDATE SET blocked_until=excluded.blocked_until",
                (IDENTITY, "2099-01-01T00:00:00Z"))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = main(["batch-run", "--runtime", str(self.settings.runtime_root), "--batch-id", batch["batchId"], "--max-requests", "1"])
        self.assertEqual(0, result)
        self.assertEqual(0, json.loads(output.getvalue())["requestsThisInvocation"])

    def add_repository(self):
        path = self.packages / "hot-1.0.0.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["links"] = {"repository": "https://github.com/example/repo1"}
        path.write_text(json.dumps(record), encoding="utf-8")
        sync_canonical_mappings(self.db, self.catalog, now=NOW - timedelta(days=2))
        self.github_calls = []
        def runner(*args):
            self.github_calls.append(args[0]); self.now += timedelta(seconds=2)
            return 0, ('HTTP/2.0 200 OK\r\n\r\n' + json.dumps({**BODY, "id": 1, "full_name": "example/repo1"})).encode()
        github = GitHubClient(self.db, identity="mixed-batch-fixture", transport=GhTransport(runner=runner), policy=RequestPolicy(jitter_fraction=0), clock=lambda: self.now)
        self.now = NOW - timedelta(days=1)
        with self.db:
            enqueue_repository(JobQueue(self.db), repository="example/repo1", slot=timestamp(self.now), bootstrap=True, now=self.now)
        run_repository_jobs(self.db, github, owner="seed-repo", max_requests=1, clock=lambda: self.now)
        self.github_calls.clear(); self.now = NOW + timedelta(seconds=3)
        return github

    def test_github_rate_limit_does_not_block_npm_and_vice_versa(self):
        self.seed(names=("hot",)); github = self.add_repository()
        policy = replace(POLICY_NPM, refresh_budget=1)
        batch = self.plan(policy)
        with self.db:
            self.db.execute("INSERT INTO rate_limits(credential_identity,resource,blocked_until) VALUES (?,'core',?) ON CONFLICT(credential_identity,resource) DO UPDATE SET blocked_until=excluded.blocked_until", (github.identity, "2099-01-01T00:00:00Z"))
        first = self.execute(batch, github=github)
        self.assertEqual(1, first["requestsThisInvocation"]); self.assertEqual([], self.github_calls)
        self.assertEqual(1, len(self.calls))
        with self.db:
            self.db.execute("UPDATE rate_limits SET blocked_until=NULL WHERE credential_identity=?", (github.identity,))
            self.db.execute("UPDATE rate_limits SET blocked_until='2099-01-01T00:00:00Z' WHERE credential_identity=?", (IDENTITY,))
        second = self.execute(batch, github=github)
        self.assertEqual(1, second["requestsThisInvocation"]); self.assertEqual(1, len(self.github_calls))
        self.assertEqual(1, len(self.calls))

    def test_mixed_sources_share_the_invocation_cap_not_credential_or_lane_quota(self):
        self.seed(names=("hot",)); github = self.add_repository()
        batch = self.plan(replace(POLICY_NPM, refresh_budget=1))
        first = self.execute(batch, budget=1, github=github)
        second = self.execute(batch, budget=1, github=github)
        self.assertEqual((1, 1), (first["requestsThisInvocation"], second["requestsThisInvocation"]))
        self.assertEqual(1, len(self.calls)); self.assertEqual(1, len(self.github_calls))
        self.assertEqual(1, second["lanes"]["ordinary"]["requests"])
        self.assertEqual(1, second["lanes"]["downloads_fast"]["requests"])

    def test_github_work_requires_an_explicit_identity_before_any_source_request(self):
        self.seed(names=("hot",)); self.add_repository()
        batch = self.plan(replace(POLICY_NPM, refresh_budget=1))
        with self.assertRaises(ValueError): self.execute(batch)
        self.assertEqual([], self.calls); self.assertEqual([], self.github_calls)
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM batch_attempts").fetchone()[0])

    def test_four_hour_batch_still_collects_complete_utc_window_only_once(self):
        self.seed(names=("hot",))
        policy = replace(POLICY_NPM, policy_id="npm-four-hour", slot_hours=4,
            refresh=replace(POLICY_NPM.refresh, policy_id="npm-four-hour", fast_hours=4))
        self.execute(self.plan(policy))
        self.now = NOW + timedelta(hours=4)
        later = self.plan(policy)
        self.assertEqual(0, later["summary"]["downloads"]["selected"])
        self.assertEqual(1, len(self.calls))

    def test_corrupt_or_future_snapshot_rolls_back_the_entire_batch_plan(self):
        self.seed()
        with self.assertRaises(ValueError):
            plan_batch(self.db, POLICY_NPM, now=NOW - timedelta(minutes=1), ranking_store=SnapshotStore(self.settings.snapshots))
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM batch_runs").fetchone()[0])
        current = self.settings.snapshots / "current.json"
        current.write_text('{"revision":"missing","manifestSha256":"wrong"}', encoding="utf-8")
        with self.assertRaises(ServiceError): self.plan()
        for table in ("batch_runs", "batch_policies", "refresh_cohorts", "download_refresh_cohorts"):
            self.assertEqual(0, self.db.execute("SELECT count(*) FROM " + table).fetchone()[0])


    def test_current_window_job_lookup_uses_its_partial_index(self):
        self.seed(); actual = []
        self.db.set_trace_callback(lambda sql: actual.append(sql) if "FROM jobs WHERE kind='npm_downloads' AND json_extract" in sql else None)
        self.plan(); self.db.set_trace_callback(None)
        self.assertTrue(actual)
        plan = " ".join(row[3] for row in self.db.execute("EXPLAIN QUERY PLAN " + actual[-1]))
        self.assertIn("npm_job_window", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_missing_snapshot_is_explicit_without_fabricated_rank_or_network(self):
        self.write("one", npm="one")
        batch = self.plan()
        self.assertEqual("unavailable", batch["summary"]["downloads"]["status"])
        self.assertEqual(0, batch["summary"]["downloads"]["selected"])
        self.assertEqual(0, self.execute(batch)["requestsThisInvocation"])
        self.assertEqual([], self.calls)

    def test_initial_bootstrap_past_phase_is_due_now_without_fake_new_protection(self):
        self.write("initial", npm="initial")
        self.build("published")
        with patch("agent_forge_service.download_batches._phase", return_value=0):
            batch = self.plan()
        self.assertEqual(1, batch["summary"]["downloads"]["ordinaryDue"])
        self.assertEqual(0, batch["summary"]["downloads"]["fastDue"])
        self.assertEqual(1, batch["summary"]["downloads"]["selected"])
