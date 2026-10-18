"""Readers hold an existing DB file open; no writable API lock directory."""
from contextlib import contextmanager
import os
import errno
from pathlib import Path
import sqlite3

from .errors import ServiceError


def _lock(stream, exclusive):
    if os.name != "nt":
        import fcntl
        try:
            fcntl.flock(stream.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise ServiceError("snapshot_busy", "snapshot has an active reader", 503) from error


class SnapshotConnection(sqlite3.Connection):
    _snapshot_lease = None

    def close(self):
        try:
            super().close()
        finally:
            if self._snapshot_lease is not None:
                self._snapshot_lease.close()
                self._snapshot_lease = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


@contextmanager
def exclusive_snapshot(path: Path):
    # Windows open readers prevent directory renames natively. The collector
    # must not keep its own open handle during that rename.
    if os.name == "nt":
        yield
        return
    with path.open("rb") as stream:
        _lock(stream, True)
        yield
