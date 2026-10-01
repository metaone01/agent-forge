#!/usr/bin/env python3
"""Migrate the archived Agent Forge v1 collection into the v2 package layout.

The migration is intentionally conservative: it keeps identity, descriptions,
links, and distribution candidates, but drops publication-time verification
claims. Every migrated Agent target is explicitly unknown unless the old record
contained an Agent identity; no compatibility range is invented.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


TYPE_MAP = {"mcp": "mcp", "plugin": "plugin", "skill": "skill", "other": "general"}
VERSION_SCHEME = {"semver": "semver", "npm": "npm", "pep440": "pep440", "calver": "calver", "date": "date"}
COLLECTED_AT = "2026-09-22T09:00:00Z"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def safe_name(value: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9._@+/-]+", "-", value).strip("/-") or "package"
    # Keep records distinct on case-insensitive filesystems (Windows) and for
    # names that differ only by punctuation.
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{readable}--{suffix}"


def agent_ids(record: dict[str, Any], package_type: str) -> list[str]:
    values: list[str] = []
    for item in record.get("supportedAgents", []):
        if isinstance(item, dict):
            value = item.get("name") or item.get("id")
        else:
            value = item
        if isinstance(value, str) and value.strip():
            values.append(value.strip().lower())
    # MCP registry records have no target Agent in the old format. DSH is the
    # first supported Agent and is the honest discovery projection for them.
    if not values:
        if package_type in {"mcp", "general"}:
            values.append("dsh")
        elif package_type in {"plugin", "skill"}:
            # The archived plugin/skill collections came from Claude's public
            # ecosystem and did not always repeat the Agent in each record.
            values.append("claude-code")
    return sorted(set(values))


def distribution(record: dict[str, Any], details: dict[str, Any]) -> dict[str, Any] | None:
    target = record.get("target") or {}
    url = http_url(target.get("url")) or http_url(record.get("repository")) or http_url(record.get("homepage"))
    if not url:
        return None
    registry_type = details.get("registryType")
    source_type = (target.get("repository") or registry_type or "other").lower()
    if source_type in {"npm", "pypi", "nuget", "cargo", "oci", "mcpb"}:
        distribution_type = "registry"
    elif source_type in {"git", "github"} or "github.com" in url:
        distribution_type = "git"
    else:
        distribution_type = "other"
    item: dict[str, Any] = {"id": "upstream", "type": distribution_type, "url": url, "priority": 10}
    ref = details.get("ref") or (record.get("_meta") or {}).get("io.github.metaone01/marketplace", {}).get("ref")
    if isinstance(ref, str) and ref:
        item["ref"] = ref
    if distribution_type == "registry":
        item["registry"] = source_type
    return item


def migrate_record(record: dict[str, Any], source_type: str, relative_path: str) -> dict[str, Any] | None:
    package_type = TYPE_MAP[source_type]
    old_target = record.get("target") or {}
    type_ref = old_target.get("typeRef") or {}
    name = str(record.get("name") or "").strip()
    if not name:
        return None
    version = str(record.get("version") or "unknown")
    details: dict[str, Any]
    if package_type == "mcp":
        identifier = type_ref.get("identifier") or type_ref.get("package") or name
        details = {"registryType": type_ref.get("registryType") or old_target.get("repository") or "git", "identifier": str(identifier)}
        if http_url(type_ref.get("registryBaseUrl")):
            details["registryBaseUrl"] = type_ref["registryBaseUrl"]
        if type_ref.get("transport") in {"stdio", "streamable-http", "sse", "other"}:
            details["transport"] = type_ref["transport"]
    elif package_type == "plugin":
        details = {"manifestPath": type_ref.get("pluginManifestPath") or ".claude-plugin/plugin.json"}
        if type_ref.get("sourceType") in {"git", "archive", "registry", "local", "other"}:
            details["sourceType"] = type_ref["sourceType"]
        if http_url(type_ref.get("marketplaceUrl")):
            details["marketplaceUrl"] = type_ref["marketplaceUrl"]
    elif package_type == "skill":
        details = {"skillPath": type_ref.get("skillMarkdownPath") or "SKILL.md"}
    else:
        details = {
            "toolType": str(type_ref.get("kind") or "agent-tool"),
            "agentUse": str(type_ref.get("agentUse") or "Agent workflow tool."),
        }

    targets = [
        {
            "agentId": agent_id,
            "agentVersionRange": None,
            "versionScheme": "unknown",
            "compatibilityStatus": "unknown",
            "compatibilityNote": "The archived source did not provide a parseable Agent version range; this record is metadata-only and unverified.",
            "status": "active",
        }
        for agent_id in agent_ids(record, package_type)
    ]
    if not targets:
        return None
    candidate = distribution(record, details)
    if candidate is None:
        return None
    links: dict[str, str] = {}
    for key, value in (("repository", record.get("repository")), ("homepage", record.get("homepage"))):
        url = http_url(value)
        if url:
            links[key] = url
    if "repository" in links and "github.com" in links["repository"]:
        links["readme"] = links["repository"].rstrip("/") + "#readme"

    keywords: list[str] = []
    for value in record.get("keywords", []):
        if isinstance(value, str) and value and value not in keywords:
            keywords.append(value[:100])
    meta = record.get("_meta") or {}
    for value in (meta.get("io.github.metaone01/other", {}) or {}).get("topics", []):
        if isinstance(value, str) and value and value not in keywords:
            keywords.append(value[:100])

    migrated: dict[str, Any] = {
        "schemaVersion": 2,
        "id": f"{package_type}.{name}",
        "name": name,
        "version": version,
        "versionScheme": VERSION_SCHEME.get(str(record.get("versionKind")), "unknown"),
        "description": str(record.get("description") or "Migrated Agent Forge metadata record."),
        "license": record.get("license") if isinstance(record.get("license"), (str, list)) else "unknown",
        "links": links,
        "keywords": keywords[:100],
        "type": package_type,
        f"{package_type}Details": details,
        "targets": targets,
        "distributions": [candidate],
        "lifecycle": {"status": "active"},
        "createdAt": COLLECTED_AT,
        "updatedAt": COLLECTED_AT,
        "publishedAt": COLLECTED_AT,
        "_meta": {
            **{key: value for key, value in meta.items() if isinstance(key, str) and "/" in key},
            "org.agentforge/migration": {"sourceSchemaVersion": 1, "collectedAt": COLLECTED_AT, "legacyPath": relative_path},
        },
    }
    return migrated


def iter_source_records(source_root: Path, source_type: str) -> Iterable[tuple[Path, dict[str, Any]]]:
    package_root = source_root / source_type / "packages"
    for path in sorted(package_root.glob("*.json")):
        yield path, load(path)


def write_manifest(output_root: Path, package_type: str, records: list[dict[str, Any]]) -> None:
    category_root = output_root / package_type
    entries: dict[str, dict[str, Any]] = {}
    for record in records:
        path = f"packages/{safe_name(record['name'])}.json"
        dump(category_root / path, record)
        entry = entries.setdefault(record["name"], {"latest": record["version"], "versions": [], "path": path})
        entry["versions"].append(record["version"])
        if str(record["version"]) > str(entry["latest"]):
            entry["latest"] = record["version"]
    for entry in entries.values():
        entry["versions"] = sorted(set(entry["versions"]))
    source = {
        "schemaVersion": 2,
        "sourceId": f"agent-forge:canonical:{package_type}",
        "name": f"Agent Forge canonical {package_type} records",
        "agentId": None,
        "type": package_type,
        "baseUrl": f"https://metaone01.github.io/agent-forge/data/canonical/{package_type}/",
        "index": "index.json",
        "revision": "packages-migrated-20261001T000000Z",
        "generatedAt": COLLECTED_AT,
        "updatedAt": COLLECTED_AT,
        "official": True,
        "description": "Canonical metadata records consumed by Agent Forge projections.",
    }
    dump(category_root / "source.json", source)
    dump(category_root / "index.json", {
        "schemaVersion": 2,
        "sourceId": source["sourceId"],
        "sourceManifest": "source.json",
        "sourceUrl": source["baseUrl"] + "source.json",
        "agentId": None,
        "type": package_type,
        "revision": source["revision"],
        "generatedAt": COLLECTED_AT,
        "updatedAt": COLLECTED_AT,
        "packages": dict(sorted(entries.items())),
    })


def migrate(input_root: Path, output_root: Path) -> dict[str, int]:
    output_root.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for old_type, package_type in TYPE_MAP.items():
        records: list[dict[str, Any]] = []
        for path, record in iter_source_records(input_root / "sources", old_type):
            migrated = migrate_record(record, old_type, str(path.relative_to(input_root)))
            if migrated is not None:
                records.append(migrated)
        write_manifest(output_root / "sources", package_type, records)
        counts[package_type] = len(records)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        shutil.rmtree(args.output)
    counts = migrate(args.input, args.output)
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
