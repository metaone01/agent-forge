"""Apply a reviewed media plan locally, with preflight hashes and byte backups.

Default is read-only preflight. --apply requires a new --backup-dir. This tool
never publishes, changes version selection, or upgrades remote consumers.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from tools.backfill_media import ROOT, TYPES, load, within
    from tools.media import PROVENANCE_KEY, media_summary
    from tools.validate import build_validator
except ModuleNotFoundError:
    from backfill_media import ROOT, TYPES, load, within
    from media import PROVENANCE_KEY, media_summary
    from validate import build_validator


def encode(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate(value: Any, schema: str) -> None:
    errors = list(build_validator(schema, submission_formats=True).iter_errors(value))
    if errors:
        raise ValueError(f"{schema}: {errors[0].message}")


def without_media(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result.pop("media", None)
    meta = result.get("_meta")
    if isinstance(meta, dict):
        meta.pop(PROVENANCE_KEY, None)
        if not meta:
            result.pop("_meta", None)
    return result


def refreshed_checksums(previous: dict[str, str], content: bytes) -> dict[str, str]:
    return {algorithm: hashlib.new(algorithm, content).hexdigest() for algorithm in previous}


def prepare_apply(root: Path, review: Path, revision: str, stamp: str
                  ) -> tuple[dict[str, tuple[bytes, bytes]], dict[str, Any]]:
    root, review = root.resolve(), review.resolve()
    canonical = (root / "sources").resolve()
    if review.is_relative_to(canonical) or canonical.is_relative_to(review):
        raise ValueError("Review must be separate from canonical sources")
    plan = load(review / "plan.json")
    if (plan.get("schemaVersion") != 2 or plan.get("mode") != "dry-run"
            or plan.get("type") not in TYPES or Path(plan.get("root", "")).resolve() != root):
        raise ValueError("Plan is not a media dry-run for this canonical root")
    changes = plan.get("changes", [])
    if not changes or plan.get("counts", {}).get("recordsChanged") != len(changes):
        raise ValueError("Plan has no changes or its count does not match")
    if plan.get("issues") or plan.get("unmatchedSources") or any(
            plan.get("counts", {}).get(key, 0) for key in ("issues", "schemaRejected", "unmatchedCachedSources")):
        raise ValueError("Resolve plan issues and unmatched sources before applying")
    package_type = plan["type"]
    prefix = f"sources/{package_type}/"
    source_rel = prefix + "source.json"
    source_bytes = within(root, source_rel).read_bytes()
    source = json.loads(source_bytes.decode("utf-8-sig"))
    validate(source, "source.schema.json")
    index_rel = prefix + source["index"]
    index_bytes = within(root, index_rel).read_bytes()
    index = json.loads(index_bytes.decode("utf-8-sig"))
    validate(index, "index.schema.json")
    if (source["type"] != package_type or index["type"] != package_type
            or index["sourceId"] != source["sourceId"] or index["agentId"] != source["agentId"]
            or index["revision"] != source["revision"]
            or within(root, prefix + index["sourceManifest"]) != within(root, source_rel)):
        raise ValueError("Source/index identity or revision mismatch")
    if revision == source["revision"]:
        raise ValueError("A fresh revision is required")
    # An external mirror cannot be upgraded by changing its declaration locally.
    if source.get("sourceMirrors") or any(row.get("relation") == "mirror" for row in source.get("relatedSources", [])):
        raise ValueError("Coordinate mirror revisions before applying this plan")
    stamp_time = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if stamp_time.tzinfo is None:
        raise ValueError("Application time must include a timezone")
    writes: dict[str, tuple[bytes, bytes]] = {}
    records: dict[str, dict[str, Any]] = {}
    for change in changes:
        relative = change["recordPath"]
        target, proposal = within(root, relative), within(review, relative)
        if not relative.startswith(prefix + "packages/") or target.suffix != ".json" or relative in writes:
            raise ValueError(f"Invalid or duplicate record path: {relative}")
        original_bytes = target.read_bytes()
        if not re.fullmatch(r"[a-fA-F0-9]{64}", change["originalSha256"]) or sha256(original_bytes) != change["originalSha256"].lower():
            raise ValueError(f"Stale original hash: {relative}")
        original = json.loads(original_bytes.decode("utf-8-sig"))
        record = load(proposal)
        validate(record, "package.schema.json")
        if without_media(original) != without_media(record):
            raise ValueError(f"Proposal changed non-media fields: {relative}")
        if (record["type"] != package_type or record.get("media") != change["media"]
                or any(record[field] != change[field] for field in ("id", "name", "version"))):
            raise ValueError(f"Proposal does not match plan identity/media: {relative}")
        old_time = original.get("updatedAt") or original.get("createdAt")
        if old_time and stamp_time <= datetime.fromisoformat(old_time.replace("Z", "+00:00")):
            raise ValueError(f"Application time must advance the record timestamp: {relative}")
        old_media = original.get("media", {})
        new_media = record.get("media", {})
        if ("icon" in old_media and old_media["icon"] != new_media.get("icon")) or new_media.get("previews", [])[:len(old_media.get("previews", []))] != old_media.get("previews", []):
            raise ValueError(f"Proposal removed or replaced existing media: {relative}")
        old_provenance = original.get("_meta", {}).get(PROVENANCE_KEY)
        new_provenance = record.get("_meta", {}).get(PROVENANCE_KEY)
        if old_provenance is not None:
            if not isinstance(old_provenance, dict) or not isinstance(new_provenance, dict):
                if old_provenance != new_provenance:
                    raise ValueError(f"Proposal replaced opaque provenance: {relative}")
            else:
                for key, value in old_provenance.items():
                    if key == "sources" and isinstance(value, list):
                        if not isinstance(new_provenance.get(key), list) or new_provenance[key][:len(value)] != value:
                            raise ValueError(f"Proposal replaced existing provenance: {relative}")
                    elif new_provenance.get(key) != value:
                        raise ValueError(f"Proposal replaced existing provenance: {relative}")
        record["updatedAt"] = stamp
        validate(record, "package.schema.json")
        writes[relative] = (original_bytes, encode(record))
        records[relative] = record
    selected = 0
    for name, entry in index["packages"].items():
        relative = prefix + entry["path"]
        selected_path = within(root, relative)
        if not selected_path.is_relative_to(canonical / package_type / "packages"):
            raise ValueError(f"Indexed record is outside canonical packages: {name}")
        if "recordRevision" in entry:
            entry["recordRevision"] = revision
        record = records.get(relative)
        if record is None:
            continue
        if name != record["name"] or entry["latest"] != record["version"] or record["version"] not in entry["versions"]:
            raise ValueError(f"Latest identity mismatch: {name}")
        if "id" in entry and entry["id"] != record["id"]:
            raise ValueError(f"Index ID mismatch: {name}")
        entry["media"] = media_summary(record)
        entry["updatedAt"] = stamp
        if "checksum" in entry:
            entry["checksum"] = refreshed_checksums(entry["checksum"], writes[relative][1])
        selected += 1
    for value in (source, index):
        for field in ("updatedAt", "generatedAt"):
            if value.get(field) and stamp_time <= datetime.fromisoformat(value[field].replace("Z", "+00:00")):
                raise ValueError("Application time must advance source/index timestamps")
        value.update(revision=revision, generatedAt=stamp, updatedAt=stamp)
    # A self-checksum hashes the normalized JSON bytes with that field omitted.
    own_checksum = index.pop("indexChecksum", None)
    if own_checksum is not None:
        index["indexChecksum"] = refreshed_checksums(own_checksum, encode(index))
    new_index_bytes = encode(index)
    if "indexChecksum" in source:
        source["indexChecksum"] = refreshed_checksums(source["indexChecksum"], new_index_bytes)
    validate(index, "index.schema.json")
    validate(source, "source.schema.json")
    writes[index_rel] = (index_bytes, new_index_bytes)
    writes[source_rel] = (source_bytes, encode(source))
    summary = {"schemaVersion": 2, "mode": "preflight", "type": package_type,
               "root": str(root), "review": str(review), "revision": revision, "appliedAt": stamp,
               "recordsChanged": len(records), "selectedRecordsChanged": selected,
               "filesChanged": len(writes), "planSha256": sha256((review / "plan.json").read_bytes())}
    return writes, summary


def replace_bytes(target: Path, content: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".media-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_writes(root: Path, review: Path, backup: Path,
                 writes: dict[str, tuple[bytes, bytes]], summary: dict[str, Any]) -> dict[str, Any]:
    root, review, backup = root.resolve(), review.resolve(), backup.resolve()
    canonical = (root / "sources").resolve()
    if backup.exists() or any(backup.is_relative_to(area) or area.is_relative_to(backup) for area in (canonical, review)):
        raise ValueError("Backup directory must be new and separate from sources and review")
    if any(within(root, relative).read_bytes() != original for relative, (original, _) in writes.items()):
        raise ValueError("Canonical files changed after preflight")
    backup.mkdir(parents=True)
    for relative, (original, _) in writes.items():
        target = within(backup, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original)
    result = {**summary, "mode": "applying", "backupDir": str(backup), "files": [
        {"path": relative, "originalSha256": sha256(original), "appliedSha256": sha256(updated)}
        for relative, (original, updated) in writes.items()]}
    report = backup / "application.json"
    report.write_bytes(encode(result))
    committed = []
    try:
        for relative, (original, updated) in writes.items():
            target = within(root, relative)
            if target.read_bytes() != original:
                raise ValueError(f"Canonical file changed while applying: {relative}")
            replace_bytes(target, updated)
            committed.append(relative)
        result["mode"] = "applied"
        replace_bytes(report, encode(result))
    except (OSError, ValueError):
        rollback_errors = []
        for relative in reversed(committed):
            original, updated = writes[relative]
            target = within(root, relative)
            try:
                if target.read_bytes() != updated:
                    raise ValueError("Refusing to overwrite a concurrent edit")
                replace_bytes(target, original)
            except (OSError, ValueError) as error:
                rollback_errors.append({"path": relative, "error": str(error)})
        result.update(mode="rollback-incomplete" if rollback_errors else "rolled-back", rollbackErrors=rollback_errors)
        report.write_bytes(encode(result))
        raise
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--review", type=Path, required=True, help="Reviewed directory containing plan.json and proposed records")
    parser.add_argument("--revision", required=True, help="Fresh shared source/index revision")
    parser.add_argument("--apply", action="store_true", help="Write only after complete preflight; no remote publication")
    parser.add_argument("--backup-dir", type=Path, help="Required with --apply; must not exist")
    args = parser.parse_args(argv)
    if args.apply and not args.backup_dir:
        parser.error("--apply requires a new --backup-dir")
    try:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        writes, summary = prepare_apply(args.root, args.review, args.revision, stamp)
        if args.apply:
            summary = apply_writes(args.root, args.review, args.backup_dir, writes, summary)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f"apply_media: {error}\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
