import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_forge_service.errors import ServiceError
from agent_forge_service.jobs import JobQueue, save_checkpoint
from agent_forge_service.storage import open_state, timestamp
from service_tests.temporary import cleanup_temporary

NOW = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="forge-job-test-")
        self.addCleanup(cleanup_temporary, self.temp, Path(self.temp.name).resolve())
        self.path = Path(self.temp.name) / "state.db"
        self.db = open_state(self.path)
        self.addCleanup(self.db.close)
        self.queue = JobQueue(self.db)

    def enqueue(self, key="one", due=NOW, max_attempts=3):
        with self.db:
            return self.queue.enqueue(key=key, kind="repo", scope="1", payload={"id": 1}, now=NOW, due_at=due, max_attempts=max_attempts)

    def test_enqueue_is_idempotent_but_conflicting_payload_is_rejected(self):
        first = self.enqueue()
        self.assertEqual(first, self.enqueue())
        with self.assertRaises(ServiceError) as error:
            with self.db:
                self.queue.enqueue(key="one", kind="repo", scope="1", payload={"id": 2}, now=NOW, due_at=NOW, max_attempts=3)
        self.assertEqual("job_conflict", error.exception.code)
        self.assertEqual(1, self.db.execute("SELECT count(*) FROM jobs").fetchone()[0])

    def test_due_jobs_and_lease_fencing(self):
        self.enqueue(due=NOW + timedelta(hours=1))
        self.assertIsNone(self.queue.claim(owner="a", now=NOW))
        lease = self.queue.claim(owner="a", now=NOW + timedelta(hours=1), lease_seconds=1)
        later = NOW + timedelta(hours=1, seconds=2)
        replacement = self.queue.claim(owner="b", now=later)
        self.assertEqual(2, replacement.attempts)
        self.assertNotEqual(lease.token, replacement.token)
        with self.assertRaises(ServiceError):
            self.queue.finish(lease, now=later)
        self.queue.finish(replacement, now=later)
        self.assertIsNone(self.queue.claim(owner="c", now=later))

    def test_expired_lease_cannot_commit_without_replacement(self):
        self.enqueue()
        lease = self.queue.claim(owner="a", now=NOW, lease_seconds=1)
        with self.assertRaises(ServiceError):
            self.queue.finish(lease, now=NOW + timedelta(seconds=2))
        self.assertEqual("leased", self.db.execute("SELECT state FROM jobs").fetchone()[0])

    def test_retry_budget_wait_does_not_consume_network_attempt(self):
        self.enqueue()
        lease = self.queue.claim(owner="a", now=NOW)
        retry = NOW + timedelta(minutes=1)
        self.queue.fail(lease, now=NOW, code="budget_wait", retry_at=retry, attempted=False)
        self.assertEqual(0, self.db.execute("SELECT attempts FROM jobs").fetchone()[0])
        self.assertIsNone(self.queue.claim(owner="a", now=NOW))
        lease = self.queue.claim(owner="a", now=retry)
        self.assertEqual(1, lease.attempts)

    def test_failed_and_crashed_attempts_eventually_die(self):
        self.enqueue(max_attempts=1)
        lease = self.queue.claim(owner="a", now=NOW, lease_seconds=1)
        self.assertIsNone(self.queue.claim(owner="b", now=NOW + timedelta(seconds=2)))
        self.assertEqual("dead", self.db.execute("SELECT state FROM jobs").fetchone()[0])
        self.enqueue(key="two", max_attempts=1)
        lease = self.queue.claim(owner="a", now=NOW)
        self.queue.fail(lease, now=NOW, code="network", retry_at=NOW + timedelta(minutes=1))
        self.assertEqual("dead", self.db.execute("SELECT state FROM jobs WHERE id=?", (lease.id,)).fetchone()[0])

    def test_observation_checkpoint_and_ack_commit_atomically(self):
        self.enqueue()
        lease = self.queue.claim(owner="a", now=NOW)
        def apply(c):
            c.execute("INSERT INTO metadata VALUES ('observation','evidence')")
            save_checkpoint(c, source_id="source", shard_key="page", cursor={"next": 2}, watermark=None,
                            complete=False, now=NOW, details={"partial": True})
            raise RuntimeError("crash before ACK")
        with self.assertRaises(RuntimeError):
            self.queue.finish(lease, now=NOW, apply=apply)
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM metadata WHERE key!='storageContract'").fetchone()[0])
        self.assertEqual(0, self.db.execute("SELECT count(*) FROM checkpoints").fetchone()[0])
        self.assertEqual("leased", self.db.execute("SELECT state FROM jobs").fetchone()[0])
        self.queue.finish(lease, now=NOW, apply=lambda c: save_checkpoint(
            c, source_id="source", shard_key="page", cursor=None, watermark=timestamp(NOW), complete=True, now=NOW, details={}))
        self.assertEqual("complete", self.db.execute("SELECT completeness FROM checkpoints").fetchone()[0])
        self.assertEqual("succeeded", self.db.execute("SELECT state FROM jobs").fetchone()[0])

    def test_partial_checkpoint_keeps_last_success_and_watermark(self):
        with self.db:
            save_checkpoint(self.db, source_id="s", shard_key="p", cursor=None, watermark=timestamp(NOW), complete=True, now=NOW, details={})
            save_checkpoint(self.db, source_id="s", shard_key="p", cursor={"next": 2}, watermark=None, complete=False,
                            now=NOW + timedelta(hours=1), details={})
        row = self.db.execute("SELECT * FROM checkpoints").fetchone()
        self.assertEqual(timestamp(NOW), row["watermark"])
        self.assertEqual(timestamp(NOW), row["last_success_at"])
        with self.assertRaises(ValueError):
            save_checkpoint(self.db, source_id="s", shard_key="p", cursor=None, watermark=timestamp(NOW), complete=False, now=NOW, details={})


    def test_unknown_schema_version_is_not_downgraded(self):
        self.db.execute("PRAGMA user_version=999")
        self.db.commit()
        with self.assertRaises(ServiceError):
            open_state(self.path)
        self.assertEqual(999, self.db.execute("PRAGMA user_version").fetchone()[0])
