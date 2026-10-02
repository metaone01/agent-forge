"""Package identity checks shared by collection, validation and projection."""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import quote


def encoded_component(value: str) -> str:
    # Literal percent filenames are decoded by HTTP servers. Tilde escapes
    # retain UTF-8 bytes while surviving one URL-decoding pass unchanged.
    return quote(value, safe="@._+-").replace("~", "%7E").replace("%", "~") or "_"


def identity_errors(records: Iterable[tuple[str, dict[str, Any]]]) -> list[str]:
    errors: list[str] = []
    ids: dict[str, tuple[str, str]] = {}
    names: dict[tuple[str, str], str] = {}
    versions: dict[tuple[str, str, str], str] = {}
    for path, record in records:
        package_id, category, name, version = (record.get(key) for key in ("id", "type", "name", "version"))
        if not all(isinstance(value, str) for value in (package_id, category, name, version)):
            continue
        identity = (category, name)
        if package_id in ids and ids[package_id] != identity:
            errors.append(f"{path}: global package id '{package_id}' belongs to both {ids[package_id]} and {identity}")
        ids[package_id] = identity
        if identity in names and names[identity] != package_id:
            errors.append(f"{path}: source-local name {identity} has different package ids '{names[identity]}' and '{package_id}'")
        names[identity] = package_id
        key = (category, name, version)
        if key in versions:
            errors.append(f"{path}: duplicate package/version {key}; first record is {versions[key]}")
        versions[key] = path
    return errors
