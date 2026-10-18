"""Finite, strict declaration parsing; upstream instructions are never executed."""
from __future__ import annotations

import math
import json
import re
from pathlib import PurePosixPath
from urllib.parse import quote

import yaml
from tools.identity import identity_errors
from tools.import_community import unknown_target
from tools.validate import build_validator

from .errors import ServiceError

MAX_DECLARATION_BYTES = 128 * 1024
FORMATS = {"claude_plugin", "agent_skill"}


def declaration_path(path, kind):
    if kind not in FORMATS or not isinstance(path, str) or len(path) > 500:
        raise ValueError("unsupported declaration path or format")
    parts = path.split("/")
    if not 1 <= len(parts) <= 8 or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", p) or p in (".", "..") for p in parts):
        raise ValueError("unsafe declaration path")
    if kind == "claude_plugin" and (len(parts) < 2 or parts[-2:] != [".claude-plugin", "plugin.json"]):
        raise ValueError("Claude plugin metadata requires .claude-plugin/plugin.json")
    if kind == "agent_skill" and parts[-1] != "SKILL.md":
        raise ValueError("Agent Skill metadata requires SKILL.md")
    return path



def declaration_association(kind, agent):
    if kind not in FORMATS or not isinstance(agent, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", agent):
        raise ValueError("Agent association must be explicit and use a supported declaration format")
    if kind == "claude_plugin" and agent != "claude-code":
        raise ValueError("Claude plugin profile only declares a Claude Code association")
    return agent


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite JSON")


def _bounded(value, depth=0, counter=None):
    counter = counter if counter is not None else [0]
    counter[0] += 1
    if depth > 24 or counter[0] > 20000:
        raise ValueError("declaration nesting or node limit")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("non-string mapping key")
        for item in value.values():
            _bounded(item, depth + 1, counter)
    elif isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1, counter)
    elif type(value) is float and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    elif value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("non-JSON declaration value")
    return value


def strict_json(raw, *, maximum=MAX_DECLARATION_BYTES):
    if not isinstance(raw, bytes) or len(raw) > maximum:
        raise ValueError("declaration byte limit")
    return _bounded(json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant))


def _frontmatter(raw):
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing Skill frontmatter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise ValueError("unterminated Skill frontmatter")
    header = "".join(lines[1:end])
    depth = count = 0
    for event in yaml.parse(header, Loader=yaml.SafeLoader):
        count += 1
        if count > 20000 or isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise ValueError("YAML aliases, anchors or excessive nodes")
        if getattr(event, "tag", None) is not None:
            raise ValueError("explicit YAML tags are not supported")
        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            if depth > 24:
                raise ValueError("YAML nesting limit")
        elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth -= 1
    node = yaml.compose(header, Loader=yaml.SafeLoader)
    def convert(node):
        if isinstance(node, yaml.MappingNode):
            return _pairs([(convert(k), convert(v)) for k, v in node.value])
        if isinstance(node, yaml.SequenceNode):
            return [convert(v) for v in node.value]
        if not isinstance(node, yaml.ScalarNode) or node.tag not in {"tag:yaml.org,2002:" + name for name in ("str", "bool", "null", "int", "float")}:
            raise ValueError("unsupported Skill frontmatter scalar")
        loader = yaml.SafeLoader("")
        try:
            return loader.construct_object(node)
        finally:
            loader.dispose()
    return _bounded(convert(node))


def parse_declaration(raw, kind):
    if not isinstance(raw, bytes) or len(raw) > MAX_DECLARATION_BYTES:
        raise ServiceError("manifest_too_large", "declaration exceeds the byte budget", 422)
    try:
        value = strict_json(raw) if kind == "claude_plugin" else _frontmatter(raw) if kind == "agent_skill" else None
        if not isinstance(value, dict):
            raise ValueError("declaration must be an object")
        # Retain declarations only, not Skill body instructions or command-bearing plugin fields.
        selected = {k: value[k] for k in ("name", "description", "version", "license") if k in value}
        if kind == "agent_skill" and isinstance(value.get("metadata"), dict) and "version" in value["metadata"]:
            if "version" in selected and selected["version"] != value["metadata"]["version"]:
                raise ValueError("conflicting declared versions")
            selected["version"] = value["metadata"]["version"]
        return selected
    except (ValueError, TypeError, UnicodeError, RecursionError, yaml.YAMLError) as error:
        raise ServiceError("manifest_invalid", "declaration syntax is unsupported or invalid", 422) from error


def normalize_declaration(fields, *, kind, github_id, full_name, path, commit, agent):
    declaration_path(path, kind)
    declaration_association(kind, agent)
    category = "plugin" if kind == "claude_plugin" else "skill"
    unit = str(PurePosixPath(path).parent.parent if category == "plugin" else PurePosixPath(path).parent)
    unit = "" if unit == "." else unit
    record = {"schemaVersion": 2, "id": f"{category}.github-{github_id}/" + quote(path, safe="._/-"),
              "type": category, "versionScheme": "unknown", "targets": [unknown_target(agent)],
              "links": {"repository": "https://github.com/" + full_name},
              "distributions": [{"id": "upstream-git", "name": "github repository", "type": "github-repo",
                                 "url": "https://github.com/" + full_name + ("/tree/" + commit + "/" + quote(unit, safe="/") if unit else ""), "ref": commit}]}
    issues = []
    for key in ("name", "description", "version", "license"):
        value = fields.get(key)
        if not isinstance(value, str) or not value.strip():
            issues.append({"code": "missing_or_invalid_declaration", "field": key})
        else:
            record[key] = value
    record["pluginDetails" if category == "plugin" else "skillDetails"] = (
        {"manifestPath": path, "sourceType": "github-repo"} if category == "plugin" else {"skillPath": path})
    name = fields.get("name")
    if isinstance(name, str):
        valid_name = re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
        if not valid_name or (kind == "agent_skill" and len(name) > 64):
            issues.append({"code": "upstream_profile_invalid", "field": "name"})
        if kind == "agent_skill" and unit and PurePosixPath(unit).name != name:
            issues.append({"code": "skill_directory_name_mismatch", "field": "name"})
    if kind == "agent_skill" and isinstance(fields.get("description"), str) and len(fields["description"]) > 1024:
        issues.append({"code": "upstream_profile_invalid", "field": "description"})
    validator = build_validator("package.schema.json", submission_formats=True)
    for error in validator.iter_errors(record):
        issues.append({"code": "schema_invalid", "path": list(error.absolute_path), "validator": error.validator})
        if len(issues) >= 32:
            break
    for _ in identity_errors([(path, record)]):
        issues.append({"code": "identity_invalid"})
    return record, issues
