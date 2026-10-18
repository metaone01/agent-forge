"""Build or verify a public query delivery; never installs or deploys it."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile

if __name__ == "__main__":
    sys.dont_write_bytecode = True
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_forge_service.bundles import _descriptor, _verify_snapshot, DEFAULT_MAX_DATABASE_BYTES
from agent_forge_service.errors import ServiceError
from agent_forge_service.publishing import digest_file
from agent_forge_service.storage import encode_json

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("project", "validate", "identity", "media", "import_community", "distribution_names", "package_query_service", "snapshot")
SCHEMAS = ("package", "index", "source", "advisory")
DEPLOY = ("deploy/Dockerfile", "deploy/Dockerfile.dockerignore", "deploy/compose.yaml", "deploy/env.example",
          "deploy/query-service/entrypoint.sh", "deploy/query-service/dockerignore",
          "deploy/query-service/DELIVERY-README.zh-CN.md", "deploy/query-service/requirements.txt",
          "deploy/query-service/agent-forge-query.service", "deploy/query-service/nginx.conf.example",
          "deploy/query-service/agent-forge-retention.service", "deploy/query-service/agent-forge-retention.timer")
REQUIRED = set(DEPLOY) | {name + ".schema.json" for name in SCHEMAS} | {
    "tools/" + name + ".py" for name in TOOLS} | {
    "agent_forge_service/" + name + ".py" for name in
    ("__init__", "app", "worker", "query", "preflight", "http_contract", "publishing", "bundles", "storage", "offline", "schema_contracts", "retention", "reader_leases", "resource_budget", "operational_log", "refresh_status")}
REQUIRED |= {"site/catalog/index.html", "site/assets/query-client.js", "site/assets/api-catalog.js",
             "site/assets/api-presentation.js", "site/assets/styles.css", ".dockerignore", "README-DEPLOYMENT.md"}
SHA = re.compile(r"[0-9a-f]{64}\Z")
MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def regular(path):
    metadata = path.lstat()
    if path.absolute() != path.resolve() or not stat.S_ISREG(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise ValueError("delivery paths must be regular files without links")
    return metadata


def directory(path):
    path = path.absolute()
    if path != path.resolve() or not path.is_dir():
        raise ValueError("delivery directories must not be links")
    return path


def files(root):
    root = directory(root)
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                    raise ValueError("delivery trees must not contain links")
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(metadata.st_mode):
                    yield Path(entry.path)
                else:
                    raise ValueError("delivery trees must contain regular files only")


def bundle_facts(bundle, expected):
    bundle = directory(bundle)
    descriptor = _descriptor(bundle, expected, DEFAULT_MAX_DATABASE_BYTES)
    # Keep the public validator's strict two-file contract unchanged. Its
    # temporary copy never modifies the input or adds files to a read-only bundle.
    with tempfile.TemporaryDirectory(prefix="forge-delivery-check-") as temporary:
        view = Path(temporary).resolve()
        if not view.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise ValueError("temporary verification directory escaped its owner")
        for name in ("manifest.json", "catalog.db"):
            shutil.copyfile(bundle / name, view / name)
        manifest, manifest_hash, database_hash = _verify_snapshot(view, descriptor["revision"], DEFAULT_MAX_DATABASE_BYTES)
    if manifest_hash != descriptor["manifest"]["sha256"] or manifest["database"] != descriptor["database"]:
        raise ValueError("bundle descriptor and verified snapshot disagree")
    return {"revision": descriptor["revision"], "bundleSha256": expected,
            "manifestSha256": manifest_hash, "databaseSha256": database_hash,
            "databaseBytes": descriptor["database"]["bytes"], "counts": manifest["counts"]}


def allowed(name):
    parts = PurePosixPath(name).parts
    if not parts or "/".join(parts) != name or any(part in (".", "..") for part in parts) or "\\" in name:
        return False
    if name in DEPLOY or name in (".dockerignore", "README-DEPLOYMENT.md"):
        return True
    if name in {value + ".schema.json" for value in SCHEMAS}:
        return True
    if len(parts) == 2 and parts[0] == "agent_forge_service" and re.fullmatch(r"[a-z_][a-z_0-9]*\.py", parts[1]):
        return True
    if name in {"tools/" + value + ".py" for value in TOOLS}:
        return True
    if name in {"bundle/" + value for value in ("bundle.json", "manifest.json", "catalog.db")}:
        return True
    if parts[0] == "site" and len(parts) > 1:
        return (not any(part.startswith(".") for part in parts[1:] if part != ".nojekyll")
                and (parts[-1] == ".nojekyll" or PurePosixPath(name).suffix in (".html", ".js", ".css", ".json", ".md", ".png", ".svg")))
    return False


def instructions(template, facts):
    values = {"REVISION": facts["revision"], "PACKAGES": str(facts["counts"]["packages"]),
              "VERSIONS": str(facts["counts"]["versions"]), "BUNDLE_SHA256": facts["bundleSha256"],
              "MANIFEST_SHA256": facts["manifestSha256"]}
    for key, value in values.items():
        template = template.replace("@@" + key + "@@", value)
    if "@@" in template:
        raise ValueError("unresolved deployment instruction placeholder")
    return template


def build(root, data, bundle, expected_bundle_sha256, output):
    root, data, bundle = directory(root), directory(data), directory(bundle)
    output = output.absolute()
    if output != output.resolve() or output.exists() or output.is_symlink():
        raise ValueError("delivery output must be a new unlinked directory")
    for protected in (root, data, bundle):
        if protected.is_relative_to(output):
            raise ValueError("delivery output must not contain its inputs")
    for protected in (root / ".git", root / ".venv", root / "snapshots", root / "sources", root / "agent_forge_service", root / "tools", root / "site", root / "deploy", data, bundle):
        if output.is_relative_to(protected):
            raise ValueError("delivery output overlaps protected input paths")
    directory(root / "agent_forge_service")
    sources = {path.relative_to(root).as_posix(): path for path in (root / "agent_forge_service").glob("*.py")}
    for name in ("app", "worker", "preflight", "query", "__init__"):
        if "agent_forge_service/" + name + ".py" not in sources:
            raise ValueError("required query module is missing")
    for name in ["tools/" + value + ".py" for value in TOOLS] + [value + ".schema.json" for value in SCHEMAS] + list(DEPLOY):
        sources[name] = root / name
    for name, path in sources.items():
        if not allowed(name):
            raise ValueError("unsupported delivery source")
        regular(path)
    facts = bundle_facts(bundle, expected_bundle_sha256)
    from tools.build_site import build_site, summary_files
    summary_files(data)
    output.mkdir(parents=True, exist_ok=False)
    # Failed outputs are retained for inspection without a complete delivery.json.
    for name, path in sources.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    build_site(root, output / "site", data, data_mode="summary")
    (output / "bundle").mkdir()
    for name in ("bundle.json", "manifest.json", "catalog.db"):
        shutil.copyfile(bundle / name, output / "bundle" / name)
    for name, expected in (("catalog.db", facts["databaseSha256"]), ("manifest.json", facts["manifestSha256"]), ("bundle.json", expected_bundle_sha256)):
        if digest_file(output / "bundle" / name) != expected:
            raise ValueError("public bundle changed during delivery copy")
    (output / ".dockerignore").write_bytes((output / "deploy/query-service/dockerignore").read_bytes())
    template = (output / "deploy/query-service/DELIVERY-README.zh-CN.md").read_text(encoding="utf-8")
    (output / "README-DEPLOYMENT.md").write_text(instructions(template, facts), encoding="utf-8", newline="\n")
    entries = []
    for path in sorted(files(output)):
        name = path.relative_to(output).as_posix()
        if not allowed(name):
            raise ValueError("unsupported file in query delivery")
        entries.append({"path": name, "bytes": regular(path).st_size, "sha256": digest_file(path)})
    if not REQUIRED <= {item["path"] for item in entries}:
        raise ValueError("query delivery is missing required files")
    manifest = {"schemaVersion": 1, "kind": "agent-forge-public-query-delivery", "snapshot": facts, "files": entries}
    (output / "delivery.json").write_text(encode_json(manifest) + "\n", encoding="utf-8", newline="\n")
    return {"directory": str(output), "deliverySha256": digest_file(output / "delivery.json"), "files": len(entries), "bytes": sum(item["bytes"] for item in entries), "snapshot": facts, "deployed": False}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate delivery descriptor key")
        result[key] = value
    return result


def verify(root, expected_delivery_sha256):
    root = directory(root)
    path = root / "delivery.json"
    if not SHA.fullmatch(expected_delivery_sha256) or regular(path).st_size > MAX_MANIFEST_BYTES or digest_file(path) != expected_delivery_sha256:
        raise ValueError("delivery descriptor differs from the trusted digest")
    manifest = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    if set(manifest) != {"schemaVersion", "kind", "snapshot", "files"} or type(manifest["schemaVersion"]) is not int or manifest["schemaVersion"] != 1 or manifest["kind"] != "agent-forge-public-query-delivery":
        raise ValueError("unsupported delivery descriptor")
    entries = manifest["files"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 20000:
        raise ValueError("invalid delivery file set")
    actual = {path.relative_to(root).as_posix() for path in files(root)}
    seen = set()
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise ValueError("invalid delivery file declaration")
        name = item["path"]
        if not isinstance(name, str) or not allowed(name) or name.casefold() in seen or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= DEFAULT_MAX_DATABASE_BYTES or not isinstance(item["sha256"], str) or not SHA.fullmatch(item["sha256"]):
            raise ValueError("unsafe or duplicate delivery file")
        seen.add(name.casefold())
        target = root / PurePosixPath(name)
        if regular(target).st_size != item["bytes"] or digest_file(target) != item["sha256"]:
            raise ValueError("delivery file checksum differs")
    declared = {item["path"] for item in entries}
    if actual != declared | {"delivery.json"} or not REQUIRED <= declared:
        raise ValueError("delivery has unexpected or missing files")
    facts = manifest["snapshot"]
    if not isinstance(facts, dict) or "bundleSha256" not in facts or bundle_facts(root / "bundle", facts["bundleSha256"]) != facts:
        raise ValueError("delivery snapshot evidence differs")
    return {"valid": True, "deliverySha256": expected_delivery_sha256, "files": len(entries), "snapshot": facts, "deployed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build")
    make.add_argument("--root", type=Path, default=ROOT)
    make.add_argument("--data", type=Path, required=True)
    make.add_argument("--bundle", type=Path, required=True)
    make.add_argument("--expected-bundle-sha256", required=True)
    make.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--directory", type=Path, required=True)
    check.add_argument("--expected-delivery-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        result = build(args.root, args.data, args.bundle, args.expected_bundle_sha256, args.output) if args.command == "build" else verify(args.directory, args.expected_delivery_sha256)
    except (OSError, ValueError, ServiceError, KeyError, TypeError, RecursionError) as error:
        print(json.dumps({"error": error.code if isinstance(error, ServiceError) else type(error).__name__, "message": str(error), "deployed": False}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
