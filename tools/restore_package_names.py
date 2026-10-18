"""Restore names from upstream evidence without changing established package ids."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    from tools.identity import identity_errors
    from tools.validate import build_validator
except ModuleNotFoundError:
    from identity import identity_errors
    from validate import build_validator

ROOT = Path(__file__).resolve().parents[1]


def legacy_name(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._@/+\-]", "-", value).strip("-/")
    return clean if clean and re.match(r"^[A-Za-z0-9@]", clean) else "package-" + clean


def original_name(record: dict) -> str | None:
    if record["type"] == "mcp":
        for ref in record.get("_meta", {}).get("org.agentforge/collection", {}).get("sources", []):
            if ref.get("repository") == "mcp-registry":
                return ref.get("entry")
        return None
    repository = urlparse(record.get("links", {}).get("repository", ""))
    if repository.hostname != "github.com":
        return None
    repo = repository.path.strip("/").removesuffix(".git")
    if record["type"] == "skill":
        path = record["skillDetails"]["skillPath"]
        return repo if path == "SKILL.md" else repo + "/" + path.removesuffix("/SKILL.md")
    if record["type"] == "plugin":
        for distribution in record.get("distributions", []):
            url = urlparse(distribution["url"])
            match = re.match(r"^/[^/]+/[^/]+/tree/[^/]+/(.+)$", url.path)
            if url.hostname == "github.com" and match:
                return repo + "/" + unquote(match.group(1))
        return repo
    return None


def restore(root: Path, apply: bool) -> dict:
    records = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in sorted((root / "sources").glob("*/packages/**/*.json"))]
    plans = {}
    for path, record in records:
        original = original_name(record)
        if original and original != record["name"] and any(ord(char) > 127 for char in original):
            if legacy_name(original) != record["name"]:
                raise ValueError(f"Ambiguous restoration: {path}")
            previous = plans.setdefault(record["id"], {"id": record["id"], "type": record["type"], "from": record["name"], "to": original})
            if previous["to"] != original:
                raise ValueError(f"Conflicting evidence for {record['id']}")
    by_id = {record["id"]: record for path, record in records}
    # A previous write may have restored a record before updating its index.
    for path in sorted((root / "sources").glob("*/index.json")):
        index = json.loads(path.read_text(encoding="utf-8"))
        for name, entry in index["packages"].items():
            record = by_id.get(entry.get("id"))
            if not record:
                continue
            original = original_name(record)
            if original and original != name and any(ord(char) > 127 for char in original) and legacy_name(original) == name:
                plans.setdefault(record["id"], {"id": record["id"], "type": record["type"], "from": name, "to": original})
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    validator = build_validator("package.schema.json")
    modified = []
    for path, record in records:
        if record["id"] in plans:
            record.update(name=plans[record["id"]]["to"], updatedAt=stamp)
            errors = list(validator.iter_errors(record))
            if errors:
                raise ValueError(f"Invalid restored record {path}: {errors[0].message}")
            modified.append((path, record))
    conflicts = identity_errors((str(path), record) for path, record in records)
    if conflicts:
        raise ValueError("\n".join(conflicts[:10]))
    indexes = []
    for path in sorted((root / "sources").glob("*/index.json")):
        index = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        for plan in plans.values():
            if plan["type"] == path.parent.name and plan["from"] in index["packages"]:
                if plan["to"] in index["packages"]:
                    raise ValueError(f"Index name conflict: {plan['to']}")
                entry = index["packages"].pop(plan["from"])
                entry["searchText"] = entry.get("searchText", "") + " " + plan["to"]
                index["packages"][plan["to"]] = entry
                changed = True
        if changed:
            index["packages"] = dict(sorted(index["packages"].items()))
            indexes.append((path, index))
    if apply:
        for path, value in modified + indexes:
            temporary = path.with_name(path.name + ".restore.tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
            temporary.replace(path)
    return {"observedAt": stamp, "applied": apply, "changedRecords": len(modified), "restoredPackages": list(plans.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = restore(ROOT, args.apply)
    destination = ROOT / "docs/research/2026-10-02-import/restored-package-names.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
