"""Bounded diagnostics retain operational facts without copying request or exception content."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import logging
import importlib.util
import socket
import time
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_forge_service.operational_log import (
    LogPolicy, OperationalLog, error_kind, route_kind, server_logging,
)
from agent_forge_service.worker import main as worker_main

SECRET = "secret-do-not-log-credentials-fixture"


def _read_stopped_log(path):
    # A lease file can remain briefly unavailable after process.wait on Windows.
    # Retry only that file; persistent errors and unreadable log data still fail.
    for attempt in range(20):
        try:
            return path.read_bytes()
        except PermissionError:
            if path.name not in (".query.lock", ".worker.lock") or attempt == 19:
                raise
            time.sleep(.025)


class OperationalLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-forge-log-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "logs"
        self.now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        self.console = io.StringIO()

    def logger(self, **kwargs):
        result = OperationalLog("query", directory=self.directory, stream=self.console,
                                clock=lambda: self.now, **kwargs)
        self.addCleanup(result.close)
        return result

    def rows(self, role="query"):
        return [json.loads(row) for path in sorted(self.directory.glob(role + ".jsonl*"))
                for row in path.read_text(encoding="ascii").splitlines()]

    def test_policy_rejects_unbounded_and_untyped_limits(self):
        for values in ({"max_bytes": True}, {"max_bytes": 4095}, {"max_bytes": 16 * 1024**2 + 1},
                       {"backups": 0}, {"backups": 21}, {"max_age_seconds": 3599},
                       {"max_age_seconds": 365 * 86400 + 1}, {"max_age_seconds": 1.5}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                LogPolicy(**values)

    def test_fixed_fields_discard_secrets_and_adversarial_types(self):
        log = self.logger()
        for event in ("http_request", SECRET, [SECRET]):
            log.emit(event, endpoint=SECRET, path=SECRET, query=SECRET, headers={"Authorization": SECRET},
                     error=RuntimeError(SECRET), error_kind=SECRET, request_id=SECRET, method=SECRET,
                     code=[SECRET], command={SECRET: SECRET}, phase=SECRET, status=True,
                     duration_ms=float("nan"), requests=-1)
        text = "".join(path.read_text() for path in self.directory.glob("*.jsonl*"))
        self.assertNotIn(SECRET, text)
        self.assertEqual(3, len(self.rows()))
        self.assertEqual("other_event", self.rows()[-1]["event"])
        self.assertNotIn("status", self.rows()[0])
        self.assertNotIn("duration_ms", self.rows()[0])

    def test_fixed_categories_never_include_dynamic_identity(self):
        self.assertEqual("packages", route_kind("/api/v1/packages"))
        self.assertEqual("package_resource", route_kind("/api/v1/packages/" + SECRET))
        self.assertEqual("discovery", route_kind("/api/v1/discoveries/" + SECRET))
        self.assertEqual("other", route_kind("/" + SECRET))
        self.assertEqual("RuntimeError", error_kind(RuntimeError(SECRET)))
        class SecretException(Exception):
            pass
        self.assertEqual("OtherError", error_kind(SecretException(SECRET)))

    def test_size_rotation_bounds_all_files_and_preserves_unrelated_contents(self):
        log = self.logger(policy=LogPolicy(4096, 2, 3600))
        sentinel = self.directory / "external.log"
        sentinel.write_text(SECRET)
        unknown = self.directory / "query.jsonl.21"
        unknown.write_text(SECRET)
        for _ in range(250):
            log.emit("http_request", endpoint="packages", status=200, duration_ms=10, request_id="a" * 32)
        log.close()
        managed = [self.directory / ("query.jsonl" + ("." + str(i) if i else "")) for i in range(3)]
        self.assertTrue(all(path.is_file() and path.stat().st_size <= 4096 for path in managed))
        self.assertLess(len(self.rows()) if not unknown.exists() else sum(len(p.read_text().splitlines()) for p in managed), 250)
        self.assertEqual(SECRET, sentinel.read_text())
        self.assertEqual(SECRET, unknown.read_text())
        for path in managed:
            self.assertTrue(all(json.loads(line)["status"] == 200 for line in path.read_text().splitlines()))

    def test_low_volume_age_retention_uses_record_age_not_append_mtime(self):
        log = self.logger(policy=LogPolicy(4096, 2, 3600))
        log.emit("service_start")
        self.now += timedelta(seconds=3599)
        log.emit("server_lifecycle")
        # The active file has just been written, yet its oldest record is due.
        self.now += timedelta(seconds=1)
        log.emit("service_stop")
        self.assertEqual(["service_stop"], [row["event"] for row in self.rows()])

    def test_restart_prunes_by_content_age_even_if_mtime_is_recent(self):
        log = self.logger(policy=LogPolicy(4096, 2, 3600))
        log.emit("service_start")
        log.close()
        os.utime(self.directory / "query.jsonl", None)
        self.now += timedelta(seconds=3601)
        restarted = self.logger(policy=LogPolicy(4096, 2, 3600))
        restarted.emit("service_stop")
        self.assertEqual(["service_stop"], [row["event"] for row in self.rows()])

    def test_age_removes_rotated_files_on_next_event(self):
        log = self.logger(policy=LogPolicy(4096, 2, 3600))
        for _ in range(100):
            log.emit("service_start")
        self.assertTrue((self.directory / "query.jsonl.1").exists())
        self.now += timedelta(seconds=3600)
        log.emit("service_stop")
        self.assertFalse((self.directory / "query.jsonl.1").exists())
        self.assertEqual(1, len(self.rows()))

    def test_clock_regression_rotates_instead_of_misdating_oldest_record(self):
        log = self.logger(policy=LogPolicy(4096, 2, 3600))
        log.emit("service_start")
        self.now -= timedelta(seconds=100)
        log.emit("service_stop")
        self.assertTrue((self.directory / "query.jsonl.1").exists())
        self.now += timedelta(seconds=3600)
        log.emit("server_lifecycle")
        self.assertEqual({"service_start", "server_lifecycle"}, {row["event"] for row in self.rows()})

    def test_unowned_existing_role_log_is_never_adopted_or_deleted(self):
        self.directory.mkdir()
        old = self.directory / "query.jsonl"
        old.write_text(SECRET)
        with self.assertRaises(ValueError):
            self.logger()
        self.assertEqual(SECRET, old.read_text())
        self.assertFalse((self.directory / ".query.owner.json").exists())

    def test_wrong_owner_marker_and_malformed_logs_are_rejected(self):
        log = self.logger()
        log.emit("service_start")
        log.close()
        path = self.directory / "query.jsonl"
        path.write_text(SECRET)
        with self.assertRaises(ValueError):
            self.logger()
        self.assertEqual(SECRET, path.read_text())
        (self.directory / ".query.owner.json").write_text('{}')
        with self.assertRaises(ValueError):
            self.logger()
        self.assertEqual(SECRET, path.read_text())

    def test_structurally_invalid_owned_records_fail_startup_with_safe_diagnostics(self):
        from agent_forge_service.app import main as api_main
        log = self.logger()
        log.close()
        path = self.directory / "query.jsonl"
        for change in ({"event": [SECRET]}, {"at": None}, {"at": [SECRET]}, {"at": SECRET}):
            row = {"role": "query", "event": "service_start", "at": self.now.isoformat(), **change}
            before = (json.dumps(row) + "\n").encode()
            path.write_bytes(before)
            errors = io.StringIO()
            with redirect_stderr(errors):
                code = api_main(["--runtime", str(self.root / "runtime"), "--catalog", str(self.root / "catalog"),
                                 "--log-directory", str(self.directory)])
            self.assertEqual(2, code)
            self.assertNotIn(SECRET, errors.getvalue())
            self.assertEqual("log_configuration_invalid", json.loads(errors.getvalue())["code"])
            self.assertEqual(before, path.read_bytes())

    def test_boolean_marker_version_is_rejected_without_adoption(self):
        log = self.logger()
        log.close()
        marker = self.directory / ".query.owner.json"
        marker.write_text(json.dumps({"schemaVersion": True, "kind": "agent-forge-operational-log", "role": "query"}))
        with self.assertRaises(ValueError):
            self.logger()

    def test_partial_file_write_cannot_merge_into_next_record_on_restart(self):
        log = self.logger()
        log.emit("service_start")
        original = log._file.write
        with patch.object(log._file, "write", side_effect=lambda payload: original(payload[:5])):
            log.emit("http_request", status=200)
        self.assertTrue(log.failed)
        log.close()
        path = self.directory / "query.jsonl"
        before = path.read_bytes()
        self.assertFalse(before.endswith(b"\n"))
        with self.assertRaises(ValueError):
            self.logger()
        self.assertEqual(before, path.read_bytes())

    def test_lowering_byte_limit_requires_review_and_preserves_existing_file(self):
        log = self.logger(policy=LogPolicy(8192, 2, 3600))
        for _ in range(60):
            log.emit("service_start")
        log.close()
        path = self.directory / "query.jsonl"
        before = path.read_bytes()
        self.assertGreater(len(before), 4096)
        with self.assertRaises(ValueError):
            self.logger(policy=LogPolicy(4096, 2, 3600))
        self.assertEqual(before, path.read_bytes())

    def test_lowering_backup_count_removes_only_owned_numbered_files(self):
        log = self.logger(policy=LogPolicy(4096, 4, 3600))
        for _ in range(250):
            log.emit("service_start")
        log.close()
        self.assertTrue((self.directory / "query.jsonl.4").exists())
        self.logger(policy=LogPolicy(4096, 1, 3600))
        self.assertFalse((self.directory / "query.jsonl.2").exists())
        self.assertTrue((self.directory / "query.jsonl.1").exists())

    def test_directory_overlap_with_code_catalog_and_runtime_is_rejected_before_creation(self):
        for directory, protected in ((self.root / "catalog" / "logs", self.root / "catalog"),
                                     (self.root / "logs", self.root / "logs" / "runtime")):
            with self.assertRaises(ValueError):
                OperationalLog("query", directory=directory, protected=[protected])
            self.assertFalse(directory.exists())

    def test_role_lock_is_exclusive_and_released_after_close(self):
        log = self.logger()
        with self.assertRaises(OSError):
            self.logger()
        log.emit("service_start")
        log.close()
        self.logger().emit("service_stop")
        self.assertEqual(2, len(self.rows()))

    def test_separate_roles_do_not_rotate_or_lock_each_others_files(self):
        query = self.logger(policy=LogPolicy(4096, 1, 3600))
        with OperationalLog("worker", directory=self.directory, policy=LogPolicy(4096, 1, 3600)) as worker:
            worker.emit("worker_complete", exit_code=0)
            before = (self.directory / "worker.jsonl").read_bytes()
            for _ in range(100):
                query.emit("http_request", status=200)
            self.assertEqual(before, (self.directory / "worker.jsonl").read_bytes())

    def test_cross_process_lock_and_reopen(self):
        log = self.logger()
        script = "from agent_forge_service.operational_log import OperationalLog; import sys; OperationalLog('query',directory=sys.argv[1]).close()"
        blocked = subprocess.run([sys.executable, "-c", script, str(self.directory)], capture_output=True, timeout=15)
        self.assertNotEqual(0, blocked.returncode)
        log.close()
        opened = subprocess.run([sys.executable, "-c", script, str(self.directory)], capture_output=True, timeout=15)
        self.assertEqual(0, opened.returncode, opened.stderr)

    def test_hardlink_file_is_rejected_without_modifying_target(self):
        log = self.logger()
        log.close()
        target = self.root / "private"
        target.write_text(SECRET)
        (self.directory / "query.jsonl").unlink()
        os.link(target, self.directory / "query.jsonl")
        with self.assertRaises(ValueError):
            self.logger()
        self.assertEqual(SECRET, target.read_text())

    def test_threaded_rotation_keeps_complete_bounded_json_records(self):
        log = self.logger(policy=LogPolicy(4096, 2, 3600))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: log.emit("http_request", status=200), range(400)))
        log.close()
        self.assertFalse(log.failed)
        for path in self.directory.glob("*.jsonl*"):
            self.assertLessEqual(path.stat().st_size, 4096)
        self.assertTrue(self.rows())

    def test_runtime_io_error_falls_back_once_without_raw_exception(self):
        log = self.logger(policy=LogPolicy(4096, 1, 3600))
        with patch.object(log, "_rotate", side_effect=OSError(SECRET)):
            for _ in range(150):
                log.emit("http_request", status=200)
        self.assertTrue(log.failed)
        self.assertNotIn(SECRET, self.console.getvalue())
        rows = [json.loads(line) for line in self.console.getvalue().splitlines()]
        self.assertEqual(1, sum(row["event"] == "log_io_error" for row in rows))
        self.assertGreater(sum(row["event"] == "http_request" for row in rows), 1)

    def test_close_releases_lease_even_if_file_close_fails(self):
        log = self.logger()
        output = log._file
        with patch.object(log, "_close_current", side_effect=OSError(SECRET)):
            with self.assertRaises(OSError):
                log.close()
        self.assertIsNone(log._lease)
        self.assertTrue(log.closed)
        output.close()
        log._file = None
        self.logger().close()

    def test_post_stop_lock_read_retry_is_bounded_and_never_skips_logs(self):
        path = self.root / '.query.lock'
        with patch.object(Path, 'read_bytes', side_effect=[PermissionError('busy'), b'0']) as read, patch('time.sleep'):
            self.assertEqual(b'0', _read_stopped_log(path))
            self.assertEqual(2, read.call_count)
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('busy')) as read, patch('time.sleep'):
            with self.assertRaises(PermissionError):
                _read_stopped_log(path)
            self.assertEqual(20, read.call_count)
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('busy')) as read:
            with self.assertRaises(PermissionError):
                _read_stopped_log(self.root / 'query.jsonl')
            self.assertEqual(1, read.call_count)

    def test_console_failure_does_not_break_request(self):
        class Broken:
            def write(self, _):
                raise OSError(SECRET)
        with OperationalLog("query", stream=Broken()) as log:
            log.emit("http_request", status=200)

    def test_server_logging_does_not_format_args_messages_or_tracebacks_and_restores_handlers(self):
        class Dangerous:
            def __str__(self):
                raise AssertionError("must not format")
        logger = logging.getLogger("uvicorn.error")
        before = (logger.handlers[:], logger.level, logger.propagate, logger.disabled)
        log = self.logger()
        with server_logging(log):
            logging.getLogger("uvicorn.access").info(SECRET)
            logger.error("%s", Dangerous(), exc_info=(ValueError, ValueError(SECRET), None))
            logging.getLogger("agent_forge_service.http_contract").error(SECRET)
        self.assertEqual(before, (logger.handlers, logger.level, logger.propagate, logger.disabled))
        self.assertEqual(2, len(self.rows()))
        self.assertNotIn(SECRET, (self.directory / "query.jsonl").read_text())

    def test_worker_scheduler_event_discards_publication_identity_and_error_text(self):
        log = self.logger()
        log.worker_event(json.dumps({"phase": "storage_wait", "requestsThisTick": 0, "publication": SECRET,
                                    "error": SECRET, "identity": SECRET, "storage": {"path": SECRET}}))
        self.assertEqual("storage_wait", self.rows()[0]["phase"])
        self.assertNotIn(SECRET, (self.directory / "query.jsonl").read_text())

    def test_worker_result_stdout_preserved_and_runtime_status_stays_read_only(self):
        runtime = self.root / "missing-runtime"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = worker_main(["storage-status", "--runtime", str(runtime), "--log-directory", str(self.directory)])
        self.assertEqual(0, code, err.getvalue())
        self.assertTrue(json.loads(out.getvalue())["allowed"])
        self.assertFalse(runtime.exists())
        self.assertEqual("worker_complete", self.rows("worker")[-1]["event"])
        self.assertEqual("", err.getvalue())

    def test_worker_configuration_and_operation_failures_do_not_expose_content(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = worker_main(["storage-status", "--runtime", str(self.root / SECRET),
                                "--log-directory", str(self.root / SECRET / "logs")])
        self.assertEqual(2, code)
        self.assertNotIn(SECRET, err.getvalue())
        self.assertEqual("log_configuration_invalid", json.loads(err.getvalue())["code"])
        out, err = io.StringIO(), io.StringIO()
        with patch("agent_forge_service.worker.Settings", side_effect=ValueError(SECRET)), redirect_stdout(out), redirect_stderr(err):
            code = worker_main(["storage-status", "--runtime", str(self.root / "runtime")])
        self.assertEqual(2, code)
        self.assertNotIn(SECRET, err.getvalue())


@unittest.skipUnless(all(importlib.util.find_spec(name) for name in ("fastapi", "uvicorn", "httpx")), "reviewed HTTP dependencies required")
class RequestLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from service_tests import test_publication_query as fixture
        self.case = fixture.PublicationQueryTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.write(fixture.record("alpha"))
        self.case.build()
        self.console = io.StringIO()
        self.log = OperationalLog("query", directory=self.case.root / "logs", stream=self.console)
        self.addCleanup(self.log.close)

    async def test_cors_framework_and_query_records_have_correlated_ids_without_payload(self):
        import httpx
        from agent_forge_service.app import create_app
        app = create_app(self.case.settings, allowed_origins=("https://allowed.example.test",), event_log=self.log)
        transport = httpx.ASGITransport(app=app, root_path="/project", raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = [await client.get("/project/api/v1/packages?q=" + SECRET,
                           headers={"Authorization": "Bearer " + SECRET, "Cookie": SECRET}),
                         await client.get("/project/api/v1/packages/" + SECRET),
                         await client.post("/project/api/v1/packages", content=SECRET),
                         await client.options("/project/api/v1/packages", headers={"Origin": "https://allowed.example.test",
                                           "Access-Control-Request-Method": "GET"})]
        rows = [json.loads(line) for line in (self.log.directory / "query.jsonl").read_text().splitlines()]
        self.assertEqual([200, 404, 405, 200], [response.status_code for response in responses])
        self.assertEqual([response.headers["x-request-id"] for response in responses], [row["request_id"] for row in rows])
        self.assertEqual(["packages", "package_resource", "packages", "packages"], [row["endpoint"] for row in rows])
        self.assertNotIn(SECRET, (self.log.directory / "query.jsonl").read_text() + self.console.getvalue())

    async def test_adapter_exception_has_same_request_id_and_no_raw_error(self):
        import httpx
        from agent_forge_service.app import create_app
        app = create_app(self.case.settings, event_log=self.log)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with patch.object(app.state.reader, "handle", side_effect=RuntimeError(SECRET)):
                response = await client.get("/api/v1/packages?q=" + SECRET)
        rows = [json.loads(line) for line in (self.log.directory / "query.jsonl").read_text().splitlines()]
        request = next(row for row in rows if row["event"] == "http_request")
        self.assertEqual(500, response.status_code)
        self.assertEqual(response.headers["x-request-id"], request["request_id"])
        self.assertEqual("RuntimeError", request["error_kind"])
        self.assertNotIn(SECRET, (self.log.directory / "query.jsonl").read_text() + self.console.getvalue() + response.text)

    async def test_reader_exception_safe_for_embedded_default_logger(self):
        import httpx
        from agent_forge_service.app import create_app
        app = create_app(self.case.settings, event_log=self.log)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        with self.assertLogs("agent_forge_service.http_contract", level="ERROR") as captured:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                with patch.object(app.state.reader, "_execute", side_effect=RuntimeError(SECRET)):
                    response = await client.get("/api/v1/packages")
        self.assertEqual(500, response.status_code)
        self.assertNotIn(SECRET, "".join(captured.output))
        self.assertIn("RuntimeError", "".join(captured.output))


@unittest.skipUnless(all(importlib.util.find_spec(name) for name in ("fastapi", "uvicorn")), "reviewed HTTP dependencies required")
class TcpLogTests(unittest.TestCase):
    def test_real_server_drops_url_headers_and_exception_secrets_while_rotating(self):
        from service_tests import test_publication_query as fixture
        case = fixture.PublicationQueryTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.write(fixture.record("alpha"))
        case.build()
        directory = case.root / "logs"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        script = """
