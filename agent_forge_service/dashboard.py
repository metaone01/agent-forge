"""Neutral, bounded summaries built from the same immutable query revision."""

import json
import time

from .errors import ServiceError

TYPES = ("mcp", "plugin", "skill", "general", "bundle")
FACET_LIMIT = 20
RECENT_LIMIT = 10
MAX_SUMMARY_BYTES = 2 * 1024 * 1024
POLICY_VERSION = "canonical-dashboard-v1"


def dashboard_key(agent=None):
    return "dashboard:" + ("global" if agent is None else "agent:" + agent)


def aggregate_dashboard(connection, agent=None, *, deadline=None):
    """Build at publication; older snapshots may use this under the query deadline."""
    def check_deadline():
        if deadline is not None and time.monotonic() >= deadline:
            raise ServiceError("query_timeout", "dashboard exceeded its execution budget", 503)

    check_deadline()
    parameters = {} if agent is None else {"agent": agent}
    membership = "" if agent is None else "WHERE EXISTS (SELECT 1 FROM package_agents a WHERE a.package_id=p.id AND a.agent=:agent)"
    types = dict.fromkeys(TYPES, 0)
    for row in connection.execute("SELECT p.type,count(*) FROM packages p " + membership + " GROUP BY p.type", parameters):
        types[row[0]] = row[1]
    versions = connection.execute("SELECT count(*) FROM package_versions v JOIN packages p ON p.id=v.package_id " + membership, parameters).fetchone()[0]
    agent_rows = connection.execute("""SELECT a.agent,p.type,count(*) AS packages,sum(v.versions) AS versions
        FROM package_agents a JOIN packages p ON p.id=a.package_id
        JOIN (SELECT package_id,count(*) AS versions FROM package_versions GROUP BY package_id) v ON v.package_id=p.id
        """ + ("WHERE a.agent=:agent " if agent is not None else "") + "GROUP BY a.agent,p.type ORDER BY a.agent,p.type", parameters)
    agents = {}
    for row in agent_rows:
        summary = agents.setdefault(row[0], {"id": row[0], "packageCount": 0, "versionCount": 0, "types": dict.fromkeys(TYPES, 0)})
        summary["types"][row[1]] = row[2]
        summary["packageCount"] += row[2]
        summary["versionCount"] += row[3]
    facets = []
    for custom in (0, 1):
        rows = connection.execute("SELECT f.kind,f.value,count(*) AS count FROM package_facets f JOIN packages p ON p.id=f.package_id "
            + (membership + " AND " if membership else "WHERE ") + "f.custom=:custom GROUP BY f.kind,f.value ORDER BY count DESC,f.kind,f.value LIMIT :limit",
            {**parameters, "custom": custom, "limit": FACET_LIMIT})
        facets.append([{"kind": row[0], "value": row[1], "count": row[2]} for row in rows])
    known, unknown = 0, 0
    rows = connection.execute("SELECT a.agent,p.targets_json FROM package_agents a JOIN packages p ON p.id=a.package_id "
        + ("WHERE a.agent=:agent" if agent is not None else ""), parameters)
    for index, row in enumerate(rows):
        if index % 256 == 0:
            check_deadline()
        targets = [target for target in json.loads(row[1]) if target["agentId"] == row[0]]
        if targets and all(target.get("compatibilityStatus") == "known" and target.get("agentVersionRange") for target in targets):
            known += 1
        else:
            unknown += 1
    recent = [{"id": row[0], "name": row[1], "displayName": row[2], "type": row[3], "summary": row[4],
               "canonicalLatest": row[5], "canonicalUpdatedAt": row[6]} for row in connection.execute(
        "SELECT p.id,p.name,p.display_name,p.type,p.summary,p.canonical_latest,p.canonical_updated_at FROM packages p "
        + membership + " ORDER BY p.canonical_updated_at DESC,p.id LIMIT :limit", {**parameters, "limit": RECENT_LIMIT})]
    check_deadline()
    return {"scope": {"agent": agent}, "policyVersion": POLICY_VERSION,
            "totals": {"packages": sum(types.values()), "versions": versions, "agents": len(agents), "bundles": types["bundle"]},
            "types": types, "agents": list(agents.values()), "facets": facets[0], "customFacets": facets[1],
            "facetLimit": FACET_LIMIT, "recentLimit": RECENT_LIMIT,
            "compatibility": {"known": known, "unknown": unknown, "unit": "package-agent-declaration"},
            "recentPackages": recent,
            "countingPolicy": {"packages": "distinct-canonical-id", "agentCoverage": "declared-package-agent-membership",
                               "versions": "all-versions-of-associated-packages", "facets": "selected-canonical-declarations",
                               "compatibility": "selected-canonical-package-agent-declarations", "recent": "canonical-updated-at"}}


def store_dashboards(connection, agents):
    from .storage import encode_json
    connection.execute("CREATE TABLE dashboard_summaries (scope TEXT PRIMARY KEY,value TEXT NOT NULL)")
    for agent in (None, *agents):
        value = aggregate_dashboard(connection, agent)
        encoded = encode_json(value)
        if len(encoded.encode("utf-8")) > MAX_SUMMARY_BYTES:
            raise ValueError("dashboard summary exceeds its byte budget")
        connection.execute("INSERT INTO dashboard_summaries VALUES (?,?)", (dashboard_key(agent), encoded))
