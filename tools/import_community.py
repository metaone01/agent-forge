"""Import metadata with explicit category evidence; retain unresolved discoveries.

Reads the cache produced by collect_upstreams.py. No upstream code is executed.
Records are schema-validated before canonical files are written.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import tarfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlparse

import yaml

try:
    from tools.identity import identity_errors
    from tools.validate import build_validator
except ModuleNotFoundError:
    from identity import identity_errors
    from validate import build_validator

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".collection-cache"
RESEARCH = ROOT / "docs/research/2026-10-02-source-inventory"
REPORTS = ROOT / "docs/research/2026-10-02-import"
TYPES = ("mcp", "plugin", "skill", "general", "bundle")
STAMP = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
REVISION = "community-20261002"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def slug(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._@/+\-]", "-", value).strip("-/")
    if not clean or not re.match(r"^[A-Za-z0-9@]", clean):
        clean = "package-" + clean
    if len(clean) > 185:
        clean = clean[:170] + "--" + hashlib.sha256(value.encode()).hexdigest()[:12]
    return clean


def file_component(value: str) -> str:
    return slug(value).replace("/", "--")[:110] + "--" + hashlib.sha256(value.encode()).hexdigest()[:12]


def repository(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("url")
    if not isinstance(value, str):
        return None
    match = re.search(r"(?:github\.com/|^github:|^)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:[/?#]|$)", value.removeprefix("git+"))
    return match.group(1).removesuffix(".git") if match else None


def row_repository(row: dict[str, Any]) -> str | None:
    for key in ("repo", "repository", "full_name", "fullName", "repoUrl", "url", "name"):
        value = repository(row.get(key))
        if value:
            return value
    return None


def text(value: Any, fallback: str = "") -> str:
    if isinstance(value, dict):
        value = value.get("zh") or value.get("en") or fallback
    return str(value).strip() if value is not None else fallback


def source_ref(repo: str, revision: str, path: str, key: str = "") -> dict[str, str]:
    value = {"repository": repo, "revision": revision, "path": path}
    if key:
        value["entry"] = key
    return value


def unknown_target(agent: str) -> dict[str, Any]:
    return {"agentId": agent, "agentVersionRange": None, "versionScheme": "unknown", "compatibilityStatus": "unknown", "compatibilityNote": "Discovery association only; no parseable upstream Agent compatibility range was supplied."}


def base_record(category: str, identity: str, name: str, description: str, version: str, agent: str) -> dict[str, Any]:
    return {"schemaVersion": 2, "id": category + "." + hashlib.sha256(identity.encode()).hexdigest(), "name": slug(name), "displayName": name[:300], "version": version, "versionScheme": "unknown", "type": category, "description": description[:4000] or name, "license": "unknown", "targets": [unknown_target(agent)], "createdAt": STAMP, "updatedAt": STAMP}


def git_distribution(repo: str, path: str = "", revision: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"id": "upstream-git", "type": "git", "url": f"https://github.com/{repo}"}
    if path:
        value["url"] += f"/tree/{revision or 'HEAD'}/{quote(path, safe='/')}"
    if revision:
        value["ref"] = revision
    return value


def registry_distribution(kind: str, identifier: str, version: str | None = None) -> dict[str, Any]:
    urls = {"npm": "https://www.npmjs.com/package/", "pypi": "https://pypi.org/project/", "nuget": "https://www.nuget.org/packages/", "cargo": "https://crates.io/crates/"}
    url = urls.get(kind, "https://registry.modelcontextprotocol.io/?search=") + quote(identifier, safe="@/")
    value: dict[str, Any] = {"id": kind + "-" + hashlib.sha256(identifier.encode()).hexdigest()[:12], "type": "registry", "url": url, "registry": kind}
    if version:
        value["version"] = version
    return value


def archive_files(repo: str) -> dict[str, bytes]:
    path = CACHE / "archives" / (repo.replace("/", "__") + ".tar.gz")
    files: dict[str, bytes] = {}
    with tarfile.open(path) as archive:
        for member in archive:
            if member.isfile() and member.size <= 2_000_000:
                relative = member.name.split("/", 1)[1]
                if relative.endswith((".json", ".md", ".yml", ".yaml", "LICENSE")):
                    handle = archive.extractfile(member)
                    if handle:
                        files[relative] = handle.read()
    return files


class Importer:
    def __init__(self, root: Path = ROOT, sources: list[dict[str, Any]] | None = None):
        self.root = root
        self.records: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.paths: dict[tuple[str, str, str], Path] = {}
        self.identities: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        self.repo_plugins: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
        self.unresolved: list[dict[str, Any]] = []
        self.coverage: Counter[str] = Counter()
        self.accepted: Counter[str] = Counter()
        self.invalid: list[dict[str, Any]] = []
        self.original_counts: Counter[str] = Counter()
        self.current: dict[tuple[str, str], tuple[str, str, str]] = {}
        self.sources = {source["repository"]: source for source in (load(RESEARCH / "catalogs.json") if sources is None else sources)}

    def put(self, record: dict[str, Any], identity: tuple[str, str, str], ref: dict[str, Any], overwrite: bool = False) -> tuple[str, str, str]:
        category, repo, subpath = identity
        identity = (category, repo.lower(), subpath)
        previous = self.identities.get(identity)
        if previous:
            old = self.records[previous]
            record["id"], record["name"] = old["id"], old["name"]
        key = (record["type"], record["name"], record["version"])
        if key in self.records:
            if self.records[key]["id"] != record["id"]:
                raise ValueError(f"Conflicting identities for {key}")
            if not overwrite:
                record = self.records[key]
            else:
                record["createdAt"] = self.records[key].get("createdAt", STAMP)
                record["_meta"] = self.records[key].get("_meta", {})
        collection = record.setdefault("_meta", {}).setdefault("org.agentforge/collection", {"sources": [], "classification": "upstream-metadata", "observedAt": STAMP})
        if ref not in collection["sources"]:
            collection["sources"].append(ref)
        self.records[key] = record
        self.identities[identity] = key
        self.current[(record["type"], record["name"])] = key
        if category == "plugin":
            self.repo_plugins[repo.lower()].add(key)
        return key

    def load_existing(self) -> None:
        for category in TYPES:
            for path in sorted((self.root / "sources" / category / "packages").rglob("*.json")):
                value = load(path)
                key = (category, value["name"], value["version"])
                self.records[key] = value
                self.paths[key] = path
                self.original_counts[category] += 1
                self.current[(category, value["name"])] = key
                repo = repository(value.get("links", {}).get("repository"))
                subpath = ""
                if category == "skill":
                    subpath = value["skillDetails"]["skillPath"]
                elif category == "plugin":
                    for distribution in value.get("distributions", []):
                        match = re.search(r"github\.com/[^/]+/[^/]+/tree/[^/]+/(.+)$", distribution["url"])
                        if match:
                            subpath = unquote(match.group(1))
                            break
                if category == "mcp":
                    repo = value["name"]
                if repo:
                    self.identities[(category, repo.lower(), subpath)] = key
                    if category == "plugin":
                        self.repo_plugins[repo.lower()].add(key)
            index_path = self.root / "sources" / category / "index.json"
            if index_path.exists():
                for name, entry in load(index_path).get("packages", {}).items():
                    key = (category, name, entry["latest"])
                    if key in self.records:
                        self.current[(category, name)] = key

    def pending(self, row: Any, ref: dict[str, Any], reason: str, claimed: str | None = None) -> None:
        self.unresolved.append({"source": ref, "claimedType": claimed, "reason": reason, "metadata": row})

    def ingest_plugin(self, row: dict[str, Any], ref: dict[str, Any], manifest: str | None = None, subpath: str = "", agent: str = "dsh", version: str | None = None, revision: str | None = None) -> None:
        repo = row_repository(row)
        if not repo:
            self.pending(row, ref, "No repository identity supplied", "plugin")
            return
        identity = ("plugin", repo.lower(), subpath)
        existing = self.identities.get(identity)
        if not manifest and not existing:
            choices = self.repo_plugins.get(repo.lower(), set())
            if not subpath and len(choices) == 1:
                existing = next(iter(choices))
        if not manifest:
            if existing:
                value = self.records[existing]
                current = self.current.get((value["type"], value["name"]))
                self.put(value, ("plugin", repo, subpath), ref)
                if current:
                    self.current[(value["type"], value["name"])] = current
                self.accepted[ref["repository"]] += 1
            else:
                self.pending(row, ref, "Plugin discovery lacks a declared manifest or distinct installable unit", "plugin")
            return
        name = repo + ("/" + subpath if subpath else "")
        if not version:
            version = text(row.get("version") or row.get("latestVersion")) or "unversioned"
        value = base_record("plugin", f"{repo.lower()}/{subpath}", name, text(row.get("description"), name), version, agent)
        value["pluginDetails"] = {"manifestPath": manifest, "sourceType": "git"}
        value["links"] = {"repository": f"https://github.com/{repo}", "readme": f"https://github.com/{repo}#readme"}
        value["distributions"] = [git_distribution(repo, subpath, revision)]
        npm = row.get("npm") or row.get("npmName") or row.get("npmPackage")
        if isinstance(npm, str) and npm:
            value["distributions"].append(registry_distribution("npm", npm, version if version != "unversioned" else None))
        license_value = row.get("license")
        if isinstance(license_value, str) and license_value:
            value["license"] = license_value
        tags = row.get("tags") or row.get("keywords") or []
        value["keywords"] = list(dict.fromkeys(str(tag)[:100] for tag in tags if tag))[:100]
        if row.get("category") in ("theme", "skin") or row.get("kind") == "skin":
            value["subtype"] = "skin"
        self.put(value, identity, ref)
        self.accepted[ref["repository"]] += 1

    def ingest_skill(self, repo: str, path: str, row: dict[str, Any], ref: dict[str, Any], revision: str | None = None, agent: str = "dsh") -> None:
        if not path.endswith("SKILL.md") or path.startswith("/") or ".." in PurePosixPath(path).parts:
            self.pending(row, ref, "Invalid or missing Skill file path", "skill")
            return
        name = repo + "/" + str(PurePosixPath(path).parent)
        if path == "SKILL.md":
            name = repo
        value = base_record("skill", repo.lower() + "/" + path, name, text(row.get("description"), name), revision or "unversioned", agent)
        value["skillDetails"] = {"skillPath": path}
        value["links"] = {"repository": f"https://github.com/{repo}", "readme": f"https://github.com/{repo}/blob/{revision or 'HEAD'}/{quote(path, safe='/')}"}
        value["distributions"] = [git_distribution(repo, str(PurePosixPath(path).parent) if path != "SKILL.md" else "", revision)]
        if isinstance(row.get("license"), str) and row["license"]:
            value["license"] = row["license"]
        self.put(value, ("skill", repo, path), ref)
        self.accepted[ref["repository"]] += 1

    def structured_dsh(self) -> None:
        repo = "dshworks/awesome-dsh-plugins"
        revision = self.sources[repo]["revision"]
        for filename, field in (("data/plugins.json", "plugins"), ("data/candidates.json", "candidates")):
            for index, row in enumerate(load(CACHE / "catalogs" / repo / filename)[field]):
                ref = source_ref(repo, revision, filename, str(index))
                self.coverage[repo] += 1
                evidence = row.get("evidence", "")
                skill = re.search(r"([A-Za-z0-9_./@+\-]*SKILL\.md)", evidence)
                manifest = re.search(r"([A-Za-z0-9_./@+\-]*(?:package\.json|plugin\.json|cordis\.ya?ml|cordis\.patch\.ya?ml))", evidence)
                if skill and row_repository(row):
                    self.ingest_skill(row_repository(row), skill.group(1), row, ref)
                elif manifest and ("dsh." in evidence or "plugin" in row.get("category", "")):
                    self.ingest_plugin(row, ref, manifest.group(1), row.get("path", ""))
                else:
                    self.pending(row, ref, "Upstream category alone does not identify an installable unit", row.get("category"))
        repo = "dshworks/awesome-dsh-themes"
        revision = self.sources[repo]["revision"]
        for filename, field in (("data/themes.json", "themes"), ("data/candidates.json", "candidates")):
            for index, row in enumerate(load(CACHE / "catalogs" / repo / filename)[field]):
                ref = source_ref(repo, revision, filename, str(index))
                self.coverage[repo] += 1
                evidence = row.get("evidence", "")
                match = re.search(r"([A-Za-z0-9_./@+\-]*(?:package\.json|plugin\.json))", evidence)
                self.ingest_plugin(row, ref, match.group(1) if match else None, row.get("path", ""))

    def skills_and_marketplace(self) -> None:
        for repo, source in self.sources.items():
            if not any(item["method"] == "tree-files" for item in source["measurements"]):
                continue
            files = archive_files(repo)
            for measurement in source["measurements"]:
                if measurement["method"] != "tree-files":
                    continue
                for path in measurement["entries"]:
                    ref = source_ref(repo, source["revision"], path)
                    self.coverage[repo] += 1
                    if path.endswith("SKILL.md"):
                        content = files[path].decode("utf-8", errors="replace")
                        match = re.match(r"\A---\s*\n(.*?)\n---(?:\s|$)", content, re.S)
                        try:
                            front = yaml.safe_load(match.group(1)) if match else {}
                            front = front if isinstance(front, dict) else {}
                        except yaml.YAMLError:
                            front = {}
                        # Internal repository authoring skills are retained as discoveries.
                        if path.startswith((".agents/", ".claude/")):
                            self.pending({"repository": repo, "path": path, "name": front.get("name")}, ref, "Repository-internal authoring Skill; publication intent not established", "skill")
                            continue
                        self.ingest_skill(repo, path, front, ref, source["revision"], "claude-code" if repo.startswith(("anthropics/", "ComposioHQ/", "obra/")) else "dsh")
                    elif path.startswith("src/") and path.endswith("README.md"):
                        self.pending({"repository": repo, "path": path}, ref, "MCP reference implementation; registry/package identity must be mapped", "mcp")
                    else:
                        try:
                            row = yaml.safe_load(files[path])
                        except yaml.YAMLError as error:
                            self.pending({"path": path}, ref, f"Unsupported YAML metadata: {error}")
                            continue
                        if not isinstance(row, dict):
                            self.pending(row, ref, "Missing structured metadata")
                            continue
                        if path.endswith("/preset.yml"):
                            self.pending(row, ref, "Preset requires resolution of real member package ids", "bundle")
                        else:
                            if not row_repository(row) and row.get("repo"):
                                row["repository"] = row["repo"]
                            self.ingest_plugin(row, ref, subpath=text(row.get("path")))

        repo = "anthropics/claude-plugins-official"
        revision = self.sources[repo]["revision"]
        for index, row in enumerate(load(CACHE / "catalogs" / repo / ".claude-plugin/marketplace.json")["plugins"]):
            self.coverage[repo] += 1
            ref = source_ref(repo, revision, ".claude-plugin/marketplace.json", row["name"])
            source = row.get("source")
            if isinstance(source, str):
                plugin_repo = repo
                subpath = source.removeprefix("./")
                plugin_revision = revision
            elif isinstance(source, dict) and repository(source.get("url")):
                plugin_repo = repository(source["url"])
                subpath = source.get("path", "")
                plugin_revision = source.get("sha") or source.get("ref")
            else:
                self.pending(row, ref, "Marketplace source has no resolvable Git identity", "plugin")
                continue
            manifest = (subpath + "/" if subpath else "") + ".claude-plugin/plugin.json"
            normalized = {**row, "repo": plugin_repo}
            self.ingest_plugin(normalized, ref, manifest, subpath, "claude-code", row.get("version") or plugin_revision or "unversioned", plugin_revision)

    def remaining_catalogs(self) -> None:
        handled = {"dshworks/awesome-dsh-plugins", "dshworks/awesome-dsh-themes", "anthropics/claude-plugins-official"}
        for repo, source in self.sources.items():
            for measurement in source["measurements"]:
                filename = measurement.get("path")
                if repo in handled or not filename:
                    continue
                if measurement["method"] == "json":
                    value = load(CACHE / "catalogs" / repo / filename)
                    if measurement.get("field"):
                        value = value[measurement["field"]]
                    rows = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
                elif measurement["method"] == "csv":
                    rows = list(enumerate(csv.DictReader(io.StringIO((CACHE / "catalogs" / repo / filename).read_text(encoding="utf-8-sig")))))
                elif measurement["method"].startswith("markdown-"):
                    rows = list(enumerate(measurement["entries"]))
                else:
                    continue
                for key, row in rows:
                    ref = source_ref(repo, source["revision"], filename, str(key))
                    self.coverage[repo] += 1
                    if isinstance(row, dict):
                        row = dict(row)
                        if isinstance(key, str) and repository(key) and not row_repository(row):
                            row["repo"] = key
                    else:
                        if measurement["method"] == "markdown-skill-file-links":
                            skill_repo = repository(row)
                            match = re.search(r"/(?:blob|tree)/([^/]+)/(.+SKILL\.md)$", row)
                            if skill_repo and match:
                                self.ingest_skill(skill_repo, match.group(2), {}, ref, match.group(1))
                                continue
                        row = {"discovered": row, "repo": repository(row)}
                    skill_repo = row_repository(row)
                    paths = row.get("skill_files")
                    if paths and skill_repo:
                        for path in paths:
                            self.ingest_skill(skill_repo, path, row, ref)
                    elif repo == "bruc3van/awesome-dsh-plugin" and filename == "data/packages.json":
                        self.pending(row, ref, "Installation hints must be matched to manifest-declared units", "plugin")
                    elif repo in {"modelcontextprotocol/servers", "punkpeye/awesome-mcp-servers", "e2b-dev/awesome-ai-agents", "VoltAgent/awesome-agent-skills"}:
                        claimed = "mcp" if "mcp" in repo or repo == "modelcontextprotocol/servers" else "skill" if "skills" in repo else "general"
                        self.pending(row, ref, "Navigation link lacks structural metadata for an installable unit", claimed)
                    else:
                        self.ingest_plugin(row, ref)

    def registry(self) -> None:
        pages = load(CACHE / "registry-fetch.json")
        if pages["errors"]:
            raise ValueError("Registry reads are incomplete")
        for page in pages["pages"]:
            for item in load(CACHE / "registry" / f"{page['page']:04d}.json")["servers"]:
                self.coverage["mcp-registry"] += 1
                server = item["server"]
                official = item.get("_meta", {}).get("io.modelcontextprotocol.registry/official", {})
                name, version = server.get("name"), server.get("version")
                ref = {"repository": "mcp-registry", "path": f"v0.1/servers?page={page['page']}", "revision": STAMP, "entry": str(name)}
                if not isinstance(name, str) or not isinstance(version, str):
                    self.pending(item, ref, "Missing original MCP server name/version", "mcp")
                    continue
                distributions = []
                details = None
                supported = {"npm", "pypi", "nuget", "cargo", "oci", "mcpb"}
                for package in server.get("packages", []):
                    kind, identifier = package.get("registryType"), package.get("identifier")
                    if kind not in supported or not isinstance(identifier, str) or not identifier:
                        continue
                    distribution = registry_distribution(kind, identifier, package.get("version"))
                    if kind == "oci":
                        distribution.update(type="oci", url="https://" + identifier.removeprefix("https://").removeprefix("http://"))
                    elif kind == "mcpb" and identifier.startswith(("https://", "http://")):
                        distribution.update(type="archive", url=identifier)
                    distributions.append(distribution)
                    if details is None:
                        details = {"registryType": kind, "identifier": identifier}
                        if package.get("registryBaseUrl"):
                            details["registryBaseUrl"] = package["registryBaseUrl"]
                        transport = package.get("transport", {}).get("type")
                        if transport in ("stdio", "sse", "streamable-http"):
                            details["transport"] = transport
                remotes = [{"url": remote["url"], "transport": remote["type"]} for remote in server.get("remotes", []) if remote.get("type") in ("sse", "streamable-http") and remote.get("url", "").startswith(("https://", "http://"))]
                remotes = list({(remote["url"], remote["transport"]): remote for remote in remotes}.values())
                for index, remote in enumerate(remotes):
                    distributions.append({"id": f"remote-{index}", "type": "other", "url": remote["url"], "install": {"type": "api", "url": remote["url"]}, "notes": "Declared MCP endpoint; not a downloadable package."})
                if not distributions:
                    self.pending(item, ref, "MCP registry entry declares no supported package or remote endpoint", "mcp")
                    continue
                if details is None:
                    details = {"registryType": "other", "identifier": name}
                if remotes:
                    details["remotes"] = remotes
                record = base_record("mcp", name, name, text(server.get("description"), name), version, "dsh")
                record["name"] = slug(name)
                record["id"] = "mcp." + record["name"] if len(record["name"]) <= 210 else record["id"]
                record["mcpDetails"] = details
                unique_distributions = {json.dumps(distribution, sort_keys=True): distribution for distribution in distributions}
                record["distributions"] = list(unique_distributions.values())
                for distribution in record["distributions"]:
                    distribution["id"] = "upstream-" + hashlib.sha256(json.dumps(distribution, sort_keys=True).encode()).hexdigest()[:16]
                record["displayName"] = text(server.get("title"), name)[:300]
                links = {}
                upstream = server.get("repository") or {}
                if upstream.get("url", "").startswith(("https://", "http://")):
                    links["repository"] = upstream["url"]
                if server.get("websiteUrl", "").startswith(("https://", "http://")):
                    links["homepage"] = server["websiteUrl"]
                if links:
                    record["links"] = links
                if official.get("status") in ("active", "deprecated"):
                    record["lifecycle"] = {"status": official["status"]}
                self.put(record, ("mcp", name, ""), ref, overwrite=True)
                self.accepted["mcp-registry"] += 1

    def npm(self) -> None:
        for keyword in ("dsh-plugin", "mcp-server"):
            snapshot = load(CACHE / "npm" / f"{keyword}.json")
            for package in snapshot["packages"]:
                ref = {"repository": "npm-search:" + keyword, "revision": STAMP, "path": "-/v1/search", "entry": package["name"]}
                self.coverage[ref["repository"]] += 1
                row = {**package, "repo": repository(package.get("links", {}).get("repository")), "npm": package["name"]}
                if keyword == "dsh-plugin":
                    self.ingest_plugin(row, ref)
                else:
                    self.pending(package, ref, "npm keyword alone does not establish MCP server identity or transport", "mcp")

    def audit_general(self) -> None:
        audit = load(CACHE / "general-audit.json")
        report = []
        for row in audit:
            if row["errors"] or row.get("truncated"):
                raise ValueError(f"Incomplete general audit: {row['name']}")
            package = json.loads(row.get("package.json", "{}"))
            if package.get("dsh") or ".claude-plugin/plugin.json" in row["pluginManifests"]:
                raise ValueError(f"General root declares an Agent plugin: {row['name']}; manual recategorization required")
            path = next(path for path in (self.root / "sources/general/packages").rglob("*.json") if load(path)["id"] == row["id"])
            record = load(path)
            record.setdefault("_meta", {})["org.agentforge/classification"] = {"reviewedAt": STAMP, "repositoryRevision": row["revision"], "decision": "retain-general", "basis": record["generalDetails"]["toolType"], "separatePluginManifests": row["pluginManifests"], "evidence": f"https://github.com/{row['repository']}/blob/{row['revision']}/README.md"}
            record["updatedAt"] = STAMP
            self.records[("general", record["name"], record["version"])] = record
            report.append({"id": row["id"], "name": row["name"], "repository": row["repository"], "revision": row["revision"], "decision": "retain-general", "toolType": record["generalDetails"]["toolType"], "agentUse": record["generalDetails"]["agentUse"], "description": row["description"], "separatePluginManifests": row["pluginManifests"]})
        dump(REPORTS / "general-audit.json", report)

    def extensions(self) -> None:
        for item in load(CACHE / "extensions.json"):
            repo, manifest, revision = item["repository"], item["manifest"], item["revision"]
            path = manifest.removesuffix("/.claude-plugin/plugin.json")
            ref = source_ref(repo, revision, manifest)
            self.coverage["general-project-extensions"] += 1
            row = {**item["metadata"], "repo": repo}
            self.ingest_plugin(row, ref, manifest, path, "claude-code", text(row.get("version")) or revision, revision)

    def finish(self, apply: bool) -> dict[str, Any]:
        validator = build_validator("package.schema.json")
        valid = {}
        for key, record in self.records.items():
            errors = list(validator.iter_errors(record))
            if errors:
                if key in self.paths:
                    raise ValueError(f"Invalid existing record {key}: {errors[0].message}")
                self.invalid.append({"type": record["type"], "name": record["name"], "version": record["version"], "errors": [error.message for error in errors]})
                self.pending(record, record.get("_meta", {}).get("org.agentforge/collection", {}).get("sources", [{}])[0], "Canonical schema validation failed", record["type"])
            else:
                valid[key] = record
        conflicts = identity_errors((str(key), record) for key, record in valid.items())
        if conflicts:
            raise ValueError("\n".join(conflicts[:20]))
        summary = {"observedAt": STAMP, "revision": REVISION, "applied": apply, "originalCounts": dict(self.original_counts), "recordCounts": dict(Counter(key[0] for key in valid)), "sourceRowsRead": dict(self.coverage), "acceptedObservations": dict(self.accepted), "pendingObservations": len(self.unresolved), "schemaRejected": len(self.invalid)}
        summary["packageCounts"] = dict(Counter(category for category, name in {(key[0], key[1]) for key in valid}))
        REPORTS.mkdir(parents=True, exist_ok=True)
        with (REPORTS / "candidates.jsonl").open("w", encoding="utf-8") as handle:
            for row in self.unresolved:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
        dump(REPORTS / "schema-rejections.json", self.invalid)
        dump(REPORTS / "summary.json", summary)
        if not apply:
            return summary
        package_entries: dict[str, dict[str, Any]] = {category: {} for category in TYPES}
        for key, record in sorted(valid.items()):
            category, name, version = key
            path = self.paths.get(key) or (self.root / "sources" / category / "packages" / file_component(name) / (file_component(version) + ".json"))
            dump(path, record)
            index_entry = package_entries[category].setdefault(name, {"id": record["id"], "latest": version, "versions": [], "path": path.relative_to(self.root / "sources" / category).as_posix()})
            index_entry["versions"].append(version)
            chosen = self.current.get((category, name))
            if chosen == key or chosen not in valid:
                index_entry.update(latest=version, path=path.relative_to(self.root / "sources" / category).as_posix(), summary=record["description"], keywords=record.get("keywords", []), searchText=" ".join(str(record.get(field, "")) for field in ("name", "displayName", "description", "keywords", "generalDetails")).lower())
        for category in TYPES:
            directory = self.root / "sources" / category
            source = load(directory / "source.json")
            source.update(revision=REVISION, generatedAt=STAMP, updatedAt=STAMP)
            source.pop("indexChecksum", None)
            index = load(directory / "index.json")
            index.update(revision=REVISION, generatedAt=STAMP, updatedAt=STAMP, packages=dict(sorted(package_entries[category].items())))
            dump(directory / "source.json", source)
            dump(directory / "index.json", index)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write schema-valid canonical records and indexes")
    args = parser.parse_args()
    importer = Importer()
    importer.load_existing()
    importer.structured_dsh()
    print("DSH manifest-declared units parsed", flush=True)
    importer.skills_and_marketplace()
    importer.extensions()
    print("Skill files and marketplace parsed", flush=True)
    importer.remaining_catalogs()
    importer.registry()
    importer.npm()
    importer.audit_general()
    print(json.dumps(importer.finish(args.apply), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
