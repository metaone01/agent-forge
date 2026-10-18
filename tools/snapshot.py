#!/usr/bin/env python3
"""Build optional per-Agent/type SQLite FTS5 snapshots from generated JSON."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def build_snapshot(data_root: Path, output_root: Path) -> int:
    count = 0
    for index_path in sorted(data_root.glob("*/**/index.json")):
        relative = index_path.relative_to(data_root)
        if len(relative.parts) != 3:
            continue
        agent_id, package_type, _ = relative.parts
        index = load(index_path)
        db_path = output_root / agent_id / f"{package_type}.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            db_path.unlink()
        connection = sqlite3.connect(db_path)
        try:
            connection.executescript(
                """
                PRAGMA journal_mode=DELETE;
                CREATE TABLE packages (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    latest TEXT NOT NULL,
                    versions_json TEXT NOT NULL,
                    path TEXT NOT NULL,
                    subtype TEXT,
                    revision TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE packages_fts USING fts5(name, subtype, content='packages', content_rowid='id');
                """
            )
            for name, entry in sorted(index.get("packages", {}).items()):
                connection.execute(
                    "INSERT INTO packages(name, latest, versions_json, path, subtype, revision) VALUES (?, ?, ?, ?, ?, ?)",
                    (name, entry["latest"], json.dumps(entry["versions"], ensure_ascii=False), entry["path"], entry.get("subtype"), index["revision"]),
                )
            connection.execute("INSERT INTO packages_fts(packages_fts) VALUES ('rebuild')")
            connection.commit()
        finally:
            connection.close()
        count += 1
    return count


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--query-runtime", type=Path)
    parser.add_argument("--agent")
    parser.add_argument("--type", choices=("mcp", "plugin", "skill", "general", "bundle"))
    parser.add_argument("--revision")
    args = parser.parse_args(argv)
    if args.query_runtime is not None:
        if args.data is not None or args.output is None or args.agent is None or args.type is None:
            parser.error("--query-runtime requires --output, --agent and --type, without --data")
        if __package__ in (None, ""):
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from agent_forge_service.offline import export_offline
        from agent_forge_service.settings import Settings
        result = export_offline(Settings(Path.cwd(), args.query_runtime), args.output,
                                agent=args.agent, package_type=args.type, revision=args.revision)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        if any(value is not None for value in (args.agent, args.type, args.revision)):
            parser.error("--agent, --type and --revision require --query-runtime")
        count = build_snapshot(args.data or Path("data"), args.output or Path("snapshots"))
        print(json.dumps({"snapshots": count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
