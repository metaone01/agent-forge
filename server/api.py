#!/usr/bin/env python3
"""Read-only HTTP query API over the Agent Forge SQLite catalog.

Standard library only (``http.server`` + ``sqlite3``). nginx serves the static
frontend and reverse-proxies ``/api/`` here. Connections are pooled and opened
read-only, so no writes are possible and per-request setup is avoided.

Endpoints
---------
GET /api/health
GET /api/manifest
GET /api/search    ?q=&agent=&type=&subtype=&sort=&page=&pageSize=
GET /api/package   ?agent=&type=&name=&version=
GET /api/dashboard
GET /api/agent-dashboard ?id=
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sqlite3
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

DEFAULT_DB = Path(os.environ.get("CATALOG_DB", "catalog.sqlite"))
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50
MAX_QUERY_LEN = 200

ENTRY_COLUMNS = (
    "agent_id, type, name, id, display_name, subtype, latest, versions_json, "
    "summary, description, keywords_json, facets_json, custom_facets_json, "
    "updated_at, media_json"
)

_META_LOCK = threading.Lock()
_META_CACHE: dict[str, object] = {}


class DBPool:
    """A tiny pool of read-only connections reused across request threads."""

    def __init__(self, path: Path, size: int = 8):
        self.path = path
        self._pool: queue.Queue[sqlite3.Connection] = queue.Queue()
        for _ in range(size):
            self._pool.put(self._open())

    def _open(self) -> sqlite3.Connection:
        # Each pooled connection is leased to one request thread at a time, so
        # cross-thread use is safe here.
        connection = sqlite3.connect(
            f"file:{self.path}?mode=ro", uri=True, timeout=15, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA mmap_size = 67108864")
        connection.execute("PRAGMA cache_size = -8000")
        return connection

    @contextmanager
    def connection(self):
        connection = self._pool.get()
        try:
            yield connection
        finally:
            self._pool.put(connection)


def _json(value):
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError):
        return None


def row_to_entry(row: sqlite3.Row) -> dict:
    return {
        "key": f"{row['agent_id']}:{row['type']}:{row['name']}",
        "id": row["id"] or "",
        "packageId": row["id"] or "",
        "routeName": row["name"],
        "name": row["name"],
        "displayName": row["display_name"] or "",
        "agentId": row["agent_id"],
        "type": row["type"],
        "subtype": row["subtype"],
        "latest": row["latest"] or "unknown",
        "versions": _json(row["versions_json"]) or [],
        "summary": row["summary"] or "",
        "description": row["description"] or "",
        "keywords": _json(row["keywords_json"]) or [],
        "facets": _json(row["facets_json"]) or {},
        "customFacets": _json(row["custom_facets_json"]) or {},
        "updatedAt": row["updated_at"],
        "media": _json(row["media_json"]),
    }


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_filters(params: dict[str, str]):
    """Return (where_sql, named_args, query)."""
    clauses: list[str] = []
    args: dict[str, str] = {}
    if params.get("agent"):
        clauses.append("agent_id = :agent")
        args["agent"] = params["agent"]
    if params.get("type"):
        clauses.append("type = :type")
        args["type"] = params["type"]
    if params.get("subtype"):
        clauses.append("subtype = :subtype")
        args["subtype"] = params["subtype"]
    query = (params.get("q") or "").strip().lower()
    if query:
        if len(query) >= 3:
            # Trigram FTS5: substring match that still uses the index.
            clauses.append("rowid IN (SELECT rowid FROM packages_fts WHERE packages_fts MATCH :fts)")
            args["fts"] = '"' + query.replace('"', '""') + '"'
        else:
            # 1-2 char queries cannot use the trigram index; a short scan is fine.
            clauses.append("search_text LIKE :like ESCAPE '\\'")
            args["like"] = f"%{escape_like(query)}%"
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, args, query


def order_clause(sort: str, query: str) -> str:
    if sort == "name":
        return " ORDER BY name COLLATE NOCASE ASC"
    if sort == "relevance" and query:
        return (
            " ORDER BY CASE"
            " WHEN lower(name) = :rel_q THEN 0"
            " WHEN lower(name) LIKE :rel_prefix ESCAPE '\\' THEN 1"
            " WHEN lower(name) LIKE :rel_contains ESCAPE '\\' THEN 2"
            " ELSE 3 END, updated_at DESC, name COLLATE NOCASE ASC"
        )
    # Default and "updated": newest first. NULLs sort last for DESC in SQLite.
    return " ORDER BY updated_at DESC, name COLLATE NOCASE ASC"


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentForgeAPI/1.1"
    pool: DBPool = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # keep systemd journal quiet
        pass

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def fail(self, status, message):
        self.send_json({"error": message}, status)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        params = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
        try:
            if route == "/api/health":
                return self.send_json({"ok": True})
            if route == "/api/manifest":
                return self.manifest()
            if route == "/api/search":
                return self.search(params)
            if route == "/api/package":
                return self.package(params)
            if route == "/api/dashboard":
                return self.dashboard("global", "")
            if route == "/api/agent-dashboard":
                return self.dashboard("agent", params.get("id", ""))
            return self.fail(404, "not found")
        except sqlite3.Error as error:  # pragma: no cover - defensive
            return self.fail(500, f"database error: {error}")

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def manifest(self):
        with _META_LOCK:
            if not _META_CACHE:
                with self.pool.connection() as connection:
                    meta_row = connection.execute(
                        "SELECT value FROM meta WHERE key='manifest'").fetchone()
                    meta = _json(meta_row["value"]) if meta_row else {}
                    agents = [r[0] for r in connection.execute(
                        "SELECT DISTINCT agent_id FROM packages ORDER BY agent_id")]
                    types = [r[0] for r in connection.execute(
                        "SELECT DISTINCT type FROM packages ORDER BY type")]
                    subtypes = [r[0] for r in connection.execute(
                        "SELECT DISTINCT subtype FROM packages WHERE subtype IS NOT NULL ORDER BY subtype")]
                    total = connection.execute("SELECT COUNT(*) FROM packages").fetchone()[0]
                    sources = [dict(r) for r in connection.execute(
                        "SELECT agent_id, type, path FROM sources ORDER BY agent_id, type")]
                _META_CACHE.update({
                    "revision": meta.get("revision"),
                    "generatedAt": meta.get("generatedAt"),
                    "cutoff": meta.get("cutoff"),
                    "schemaVersion": meta.get("schemaVersion"),
                    "packageCount": total,
                    "agents": agents,
                    "types": types,
                    "subtypes": subtypes,
                    "sources": sources,
                })
        return self.send_json(dict(_META_CACHE))

    def search(self, params: dict[str, str]):
        try:
            page = max(0, int(params.get("page", "0")))
        except ValueError:
            page = 0
        try:
            page_size = int(params.get("pageSize", str(DEFAULT_PAGE_SIZE)))
        except ValueError:
            page_size = DEFAULT_PAGE_SIZE
        page_size = max(1, min(MAX_PAGE_SIZE, page_size))
        if len(params.get("q", "")) > MAX_QUERY_LEN:
            return self.fail(400, "query too long")
        sort = params.get("sort", "relevance")

        where, filters, query = build_filters(params)
        order = order_clause(sort, query)
        bindings = dict(filters)
        if query and sort == "relevance":
            bindings.update({
                "rel_q": query,
                "rel_prefix": escape_like(query) + "%",
                "rel_contains": "%" + escape_like(query) + "%",
            })
        bindings["limit"] = page_size
        bindings["offset"] = page * page_size

        with self.pool.connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM packages{where}", filters).fetchone()[0]
            rows = connection.execute(
                f"SELECT {ENTRY_COLUMNS} FROM packages{where}{order} LIMIT :limit OFFSET :offset",
                bindings,
            ).fetchall()

        return self.send_json({
            "total": total,
            "page": page,
            "pageSize": page_size,
            "entries": [row_to_entry(r) for r in rows],
        })

    def package(self, params: dict[str, str]):
        agent = params.get("agent")
        package_type = params.get("type")
        name = params.get("name")
        if not (agent and package_type and name):
            return self.fail(400, "agent, type and name are required")
        with self.pool.connection() as connection:
            row = connection.execute(
                f"SELECT {ENTRY_COLUMNS} FROM packages WHERE agent_id=? AND type=? AND name=?",
                (agent, package_type, name),
            ).fetchone()
            if not row:
                return self.fail(404, "package not found")
            version = params.get("version") or row["latest"]
            detail_row = connection.execute(
                "SELECT json FROM details WHERE agent_id=? AND type=? AND name=? AND version=?",
                (agent, package_type, name, version),
            ).fetchone()
            if not detail_row:
                detail_row = connection.execute(
                    "SELECT json FROM details WHERE agent_id=? AND type=? AND name=? ORDER BY version DESC LIMIT 1",
                    (agent, package_type, name),
                ).fetchone()
        entry = row_to_entry(row)
        detail = _json(detail_row["json"]) if detail_row else {}
        merged = {**entry, **(detail or {})}
        merged["name"] = (detail or {}).get("name") or entry["name"]
        merged["packageId"] = (detail or {}).get("packageId") or (detail or {}).get("id") or entry["packageId"]
        return self.send_json(merged)

    def dashboard(self, scope, identifier):
        with self.pool.connection() as connection:
            row = connection.execute(
                "SELECT json FROM dashboards WHERE scope=? AND id=?",
                (scope, identifier),
            ).fetchone()
        if not row:
            return self.fail(404, "dashboard not found")
        return self.send_json(_json(row["json"]) or {})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8791")))
    parser.add_argument("--pool", type=int, default=8)
    args = parser.parse_args()
    if not args.db.is_file():
        raise SystemExit(f"api: database not found: {args.db}")
    Handler.pool = DBPool(args.db, size=args.pool)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"agent-forge api on http://{args.host}:{args.port} using {args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
