"""Build synthetic media metadata through SQLite and the decoded GET contract."""
import argparse
import json
from pathlib import Path
from agent_forge_service.http_contract import HttpReadService
from agent_forge_service.publishing import build_and_publish
from agent_forge_service.settings import Settings
from service_tests.test_publication_query import NOW, record


def build(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "theme-browser-fixture.json"
    if path.exists():
        raise ValueError("Use a fresh output")
    catalog = output / "catalog"
    packages = catalog / "sources/plugin/packages"
    packages.mkdir(parents=True)
    index = {}
    identities = []
    for name, preview in [("cover", "cover.png"), ("failure", "failed.png"), ("plain", None)]:
        value = record(name)
        value["displayName"] = {"cover": "数据库工具 / Database tools", "failure": "失败图片 / Failed preview", "plain": "没有图片 / No media"}[name]
        if preview:
            value["media"] = {"previews": [{"url": "https://media.example.test/" + preview, "alt": name + " preview"}]}
        (packages / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        index[name] = {"latest": "1.0.0"}
        identities.append(value["id"])
    (catalog / "sources/plugin/index.json").write_text(json.dumps({"packages": index}), encoding="utf-8")
    settings = Settings(catalog, output / "runtime")
    build_and_publish(settings, revision="theme-one", now=NOW)
    reader = HttpReadService(settings)
    cases = []
    def add(route, params):
        response = reader.handle("GET", route, list(params.items()))
        body = json.loads(response.body)
        cases.append({"revision": "theme-one", "path": route, "parameters": params, "status": response.status, "body": body})
        return body
    for binding in [{}, {"revision": "theme-one"}]:
        add("/api/v1/manifest", binding)
        add("/api/v1/dashboard", binding)
        add("/api/v1/packages", {"limit": "20", "sort": "canonical_updated", **binding})
    add("/api/v1/facets", {"revision": "theme-one"})
    add("/api/v1/recommendation-sources", {"revision": "theme-one", "limit": "20"})
    for identity in identities:
        add("/api/v1/packages/" + identity + "/detail", {"revision": "theme-one"})
        add("/api/v1/packages/" + identity + "/versions", {"revision": "theme-one", "limit": "20"})
        add("/api/v1/packages/" + identity + "/recommendations", {"revision": "theme-one", "limit": "20", "status": "all", "version": "1.0.0"})
    page = next(item["body"] for item in cases if item["path"] == "/api/v1/packages")
    assert len(page["items"]) == 3
    assert next(item for item in page["items"] if item["id"] == "plugin.cover")["media"]["previews"][0]["url"].endswith("/cover.png")
    payload = {"kind": "Synthetic canonical metadata published to real SQLite and decoded GET responses; no real FastAPI or upstream images", "cases": cases, "id": "plugin.cover"}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"fixture": str(path), "cases": len(cases), "packages": 3}))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    build(parser.parse_args().output)
