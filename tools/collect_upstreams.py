"""Cache public metadata without installing or executing upstream projects."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "docs/research/2026-10-02-source-inventory"
CACHE = ROOT / ".collection-cache"


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def gh_bytes(endpoint: str) -> bytes:
    for attempt in range(3):
        response = subprocess.run(["gh", "api", endpoint], capture_output=True, timeout=180)
        if response.returncode == 0:
            return response.stdout
        if attempt == 2:
            raise RuntimeError(response.stderr.decode("utf-8", errors="replace")[:600])
        time.sleep(2)
    raise AssertionError("unreachable")


def gh_json(endpoint: str) -> Any:
    return json.loads(gh_bytes(endpoint))


def file_bytes(repo: str, path: str, revision: str) -> bytes:
    value = gh_json(f"repos/{repo}/contents/{urllib.parse.quote(path)}?ref={revision}")
    if not value.get("content"):
        value = gh_json(f"repos/{repo}/git/blobs/{value['sha']}")
    return base64.b64decode(value["content"])


def web_json(url: str) -> Any:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Agent-Forge-Metadata"}), timeout=60) as response:
                return json.load(response)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)
    raise AssertionError("unreachable")


def cached_path(repo: str, path: str) -> Path:
    return CACHE / "catalogs" / repo / path


def collect_catalog(source: dict[str, Any]) -> dict[str, Any]:
    repo, revision = source["repository"], source["revision"]
    fetched: list[str] = []
    errors: list[str] = []
    for measurement in source["measurements"]:
        path = measurement.get("path")
        if not path or path in fetched:
            continue
        destination = cached_path(repo, path)
        try:
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(file_bytes(repo, path, revision))
            fetched.append(path)
        except Exception as error:
            errors.append(f"{path}: {error}")
    # Archive reads are limited to small curated catalogs with file-per-entry data.
    if any(item["method"] == "tree-files" for item in source["measurements"]):
        destination = CACHE / "archives" / (repo.replace("/", "__") + ".tar.gz")
        try:
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(gh_bytes(f"repos/{repo}/tarball/{revision}"))
            fetched.append(str(destination.relative_to(CACHE)))
        except Exception as error:
            errors.append(f"archive: {error}")
    return {"repository": repo, "revision": revision, "fetched": fetched, "errors": errors}


def collect_general(path: Path) -> dict[str, Any]:
    record = json.loads(path.read_text(encoding="utf-8"))
    repo = record["links"]["repository"].split("github.com/", 1)[1].removesuffix(".git").strip("/")
    destination = CACHE / "general" / (record["id"] + ".json")
    if destination.exists():
        return json.loads(destination.read_text(encoding="utf-8"))
    result: dict[str, Any] = {"id": record["id"], "name": record["name"], "repository": repo, "errors": []}
    try:
        metadata = gh_json(f"repos/{repo}")
        tree = gh_json(f"repos/{repo}/git/trees/HEAD?recursive=1")
        result.update(revision=tree["sha"], truncated=tree["truncated"], description=metadata.get("description"))
        result["rootFiles"] = [item["path"] for item in tree["tree"] if item["type"] == "blob" and "/" not in item["path"]]
        result["pluginManifests"] = [item["path"] for item in tree["tree"] if item["type"] == "blob" and (item["path"].endswith("/.claude-plugin/plugin.json") or item["path"] in (".claude-plugin/plugin.json", "cordis.yml", "cordis.yaml", "cordis.patch.yml"))]
        for filename in ("README.md", "readme.md", "package.json", "pyproject.toml"):
            if filename in result["rootFiles"]:
                result[filename] = file_bytes(repo, filename, tree["sha"]).decode("utf-8", errors="replace")
    except Exception as error:
        result["errors"].append(str(error))
    save(destination, result)
    return result


def collect_registry_page(page: dict[str, Any], previous: str | None) -> dict[str, Any]:
    destination = CACHE / "registry" / f"{page['page']:04d}.json"
    url = "https://registry.modelcontextprotocol.io/v0.1/servers?limit=100&version=latest"
    if previous:
        url += "&cursor=" + urllib.parse.quote(previous, safe="")
    if not destination.exists():
        save(destination, web_json(url))
    value = json.loads(destination.read_text(encoding="utf-8"))
    return {"page": page["page"], "count": len(value["servers"]), "nextCursor": value["metadata"].get("nextCursor"), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("catalogs", "general", "registry", "search", "extensions"))
    args = parser.parse_args()
    errors: list[str] = []
    if args.mode == "catalogs":
        sources = json.loads((RESEARCH / "catalogs.json").read_text(encoding="utf-8-sig"))
        results = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            tasks = {pool.submit(collect_catalog, source): source["repository"] for source in sources}
            for task in as_completed(tasks):
                value = task.result()
                results.append(value)
                errors.extend(value["errors"])
                save(CACHE / "catalog-fetch.json", results)
                print(f"catalog {value['repository']}: files={len(value['fetched'])} errors={len(value['errors'])}", flush=True)
    elif args.mode == "general":
        results = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            tasks = [pool.submit(collect_general, path) for path in (ROOT / "sources/general/packages").rglob("*.json")]
            for task in as_completed(tasks):
                value = task.result()
                results.append(value)
                errors.extend(value["errors"])
                print(f"general {value['name']}: errors={len(value['errors'])}", flush=True)
        save(CACHE / "general-audit.json", sorted(results, key=lambda item: item["name"].lower()))
    elif args.mode == "registry":
        snapshot = json.loads((RESEARCH / "mcp-registry.json").read_text(encoding="utf-8-sig"))
        results = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            tasks = {pool.submit(collect_registry_page, page, snapshot["pages"][index - 1]["nextCursor"] if index else None): page["page"] for index, page in enumerate(snapshot["pages"])}
            for task in as_completed(tasks):
                try:
                    results.append(task.result())
                except Exception as error:
                    errors.append(f"page {tasks[task]}: {error}")
                if len(results) % 25 == 0:
                    print(f"registry pages={len(results)} errors={len(errors)}", flush=True)
        save(CACHE / "registry-fetch.json", {"pages": sorted(results, key=lambda page: page["page"]), "errors": errors})
    elif args.mode == "extensions":
        audit = json.loads((CACHE / "general-audit.json").read_text(encoding="utf-8"))
        results = []
        for entry in audit:
            for manifest in entry["pluginManifests"]:
                if manifest.startswith("examples/"):
                    continue
                path = CACHE / "extensions" / entry["repository"] / manifest
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(file_bytes(entry["repository"], manifest, entry["revision"]))
                results.append({"repository": entry["repository"], "revision": entry["revision"], "manifest": manifest, "metadata": json.loads(path.read_text(encoding="utf-8"))})
        save(CACHE / "extensions.json", results)
        print(f"general-project extensions={len(results)}", flush=True)
    else:
        for keyword in ("dsh-plugin", "mcp-server"):
            results = []
            first = web_json(f"https://registry.npmjs.org/-/v1/search?text=keywords:{keyword}&size=250&from=0")
            results.extend(first["objects"])
            with ThreadPoolExecutor(max_workers=4) as pool:
                tasks = [pool.submit(web_json, f"https://registry.npmjs.org/-/v1/search?text=keywords:{keyword}&size=250&from={offset}") for offset in range(250, first["total"], 250)]
                for task in as_completed(tasks):
                    results.extend(task.result()["objects"])
            unique = {item["package"]["name"]: item["package"] for item in results}
            save(CACHE / "npm" / f"{keyword}.json", {"reportedTotal": first["total"], "packages": list(unique.values()), "returnedRows": len(results)})
            print(f"npm {keyword}: unique={len(unique)} reported={first['total']}", flush=True)
    print(json.dumps({"mode": args.mode, "errors": errors}, ensure_ascii=False), flush=True)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
