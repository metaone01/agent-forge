"""Create real SQLite-backed response fixtures for the optional browser acceptance test."""

import argparse
import json
from pathlib import Path

from agent_forge_service.http_contract import HttpReadService
from agent_forge_service.publishing import build_and_publish
from agent_forge_service.query import QueryService
from agent_forge_service.settings import Settings
from service_tests.test_publication_query import NOW, record


def create_fixture(output):
    output.mkdir(parents=True, exist_ok=True)
    path = output / "dashboard-browser-fixtures.json"
    if path.exists():
        raise ValueError("Fixture output must be new")
    catalog = output / "fixture-catalog"
    packages = catalog / "sources/plugin/packages"
    packages.mkdir(parents=True)
    index = {}
    identity = "dsh/plugin/数据库/metrics"
    for number in range(23):
        name = "alpha" if number == 0 else "item-%02d" % number
        value = record(name, identity=identity if number == 0 else None,
                       agents=("dsh", "claude-code") if number == 0 else ("claude-code",) if number == 1 else ("dsh",))
        if number == 0:
            value["displayName"] = "数据库工具 <script>window.__injected=1</script>"
            value["customFacets"] = {"capabilities": ["gui.observe"], "other": ["<b>personal</b>"]}
        (packages / (name + "-1.json")).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        index[name] = {"latest": "1.0.0"}
    index_path = catalog / "sources/plugin/index.json"
    index_path.write_text(json.dumps({"packages": index}), encoding="utf-8")
    settings = Settings(catalog, output / "fixture-runtime")
    cases = []
    expected = {}
    for revision in ("browser-one", "browser-two"):
        if revision == "browser-two":
            new = record("new-arrival", updated="2026-10-11T00:00:00Z")
            (packages / "new-arrival.json").write_text(json.dumps(new), encoding="utf-8")
            index["new-arrival"] = {"latest": "1.0.0"}
            index_path.write_text(json.dumps({"packages": index}), encoding="utf-8")
        build_and_publish(settings, revision=revision, now=NOW)
        reader = HttpReadService(settings)
        def add(route, parameters):
            response = reader.handle("GET", route, list(parameters.items()))
            cases.append({"revision": revision, "path": route, "parameters": parameters,
                          "status": response.status, "body": json.loads(response.body)})
            return json.loads(response.body)
        for selected in (None, "browser-one", *([] if revision == "browser-one" else ["browser-two"])):
            binding = {} if selected is None else {"revision": selected}
            add("/api/v1/manifest", binding)
            add("/api/v1/dashboard", binding)
            for agent in ("dsh", "claude-code", "missing"):
                add("/api/v1/dashboard", {**binding, "agent": agent})
            pinned = selected or revision
            add("/api/v1/facets", {"revision": pinned})
            add("/api/v1/recommendation-sources", {"revision": pinned, "limit": "20"})
            for agent in (None, "dsh"):
                filters = {"limit": "20", "sort": "canonical_updated", **({"agent": agent} if agent else {})}
                data = add("/api/v1/packages", {**filters, **binding})
                if data.get("nextCursor"):
                    add("/api/v1/packages", {**filters, "revision": pinned, "cursor": data["nextCursor"]})
            add("/api/v1/packages/" + identity + "/detail", {"revision": pinned})
            add("/api/v1/packages/" + identity + "/versions", {"revision": pinned, "limit": "20"})
            add("/api/v1/packages/" + identity + "/recommendations", {"revision": pinned, "limit": "20", "status": "all", "version": "1.0.0"})
        expected[revision] = QueryService(settings).dashboard()["totals"]
        for code in ("service_busy", "revision_unavailable"):
            response = reader.error_response(code, 503 if code == "service_busy" else 410)
            cases.append({"revision": revision, "path": "/errors/" + code, "parameters": {}, "status": response.status, "body": json.loads(response.body)})
    with path.open("x", encoding="utf-8") as stream:
        json.dump({"kind": "SQLite publication and HTTP contract fixtures", "id": identity, "expected": expected, "cases": cases}, stream, ensure_ascii=False)
    print(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    create_fixture(parser.parse_args().output)
