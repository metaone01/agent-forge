"""Plan v2 image backfills from saved observations. Never writes canonical sources.

Default: report only. --patch-dir emits complete proposed records and a hash-guarded
plan for review; these are not an apply-ready canonical source tree or publication.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from tools.media import MEDIA_FIELDS, PROVENANCE_KEY, normalize_media, merge_media
    from tools.validate import build_validator
except ModuleNotFoundError:
    from media import MEDIA_FIELDS, PROVENANCE_KEY, normalize_media, merge_media
    from validate import build_validator

ROOT = Path(__file__).resolve().parents[1]
TYPES = ("mcp", "plugin", "skill", "general", "bundle")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def within(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or ".." in relative.split("/"):
        raise ValueError(f"Unsafe relative path: {relative!r}")
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise ValueError(f"Path escapes root: {relative!r}")
    return target


class CachedObservations:
    def __init__(self, cache: Path):
        self.cache = cache
        self.documents: dict[str, Any] = {}
        manifest = cache / "catalog-fetch.json"
        self.revisions = {row["repository"]: row for row in load(manifest)} if manifest.exists() else {}

    def get(self, source: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        repo, path, revision = (source.get(key) for key in ("repository", "path", "revision"))
        entry = str(source.get("entry", ""))
        if not all(isinstance(value, str) and value for value in (repo, path, revision)) or not entry.isdecimal() or not path.endswith(".json"):
            return None, None
        fetched = self.revisions.get(repo)
        if not fetched or revision != fetched.get("revision") or path not in fetched.get("fetched", []):
            return None, "cached metadata revision or path cannot be matched to the recorded source"
        relative = f"catalogs/{repo}/{path}"
        file = within(self.cache, relative)
        if not file.is_file():
            return None, "recorded metadata file is missing from the local cache"
        if relative not in self.documents:
            self.documents[relative] = load(file)
        document = self.documents[relative]
        arrays = [value for key, value in document.items() if key in ("plugins", "themes", "candidates", "repositories", "records") and isinstance(value, list)] if isinstance(document, dict) else [document] if isinstance(document, list) else []
        if len(arrays) != 1 or int(entry) >= len(arrays[0]):
            return None, "cached row locator is ambiguous or out of range"
        row = arrays[0][int(entry)]
        return (row, None) if isinstance(row, dict) else (None, "cached row is not an object")


def plan_backfill(root: Path, cache: Path, observations: Path | None = None,
                  package_type: str = "plugin", limit: int | None = None) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    root = root.resolve()
    explicit: dict[str, list[dict[str, Any]]] = {}
    if observations:
        for line in observations.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            relative = item["recordPath"]
            target = within(root, relative)
            source_root = (root / "sources" / package_type / "packages").resolve()
            if not target.is_relative_to(source_root) or target.suffix != ".json":
                raise ValueError("Observation must identify a package JSON within the selected canonical type")
            if not isinstance(item.get("metadata"), dict) or not isinstance(item.get("source"), dict) or not item["source"]:
                raise ValueError("Observation needs metadata and a nonempty provenance source")
            explicit.setdefault(relative, []).append(item)
    cached = CachedObservations(cache)
    counts: Counter[str] = Counter({key: 0 for key in ("recordsScanned", "cachedRowsMatched", "unmatchedCachedSources", "mediaObservations", "schemaRejected", "iconsAdded", "previewsAdded", "metadataOnlyChanges", "skinRecordsChanged")})
    issues, changes, proposed = [], [], {}
    unmatched: dict[str, dict[str, Any]] = {}
    validator = build_validator("package.schema.json", submission_formats=True)
    paths = sorted((root / "sources" / package_type / "packages").rglob("*.json"))
    if limit is not None:
        paths = paths[:limit]
    seen = set()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        within(root, relative)
        seen.add(relative)
        original_bytes = path.read_bytes()
        original = json.loads(original_bytes.decode("utf-8-sig"))
        record = copy.deepcopy(original)
        counts["recordsScanned"] += 1
        rows = list(explicit.get(relative, []))
        legacy = original.get("_meta", {}).get("org.agentforge/media")
        if isinstance(legacy, dict) and any(key in legacy for key in MEDIA_FIELDS):
            rows.insert(0, {"metadata": {**legacy, "name": record["name"]}, "source": {"recordPath": relative, "extension": "org.agentforge/media"}})
        for source in original.get("_meta", {}).get("org.agentforge/collection", {}).get("sources", []):
            row, error = cached.get(source)
            if error:
                counts["unmatchedCachedSources"] += 1
                locator = {key: source.get(key) for key in ("repository", "revision", "path")}
                key = json.dumps(locator, sort_keys=True)
                group = unmatched.setdefault(key, {"source": locator, "reason": error, "observations": 0, "exampleRecordPaths": []})
                group["observations"] += 1
                if len(group["exampleRecordPaths"]) < 3:
                    group["exampleRecordPaths"].append(relative)
                continue
            if row is not None:
                counts["cachedRowsMatched"] += 1
                rows.append({"metadata": row, "source": source})
        for item in rows:
            metadata = item["metadata"]
            if not any(key in metadata for key in MEDIA_FIELDS):
                continue
            counts["mediaObservations"] += 1
            # Numeric locators alone are insufficient after an upstream row reorder.
            repo = metadata.get("repo") or metadata.get("repository")
            expected = record.get("links", {}).get("repository", "").removeprefix("https://github.com/").rstrip("/").removesuffix(".git")
            if isinstance(repo, str) and expected and repo.removeprefix("https://github.com/").rstrip("/").removesuffix(".git").lower() != expected.lower():
                issues.append({"recordPath": relative, "source": item["source"], "reason": "metadata repository does not match the canonical package"})
                continue
            media, problems = normalize_media(metadata, item.get("assetContext"))
            problems.extend(merge_media(record, media, item["source"]))
            issues.extend({"recordPath": relative, "source": item["source"], **problem} for problem in problems)
        if record == original:
            continue
        errors = list(validator.iter_errors(record))
        if errors:
            issues.append({"recordPath": relative, "reason": "proposed record failed schema validation", "errors": [error.message for error in errors]})
            counts["schemaRejected"] += 1
            continue
        old, new = original.get("media", {}), record.get("media", {})
        counts["iconsAdded"] += int("icon" in new and "icon" not in old)
        counts["previewsAdded"] += len(new.get("previews", [])) - len(old.get("previews", []))
        counts["metadataOnlyChanges"] += int(old == new)
        counts["skinRecordsChanged"] += int(record.get("subtype") == "skin")
        proposed[relative] = record
        changes.append({"recordPath": relative, "id": record["id"], "name": record["name"], "version": record["version"], "subtype": record.get("subtype"),
                        "originalSha256": hashlib.sha256(original_bytes).hexdigest(), "media": new})
    missing = set(explicit) - seen
    if missing:
        raise ValueError("Explicit observations not scanned (missing records or --limit): " + ", ".join(sorted(missing)))
    counts["recordsChanged"] = len(changes)
    counts["issues"] = len(issues)
    report = {"schemaVersion": 2, "mode": "dry-run", "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
              "root": str(root), "type": package_type, "counts": dict(counts), "changes": changes, "issues": issues, "unmatchedSources": list(unmatched.values()),
              "applyGate": "Upgrade strict validators/consumers first. Review hashes and conflicts; applying records also requires canonical index/revision regeneration before projection and publication."}
    return report, proposed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--cache", type=Path, help="Local saved metadata cache; defaults to ROOT/.collection-cache")
    parser.add_argument("--observations", type=Path, help="Optional JSONL with recordPath, metadata, source and explicit assetContext")
    parser.add_argument("--type", choices=TYPES, default="plugin")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--report", type=Path, help="Default ROOT/.collection-cache/media-backfill-report.json")
    parser.add_argument("--patch-dir", type=Path, help="New, separate review directory; never canonical sources")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    root = args.root.resolve()
    report_path = (args.report or root / ".collection-cache/media-backfill-report.json").resolve()
    patch_dir = args.patch_dir.resolve() if args.patch_dir else None
    canonical = (root / "sources").resolve()
    if report_path.is_relative_to(canonical) or report_path.suffix != ".json" or report_path.name.endswith(".schema.json"):
        parser.error("report must be a separate JSON report, not canonical metadata or schemas")
    if args.observations and report_path == args.observations.resolve():
        parser.error("report must not replace the observation input")
    if report_path.exists():
        try:
            previous = load(report_path)
        except (OSError, ValueError):
            parser.error("refusing to replace a file that is not a media dry-run report")
        if not isinstance(previous, dict) or previous.get("mode") != "dry-run" or previous.get("schemaVersion") != 2 or previous.get("type") not in TYPES or not isinstance(previous.get("changes"), list):
            parser.error("refusing to replace a file that is not a media dry-run report")
    if patch_dir and (patch_dir.is_relative_to(canonical) or canonical.is_relative_to(patch_dir) or patch_dir.exists()):
        parser.error("patch directory must be new and separate from canonical sources")
    try:
        report, proposed = plan_backfill(root, args.cache or root / ".collection-cache", args.observations, args.type, args.limit)
        if patch_dir:
            patch_dir.mkdir(parents=True)
            for relative, value in proposed.items():
                target = within(patch_dir, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (patch_dir / "plan.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(2, f"backfill_media: {error}\n")
    print(json.dumps({"mode": "dry-run", "report": str(report_path), "patchDir": str(patch_dir) if patch_dir else None, **report["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
