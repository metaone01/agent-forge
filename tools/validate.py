# /// script
# dependencies = ["jsonschema>=4.23", "referencing>=0.36"]
# ///

"""Validate Agent Forge schemas, examples, and independent sources."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILES = (
    "package.schema.json",
    "index.schema.json",
    "advisory.schema.json",
    "source.schema.json",
)
CATEGORY_TYPES = {
    "mcp": "mcp",
    "agent-plugin": "plugin",
    "skill": "skill",
    "generic": "other",
}


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def category_for_target(target_type: str) -> str:
    return CATEGORY_TYPES[target_type]


def serialized_meta_size(instance: dict[str, Any]) -> int:
    if "_meta" not in instance:
        return 0
    return len(
        json.dumps(instance["_meta"], ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def schema_registry() -> Registry:
    resources: list[tuple[str, Resource[Any]]] = []
    for filename in SCHEMA_FILES:
        path = ROOT / filename
        schema = load_json(path)
        resource = Resource.from_contents(schema)
        resources.append((schema["$id"], resource))
        resources.append((path.resolve().as_uri(), resource))
    return Registry().with_resources(resources)


def build_validator(schema_name: str) -> Draft202012Validator:
    schema = load_json(ROOT / schema_name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        registry=schema_registry(),
        format_checker=FormatChecker(),
    )


def format_error(path: Path, error: Any) -> str:
    location = "/".join(str(part) for part in error.absolute_path) or "<root>"
    return f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}:{location}: {error.message}"


def validate_instance(path: Path, schema_name: str) -> list[str]:
    try:
        instance = load_json(path)
    except (OSError, json.JSONDecodeError) as error:
        return [f"{path}: {error}"]
    return [
        format_error(path, error)
        for error in sorted(build_validator(schema_name).iter_errors(instance), key=str)
    ]


def validate_source_package(path: Path, source_category: str) -> tuple[list[str], list[str]]:
    errors = validate_instance(path, "package.schema.json")
    warnings: list[str] = []
    if errors:
        return errors, warnings

    instance = load_json(path)
    actual_category = category_for_target(instance["target"]["type"])
    if actual_category != source_category:
        errors.append(
            f"{path}: target.type belongs to source category '{actual_category}', "
            f"not '{source_category}'"
        )
    meta_size = serialized_meta_size(instance)
    if meta_size > 4096:
        warnings.append(
            f"{path}: serialized _meta is {meta_size} bytes; recommended maximum is 4096"
        )
    return errors, warnings


def validate_source_directory(source_dir: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    source_path = source_dir / "source.json"
    index_path = source_dir / "index.json"

    errors.extend(validate_instance(source_path, "source.schema.json"))
    errors.extend(validate_instance(index_path, "index.schema.json"))
    if errors:
        return errors, warnings

    source = load_json(source_path)
    index = load_json(index_path)
    directory_category = source_dir.name
    if source["category"] != directory_category:
        errors.append(
            f"{source_path}: source category '{source['category']}' does not match "
            f"directory '{directory_category}'"
        )
    if index["source"] != source["name"]:
        errors.append(
            f"{index_path}: index source '{index['source']}' does not match "
            f"manifest name '{source['name']}'"
        )
    if index["category"] != source["category"]:
        errors.append(
            f"{index_path}: index category '{index['category']}' does not match "
            f"manifest category '{source['category']}'"
        )

    indexed_paths: set[Path] = set()
    for package_name, entry in index["packages"].items():
        if entry["latest"] not in entry["versions"]:
            errors.append(
                f"{index_path}: package '{package_name}' latest version "
                "is not present in versions"
            )
        relative_path = entry.get("path")
        if relative_path is None:
            continue
        package_path = source_dir / relative_path
        indexed_paths.add(package_path.resolve())
        if not package_path.is_file():
            errors.append(
                f"{index_path}: indexed package path '{relative_path}' does not exist"
            )
            continue
        package_errors, package_warnings = validate_source_package(
            package_path, source["category"]
        )
        errors.extend(package_errors)
        warnings.extend(package_warnings)
        if not package_errors:
            package = load_json(package_path)
            if package["name"] != package_name:
                errors.append(
                    f"{package_path}: package name '{package['name']}' does not match "
                    f"index key '{package_name}'"
                )
            if package["version"] not in entry["versions"]:
                errors.append(
                    f"{package_path}: package version '{package['version']}' is not "
                    f"listed for '{package_name}'"
                )

    records_dir = source_dir / "packages"
    if records_dir.exists():
        for package_path in sorted(records_dir.rglob("*.json")):
            if package_path.resolve() in indexed_paths:
                continue
            package_errors, package_warnings = validate_source_package(
                package_path, source["category"]
            )
            errors.extend(package_errors)
            warnings.extend(package_warnings)
            warnings.append(f"{package_path}: package record is not listed in index.json")
    return errors, warnings


def validate_all() -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    try:
        for schema_name in SCHEMA_FILES:
            Draft202012Validator.check_schema(load_json(ROOT / schema_name))
        schema_registry()
    except Exception as error:
        errors.append(f"schema validation failed: {error}")
        return errors, warnings

    for source_dir in sorted((ROOT / "sources").iterdir()):
        if not source_dir.is_dir():
            continue
        source_errors, source_warnings = validate_source_directory(source_dir)
        errors.extend(source_errors)
        warnings.extend(source_warnings)

    examples = ROOT / "examples"
    if examples.exists():
        schema_by_name = {
            "index.json": "index.schema.json",
            "advisory.json": "advisory.schema.json",
        }
        for path in sorted(examples.glob("*.json")):
            schema_name = schema_by_name.get(path.name, "package.schema.json")
            errors.extend(validate_instance(path, schema_name))
            if schema_name == "package.schema.json":
                meta_size = serialized_meta_size(load_json(path))
                if meta_size > 4096:
                    warnings.append(
                        f"{path}: serialized _meta is {meta_size} bytes; recommended maximum is 4096"
                    )

    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", type=Path)
    parser.add_argument("schema", nargs="?", default="package.schema.json")
    parser.add_argument("--all", action="store_true", dest="validate_everything")
    args = parser.parse_args(argv)

    if args.validate_everything:
        errors, warnings = validate_all()
    elif args.file:
        path = args.file if args.file.is_absolute() else ROOT / args.file
        errors = validate_instance(path, args.schema)
        warnings = []
    else:
        parser.error("provide --all or a JSON file")

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if errors:
        return 1
    print(f"validation passed ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
