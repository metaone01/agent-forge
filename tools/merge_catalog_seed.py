#!/usr/bin/env python3
"""Merge migrated records and the existing seed records into one package tree.

This is a one-time catalog bootstrap helper.  It keeps one canonical record per
(type, name, version), then rebuilds the category indexes used by validation.
It does not contact or execute any upstream distribution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


TYPES = ("mcp", "plugin", "skill", "general", "bundle")


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_name(value: str) -> str:
    readable = "".join(char if char.isalnum() or char in ".@+-" else "-" for char in value).strip("-") or "package"
    return f"{readable}--{hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]}"


def records(root: Path, package_type: str) -> dict[tuple[str, str], dict[str, Any]]:
    found: dict[tuple[str, str], dict[str, Any]] = {}
    package_root = root / "sources" / package_type / "packages"
    if not package_root.exists():
        return found
    for path in sorted(package_root.rglob("*.json")):
        value = load(path)
        key = (str(value.get("name", "")), str(value.get("version", "")))
        if key[0] and key[1]:
            found.setdefault(key, value)
    return found


def rebuild(output: Path, package_type: str, values: dict[tuple[str, str], dict[str, Any]]) -> None:
    category = output / "sources" / package_type
    package_entries: dict[str, dict[str, Any]] = {}
    for (name, version), value in sorted(values.items()):
        relative = Path("packages") / safe_name(name) / f"{safe_name(version)}.json"
        dump(category / relative, value)
        entry = package_entries.setdefault(name, {"latest": version, "versions": [], "path": relative.as_posix()})
        entry["versions"].append(version)
        if str(version) > str(entry["latest"]):
            entry["latest"] = version
        entry["path"] = relative.as_posix()
    for entry in package_entries.values():
        entry["versions"] = sorted(set(entry["versions"]))
    revision = "packages-seed-20261001T000000Z"
    base_url = f"https://metaone01.github.io/agent-forge/data/canonical/{package_type}/"
    source = {
        "schemaVersion": 2,
        "sourceId": f"agent-forge:canonical:{package_type}",
        "name": f"Agent Forge canonical {package_type} records",
        "agentId": None,
        "type": package_type,
        "baseUrl": base_url,
        "index": "index.json",
        "revision": revision,
        "generatedAt": "2026-10-01T00:00:00Z",
        "updatedAt": "2026-10-01T00:00:00Z",
        "official": True,
        "description": "Canonical metadata records consumed by Agent Forge projections.",
    }
    dump(category / "source.json", source)
    dump(category / "index.json", {
        "schemaVersion": 2,
        "sourceId": source["sourceId"],
        "sourceManifest": "source.json",
        "sourceUrl": base_url + "source.json",
        "agentId": None,
        "type": package_type,
        "revision": revision,
        "generatedAt": source["generatedAt"],
        "updatedAt": source["updatedAt"],
        "packages": dict(sorted(package_entries.items())),
    })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--migrated", type=Path, required=True)
    parser.add_argument("--existing", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        shutil.rmtree(args.output)
    args.output.mkdir(parents=True)
    counts: dict[str, int] = {}
    for package_type in TYPES:
        merged = records(args.migrated, package_type)
        for key, value in records(args.existing, package_type).items():
            merged.setdefault(key, value)
        rebuild(args.output, package_type, merged)
        counts[package_type] = len(merged)
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
