"""Admission checks and expendable HTTP cache limits; observation facts stay intact."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import sqlite3

from .errors import ServiceError
from .storage import timestamp


@dataclass(frozen=True)
class CachePolicy:
    max_entries: int = 1024
    max_bytes: int = 32 * 1024 ** 2
    max_age_seconds: int = 7 * 86400

    def __post_init__(self):
        for name, low, high in (("max_entries",1,10000),("max_bytes",1024,1024**3),("max_age_seconds",60,90*86400)):
            value=getattr(self,name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("invalid HTTP cache budget: " + name)


@dataclass(frozen=True)
class ResourceLimits:
    max_state_bytes: int = 1024 ** 3
    min_free_bytes: int = 512 * 1024 ** 2
    cache: CachePolicy = CachePolicy()

    def __post_init__(self):
        if type(self.max_state_bytes) is not int or not 1024**2 <= self.max_state_bytes <= 32*1024**3:
            raise ValueError("max_state_bytes must be in [1 MiB,32 GiB]")
        if type(self.min_free_bytes) is not int or not 0 <= self.min_free_bytes <= 1024**4:
            raise ValueError("min_free_bytes must be in [0,1 TiB]")
        if not isinstance(self.cache,CachePolicy):
            raise ValueError("cache limits must be explicit")


DEFAULT_LIMITS=ResourceLimits()


class WorkerConnection(sqlite3.Connection):
    resource_limits = DEFAULT_LIMITS


def storage_status(runtime: Path, limits=DEFAULT_LIMITS):
    """Only stat/disk-usage calls; no state opens, directories, caches or secrets."""
    if not isinstance(limits,ResourceLimits):
        raise ValueError("resource limits must be explicit")
    runtime=Path(runtime).absolute()
    if runtime != runtime.resolve():
        raise ValueError("runtime path must not contain links or traversal")
    if runtime.exists() and not runtime.is_dir():
        raise ValueError("runtime must be a directory")
    sizes={}
    for name in ("state.db","state.db-wal","state.db-shm"):
        path=runtime/name
        if path.is_symlink():raise ValueError("private state files must not be links")
        if path.exists():
            import stat
            info=path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info,"st_file_attributes",0)&0x400:
                raise ValueError("private state files must be ordinary unlinked files")
            sizes[name]=info.st_size
        else:sizes[name]=0
    parent=runtime
    while not parent.exists():parent=parent.parent
    disk=shutil.disk_usage(parent)
    state_bytes=sum(sizes.values())
    reasons=[]
    if state_bytes>=limits.max_state_bytes:reasons.append("state_budget_exhausted")
    if disk.free<limits.min_free_bytes:reasons.append("disk_reserve_low")
    return {"scope":"local-worker-storage-admission","allowed":not reasons,"reasons":reasons,
            "stateFilesBytes":sizes,"stateTotalBytes":state_bytes,"freeBytes":disk.free,
            "limits":{"maxStateBytes":limits.max_state_bytes,"minFreeBytes":limits.min_free_bytes,
                      "cacheMaxEntries":limits.cache.max_entries,"cacheMaxBytes":limits.cache.max_bytes,
                      "cacheMaxAgeSeconds":limits.cache.max_age_seconds},
            "observationFactsDeleted":False,"privateCredentialsIncluded":False}


def require_runtime_room(runtime: Path,limits=DEFAULT_LIMITS):
    status=storage_status(runtime,limits)
    if not status["allowed"]:
        raise ServiceError("storage_budget_wait","worker storage budget requires operator attention",503)
    return status


def require_collection_room(connection):
    rows=connection.execute("PRAGMA database_list").fetchall()
    path=next((row[2] for row in rows if row[1]=="main"),"")
    if path:
        require_runtime_room(Path(path).parent,getattr(connection,"resource_limits",DEFAULT_LIMITS))


def _instant(value):
    return datetime.fromisoformat(timestamp(value).replace("Z","+00:00"))


def cached_response(connection,key,identity,*,now):
    row=connection.execute("SELECT * FROM http_cache WHERE request_key=? AND credential_identity=?",(key,identity)).fetchone()
    if row is None:return None
    policy=getattr(connection,"resource_limits",DEFAULT_LIMITS).cache
    try:
        checked=_instant(row["checked_at"])
        if checked > now or (now-checked).total_seconds()>=policy.max_age_seconds:return None
    except (TypeError,ValueError,AttributeError):return None
    # Cache budget is a total UTF-8 data budget including validators and keys.
    if sum(len(str(value).encode("utf-8")) for value in row if value is not None)>policy.max_bytes:return None
    return row


_SIZE=" + ".join("coalesce(length(CAST("+column+" AS BLOB)),0)" for column in
    ("request_key","credential_identity","etag","last_modified","body_json","body_hash","observed_at","checked_at"))


def cache_status(connection):
    row=connection.execute("SELECT count(*),coalesce(sum("+_SIZE+"),0) FROM http_cache").fetchone()
    return {"entries":row[0],"logicalBytes":row[1],"byteBasis":"UTF-8 values, excluding SQLite pages and indexes"}


def store_cached_response(connection,values,*,now):
    """Call inside the client's cache transaction; never touch observation tables."""
    if not connection.in_transaction:
        # A with-connection context begins its transaction on the first DML.
        connection.execute("BEGIN IMMEDIATE")
    policy=getattr(connection,"resource_limits",DEFAULT_LIMITS).cache
    size=sum(len(str(value).encode("utf-8")) for value in values if value is not None)
    if size<=policy.max_bytes:
        connection.execute("INSERT INTO http_cache VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(request_key) DO UPDATE SET "
            "credential_identity=excluded.credential_identity,etag=excluded.etag,last_modified=excluded.last_modified,"
            "body_json=excluded.body_json,body_hash=excluded.body_hash,observed_at=excluded.observed_at,checked_at=excluded.checked_at",values)
    else:
        connection.execute("DELETE FROM http_cache WHERE request_key=?",(values[0],))
    cutoff=timestamp(now-timedelta(seconds=policy.max_age_seconds))
    connection.execute("DELETE FROM http_cache WHERE checked_at<=? OR checked_at>?",(cutoff,timestamp(now)))
    # At most max_entries current rows are retained. Timestamp/key order is
    # deterministic; a 304 updates checked_at but never fabricates observed_at.
    rows=connection.execute("SELECT request_key,("+_SIZE+") AS bytes FROM http_cache ORDER BY checked_at DESC,request_key").fetchall()
    kept_bytes=0;kept=0;remove=[]
    for key,bytes_count in rows:
        if kept>=policy.max_entries or kept_bytes+bytes_count>policy.max_bytes:
            remove.append((key,))
        else:kept+=1;kept_bytes+=bytes_count
    connection.executemany("DELETE FROM http_cache WHERE request_key=?",remove)
