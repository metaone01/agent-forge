import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from agent_forge_service.dashboard import aggregate_dashboard
from agent_forge_service.errors import ServiceError
from agent_forge_service.http_contract import HttpReadService
from agent_forge_service.publishing import atomic_json, build_and_publish, digest_file
from agent_forge_service.query import QueryService
from agent_forge_service.settings import Settings
from service_tests.test_publication_query import NOW, record


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup)
        self.root = Path(self.temporary.name)
        self.catalog = self.root / "catalog"
        self.packages = self.catalog / "sources/plugin/packages"
        self.packages.mkdir(parents=True)
        self.settings = Settings(self.catalog, self.root / "runtime")
        alpha = record("alpha", agents=("dsh", "claude-code"))
        alpha["targets"][0].update(agentVersionRange=">=1.0.0", versionScheme="semver", compatibilityStatus="known")
        alpha["customFacets"] = {"capabilities": ["gui.observe"], "other": ["<script>custom</script>"]}
        self.write(alpha)
        self.write(record("alpha", version="2.0.0", agents=("dsh", "claude-code")))
        self.write(record("beta", agents=("claude-code",)))
        self.index = {"alpha": {"latest": "1.0.0"}, "beta": {"latest": "1.0.0"}}
        self.build("one")

    def cleanup(self):
        for attempt in range(6):
            try:
                self.temporary.cleanup()
                return
            except OSError as error:
                if getattr(error, "winerror", None) != 145 or attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def write(self, value):
        (self.packages / (value["name"] + "-" + value["version"] + ".json")).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def build(self, revision):
        (self.catalog / "sources/plugin/index.json").write_text(json.dumps({"packages": self.index}), encoding="utf-8")
        return build_and_publish(self.settings, revision=revision, now=NOW)

    def get(self, **parameters):
        return HttpReadService(self.settings).handle("GET", "/api/v1/dashboard", [(key,str(value)) for key,value in parameters.items()])

    def test_shared_packages_are_unique_globally_but_each_agent_has_coverage(self):
        data = QueryService(self.settings).dashboard()
        self.assertEqual({"packages": 2, "versions": 3, "agents": 2, "bundles": 0}, data["totals"])
        self.assertEqual(3, sum(agent["packageCount"] for agent in data["agents"]))
        self.assertEqual("precomputed", data["projectionMode"])
        self.assertEqual({"known": 1, "unknown": 2, "unit": "package-agent-declaration"}, data["compatibility"])
        self.assertEqual("distinct-canonical-id", data["countingPolicy"]["packages"])

    def test_agent_scoped_versions_and_compatibility(self):
        data = QueryService(self.settings).dashboard(agent="dsh")
        self.assertEqual({"packages": 1, "versions": 2, "agents": 1, "bundles": 0}, data["totals"])
        self.assertEqual(1, data["compatibility"]["known"])
        self.assertEqual(0, data["compatibility"]["unknown"])
        self.assertEqual(["plugin.alpha"], [item["id"] for item in data["recentPackages"]])

    def test_facets_have_independent_namespaces_and_known_zeros(self):
        data = QueryService(self.settings).dashboard()
        self.assertEqual(2, data["facets"][0]["count"])
        custom = next(item for item in data["customFacets"] if item["value"] == "gui.observe")
        self.assertEqual(1, custom["count"])
        self.assertEqual(0, data["types"]["mcp"])
        self.assertEqual(5, len(data["types"]))

    def test_missing_agent_and_bad_parameters_are_explicit(self):
        response = self.get(agent="missing")
        self.assertEqual(404, response.status)
        self.assertEqual("agent_not_found", json.loads(response.body)["error"]["code"])
        for parameters in ({"agent": "x" * 101}, {"agent": ""}, {"sort": "most_used"}):
            self.assertEqual(400, self.get(**parameters).status)
        response = HttpReadService(self.settings).handle("GET", "/api/v1/dashboard", [("agent","dsh"),("agent","dsh")])
        self.assertEqual(400, response.status)

    def test_revision_remains_pinned_after_publication(self):
        reader = HttpReadService(self.settings)
        first = reader.handle("GET", "/api/v1/dashboard")
        self.write(record("gamma"))
        self.index["gamma"] = {"latest": "1.0.0"}
        self.build("two")
        pinned = reader.handle("GET", "/api/v1/dashboard", [("revision","one")])
        current = reader.handle("GET", "/api/v1/dashboard")
        self.assertEqual(2, json.loads(pinned.body)["totals"]["packages"])
        self.assertEqual(3, json.loads(current.body)["totals"]["packages"])
        self.assertEqual("one", json.loads(pinned.body)["revision"])
        self.assertNotEqual(first.headers["ETag"], current.headers["ETag"])
        agent = reader.handle("GET", "/api/v1/dashboard", [("revision","one"),("agent","dsh")])
        self.assertNotEqual(pinned.headers["ETag"], agent.headers["ETag"])
        not_modified = reader.handle("GET", "/api/v1/dashboard", [("revision","one"),("agent","dsh")], if_none_match=agent.headers["ETag"])
        self.assertEqual(304, not_modified.status)

    def test_current_switch_during_query_does_not_mix_summary_and_envelope(self):
        original = QueryService.dashboard
        def switching(service, **parameters):
            if not (self.settings.snapshots / "two").exists():
                self.write(record("gamma"))
                self.index["gamma"] = {"latest": "1.0.0"}
                self.build("two")
            return original(service, **parameters)
        with patch.object(QueryService, "dashboard", switching):
            response = self.get()
        self.assertEqual("one", json.loads(response.body)["revision"])
        self.assertEqual(2, json.loads(response.body)["totals"]["packages"])

    def test_precomputed_reads_do_not_run_aggregation(self):
        with patch("agent_forge_service.dashboard.aggregate_dashboard", side_effect=AssertionError("must not aggregate")):
            self.assertEqual(200, self.get(agent="dsh").status)

    def test_old_snapshots_aggregate_without_private_state(self):
        path = self.settings.snapshots / "one"
        with closing(sqlite3.connect(path / "catalog.db")) as connection:
            connection.execute("DROP TABLE dashboard_summaries")
            connection.commit()
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        manifest["features"].pop("dashboard")
        manifest["database"].update(sha256=digest_file(path / "catalog.db"), bytes=(path / "catalog.db").stat().st_size)
        atomic_json(path / "manifest.json", manifest)
        atomic_json(self.settings.snapshots / "current.json", {"revision": "one", "manifestSha256": digest_file(path / "manifest.json")})
        state_hash = digest_file(self.settings.state_path)
        data = QueryService(self.settings).dashboard(agent="dsh")
        self.assertEqual("on-demand", data["projectionMode"])
        self.assertEqual(1, data["totals"]["packages"])
        self.assertEqual(state_hash, digest_file(self.settings.state_path))

    def test_aggregation_failure_preserves_current_revision(self):
        with patch("agent_forge_service.dashboard.store_dashboards", side_effect=RuntimeError("aggregation failed")):
            with self.assertRaisesRegex(RuntimeError, "aggregation failed"):
                self.build("two")
        self.assertEqual("one", QueryService(self.settings).dashboard()["revision"])
        self.assertFalse((self.settings.snapshots / "two").exists())

    def test_python_aggregation_obeys_deadline(self):
        with closing(sqlite3.connect(self.settings.snapshots / "one/catalog.db")) as connection:
            with self.assertRaises(ServiceError) as caught:
                aggregate_dashboard(connection, deadline=0)
        self.assertEqual("query_timeout", caught.exception.code)

    def test_empty_catalog_returns_real_zero_counts(self):
        for path in self.packages.iterdir():
            path.unlink()
        self.index = {}
        self.build("empty")
        data = QueryService(self.settings).dashboard()
        self.assertEqual({"packages": 0, "versions": 0, "agents": 0, "bundles": 0}, data["totals"])
        self.assertEqual([], data["recentPackages"])
        self.assertEqual([], data["facets"])

    def test_unknown_latest_does_not_claim_known_compatibility(self):
        self.index["alpha"] = {"latest": "missing-version"}
        self.build("unknown")
        data = QueryService(self.settings).dashboard(agent="dsh")
        self.assertEqual(0, data["compatibility"]["known"])
        self.assertEqual(1, data["compatibility"]["unknown"])
        self.assertIsNone(data["recentPackages"][0]["canonicalLatest"])

    def test_facets_and_recent_rows_are_bounded_and_ties_are_stable(self):
        for number in range(25):
            name = "extra-%02d" % number
            value = record(name)
            value["customFacets"] = {"other": ["tag-%02d" % number]}
            self.write(value)
            self.index[name] = {"latest": "1.0.0"}
        self.build("many")
        data = QueryService(self.settings).dashboard()
        self.assertEqual(20, len(data["customFacets"]))
        self.assertEqual(10, len(data["recentPackages"]))
        ids = [item["id"] for item in data["recentPackages"]]
        self.assertEqual(sorted(ids), ids)
        tied = [(item["kind"], item["value"]) for item in data["customFacets"] if item["count"] == 1]
        self.assertEqual(sorted(tied), tied)

    def test_python_scan_checks_deadline_between_rows(self):
        with closing(sqlite3.connect(self.settings.snapshots / "one/catalog.db")) as connection:
            with patch("agent_forge_service.dashboard.time.monotonic", side_effect=(0, 2)):
                with self.assertRaises(ServiceError) as caught:
                    aggregate_dashboard(connection, deadline=1)
        self.assertEqual("query_timeout", caught.exception.code)

    def test_explicit_missing_revision_is_not_current_fallback(self):
        response = self.get(revision="retired")
        self.assertEqual(410, response.status)
        self.assertEqual("revision_unavailable", json.loads(response.body)["error"]["code"])

    def test_semantic_validation_failure_never_publishes_revision(self):
        with patch("agent_forge_service.bundles._verify_snapshot", side_effect=ValueError("semantic validation failed")):
            with self.assertRaisesRegex(ValueError, "semantic validation failed"):
                self.build("invalid")
        self.assertFalse((self.settings.snapshots / "invalid").exists())
        self.assertEqual("one", QueryService(self.settings).dashboard()["revision"])
