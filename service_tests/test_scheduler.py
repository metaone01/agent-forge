"""UTC scheduler fixtures use real state, queues and snapshots without public GETs."""
import contextlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_forge_service import scheduler
from agent_forge_service.batches import batch_status
from agent_forge_service.bundles import export_bundle, import_bundle, activate_snapshot
from agent_forge_service.download_batches import DownloadsBatchPolicy
from agent_forge_service.downloads import DownloadsClient, DownloadsResponse
from agent_forge_service.errors import ServiceError
from agent_forge_service.github import GhTransport
from agent_forge_service.github_client import GitHubClient, RequestPolicy
from agent_forge_service.jobs import JobQueue
from agent_forge_service.publishing import SnapshotStore, _build_and_publish_locked, atomic_json, build_and_publish, digest_file
from agent_forge_service.query import Query, QueryService
from agent_forge_service.scheduler import ScheduleOptions, scheduled_tick, scheduler_status, run_schedule
from agent_forge_service.settings import Settings
from agent_forge_service.state_recovery import create_state_backup, restore_state_backup
from agent_forge_service.storage import encode_json, open_state, timestamp
from agent_forge_service.worker import main
from service_tests.test_batches import NOW, POLICY
from service_tests.test_collector import BODY
from service_tests.test_downloads import body_for
from service_tests.test_publication_query import record
from service_tests.temporary import cleanup_temporary

