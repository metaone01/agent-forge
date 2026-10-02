"""Name requested distribution platforms and inventory other installation sources."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]

SOURCE_LABELS = {
    "github-repo": "github repository", "npm-pkg": "npm package",
    "pypi-pkg": "pypi package", "nuget-pkg": "nuget package", "crates-pkg": "crates.io rust crate",
    "ghcr-image": "github container image", "dockerhub-image": "docker hub image",
    "quay-image": "quay container image", "gcp-artifact-image": "google artifact registry image",
    "oci-image": "oci container image", "github-mcpb": "github mcpb package",
    "gitlab-mcpb": "gitlab mcpb package", "mcpb-pkg": "mcpb package",
    "mcp-endpoint": "remote mcp endpoint",
}


def distribution_type(distribution: dict, package_type: str | None = None) -> str | None:
    host = (urlparse(distribution.get("url", "")).hostname or "").lower()
    kind = distribution.get("type")
    registry = distribution.get("registry", "").lower()
    if kind in {"git", "github-repo"} and host in {"github.com", "www.github.com"}:
        return "github-repo"
    if kind == "registry" or kind in {"npm-pkg", "pypi-pkg", "nuget-pkg", "crates-pkg"}:
        for platform, source, domains in (
            ("npm", "npm-pkg", {"npmjs.com", "www.npmjs.com", "registry.npmjs.org"}),
            ("pypi", "pypi-pkg", {"pypi.org", "www.pypi.org"}),
            ("nuget", "nuget-pkg", {"nuget.org", "www.nuget.org", "api.nuget.org"}),
            ("cargo", "crates-pkg", {"crates.io"}),
        ):
            if registry == platform or (not registry and host in domains):
                return source
    if registry == "oci" or kind == "oci" or kind in {"ghcr-image", "dockerhub-image", "quay-image", "gcp-artifact-image", "oci-image"}:
        if host == "ghcr.io":
            return "ghcr-image"
        if host in {"docker.io", "hub.docker.com", "registry-1.docker.io"}:
            return "dockerhub-image"
        if host == "quay.io":
            return "quay-image"
        if host.endswith(".pkg.dev"):
            return "gcp-artifact-image"
        return "oci-image"
    if registry == "mcpb" or kind in {"github-mcpb", "gitlab-mcpb", "mcpb-pkg"}:
        if host in {"github.com", "release-assets.githubusercontent.com"}:
            return "github-mcpb"
        if host == "gitlab.com":
            return "gitlab-mcpb"
        return "mcpb-pkg"
    if kind == "mcp-endpoint" or (package_type == "mcp" and kind == "other" and distribution.get("install", {}).get("type") == "api"):
        return "mcp-endpoint"
    return None


def distribution_name(distribution: dict, package_type: str | None = None) -> str | None:
    return SOURCE_LABELS.get(distribution_type(distribution, package_type))


def normalize_distribution(distribution: dict, package_type: str | None = None) -> bool:
    name = distribution_name(distribution, package_type)
    if name is None:
        return False
    kind = distribution_type(distribution, package_type)
    changed = distribution.get("name") != name or distribution["type"] != kind
    distribution.update(name=name, type=kind)
    return changed


def normalize_plugin_source(record: dict) -> bool:
    details = record.get("pluginDetails", {})
    host = (urlparse(record.get("links", {}).get("repository", "")).hostname or "").lower()
    if details.get("sourceType") == "git" and host in {"github.com", "www.github.com"}:
        details["sourceType"] = "github-repo"
        return True
    return False


def rename(root: Path, apply: bool) -> dict:
    observations = Counter()
    packages = defaultdict(set)
    hosts = defaultdict(Counter)
    kinds = defaultdict(Counter)
    install_types = defaultdict(Counter)
    examples = defaultdict(list)
    changed_records = changed_distributions = 0
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for path in sorted((root / "sources").glob("*/packages/**/*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        changed = normalize_plugin_source(record)
        for distribution in record.get("distributions", []):
            name = distribution_name(distribution, record["type"])
            if normalize_distribution(distribution, record["type"]):
                changed_distributions += 1
                changed = True
            key = name or distribution.get("registry") or distribution["type"]
            observations[key] += 1
            packages[key].add(record["id"])
            host = urlparse(distribution["url"]).hostname or "unknown"
            hosts[key][host] += 1
            kinds[key][distribution["type"]] += 1
            install_types[key][distribution.get("install", {}).get("type", "unspecified")] += 1
            if len(examples[key]) < 3 and distribution["url"] not in examples[key]:
                examples[key].append(distribution["url"])
        if changed:
            changed_records += 1
            if apply:
                record["updatedAt"] = stamp
                path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return {
        "observedAt": stamp,
        "applied": apply,
        "changedRecords": changed_records,
        "changedDistributions": changed_distributions,
        "sources": [
            {"name": key, "distributionObservations": observations[key], "packages": len(packages[key]),
             "hostCount": len(hosts[key]), "hosts": dict(hosts[key].most_common()),
             "types": dict(kinds[key]), "installTypes": dict(install_types[key]), "examples": examples[key]}
            for key in sorted(observations)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = rename(ROOT, args.apply)
    destination = ROOT / "docs/research/2026-10-02-import/distribution-sources.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({**report, "sources": [{key: value for key, value in source.items() if key != "hosts"} for source in report["sources"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