import sys
from agent_forge_service.app import main
from agent_forge_service.http_contract import HttpReadService
original = HttpReadService.handle
def injected(self, method, path, pairs=(), **kwargs):
    if path.startswith('/boom/'):
        raise RuntimeError(sys.argv[-1])
    return original(self, method, path, pairs, **kwargs)
HttpReadService.handle = injected
raise SystemExit(main(sys.argv[1:-1]))
"""
        console_path = case.root / "server-console.txt"
        console = console_path.open("wb")
        process = subprocess.Popen([sys.executable, "-c", script, "--runtime", str(case.settings.runtime_root),
                     "--catalog", str(case.catalog), "--port", str(port), "--log-directory", str(directory),
                     "--log-max-bytes", "4096", "--log-backups", "2", SECRET],
                    stdout=console, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        opener = build_opener(ProxyHandler({}))
        def request(path):
            req = Request("http://127.0.0.1:" + str(port) + path,
                          headers={"Authorization": "Bearer " + SECRET, "Cookie": SECRET, "X-Request-ID": SECRET})
            try:
                response = opener.open(req, timeout=3)
            except HTTPError as error:
                response = error
            with response:
                response.read()
                return response.status, response.headers.get("x-request-id")
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    if request("/health/ready")[0] == 200:
                        break
                except OSError:
                    pass
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail("local server failed readiness")
                time.sleep(.05)
            statuses = [request("/private/" + SECRET + "?token=" + SECRET)[0] for _ in range(110)]
            self.assertEqual({404, 429}, set(statuses))
            status, request_id = request("/boom/" + SECRET + "?q=" + SECRET)
            self.assertEqual(500, status)
            rows = [json.loads(line) for path in directory.glob("query.jsonl*") for line in path.read_text().splitlines()]
            self.assertTrue(any(row.get("request_id") == request_id and row.get("status") == 500 for row in rows))
            self.assertTrue(any(row["event"] == "server_error" for row in rows))
            self.assertEqual(3, len(list(directory.glob("query.jsonl*"))))
            self.assertTrue(all(path.stat().st_size <= 4096 for path in directory.glob("query.jsonl*")))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            console.close()
        self.assertIsNotNone(process.poll())
        for path in directory.iterdir():
            self.assertNotIn(SECRET.encode(), _read_stopped_log(path))
        self.assertNotIn(SECRET, console_path.read_text(errors="replace"))


if __name__ == "__main__":
    unittest.main()
