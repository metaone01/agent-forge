#!/usr/bin/env python3
"""Build a single query SQLite database from projected Agent Forge data.

Input is the projection produced by ``tools/project.py`` (``data/``): a
``manifest.json``, per source ``<agent>/<type>/index.json`` indexes, per version
detail JSON files, and dashboard aggregates. Output is one ``catalog.sqlite``
file that the read-only query API (``server/api.py``) serves.

This tool uses only the Python standard library and FTS5.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = OFF;
PRAGMA synchronous = OFF;
PRAGMA temp_store = MEMORY;

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE sources (
    agent_id TEXT NOT NULL,
    type     TEXT NOT NULL,
    path     TEXT NOT NULL,
    PRIMARY KEY (agent_id, type)
);

CREATE TABLE packages (
    agent_id      TEXT NOT NULL,
    type          TEXT NOT NULL,
    name          TEXT NOT NULL,
    id            TEXT,
    display_name  TEXT,
    subtype       TEXT,
    latest        TEXT,
    versions_json TEXT NOT NULL DEFAULT '[]',
    summary       TEXT,
    description   TEXT,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    facets_json   TEXT NOT NULL DEFAULT '{}',
    custom_facets_json TEXT NOT NULL DEFAULT '{}',
    updated_at    TEXT,
    detail_path   TEXT,
    media_json    TEXT,
    search_text   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (agent_id, type, name)
);

CREATE INDEX idx_packages_agent_type ON packages (agent_id, type);
CREATE INDEX idx_packages_subtype    ON packages (subtype);
CREATE INDEX idx_packages_updated    ON packages (updated_at);
CREATE INDEX idx_packages_name       ON packages (name);
CREATE INDEX idx_packages_type       ON packages (type);

CREATE TABLE details (
    agent_id TEXT NOT NULL,
    type     TEXT NOT NULL,
    name     TEXT NOT NULL,
    version  TEXT NOT NULL,
    json     TEXT NOT NULL,
    PRIMARY KEY (agent_id, type, name, version)
);

CREATE TABLE dashboards (
    scope TEXT NOT NULL,
    id    TEXT NOT NULL,
    json  TEXT NOT NULL,
    PRIMARY KEY (scope, id)
);

CREATE VIRTUAL TABLE packages_fts USING fts5 (
    name,
    summary,
    keywords,
    facets,
    search_text UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def text(value) -> str:
    return "" if value is None else str(value)


def as_list(value) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def as_dict(value) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def flatten(value) -> str:
    if isinstance(value, list):
        return " ".join(flatten(item) for item in value)
    if isinstance(value, dict):
        return " ".join(flatten(item) for item in value.values())
    return text(value)


def build(data_root: Path, output: Path) -> dict:
    manifest_path = data_root / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"build_db: missing {manifest_path}")
    manifest = load(manifest_path)

    if output.exists():
        output.unlink()
    output.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(output)
    stats = {"packages": 0, "details": 0, "sources": 0}
    try:
        connection.executescript(SCHEMA)

        connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            ("manifest", json.dumps({
                "revision": manifest.get("revision"),
                "generatedAt": manifest.get("generatedAt"),
                "cutoff": manifest.get("cutoff"),
                "schemaVersion": manifest.get("schemaVersion"),
                "packageCount": manifest.get("packageCount"),
            }, ensure_ascii=False)),
        )

        sources = manifest.get("sources") or []
        source_pairs = []
        for source in sources:
            agent_id = source.get("agentId")
            package_type = source.get("type")
            index_path = source.get("path")
            if not agent_id or not package_type or not index_path:
                continue
            connection.execute(
                "INSERT OR REPLACE INTO sources(agent_id, type, path) VALUES (?, ?, ?)",
                (agent_id, package_type, index_path),
            )
            source_pairs.append((agent_id, package_type, index_path))
            stats["sources"] += 1

        package_rows = []
        fts_rows = []
        detail_rows = []

        for agent_id, package_type, index_path in source_pairs:
            index_file = data_root / index_path.replace("\\", "/")
            if not index_file.is_file():
                # manifest paths are relative to data/, but tolerate a leading data/
                index_file = data_root / index_path.replace("\\", "/").removeprefix("data/")
            if not index_file.is_file():
                print(f"build_db: skip missing index {index_path}", file=sys.stderr)
                continue
            index = load(index_file)
            index_dir = index_file.parent
            revision = index.get("revision") or manifest.get("revision")

            packages = index.get("packages")
            if isinstance(packages, list):
                items = [(item.get("name") or item.get("id"), item) for item in packages]
            elif isinstance(packages, dict):
                items = list(packages.items())
            else:
                items = []

            for fallback_name, record in items:
                if not isinstance(record, dict):
                    continue
                name = record.get("name") or fallback_name
                if not name:
                    continue
                latest = text(record.get("latest") or record.get("version") or "unknown")
                versions = as_list(record.get("versions")) or [latest]
                keywords = as_list(record.get("keywords"))
                facets = as_dict(record.get("facets"))
                custom_facets = as_dict(record.get("customFacets"))
                summary = text(record.get("summary") or record.get("description"))
                description = text(record.get("description") or record.get("summary"))
                subtype = record.get("subtype")
                updated_at = record.get("updatedAt") or index.get("updatedAt") or index.get("generatedAt")
                media = record.get("media")
                record_id = record.get("id") or record.get("packageId")
                display_name = record.get("displayName")
                detail_path = record.get("path")

                # searchText mirrors tools/project.py so server search matches the
                # historical static-index behaviour.
                search_parts = [
                    text(name), text(record.get("name")), text(record.get("id")),
                    text(record.get("packageId")), text(display_name), summary,
                    flatten(keywords), flatten(facets), flatten(custom_facets), text(subtype),
                ]
                search_text = " ".join(part for part in search_parts if part)

                package_rows.append((
                    agent_id, package_type, name, record_id, display_name, subtype,
                    latest, json.dumps(versions, ensure_ascii=False), summary, description,
                    json.dumps(keywords, ensure_ascii=False),
                    json.dumps(facets, ensure_ascii=False),
                    json.dumps(custom_facets, ensure_ascii=False),
                    updated_at, detail_path,
                    json.dumps(media, ensure_ascii=False) if media else None,
                    search_text.lower(),
                ))
                fts_rows.append((
                    text(name), summary, " ".join(text(k) for k in keywords),
                    flatten(facets) + " " + flatten(custom_facets), search_text.lower(),
                ))
                stats["packages"] += 1

                if detail_path and not str(detail_path).startswith("http"):
                    detail_file = index_dir / str(detail_path).replace("\\", "/")
                    if detail_file.is_file():
                        try:
                            detail = load(detail_file)
                        except (OSError, ValueError):
                            detail = None
                        if detail is not None:
                            detail_rows.append((
                                agent_id, package_type, name, latest,
                                json.dumps(detail, ensure_ascii=False),
                            ))
                            stats["details"] += 1

        connection.executemany(
            "INSERT OR REPLACE INTO packages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            package_rows,
        )
        connection.executemany(
            "INSERT INTO packages_fts(name, summary, keywords, facets, search_text) VALUES (?,?,?,?,?)",
            fts_rows,
        )
        connection.executemany(
            "INSERT OR REPLACE INTO details(agent_id, type, name, version, json) VALUES (?,?,?,?,?)",
            detail_rows,
        )

        # Dashboard aggregates: global + per agent.
        global_dashboard = data_root / "dashboard.json"
        if global_dashboard.is_file():
            connection.execute(
                "INSERT OR REPLACE INTO dashboards(scope, id, json) VALUES ('global', '', ?)",
                (global_dashboard.read_text(encoding="utf-8"),),
            )
        for agent_dashboard in sorted((data_root / "agents").glob("*/dashboard.json")):
            agent_id = agent_dashboard.parent.name
            connection.execute(
                "INSERT OR REPLACE INTO dashboards(scope, id, json) VALUES ('agent', ?, ?)",
                (agent_id, agent_dashboard.read_text(encoding="utf-8")),
            )

        connection.commit()
        connection.execute("INSERT INTO packages_fts(packages_fts) VALUES ('optimize')")
        connection.commit()
        connection.execute("VACUUM")
        connection.commit()
    finally:
        connection.close()

    stats["output"] = str(output)
    stats["bytes"] = output.stat().st_size if output.exists() else 0
    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("catalog.sqlite"))
    args = parser.parse_args()
    stats = build(args.data, args.output)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