POLICY = replace(POLICY, refresh_budget=0, bootstrap_budget=4)
ZERO = replace(POLICY, bootstrap_budget=0)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="forge-scheduler-test-")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(cleanup_temporary, self.temp, self.root)
        self.catalog = self.root / "catalog"
        self.packages = self.catalog / "sources/plugin/packages"
        self.packages.mkdir(parents=True)
        self.settings = Settings(self.catalog, self.root / "runtime")
        self.now = NOW
        self.calls = []
        self.publications = []
        self.advance = timedelta(seconds=7)
        self.response_status = 200
        self.on_request = None
        self.stopped = False
        self.policy = POLICY
        self.options = ScheduleOptions(2, 3600)

    def write(self, name="alpha", repository="example/repo1", *, agents=("dsh",), npm=None):
        value = record(name, agents=agents)
        value["links"] = {"repository": "https://github.com/" + repository}
        value["distributions"] = [{"id": "repo", "type": "github-repo", "url": "https://github.com/" + repository}]
        if npm:
            value["distributions"].append({"id": "npm", "type": "npm-pkg", "url": "https://www.npmjs.com/package/" + npm})
        self.packages.joinpath(name + ".json").write_text(json.dumps(value), encoding="utf-8")
        return value

    def client(self, connection):
        def runner(*args):
            self.calls.append(args[0])
            endpoint = args[0][-1]
            self.now += self.advance
            if self.on_request:
                self.on_request()
            if endpoint.startswith("search/"):
                body = {"total_count": 0, "incomplete_results": False, "items": []}
            else:
                identity = int(endpoint.rsplit("/", 1)[-1].removeprefix("repo"))
                body = {**BODY, "id": identity, "full_name": "example/repo" + str(identity)}
            if self.response_status >= 400:
                body = {"message": "limited" if self.response_status == 429 else "temporary failure"}
            headers = 'HTTP/2.0 ' + str(self.response_status) + ' status\r\nETag: "fixture"\r\n'
            if self.response_status == 429:
                headers += "Retry-After: 120\r\n"
            return (0 if self.response_status < 400 else 1), (headers + "\r\n" + json.dumps(body)).encode()
        return GitHubClient(connection, identity="scheduler-fixture", transport=GhTransport(runner=runner),
                            policy=RequestPolicy(jitter_fraction=0), clock=lambda: self.now)

    def publish(self, settings, **kwargs):
        self.publications.append(kwargs["revision"])
        return _build_and_publish_locked(settings, **kwargs)

    def tick(self, **kwargs):
        values = {"identity": "scheduler-fixture", "clock": lambda: self.now,
                  "client_factory": self.client, "publisher": self.publish}
        values.update(kwargs)
        return scheduled_tick(self.settings, self.policy, self.options, **values)

    def state(self):
        return scheduler_status(self.settings.state_path, self.policy.policy_id)

    def db_call(self, operation):
        connection = open_state(self.settings.state_path)
        try:
            with connection:
                return operation(connection)
        finally:
            connection.close()

    def persist(self, state):
        self.db_call(lambda db: db.execute("UPDATE metadata SET value=? WHERE key=?",
            (encode_json({k: v for k, v in state.items() if k != "configured"}), scheduler.PREFIX + self.policy.policy_id)))

    def manifest(self):
        return SnapshotStore(self.settings.snapshots).current()

    def assert_code(self, code, function):
        with self.assertRaises(ServiceError) as caught:
            function()
        self.assertEqual(code, caught.exception.code)

    def fail_before_publish(self):
        self.write()
        with self.assertRaisesRegex(OSError, "fixture publication"):
            self.tick(publisher=lambda *a, **k: (_ for _ in ()).throw(OSError("fixture publication")))
        self.assertEqual("publishing", self.state()["active"]["phase"])

    def orphan_revision(self):
        original = scheduler.atomic_json
        def fail(path, value):
            if path.name == "current.json":
                raise OSError("fixture pointer interruption")
            return original(path, value)
        with patch("agent_forge_service.publishing.atomic_json", side_effect=fail):
            with self.assertRaises(OSError):
                self.tick()
        active = self.state()["active"]
        self.assertTrue((self.settings.snapshots / active["revision"]).is_dir())
        self.assertFalse((self.settings.snapshots / "current.json").exists())
        return active

    def test_options_reject_implicit_or_unbounded_values(self):
        for values in ((0, 60), (101, 60), (True, 60), (1, 0), (1, 28801), (1, True)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ScheduleOptions(*values).validate(POLICY)
        for options in (ScheduleOptions(1, 60, 0), ScheduleOptions(1, 60, 61), ScheduleOptions(1, 60, True), ScheduleOptions(1, 60, include_undated=1)):
            with self.assertRaises(ValueError):
                options.validate(POLICY)

    def test_identity_owner_and_cancellation_validate_before_runtime_creation(self):
        for kwargs in ({"identity": None}, {"identity": "a b"}, {"owner": "a b"}, {"should_stop": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.tick(**kwargs)
        self.assertFalse(self.settings.runtime_root.exists())

    def test_stopped_tick_does_not_create_state_or_start_work(self):
        result = self.tick(should_stop=lambda: True)
        self.assertEqual("interrupted", result["phase"])
        self.assertFalse(self.settings.runtime_root.exists())
        self.assertEqual([], self.calls)

    def test_utc_four_and_eight_hour_slots_and_plus_eight_are_identical(self):
        self.policy = ZERO
        self.write()
        for hours in (8, 4):
            self.policy = replace(ZERO, policy_id="slot-" + str(hours), slot_hours=hours,
                                  refresh=replace(ZERO.refresh, policy_id="slot-" + str(hours), fast_hours=hours))
            self.settings = Settings(self.catalog, self.root / ("runtime-" + str(hours)))
            self.now = NOW.replace(hour=hours - 1, minute=59, second=59)
            first = self.tick()
            self.assertEqual(timestamp(NOW.replace(hour=0)), first["publication"]["slot"])
            self.now += timedelta(seconds=1)
            second = self.tick()
            self.assertEqual(timestamp(NOW.replace(hour=hours)), second["publication"]["slot"])
            self.now = self.now.astimezone(timezone(timedelta(hours=8)))
            self.assertEqual("idle", self.tick()["phase"])
            self.now = NOW.replace(hour=23, minute=59, second=59)
            self.tick()
            self.now += timedelta(seconds=1)
            self.assertEqual(timestamp(self.now.replace(hour=0)), self.tick()["publication"]["slot"])

    def test_same_slot_does_not_repeat_get_or_publication(self):
        self.write()
        first = self.tick()
        self.assertEqual("published", first["phase"])
        second = self.tick()
        self.assertEqual("idle", second["phase"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.publications))
        self.assertEqual(first["publication"], second["publication"])
        self.assertEqual(timestamp(NOW.replace(hour=16)), second["nextWakeAt"])

    def test_shared_repository_and_agents_do_not_duplicate_requests(self):
        self.write("alpha", agents=("dsh", "claude-code"))
        self.write("beta", agents=("claude-code",))
        result = self.tick()
        self.assertEqual(1, result["requestsThisTick"])
        self.assertEqual({"packages": 2, "versions": 2}, self.manifest()["counts"])
        self.assertEqual(1, self.manifest()["collectionBatch"]["confirmedRequests"])
        self.assertEqual(2, QueryService(self.settings).dashboard()["totals"]["packages"])

    def test_request_slice_resumes_in_same_batch_after_restart(self):
        self.write("one", "example/repo1")
        self.write("two", "example/repo2")
        self.options = replace(self.options, max_requests=1)
        first = self.tick()
        self.assertEqual("collecting", first["phase"])
        batch = self.state()["active"]["batchId"]
        second = self.tick()
        self.assertEqual("published", second["phase"])
        self.assertEqual(batch, second["publication"]["batchId"])
        self.assertEqual(2, len(self.calls))
        self.assertEqual(1, len(self.publications))

    def test_collection_deadline_settles_inflight_get_and_preserves_pending_work(self):
        self.write("one", "example/repo1")
        self.write("two", "example/repo2")
        self.options = replace(self.options, max_batch_seconds=5)
        first = self.tick()
        self.assertEqual("published", first["phase"])
        context = self.manifest()["collectionBatch"]
        self.assertEqual("collection_deadline", context["completionReason"])
        self.assertTrue(context["partial"])
        self.assertEqual(1, context["confirmedRequests"])
        self.assertEqual(1, self.db_call(lambda db: db.execute("SELECT count(*) FROM jobs WHERE state='succeeded'").fetchone()[0]))
        self.assertEqual("idle", self.tick()["phase"])
        self.assertEqual(1, len(self.calls))

    def test_stop_during_get_finishes_lease_but_defers_build(self):
        self.write()
        self.on_request = lambda: setattr(self, "stopped", True)
        result = self.tick(should_stop=lambda: self.stopped)
        self.assertEqual("interrupted", result["phase"])
        self.assertEqual(1, result["requestsThisTick"])
        self.assertEqual([], self.publications)
        self.assertEqual("succeeded", self.db_call(lambda db: db.execute("SELECT state FROM jobs").fetchone()[0]))
        self.on_request = None
        self.stopped = False
        self.assertEqual("published", self.tick()["phase"])
        self.assertEqual(1, len(self.calls))

    def test_stop_after_claim_suppresses_get_and_releases_unattempted_lease(self):
        self.write()
        original = JobQueue.claim
        def claim(queue, **kwargs):
            lease = original(queue, **kwargs)
            if lease:
                self.stopped = True
            return lease
        with patch.object(JobQueue, "claim", claim):
            result = self.tick(should_stop=lambda: self.stopped)
        self.assertEqual("interrupted", result["phase"])
        self.assertEqual([], self.calls)
        row = self.db_call(lambda db: tuple(db.execute("SELECT state,attempts FROM jobs").fetchone()))
        self.assertEqual(("retry_wait", 0), row)
        self.stopped = False
        self.now += timedelta(seconds=2)
        self.assertEqual("published", self.tick()["phase"])
        self.assertEqual(1, len(self.calls))

    def test_slot_advance_during_get_does_not_start_another_request(self):
        self.write()
        self.now = NOW - timedelta(seconds=2)
        first = self.tick()
        self.assertEqual("slot_advanced", first["phase"])
        self.assertEqual([], self.publications)
        second = self.tick()
        self.assertEqual("published", second["phase"])
        self.assertEqual(timestamp(NOW), second["publication"]["slot"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual("superseded_by_latest_slot", self.state()["history"][0]["completionReason"])

    def test_restart_coalesces_missed_slots_instead_of_replaying_all_gets(self):
        self.policy = ZERO
        self.write()
        self.tick()
        self.now += timedelta(days=1)
        result = self.tick()
        self.assertEqual(2, result["publication"]["coalescedSlots"])
        self.assertEqual(2, self.manifest()["collectionBatch"]["coalescedSlots"])
        self.assertEqual(2, len(self.publications))

    def test_clock_rollback_waits_without_request_or_build(self):
        self.write()
        self.tick()
        saved = self.state()["lastTickAt"]
        self.now = NOW - timedelta(hours=1)
        result = self.tick()
        self.assertEqual("clock_wait", result["phase"])
        self.assertEqual(saved, result["nextWakeAt"])
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.publications))

    def test_clock_rollback_during_get_does_not_move_scheduler_time_backward(self):
        self.write()
        self.advance = -timedelta(seconds=1)
        first = self.tick()
        self.assertEqual("clock_wait", first["phase"])
        self.assertEqual(timestamp(NOW), self.state()["lastTickAt"])
        self.assertEqual([], self.publications)
        self.now = NOW + timedelta(seconds=8)
        self.advance = timedelta(seconds=7)
        self.assertEqual("published", self.tick()["phase"])
        self.assertEqual(1, len(self.calls))

    def test_limit_backoff_is_persistent_and_no_poll_get_occurs_early(self):
        self.write()
        self.response_status = 429
        first = self.tick()
        self.assertEqual("collecting", first["phase"])
        self.assertEqual(1, len(self.calls))
        second = self.tick()
        self.assertEqual("collecting", second["phase"])
        self.assertEqual(0, second["requestsThisTick"])
        self.assertEqual(1, len(self.calls))
        self.now += timedelta(seconds=121)
        self.response_status = 200
        self.assertEqual("published", self.tick()["phase"])
        self.assertEqual(2, len(self.calls))

    def test_exhausted_discovery_budget_publishes_explicit_partial_result(self):
        self.policy = replace(ZERO, discovery_budget=1, topics=("mcp-server",))
        self.write()
        result = self.tick()
        self.assertEqual("published", result["phase"])
        context = self.manifest()["collectionBatch"]
        self.assertEqual("request_budgets_exhausted", context["completionReason"])
        self.assertTrue(context["partial"])
        self.assertFalse(context["selectedWorkSettled"])
        self.assertEqual("not_guaranteed", context["catalogCoverage"])
        self.assertEqual(1, len(self.calls))

    def test_unknown_attempt_is_not_refunded_after_crash(self):
        self.write()
        self.policy = replace(POLICY, bootstrap_budget=1)
        with patch("agent_forge_service.batches.run_repository_jobs", side_effect=RuntimeError("crash after reservation")):
            with self.assertRaises(RuntimeError):
                self.tick()
        result = self.tick()
        self.assertEqual("published", result["phase"])
        context = self.manifest()["collectionBatch"]
        self.assertEqual(1, context["uncertainAttempts"])
        self.assertEqual(1, context["reservedRequests"])
        self.assertEqual(0, context["confirmedRequests"])
        self.assertTrue(context["partial"])
        self.assertEqual([], self.calls)

    def test_publishing_restarts_without_recollecting_even_after_next_slot(self):
        self.fail_before_publish()
        self.now = NOW + timedelta(hours=8)
        recovered = self.tick()
        self.assertEqual("published", recovered["phase"])
        self.assertEqual(timestamp(NOW), recovered["publication"]["slot"])
        self.assertEqual(timestamp(self.now), recovered["nextWakeAt"])
        latest = self.tick()
        self.assertEqual(timestamp(self.now), latest["publication"]["slot"])
        self.assertEqual(1, len(self.calls))

    def test_revision_renamed_before_pointer_is_recovered_without_rebuild(self):
        self.write()
        active = self.orphan_revision()
        manifest_hash = digest_file(self.settings.snapshots / active["revision"] / "manifest.json")
        result = self.tick()
        self.assertEqual(active["revision"], result["publication"]["revision"])
        self.assertEqual(manifest_hash, digest_file(self.settings.snapshots / active["revision"] / "manifest.json"))
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.publications))

    def test_pointer_written_before_receipt_is_recovered_without_rebuild(self):
        self.write()
        original = scheduler._save
        def fail(connection, state):
            if state["lastPublished"] and state["active"] is None:
                raise OSError("receipt interrupted")
            return original(connection, state)
        with patch("agent_forge_service.scheduler._save", side_effect=fail):
            with self.assertRaises(OSError):
                self.tick()
        pointer_hash = digest_file(self.settings.snapshots / "current.json")
        self.assertEqual("publishing", self.state()["active"]["phase"])
        self.assertEqual("published", self.tick()["phase"])
        self.assertEqual(pointer_hash, digest_file(self.settings.snapshots / "current.json"))
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, len(self.publications))

    def test_external_pointer_change_is_never_overwritten_on_retry(self):
        self.fail_before_publish()
        self.settings.snapshots.mkdir(parents=True)
        atomic_json(self.settings.snapshots / "current.json", {"revision": "external"})
        before = digest_file(self.settings.snapshots / "current.json")
        self.assert_code("publication_conflict", self.tick)
        self.assertEqual(before, digest_file(self.settings.snapshots / "current.json"))
        self.assertEqual(1, len(self.calls))

    def test_pointer_change_during_recovery_validation_is_rejected(self):
        self.write()
        self.orphan_revision()
        original = scheduler._verify_snapshot
        def verify(*args):
            result = original(*args)
            atomic_json(self.settings.snapshots / "current.json", {"revision": "external"})
            return result
        with patch("agent_forge_service.scheduler._verify_snapshot", side_effect=verify):
            self.assert_code("publication_conflict", self.tick)
        self.assertEqual({"revision": "external"}, json.loads(self.settings.snapshots.joinpath("current.json").read_text()))

    def test_snapshot_change_during_recovery_validation_is_rejected(self):
        self.write()
        active = self.orphan_revision()
        original = scheduler._verify_snapshot
        def verify(*args):
            result = original(*args)
            with (self.settings.snapshots / active["revision"] / "catalog.db").open("ab") as stream:
                stream.write(b"changed")
            return result
        with patch("agent_forge_service.scheduler._verify_snapshot", side_effect=verify):
            self.assert_code("snapshot_checksum", self.tick)
        self.assertFalse(self.settings.snapshots.joinpath("current.json").exists())

    def test_orphan_staging_is_preserved_and_new_revision_allocated(self):
        self.fail_before_publish()
        active = self.state()["active"]
        orphan = self.settings.snapshots / ".staging" / active["revision"]
        orphan.mkdir(parents=True)
        (orphan / "partial.bin").write_bytes(b"preserve evidence")
        result = self.tick()
        self.assertNotEqual(active["revision"], result["publication"]["revision"])
        self.assertEqual(b"preserve evidence", (orphan / "partial.bin").read_bytes())
        self.assertEqual(1, len(self.calls))

    def test_staging_attempts_are_bounded_without_deleting_old_evidence(self):
        self.fail_before_publish()
        def fail(settings, **kwargs):
            (settings.snapshots / ".staging" / kwargs["revision"]).mkdir(parents=True)
            raise OSError("orphan created")
        for _ in range(6):
            with self.assertRaises(OSError):
                self.tick(publisher=fail)
        self.assertEqual(5, len(self.state()["active"]["abandonedRevisions"]))
        self.assert_code("publication_retries_exhausted", self.tick)
        self.assertEqual(6, len(list((self.settings.snapshots / ".staging").iterdir())))

    def test_policy_identity_cannot_change_parameters(self):
        self.policy = ZERO
        self.write()
        self.tick()
        before = self.state()
        self.policy = replace(ZERO, refresh_budget=1)
        self.assert_code("batch_policy_conflict", self.tick)
        self.assertEqual(before, self.state())
        self.assertEqual(1, len(self.publications))

    def test_corrupt_state_fails_closed_with_structured_error(self):
        self.fail_before_publish()
        original = self.state()
        mutations = [lambda s: s.pop("policyHash"), lambda s: s.update(policyHash=123),
            lambda s: s.update(schemaVersion=True), lambda s: s.update(history=[None]),
            lambda s: s.update(extra=True), lambda s: s["active"].pop("expectedPointerSha256"),
            lambda s: s["active"].update(batchId="wrong"), lambda s: s["active"].update(startedAt=1),
            lambda s: s["active"].update(includeUndated=1), lambda s: s["active"].update(skippedSlots=True),
            lambda s: s["active"].update(abandonedRevisions="wrong"),
            lambda s: s["active"].update(revision="../escape"),
            lambda s: s["active"].update(expectedPointerSha256="wrong"),
            lambda s: s["active"].update(phase="other"),
            lambda s: s["active"].update(collectionFinishedAt="invalid"),
            lambda s: s["active"].update(deadlineAt=timestamp(NOW + timedelta(hours=9)))]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = json.loads(json.dumps(original))
                mutate(value)
                self.persist(value)
                self.assert_code("scheduler_state_invalid", self.tick)
        self.persist(original)
        self.assertEqual("published", self.tick()["phase"])

    def test_duplicate_json_key_is_rejected(self):
        self.policy = ZERO
        self.write()
        self.tick()
        raw = encode_json({k: v for k, v in self.state().items() if k != "configured"})
        raw = raw.replace('"history":', '"policyId":"duplicate","history":')
        self.db_call(lambda db: db.execute("UPDATE metadata SET value=? WHERE key=?", (raw, scheduler.PREFIX + self.policy.policy_id)))
        self.assert_code("scheduler_state_invalid", self.tick)

    def test_status_is_read_only_and_missing_runtime_is_not_created(self):
        self.assertFalse(scheduler_status(self.settings.state_path, self.policy.policy_id)["configured"])
        self.assertFalse(self.settings.runtime_root.exists())
        self.policy = ZERO
        self.write()
        self.tick()
        before = digest_file(self.settings.state_path)
        for _ in range(3):
            self.assertTrue(self.state()["configured"])
        self.assertEqual(before, digest_file(self.settings.state_path))

    def test_private_state_backup_restores_inflight_scheduler_and_budget(self):
        self.fail_before_publish()
        before = self.state()
        backup = self.root / "state-backup"
        created = create_state_backup(self.settings, backup)
        restored = Settings(self.catalog, self.root / "restored-runtime")
        restore_state_backup(restored, backup, expected_backup_sha256=created["backupSha256"])
        self.settings = restored
        self.assertEqual(before, self.state())
        self.assertEqual("published", self.tick()["phase"])
        self.assertEqual(1, len(self.calls))

    def test_public_bundle_preserves_provenance_without_private_state(self):
        self.write()
        self.tick()
        context = self.manifest()["collectionBatch"]
        bundle = self.root / "public-bundle"
        exported = export_bundle(self.settings, bundle)
        target = Settings(self.catalog, self.root / "receiver")
        imported = import_bundle(target, bundle, expected_bundle_sha256=exported["bundleSha256"])
        activate_snapshot(target, imported["revision"], expected_manifest_sha256=imported["manifestSha256"], expected_pointer_sha256="absent")
        self.assertEqual(context, SnapshotStore(target.snapshots).current()["collectionBatch"])
        self.assertEqual(1, len(QueryService(target).packages()["items"]))
        self.assertFalse(target.state_path.exists())

    def test_provenance_must_match_durable_request_accounting(self):
        self.write()
        result = self.tick()
        context = self.manifest()["collectionBatch"]
        self.assertEqual(result["publication"]["batchId"], context["batchId"])
        self.assertEqual(context, self.db_call(lambda db: scheduler.validate_collection_batch(context, db, self.manifest()["generatedAt"])))
        invalid = dict(context, confirmedRequests=context["confirmedRequests"] + 1)
        with self.assertRaises(ValueError):
            self.db_call(lambda db: scheduler.validate_collection_batch(invalid, db, self.manifest()["generatedAt"]))

    def test_npm_daily_lane_runs_without_gh_and_deduplicates_same_day(self):
        self.policy = replace(ZERO, downloads=DownloadsBatchPolicy(2, 1, 168, .25))
        self.write(npm="express")
        build_and_publish(self.settings, now=NOW - timedelta(hours=1), revision="seed")
        downloads = []
        def factory(connection):
            def get(request, **kwargs):
                downloads.append(request)
                self.now += timedelta(seconds=2)
                return DownloadsResponse(200, {}, body_for(request))
            return DownloadsClient(connection, transport=SimpleNamespace(get=get), clock=lambda: self.now)
        first = self.tick(identity=None, downloads_factory=factory)
        self.assertEqual("published", first["phase"])
        self.assertEqual(1, len(downloads))
        self.assertEqual([], self.calls)
        self.now = NOW + timedelta(hours=8)
        self.tick(identity=None, downloads_factory=factory)
        self.assertEqual(1, len(downloads))
        self.assertEqual(1, len(QueryService(self.settings).packages(Query(sort="downloads", metric_source="npmjs.org"))["items"]))

    def test_foreground_loop_waits_quietly_and_returns_on_stop(self):
        waits = []
        class Stop:
            def is_set(self):
                return len(waits) >= 3
            def wait(self, seconds):
                waits.append(seconds)
        events = []
        def tick(*args, **kwargs):
            return {"phase": "idle", "requestsThisTick": 0, "publication": None, "nextWakeAt": timestamp(NOW + timedelta(hours=8))}
        result = run_schedule(self.settings, ZERO, self.options, stop=Stop(), clock=lambda: NOW, emit=events.append, tick=tick)
        self.assertEqual([1, 1, 1], waits)
        self.assertEqual(1, len(events))
        self.assertEqual("stopped", result["phase"])

    def test_foreground_fatal_state_error_exits_without_spin(self):
        stop = threading.Event()
        def tick(*args, **kwargs):
            raise ServiceError("scheduler_state_invalid", "fixture", 503)
        self.assert_code("scheduler_state_invalid", lambda: run_schedule(self.settings, ZERO, self.options, stop=stop, tick=tick))

    def test_cli_zero_network_tick_and_status(self):
        self.write()
        config = {"schemaVersion": 1, "policyId": ZERO.policy_id, "slotHours": 8,
            "refresh": {"topN": 2, "protectionHours": 168, "fastHours": 8, "ordinaryHours": 24, "longtailFraction": .25},
            "budgets": {"refresh": 0, "bootstrap": 0, "discovery": 0, "history": 0},
            "discovery": {"topics": [], "start": ZERO.discovery_start, "overlapHours": 24,
                          "historyStart": ZERO.history_start, "historyLagHours": 24, "historyChunkHours": 24,
                          "minShardSeconds": 60, "maxShards": 255}}
        policy_path = self.root / "policy.json"
        policy_path.write_text(json.dumps(config), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["schedule-tick", "--runtime", str(self.settings.runtime_root), "--catalog", str(self.catalog),
                         "--policy", str(policy_path), "--max-requests", "1", "--max-batch-seconds", "60"])
        self.assertEqual(0, code)
        self.assertEqual("published", json.loads(output.getvalue())["phase"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["schedule-status", "--runtime", str(self.settings.runtime_root), "--policy-id", ZERO.policy_id])
        self.assertEqual(0, code)
        self.assertTrue(json.loads(output.getvalue())["configured"])

    def test_deadline_after_claim_suppresses_get_and_preserves_unattempted_job(self):
        self.write()
        self.options = replace(self.options, max_batch_seconds=5)
        original = JobQueue.claim
        def claim(queue, **kwargs):
            lease = original(queue, **kwargs)
            if lease:
                self.now += timedelta(seconds=6)
            return lease
        with patch.object(JobQueue, "claim", claim):
            result = self.tick()
        self.assertEqual("published", result["phase"])
        self.assertEqual("collection_deadline", result["publication"]["completionReason"])
        self.assertEqual([], self.calls)
        self.assertEqual(("retry_wait", 0), self.db_call(lambda db: tuple(db.execute("SELECT state,attempts FROM jobs").fetchone())))
        self.assertEqual(0, self.manifest()["collectionBatch"]["reservedRequests"])

    def test_planning_failure_is_persisted_and_resumed_idempotently(self):
        self.write()
        with patch("agent_forge_service.scheduler.plan_batch", side_effect=OSError("plan interrupted")):
            with self.assertRaises(OSError):
                self.tick()
        active = self.state()["active"]
        self.assertEqual("planning", active["phase"])
        self.now += timedelta(seconds=1)
        result = self.tick()
        self.assertEqual("published", result["phase"])
        self.assertEqual(active["revision"], result["publication"]["revision"])
        self.assertEqual(1, len(self.calls))

    def test_clock_rollback_after_build_defers_receipt_without_duplicate_work(self):
        self.write()
        def publish(settings, **kwargs):
            manifest = self.publish(settings, **kwargs)
            self.now -= timedelta(hours=1)
            return manifest
        result = self.tick(publisher=publish)
        self.assertEqual("clock_wait", result["phase"])
        self.assertEqual("publishing", self.state()["active"]["phase"])
        self.assertTrue(self.settings.snapshots.joinpath("current.json").exists())
        self.now = NOW + timedelta(seconds=60)
        self.assertEqual("published", self.tick()["phase"])
        self.assertEqual(1, len(self.publications))
        self.assertEqual(1, len(self.calls))

    def test_stop_before_build_preserves_publication_for_restart(self):
        self.write()
        original = scheduler._collection_batch
        def context(*args):
            result = original(*args)
            self.stopped = True
            return result
        with patch("agent_forge_service.scheduler._collection_batch", side_effect=context):
            result = self.tick(should_stop=lambda: self.stopped)
        self.assertEqual("interrupted", result["phase"])
        self.assertEqual("publishing", self.state()["active"]["phase"])
        self.assertEqual([], self.publications)
        self.stopped = False
        self.assertEqual("published", self.tick()["phase"])
        self.assertEqual(1, len(self.calls))

    def test_status_corrupt_sqlite_returns_structured_error(self):
        self.settings.state_path.parent.mkdir(parents=True)
        self.settings.state_path.write_bytes(b"not a SQLite database")
        self.assert_code("scheduler_state_invalid", self.state)

    def test_scheduler_history_is_bounded_after_many_slots(self):
        self.policy = ZERO
        self.write()
        for _ in range(21):
            self.tick()
            self.now += timedelta(hours=8)
        state = self.state()
        self.assertEqual(20, len(state["history"]))
        self.assertEqual(state["lastPublished"], state["history"][-1])
        self.assertLess(len(encode_json(state).encode()), scheduler.STATE_BYTES)

    def test_provenance_rejects_wrong_interval_and_missing_source_policy(self):
        self.write()
        self.tick()
        context = self.manifest()["collectionBatch"]
        for invalid in (dict(context, slotHours=4), dict(context, policyId="unknown"), dict(context, startedAt=1)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.db_call(lambda db: scheduler.validate_collection_batch(invalid, db, self.manifest()["generatedAt"]))

    def test_active_batch_missing_from_state_is_rejected_before_network(self):
        self.fail_before_publish()
        batch_id = self.state()["active"]["batchId"]
        self.db_call(lambda db: db.execute("DELETE FROM batch_jobs WHERE batch_id=?", (batch_id,)))
        self.db_call(lambda db: db.execute("DELETE FROM batch_attempts WHERE batch_id=?", (batch_id,)))
        self.db_call(lambda db: db.execute("DELETE FROM batch_runs WHERE id=?", (batch_id,)))
        self.assert_code("scheduler_state_invalid", self.tick)
        self.assertEqual(1, len(self.calls))

    def test_daily_refresh_due_at_exact_utc_boundary_after_previous_get_finishes_late(self):
        self.policy = replace(POLICY, refresh_budget=4)
        self.write("one", "example/repo1")
        self.write("two", "example/repo2")
        self.tick()
        self.now = NOW + timedelta(days=1)
        result = self.tick()
        self.assertEqual("published", result["phase"])
        self.assertEqual(2, result["requestsThisTick"])
        self.assertEqual(4, len(self.calls))
        self.assertEqual(timestamp(NOW + timedelta(days=1)), result["publication"]["slot"])
        self.assertEqual(timestamp(NOW + timedelta(days=2)), self.db_call(lambda db: db.execute("SELECT min(next_due_at) FROM refresh_resources").fetchone()[0]))
        self.assertGreater(self.db_call(lambda db: db.execute("SELECT min(last_success_at) FROM refresh_resources").fetchone()[0]), timestamp(NOW + timedelta(days=1)))

    def test_fast_four_and_eight_hour_refresh_is_due_on_the_next_slot(self):
        from agent_forge_service.batches import plan_batch, run_batch
        from agent_forge_service.scheduling import RankedResource
        for hours in (4, 8):
            with self.subTest(hours=hours):
                policy = replace(POLICY, slot_hours=hours, refresh_budget=4,
                                 refresh=replace(POLICY.refresh, fast_hours=hours))
                self.settings = Settings(self.catalog, self.root / ("fast-" + str(hours)))
                self.policy = policy
                self.now = NOW - timedelta(days=1)
                self.write()
                self.tick()
                self.now = NOW
                def plan_and_run(connection):
                    ranking = RankedResource("github-repository:1", "dsh", "plugin", "stars-growth", 1, "fixture-ranking")
                    batch = plan_batch(connection, policy, now=self.now, rankings=(ranking,), ranking_revision="fixture-ranking")
                    client = self.client(connection)
                    return run_batch(connection, client, batch["batchId"], owner="fixture", max_requests=1, clock=lambda: self.now)
                self.assertEqual(1, self.db_call(plan_and_run)["requestsThisInvocation"])
                self.now = NOW + timedelta(hours=hours)
                before = len(self.calls)
                result = self.db_call(plan_and_run)
                self.assertEqual(1, result["requestsThisInvocation"])
                self.assertEqual(before + 1, len(self.calls))
                self.assertEqual(timestamp(NOW + timedelta(hours=2 * hours)), self.db_call(lambda db: db.execute("SELECT next_due_at FROM refresh_resources").fetchone()[0]))

    def test_internal_signal_polling_does_not_repeat_ticks_or_write_state(self):
        waits = []
        ticks = []
        class Stop:
            def is_set(self):
                return len(waits) >= 65
            def wait(self, seconds):
                waits.append(seconds)
        def tick(*args, **kwargs):
            ticks.append(kwargs)
            return {"phase": "idle", "requestsThisTick": 0, "nextWakeAt": timestamp(NOW + timedelta(hours=8))}
        run_schedule(self.settings, ZERO, self.options, stop=Stop(), clock=lambda: NOW, tick=tick, emit=lambda value: None)
        self.assertEqual(2, len(ticks))
        self.assertEqual(65, len(waits))
        self.assertTrue(all(0 < value <= 1 for value in waits))
        self.assertFalse(self.settings.runtime_root.exists())


if __name__ == "__main__":
    unittest.main()
