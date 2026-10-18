"""Durable at-least-once jobs with expiring, fenced leases."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from .errors import ServiceError
from .storage import encode_json, timestamp


def _text(value: str, label: str, maximum: int = 200):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(f"invalid {label}")


@contextmanager
def _transaction(connection):
    if connection.in_transaction:
        raise ValueError("job transitions require their own transaction")
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        yield


@dataclass(frozen=True)
class Lease:
    id: str
    kind: str
    scope: str
    payload: dict
    attempts: int
    owner: str
    token: str
    until: str


class JobQueue:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def enqueue(self, *, key: str, kind: str, scope: str, payload: dict,
                now: str | datetime, due_at: str | datetime, max_attempts: int = 5) -> str:
        # Deliberately no commit: enqueue and a page checkpoint may share one transaction.
        for value, label in ((key, "job key"), (kind, "job kind"), (scope, "job scope")):
            _text(value, label, 500)
        if type(max_attempts) is not int or not 1 <= max_attempts <= 100 or not isinstance(payload, dict):
            raise ValueError("invalid job policy or payload")
        encoded = encode_json(payload)
        if len(encoded.encode("utf-8")) > 16384:
            raise ValueError("job payload too large")
        identity = hashlib.sha256(key.encode("utf-8")).hexdigest()
        old = self.connection.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
        if old:
            if (old["kind"], old["scope"], old["payload_json"], old["max_attempts"]) != (kind, scope, encoded, max_attempts):
                raise ServiceError("job_conflict", "idempotency key has different job input", 409)
            from .source_controls import bind_job
            bind_job(self.connection,identity,kind,payload)
            return identity
        self.connection.execute(
            "INSERT INTO jobs(id,kind,scope,payload_json,state,max_attempts,next_run,created_at) "
            "VALUES (?,?,?,?,'pending',?,?,?)",
            (identity, kind, scope, encoded, max_attempts, timestamp(due_at), timestamp(now)))
        from .source_controls import bind_job
        bind_job(self.connection,identity,kind,payload)
        return identity

    def claim(self, *, owner: str, now: str | datetime, lease_seconds: int = 90,
              kind: str | None = None, job_ids: tuple[str, ...] | None = None,
              scopes: tuple[str, ...] | None = None) -> Lease | None:
        _text(owner, "lease owner")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid lease duration")
        if kind is not None:
            _text(kind, "job kind")
        filters, arguments = [], []
        if kind is not None:
            filters.append("kind=?"); arguments.append(kind)
        for column, values in (("id", job_ids), ("scope", scopes)):
            if values is not None:
                if not isinstance(values, tuple) or len(values) > 1000:
                    raise ValueError("claim filters must be bounded tuples")
                for value in values:
                    _text(value, "claim filter", 500)
                if not values:
                    return None
                filters.append(column + " IN (" + ",".join("?" for _ in values) + ")")
                arguments.extend(values)
        now = timestamp(now)
        until = timestamp(datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(seconds=lease_seconds))
        with _transaction(self.connection):
            from .source_controls import has_controls,enabled_clause
            if has_controls(self.connection):
                filters.append(enabled_clause())
            self.connection.execute(
                "UPDATE jobs SET state='dead',last_error='lease_expired',finished_at=?,"
                "lease_owner=NULL,lease_token=NULL,lease_until=NULL "
                "WHERE state='leased' AND lease_until<=? AND attempts>=max_attempts", (now, now))
            row = self.connection.execute(
                "SELECT * FROM jobs WHERE attempts<max_attempts AND "
                "((state IN ('pending','retry_wait') AND next_run<=?) OR "
                "(state='leased' AND lease_until<=?)) "
                + ("AND " + " AND ".join(filters) + " " if filters else "") + "ORDER BY next_run,created_at,id LIMIT 1",
                (now, now, *arguments)).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(16)
            self.connection.execute(
                "UPDATE jobs SET state='leased',attempts=attempts+1,lease_owner=?,lease_token=?,lease_until=? WHERE id=?",
                (owner, token, until, row["id"]))
            return Lease(row["id"], row["kind"], row["scope"], json.loads(row["payload_json"]),
                         row["attempts"] + 1, owner, token, until)

    def _assert_lease(self, lease: Lease, now: str):
        row = self.connection.execute(
            "SELECT * FROM jobs WHERE id=? AND state='leased' AND lease_owner=? "
            "AND lease_token=? AND lease_until>?", (lease.id, lease.owner, lease.token, now)).fetchone()
        if row is None:
            raise ServiceError("lease_lost", "job lease expired or belongs to another attempt", 409)
        return row

    def finish(self, lease: Lease, *, now: str | datetime, apply=None):
        now = timestamp(now)
        with _transaction(self.connection):
            self._assert_lease(lease, now)
            if apply is not None:
                apply(self.connection)
            self.connection.execute(
                "UPDATE jobs SET state='succeeded',finished_at=?,last_error=NULL,"
                "lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE id=?", (now, lease.id))

    def fail(self, lease: Lease, *, now: str | datetime, code: str,
             retry_at: str | datetime | None, attempted: bool = True):
        _text(code, "public-safe error code", 100)
        now = timestamp(now)
        if retry_at is not None and timestamp(retry_at) <= now:
            raise ValueError("retry must be in the future")
        with _transaction(self.connection):
            row = self._assert_lease(lease, now)
            attempts = row["attempts"] - (0 if attempted else 1)
            dead = retry_at is None or attempts >= row["max_attempts"]
            self.connection.execute(
                "UPDATE jobs SET state=?,attempts=?,next_run=?,last_error=?,finished_at=?,"
                "lease_owner=NULL,lease_token=NULL,lease_until=NULL WHERE id=?",
                ("dead" if dead else "retry_wait", attempts, timestamp(retry_at) if retry_at else now,
                 code, now if dead else None, lease.id))


def save_checkpoint(connection: sqlite3.Connection, *, source_id: str, shard_key: str,
                    cursor: dict | None, watermark: str | None, complete: bool,
                    now: str | datetime, details: dict):
    for value, label in ((source_id, "source"), (shard_key, "shard")):
        _text(value, label, 500)
    if not isinstance(details, dict) or cursor is not None and not isinstance(cursor, dict):
        raise ValueError("invalid checkpoint")
    if not complete and watermark is not None:
        raise ValueError("partial page cannot advance the successful watermark")
    previous = connection.execute("SELECT watermark,last_success_at FROM checkpoints WHERE source_id=? AND shard_key=?",
                                  (source_id, shard_key)).fetchone()
    new_mark = timestamp(watermark) if complete and watermark else (previous["watermark"] if previous else None)
    if previous and previous["watermark"] and new_mark and new_mark < previous["watermark"]:
        raise ValueError("checkpoint watermark cannot go backwards")
    connection.execute(
        "INSERT INTO checkpoints VALUES (?,?,?,?,?,?,?) ON CONFLICT(source_id,shard_key) DO UPDATE SET "
        "cursor_json=excluded.cursor_json,watermark=excluded.watermark,last_success_at=excluded.last_success_at,"
        "completeness=excluded.completeness,details_json=excluded.details_json",
        (source_id, shard_key, encode_json(cursor) if cursor is not None else None, new_mark,
         timestamp(now) if complete else (previous["last_success_at"] if previous else None),
         "complete" if complete else "partial", encode_json(details)))
