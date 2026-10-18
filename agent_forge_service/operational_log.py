"""Bounded fixed-field diagnostics: no raw HTTP or exception payload formatting."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading

@dataclass(frozen=True)
class LogPolicy:
    max_bytes: int = 1024 ** 2
    backups: int = 3
    max_age_seconds: int = 7 * 86400

    def __post_init__(self):
        for name, low, high in (("max_bytes", 4096, 16 * 1024**2), ("backups", 1, 20),
                               ("max_age_seconds", 3600, 365 * 86400)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("invalid operational log policy: " + name)

_EVENTS = {
    "http_request", "log_io_error", "other_event", "server_error", "server_lifecycle",
    "server_warning", "service_start", "service_stop", "worker_complete", "worker_error",
    "worker_tick"
}
_PHASES = {
    "clock_wait", "collecting", "idle", "interrupted", "other", "planning", "published",
    "publishing", "retry_wait", "slot_advanced", "stopped", "storage_wait"
}
_ENDPOINTS = {
    "changes", "dashboard", "discovery", "facets", "live", "manifest", "other", "package_resource",
    "packages", "ready", "recommendation-sources", "recommendations", "releases", "source-controls",
    "sources"
}
_ERRORS = {
    "AssertionError", "CancelledError", "FileNotFoundError", "IntegrityError", "KeyError",
    "OSError", "OperationalError", "OtherError", "PermissionError", "RuntimeError", "TimeoutError",
    "TypeError", "ValueError"
}
_COMMANDS = {
    "backup", "batch-plan", "batch-run", "batch-status", "build", "candidate-review-inspect",
    "candidate-review-record", "collect-enqueue", "collect-run", "collect-status", "detail",
    "discover-enqueue", "discover-history", "discover-incremental", "discover-run",
    "downloads-enqueue", "downloads-plan", "downloads-run", "downloads-status", "manifest-enqueue",
    "manifest-plan", "manifest-run", "manifest-status", "map-canonical", "map-enqueue", "metrics",
    "offline-export", "offline-verify", "other", "query", "recommendation-enqueue",
    "recommendation-query", "recommendation-run", "recommendation-sources", "recommendation-status",
    "release-enqueue", "release-plan", "release-query", "release-run", "release-status",
    "refresh-status", "retention-apply", "retention-plan", "retention-run", "schedule-run", "schedule-status",
    "schedule-tick", "snapshot-activate", "snapshot-export", "snapshot-import", "snapshot-status",
    "source-control-inspect", "source-control-record", "source-controls-query", "state-backup",
    "state-backup-verify", "state-recovery-status", "state-restore", "storage-status", "verify"
}

_CODES = {
    "domain_error", "internal_error", "log_configuration_invalid", "publication_conflict",
    "query_timeout", "state_version", "storage_budget_wait", "worker_busy", "worker_failed"
}


def error_kind(error):
    name = type(error).__name__
    return name if name in _ERRORS else "OtherError"


def route_kind(path):
    fixed = {"/health/live": "live", "/health/ready": "ready"}
    if path in fixed:
        return fixed[path]
    if path.startswith("/api/v1/packages/"):
        return "package_resource"
    if path.startswith("/api/v1/discoveries/"):
        return "discovery"
    suffix = path.removeprefix("/api/v1/")
    return suffix if path.startswith("/api/v1/") and suffix in _ENDPOINTS else "other"


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("operational log files must be unlinked ordinary files")
    return info


def _open(path, flags):
    before = _regular(path) if path.exists() or path.is_symlink() else None
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or before and (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)):
            raise ValueError("operational log file changed while opening")
    except BaseException:
        os.close(fd)
        raise
    return fd


class OperationalLog:
    def __init__(self, role, *, directory=None, policy=LogPolicy(), protected=(), stream=None,
                 clock=None, console_enabled=True):
        if role not in ("query", "worker") or not isinstance(policy, LogPolicy):
            raise ValueError("invalid operational logger configuration")
        self.role = role
        self.policy = policy
        self.directory = None
        self.stream = stream if stream is not None else sys.stderr
        self.console_enabled = console_enabled
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._mutex = threading.RLock()
        self._file = None
        self._lease = None
        self.failed = False
        self._warned = False
        self.closed = False
        self._started = None
        self._next_expiry = float("inf")
        if directory is None:
            return
        root = Path(directory).absolute()
        if root != root.resolve():
            raise ValueError("log directory must not contain links or traversal")
        for path in protected:
            path = Path(path).resolve()
            if root.is_relative_to(path) or path.is_relative_to(root):
                raise ValueError("log directory must not overlap catalog, code or runtime")
        if root.exists() and (not root.is_dir() or getattr(root.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("log directory must be an ordinary directory")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory = root
        try:
            self._lease = os.fdopen(_open(root / ("." + role + ".lock"), os.O_RDWR | os.O_CREAT), "r+b")
            if os.fstat(self._lease.fileno()).st_size == 0:
                self._lease.write(b"0")
                self._lease.flush()
            self._lease.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            marker = root / ("." + role + ".owner.json")
            expected = {"schemaVersion": 1, "kind": "agent-forge-operational-log", "role": role}
            if marker.exists() or marker.is_symlink():
                if _regular(marker).st_size > 1024:
                    raise ValueError("log ownership marker does not match this service")
                declared = json.loads(marker.read_text(encoding="utf-8"))
                if (not isinstance(declared, dict) or type(declared.get("schemaVersion")) is not int
                        or declared != expected):
                    raise ValueError("log ownership marker does not match this service")
            else:
                if any(self._path(i).exists() or self._path(i).is_symlink() for i in range(21)):
                    raise ValueError("preexisting log files have no ownership marker")
                with os.fdopen(_open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as output:
                    output.write((json.dumps(expected) + "\n").encode())
            self._validate_files()
            self._prune()
            self._open_current()
        except BaseException:
            self.close()
            raise

    def _path(self, index=0):
        return self.directory / (self.role + ".jsonl" + ("." + str(index) if index else ""))

    def _validate_files(self):
        for index in range(21):
            path=self._path(index)
            if path.exists() or path.is_symlink():
                if _regular(path).st_size > self.policy.max_bytes:
                    raise ValueError("existing owned log exceeds the configured byte limit; use a new reviewed directory")

    def _oldest(self, path):
        info = _regular(path)
        if not info.st_size:
            return info.st_mtime
        with os.fdopen(_open(path, os.O_RDONLY), "rb") as source:
            line = source.readline(2049)
            source.seek(-1, os.SEEK_END)
            complete = source.read(1) == b"\n"
        if len(line) > 2048 or not line.endswith(b"\n") or not complete:
            raise ValueError("invalid owned operational log record")
        row = json.loads(line)
        if (not isinstance(row, dict) or row.get("role") != self.role
                or not isinstance(row.get("event"), str) or row["event"] not in _EVENTS
                or not isinstance(row.get("at"), str)):
            raise ValueError("invalid owned operational log record")
        stamp = datetime.fromisoformat(row["at"])
        if stamp.tzinfo is None:
            raise ValueError("operational log timestamp must include timezone")
        return stamp.timestamp()

    def _prune(self):
        now = self.clock().timestamp()
        self._next_expiry = float("inf")
        for index in range(21):
            path = self._path(index)
            if not (path.exists() or path.is_symlink()):
                continue
            expires = self._oldest(path) + self.policy.max_age_seconds
            if index > self.policy.backups or expires <= now:
                path.unlink()
            else:
                self._next_expiry = min(self._next_expiry, expires)

    def _open_current(self):
        if self._file is None:
            path = self._path()
            self._started = self._oldest(path) if path.exists() and _regular(path).st_size else None
            self._file = os.fdopen(_open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT), "ab", buffering=0)

    def _close_current(self):
        output, self._file = self._file, None
        if output is not None:
            output.close()

    def _rotate(self):
        self._close_current()
        for index in range(self.policy.backups, 0, -1):
            target, source = self._path(index), self._path(index - 1)
            if target.exists():
                _regular(target)
            if source.exists():
                _regular(source)
                os.replace(source, target)
        self._prune()
        self._open_current()

    def _console(self, payload):
        if not self.console_enabled:
            return
        try:
            self.stream.write(payload.decode("ascii"))
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def emit(self, event, **fields):
        with self._mutex:
            if self.closed:
                return
            data = {"at": self.clock().astimezone(timezone.utc).isoformat(), "role": self.role,
                    "event": event if isinstance(event, str) and event in _EVENTS else "other_event"}
            for name, allowed, fallback in (("phase", _PHASES, "other"), ("endpoint", _ENDPOINTS, "other"),
                                            ("command", _COMMANDS, "other"), ("code", _CODES, "domain_error"),
                                            ("error_kind", _ERRORS, "OtherError")):
                if name in fields:
                    value = fields[name]
                    data[name] = value if isinstance(value, str) and value in allowed else fallback
            method = fields.get("method")
            if isinstance(method, str) and method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}:
                data["method"] = method
            for name, minimum, maximum in (("status", 100, 599), ("requests", 0, 100000), ("exit_code", 0, 255)):
                value = fields.get(name)
                if type(value) is int and minimum <= value <= maximum:
                    data[name] = value
            duration = fields.get("duration_ms")
            if type(duration) in (int, float) and math.isfinite(duration) and 0 <= duration <= 86400000:
                data["duration_ms"] = round(duration, 3)
            identity = fields.get("request_id")
            if isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{32}", identity):
                data["request_id"] = identity
            payload = (json.dumps(data, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
            if self.directory is None or self.failed:
                self._console(payload)
                return
            try:
                now = self.clock().timestamp()
                if now >= self._next_expiry:
                    self._close_current()
                    self._prune()
                    self._open_current()
                # A backwards clock must not append older records behind the recorded oldest timestamp.
                if self._started is not None and now < self._started:
                    self._rotate()
                if os.fstat(self._file.fileno()).st_size + len(payload) > self.policy.max_bytes:
                    self._rotate()
                if self._file.write(payload) != len(payload):
                    raise OSError("short operational log write")
                if self._started is None:
                    self._started = now
                    self._next_expiry = min(self._next_expiry, now + self.policy.max_age_seconds)
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                self.failed = True
                if not self._warned:
                    self._warned = True
                    warning = {"at": data["at"], "role": self.role, "event": "log_io_error"}
                    self._console((json.dumps(warning) + "\n").encode("ascii"))
                self._console(payload)

    def worker_event(self, raw):
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                value = {}
        except (ValueError, TypeError):
            value = {}
        self.emit("worker_tick", phase=value.get("phase"), requests=value.get("requestsThisTick"),
                  code=value.get("error"))

    def close(self):
        with self._mutex:
            try:
                self._close_current()
            finally:
                if self._lease is not None:
                    self._lease.close()
                    self._lease = None
                self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _ServerHandler(logging.Handler):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def emit(self, record):
        event = ("server_error" if record.levelno >= logging.ERROR else
                 "server_warning" if record.levelno >= logging.WARNING else "server_lifecycle")
        kind = error_kind(record.exc_info[1]) if record.exc_info and record.exc_info[1] else None
        # Do not call getMessage(), format args or attach raw traces.
        self.events.emit(event, **({"error_kind": kind} if kind else {}))


@contextmanager
def server_logging(events):
    saved = []
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "asyncio", "agent_forge_service"):
        logger = logging.getLogger(name)
        saved.append((logger, logger.handlers[:], logger.level, logger.propagate, logger.disabled))
        logger.handlers = [_ServerHandler(events)] if name != "uvicorn.access" else []
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.disabled = name == "uvicorn.access"
    try:
        yield
    finally:
        for logger, handlers, level, propagate, disabled in saved:
            logger.handlers = handlers
            logger.setLevel(level)
            logger.propagate = propagate
            logger.disabled = disabled


def add_log_arguments(parser):
    parser.add_argument("--log-directory", type=Path)
    parser.add_argument("--log-max-bytes", type=int, default=1024**2)
    parser.add_argument("--log-backups", type=int, default=3)
    parser.add_argument("--log-max-age-seconds", type=int, default=7 * 86400)


def from_arguments(role, args, *, protected=(), console_enabled=True):
    return OperationalLog(role, directory=args.log_directory,
                          policy=LogPolicy(args.log_max_bytes, args.log_backups, args.log_max_age_seconds),
                          protected=protected, console_enabled=console_enabled)


class RequestLogMiddleware:
    """Observe fixed request fields, including CORS/framework failures, without reading bodies."""
    def __init__(self, app, *, events):
        self.app = app
        self.events = events

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        import time
        import uuid
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["operational_request_id"] = request_id
        status = 500
        failure = None

        async def record_start(message):
            nonlocal request_id, status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                existing = next((value for key, value in headers if key.lower() == b"x-request-id"), None)
                if existing is not None and re.fullmatch(b"[0-9a-f]{32}", existing):
                    request_id = existing.decode("ascii")
                else:
                    headers = [(key, value) for key, value in headers if key.lower() != b"x-request-id"]
                    headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, record_start)
        except Exception as error:
            failure = error_kind(error)
            raise
        finally:
            path = scope.get("path", "")
            prefix = scope.get("root_path", "")
            if prefix and path.startswith(prefix + "/"):
                path = path[len(prefix):]
            fields = dict(method=scope.get("method"), endpoint=route_kind(path), status=status,
                          duration_ms=(time.monotonic() - started) * 1000, request_id=request_id)
            if failure:
                fields["error_kind"] = failure
            self.events.emit("http_request", **fields)
