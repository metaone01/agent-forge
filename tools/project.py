#!/usr/bin/env python3
"""Generate read-only Agent Forge Pages projections from canonical JSON records.

The generator deliberately uses only the Python standard library.  JSON Schema
validation remains a separate concern (``tools/validate.py``) and can be run
before this command in CI.  Package records are read from
``sources/<type>/packages/**/*.json``; an optional ``--input`` directory may
be used for a staging tree in tests or pull requests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from tools.identity import encoded_component, identity_errors
    from tools.media import media_summary
except ModuleNotFoundError:
    from identity import encoded_component, identity_errors
    from media import media_summary

ROOT = Path(__file__).resolve().parents[1]
TYPES = ("mcp", "plugin", "skill", "general", "bundle")
DEFAULT_BASE_URL = "https://metaone01.github.io/agent-forge/data"


def load(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def dump(path: Path, value: Any) -> bytes:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    encoded = payload.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)
    return encoded


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def record_time(record: dict[str, Any]) -> datetime | None:
    """Return the latest mutation/publication timestamp available on a record."""
    candidates = [
        parse_timestamp(record.get(field))
        for field in ("updatedAt", "publishedAt", "createdAt")
    ]
    values = [value for value in candidates if value is not None]
    return max(values) if values else None


def safe_component(value: str) -> str:
    return encoded_component(value)


def clone_for_target(record: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """Copy a package and retain only the selected Agent target."""
    projected = json.loads(json.dumps(record, ensure_ascii=False))
    projected["targets"] = [
        target for target in record.get("targets", []) if target.get("agentId") == agent_id
    ]
    # A target is always the projection boundary.  This is deliberately a
    # shallow semantic operation; links, distributions and facets remain the
    # publisher's metadata claims and are not rewritten or verified.
    return projected


def iter_records(input_root: Path) -> Iterable[tuple[Path, dict[str, Any]]]:
    """Yield canonical records, de-duplicating by path and excluding examples."""
    records_root = input_root / "sources"
    if not records_root.exists():
        return
    for category in TYPES:
        category_root = records_root / category / "packages"
        if not category_root.exists():
            continue
        for path in sorted(category_root.rglob("*.json")):
            try:
                record = load(path)
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(f"cannot read canonical record {path}: {error}") from error
            if not isinstance(record, dict):
                raise ValueError(f"canonical record {path} is not an object")
            if record.get("type") != category:
                raise ValueError(
                    f"canonical record {path} type {record.get('type')!r} does not match {category!r}"
                )
            yield path, record


def source_template(input_root: Path, package_type: str) -> dict[str, Any]:
    path = input_root / "sources" / package_type / "source.json"
    if path.exists():
        value = load(path)
        if isinstance(value, dict):
            return value
    return {
        "schemaVersion": 2,
        "sourceId": f"agent-forge:dsh:{package_type}",
        "name": f"Agent Forge {package_type.title()}",
        "agentId": "dsh",
        "type": package_type,
        "index": "index.json",
        "description": f"Read-only {package_type} metadata.",
        "priority": 0,
        "official": True,
    }


def source_key(template: dict[str, Any], agent_id: str, package_type: str) -> str:
    return f"agent-forge:{agent_id}:{package_type}"


def eligible(record: dict[str, Any], cutoff: datetime, include_undated: bool) -> bool:
    timestamp = record_time(record)
    if timestamp is None:
        return include_undated
    return timestamp <= cutoff


def projected_record_path(name: str, version: str) -> str:
    # Hashes preserve distinct upstream casing on case-insensitive filesystems.
    name_suffix = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    version_suffix = hashlib.sha256(version.encode("utf-8")).hexdigest()[:12]
    return f"packages/{safe_component(name)[:110]}--{name_suffix}/{safe_component(version)[:64]}--{version_suffix}.json"


def build_projection(
    input_root: Path,
    output_root: Path,
    revision: str,
    generated_at: datetime,
    cutoff: datetime,
    base_url: str,
    include_undated: bool,
) -> dict[str, Any]:
    projections: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    source_templates = {category: source_template(input_root, category) for category in TYPES}
    preferred_versions = {}
    for category in TYPES:
        index_path = input_root / "sources" / category / "index.json"
        if index_path.exists():
            preferred_versions[category] = {
                name: entry["latest"] for name, entry in load(index_path).get("packages", {}).items()
            }
    warnings: list[str] = []
    canonical = list(iter_records(input_root))
    conflicts = identity_errors((str(path), record) for path, record in canonical)
    if conflicts:
        raise ValueError("; ".join(conflicts))
    for path, record in canonical:
        timestamp = record_time(record)
        if timestamp is None:
            warnings.append(f"undated canonical record included: {path.relative_to(input_root)}")
        if not eligible(record, cutoff, include_undated):
            continue
        targets = record.get("targets") or []
        for target in targets:
            agent_id = target.get("agentId")
            if isinstance(agent_id, str) and agent_id:
                projections[(agent_id, record["type"])].append(
                    clone_for_target(record, agent_id)
                )

    # Materialize every configured Agent/type projection, even when currently
    # empty.  Consumers can discover the full vocabulary without special cases.
    configured_agents = {
        template.get("agentId")
        for template in source_templates.values()
        if isinstance(template.get("agentId"), str) and template.get("agentId")
    }
    projection_keys = set(projections)
    projection_keys.update((agent_id, package_type) for agent_id in configured_agents for package_type in TYPES)

    all_packages: list[dict[str, Any]] = []
    agent_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for (agent_id, package_type) in sorted(projection_keys):
        records = projections.get((agent_id, package_type), [])
        records.sort(key=lambda item: (item.get("name", ""), item.get("version", "")))
        source_dir = output_root / agent_id / package_type
        source_dir.mkdir(parents=True, exist_ok=True)
        template = dict(source_templates[package_type])
        template["schemaVersion"] = 2
        template["sourceId"] = source_key(template, agent_id, package_type)
        template["agentId"] = agent_id
        template["type"] = package_type
        template["baseUrl"] = f"{base_url.rstrip('/')}/{agent_id}/{package_type}/"
        template["index"] = "index.json"
        template["revision"] = revision
        template["generatedAt"] = generated_at.isoformat().replace("+00:00", "Z")
        template["updatedAt"] = template.get("updatedAt") or template["generatedAt"]
        template["mirrorOf"] = template.get("mirrorOf")
        template["sourceMirrors"] = [
            {**mirror, "revision": revision}
            for mirror in template.get("sourceMirrors", [])
            if isinstance(mirror, dict)
        ]
        manifest_bytes = dump(source_dir / "source.json", template)

        package_entries: dict[str, dict[str, Any]] = {}
        available_versions: dict[str, set[str]] = defaultdict(set)
        for record in records:
            available_versions[record["name"]].add(record["version"])
        for record in records:
            name = record["name"]
            version = record["version"]
            rel_path = projected_record_path(name, version)
            package_bytes = dump(source_dir / rel_path, record)
            entry = package_entries.setdefault(
                name,
                {"latest": version, "versions": [], "path": rel_path},
            )
            entry["versions"].append(version)
            entry["recordRevision"] = revision
            preferred = preferred_versions.get(package_type, {}).get(name)
            # Honor the upstream latest designation only when this projection
            # includes that version; retain a deterministic fallback otherwise.
            if preferred in available_versions[name]:
                entry["latest"] = preferred
            elif str(version) > str(entry["latest"]):
                entry["latest"] = version
            if version == entry["latest"]:
                entry.pop("displayName", None)
                if record.get("displayName"):
                    entry["displayName"] = record["displayName"]
                entry.pop("media", None)
                summary_media = media_summary(record)
                if summary_media:
                    entry["media"] = summary_media
                entry.pop("updatedAt", None)
                if record.get("updatedAt") or record.get("publishedAt") or record.get("createdAt"):
                    entry["updatedAt"] = record.get("updatedAt") or record.get("publishedAt") or record.get("createdAt")
                entry.update(
                    path=rel_path,
                    id=record["id"],
                    summary=record["description"],
                    keywords=record.get("keywords", []),
                    facets=record.get("facets", {}),
                    customFacets=record.get("customFacets", {}),
                    subtype=record.get("subtype"),
                    searchText=" ".join(str(record.get(field, "")) for field in (
                        "name", "displayName", "description", "keywords", "facets", "customFacets", "generalDetails"
                    )).lower(),
                    checksum={"sha256": sha256_bytes(package_bytes)},
                )
            all_packages.append({"agentId": agent_id, "type": package_type, **record})
            agent_counts[agent_id]["versions"] += 1
            agent_counts[agent_id]["packages"] += 1
        for entry in package_entries.values():
            entry["versions"] = sorted(set(entry["versions"]))
        index = {
            "schemaVersion": 2,
            "sourceId": template["sourceId"],
            "sourceManifest": "source.json",
            "sourceUrl": f"{template['baseUrl']}source.json",
            "agentId": agent_id,
            "type": package_type,
            "revision": revision,
            "generatedAt": template["generatedAt"],
            "updatedAt": template.get("updatedAt"),
            "ttl": 28800,
            "packages": dict(sorted(package_entries.items())),
        }
        index_bytes = dump(source_dir / "index.json", index)
        # The source manifest checksum is over the generated index bytes.  The
        # manifest is rewritten once, so its own checksum never participates in
        # the digest and cannot create a self-referential value.
        template["indexChecksum"] = {"sha256": sha256_bytes(index_bytes)}
        dump(source_dir / "source.json", template)

    dashboard = make_dashboard(all_packages, revision, generated_at)
    dashboard["agents"] = []
    for agent_id in sorted({item["agentId"] for item in all_packages} | configured_agents):
        agent_records = [item for item in all_packages if item["agentId"] == agent_id]
        agent_dashboard = make_dashboard(agent_records, revision, generated_at)
        dashboard["agents"].append({
            "id": agent_id,
            "name": agent_id,
            "packageCount": agent_dashboard["counts"]["packages"],
            "versionCount": agent_dashboard["counts"]["versions"],
            "types": agent_dashboard["byType"],
            "subtypes": agent_dashboard["bySubtype"],
        })
    dump(output_root / "dashboard.json", dashboard)
    for agent_id in sorted(configured_agents | {item["agentId"] for item in all_packages}):
        agent_dashboard = make_dashboard(
            [item for item in all_packages if item["agentId"] == agent_id], revision, generated_at
        )
        dump(output_root / "agents" / agent_id / "dashboard.json", agent_dashboard)
    canonical_digest_payload = json.dumps(
        sorted(all_packages, key=lambda item: (item.get("agentId", ""), item.get("type", ""), item.get("id", ""), item.get("version", ""))),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    catalog = {
        "schemaVersion": 2,
        "revision": revision,
        "generatedAt": generated_at.isoformat().replace("+00:00", "Z"),
        "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
        "packageCount": len(all_packages),
        "contentDigest": {"sha256": sha256_bytes(canonical_digest_payload)},
        "sources": [
            {"agentId": agent, "type": package_type, "path": f"{agent}/{package_type}/index.json"}
            for agent, package_type in sorted(projection_keys)
        ],
        "warnings": warnings,
    }
    dump(output_root / "catalog.json", catalog)
    # ``manifest.json`` is the stable entry point used by Pages clients.  Keep
    # it separate from source manifests so a client can discover every
    # Agent/type projection with a single request.
    dump(output_root / "manifest.json", {
        "schemaVersion": 2,
        "revision": revision,
        "generatedAt": catalog["generatedAt"],
        "cutoff": catalog["cutoff"],
        "catalog": "catalog.json",
        "dashboard": "dashboard.json",
        "sources": catalog["sources"],
        "packageCount": len(all_packages),
        "contentDigest": catalog["contentDigest"],
    })
    return {"packageCount": len(all_packages), "sources": len(projection_keys), "warnings": warnings}


def make_dashboard(records: list[dict[str, Any]], revision: str, generated_at: datetime) -> dict[str, Any]:
    type_counts = Counter(record.get("type") for record in records)
    subtype_counts = Counter(record.get("subtype") or "unspecified" for record in records)
    lifecycle_counts = Counter((record.get("lifecycle") or {}).get("status", "unspecified") for record in records)
    facet_counts: Counter[str] = Counter()
    custom_facet_counts: Counter[str] = Counter()
    known = unknown = 0
    bundle_members = 0
    for record in records:
        for group, counter in ((record.get("facets") or {}, facet_counts), (record.get("customFacets") or {}, custom_facet_counts)):
            for values in group.values():
                if isinstance(values, list):
                    counter.update(values)
        for target in record.get("targets", []):
            if target.get("compatibilityStatus") == "unknown":
                unknown += 1
            else:
                known += 1
        if record.get("type") == "bundle":
            bundle_members += len((record.get("bundleDetails") or {}).get("members", []))
    recent = sorted(
        records,
        key=lambda record: (record.get("updatedAt") or record.get("publishedAt") or "", record.get("name", "")),
        reverse=True,
    )[:12]
    return {
        "schemaVersion": 2,
        "revision": revision,
        "generatedAt": generated_at.isoformat().replace("+00:00", "Z"),
        "disclaimer": "Integrated metadata claims are not source verification or a safety guarantee.",
        "counts": {
            "records": len(records),
            "versions": len(records),
            "packages": len({record.get("id") for record in records}),
            "bundles": sum(1 for record in records if record.get("type") == "bundle"),
            "bundleMembers": bundle_members,
            "knownCompatibilityTargets": known,
            "unknownCompatibilityTargets": unknown,
        },
        "byType": dict(sorted(type_counts.items())),
        "bySubtype": dict(sorted(subtype_counts.items())),
        "byLifecycle": dict(sorted(lifecycle_counts.items())),
        "facets": dict(facet_counts.most_common()),
        "customFacets": dict(custom_facet_counts.most_common()),
        "recentPackages": [
            {
                "id": record.get("id"),
                "name": record.get("name"),
                "displayName": record.get("displayName"),
                "version": record.get("version"),
                "type": record.get("type"),
                "description": record.get("description"),
                "updatedAt": record.get("updatedAt") or record.get("publishedAt") or record.get("createdAt"),
            }
            for record in recent
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT, help="canonical repository root")
    parser.add_argument("--output", type=Path, default=ROOT / "data", help="projection output directory")
    parser.add_argument("--revision", help="shared release revision; default is UTC timestamp")
    parser.add_argument("--now", help="generation time as ISO-8601 UTC, useful for tests")
    parser.add_argument("--cutoff-minutes", type=int, default=10)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--include-undated", action="store_true", help="include records without timestamps")
    parser.add_argument("--clean", action="store_true", help="remove the output directory before generation")
    args = parser.parse_args(argv)
    generated_at = parse_timestamp(args.now) if args.now else utc_now()
    if generated_at is None:
        parser.error("--now must be an ISO-8601 timestamp")
    cutoff = generated_at - timedelta(minutes=args.cutoff_minutes)
    revision = args.revision or generated_at.strftime("%Y%m%dT%H%M%SZ")
    input_root = args.input.resolve()
    output_root = args.output.resolve()
    if args.clean and output_root.exists():
        shutil.rmtree(output_root)
    try:
        result = build_projection(
            input_root, output_root, revision, generated_at, cutoff, args.base_url, args.include_undated
        )
    except (OSError, ValueError, KeyError) as error:
        print(f"projection failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"revision": revision, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
