"""Public discovery facts reconstructed from dated evidence, never canonical adoption."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from itertools import chain
from urllib.parse import quote, urlencode

from .collector import normalize_repository
from .github import GitHubRequest
from .storage import encode_json, timestamp

POLICY = "discovery-evidence-v1"
MAX_ITEM_BYTES = 256 * 1024
MAX_SOURCES = 20
STALE_SECONDS = 86400
COLUMNS = {
    "discoveries": {"id", "kind", "name", "display_name", "summary", "type", "subtype", "first_seen_at", "last_seen_at", "keywords_json", "search_text", "data_json"},
    "discovery_agents": {"discovery_id", "agent"},
    "discovery_facets": {"discovery_id", "kind", "value", "custom"},
    "discoveries_fts": {"name", "display_name", "summary", "search_text"},
}
REPOSITORY_FIELDS = {"githubRepoId", "fullName", "url", "topics", "archived", "disabled", "defaultBranch", "createdAt", "updatedAt", "pushedAt", "stars", "forks"}
ITEM_FIELDS = {"id", "scope", "kind", "name", "displayName", "summary", "type", "subtype", "firstSeenAt", "lastSeenAt", "agents", "keywords", "facets", "customFacets", "classification", "reviewStatus", "repository", "declaredMetadata", "issues", "sourceEvidence", "sourceCount", "evidenceTruncated", "manifestEvidence", "lifecycle"}
ISSUE_CODES = {"missing_or_invalid_declaration", "canonical_identity_conflict", "upstream_profile_invalid", "skill_directory_name_mismatch", "schema_invalid", "identity_invalid"}
MANIFEST_STATES = {"metadata_found", "normalized", "schema_valid", "needs_metadata", "identity_conflict", "review_pending"}


def create_schema(connection, *, trigram):
    connection.executescript("""
        CREATE TABLE discoveries (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, display_name TEXT,
            summary TEXT NOT NULL, type TEXT, subtype TEXT, first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL, keywords_json TEXT NOT NULL, search_text TEXT NOT NULL,
            data_json TEXT NOT NULL
        );
        CREATE INDEX discoveries_first_seen ON discoveries(first_seen_at DESC,id);
        CREATE INDEX discoveries_name ON discoveries(name,id);
        CREATE TABLE discovery_agents (discovery_id TEXT NOT NULL REFERENCES discoveries(id), agent TEXT NOT NULL,
            PRIMARY KEY(discovery_id,agent));
        CREATE INDEX discovery_agents_lookup ON discovery_agents(agent,discovery_id);
        CREATE TABLE discovery_facets (discovery_id TEXT NOT NULL REFERENCES discoveries(id), kind TEXT NOT NULL,
            value TEXT NOT NULL, custom INTEGER NOT NULL, PRIMARY KEY(discovery_id,kind,value,custom));
        CREATE INDEX discovery_facets_lookup ON discovery_facets(custom,kind,value,discovery_id);
        CREATE VIRTUAL TABLE discoveries_fts USING fts5(name,display_name,summary,search_text,
            content='discoveries',content_rowid='rowid',tokenize='unicode61');
    """)
    if trigram:
        connection.execute("CREATE VIRTUAL TABLE discoveries_trigram USING fts5(name,display_name,summary,search_text,content='discoveries',content_rowid='rowid',tokenize='trigram')")


def _source_id(topic, field):
    return "github-search:" + hashlib.sha256(encode_json([topic, field]).encode()).hexdigest()


def _date(value, as_of):
    if not isinstance(value, str) or timestamp(value) != value or value > as_of:
        raise ValueError("discovery evidence timestamp is invalid or after publication")
    return value


def _string(value, maximum=4096, *, allow_controls=False):
    if not isinstance(value, str) or len(value) > maximum or not allow_controls and any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ValueError("invalid public discovery text")
    return value


def _public_text(value, maximum):
    return re.sub(r"[\x00-\x1f\x7f]", " ", value or "")[:maximum]


def _sha(value, length=64):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value):
        raise ValueError("invalid discovery evidence hash")
    return value


def _search_evidence(row):
    request = GitHubRequest.search(topic=row["topic"], field=row["time_field"],
        start=row["window_start"], end=row["window_end"], page=row["page"]).validate()
    return {"sourceId": _source_id(row["topic"], row["time_field"]), "kind": "github_search",
        "url": "https://api.github.com/" + request.endpoint + "?" + urlencode(request.params),
        "evidenceHash": row["evidence_hash"], "observedAt": row["observed_at"], "checkedAt": row["checked_at"],
        "topic": row["topic"], "timeField": row["time_field"], "windowStart": row["window_start"],
        "windowEnd": row["window_end"], "page": row["page"], "incomplete": bool(row["incomplete"]),
        "totalCount": row["total_count"], "resultCount": row["result_count"]}


def _base(identity, kind, name, first, last, repository):
    return {"id": identity, "scope": "discovery", "kind": kind, "name": name,
        "displayName": None, "summary": "", "type": None, "subtype": None,
        "firstSeenAt": first, "lastSeenAt": last, "agents": [], "keywords": [],
        "facets": {}, "customFacets": {}, "classification": "discovered",
        "reviewStatus": "unreviewed", "repository": repository, "declaredMetadata": None,
        "issues": [], "sourceEvidence": [], "sourceCount": 0, "evidenceTruncated": False,
        "manifestEvidence": None, "lifecycle": [{"state": "discovered", "observedAt": first}]}


def _repositories(state, as_of, *, repository_id=None):
    # Mutable candidate rows may contain a later rename; read the dated search bodies instead.
    sql = """SELECT p.*,r.topic,r.time_field,s.window_start,s.window_end,t.page,e.body_json
        FROM discovery_pages p JOIN discovery_tasks t ON t.job_id=p.job_id
        JOIN discovery_runs r ON r.id=t.run_id JOIN discovery_shards s ON s.id=t.shard_id
        JOIN collection_evidence e ON e.hash=p.evidence_hash
        WHERE p.checked_at<=? AND p.observed_at<=? AND r.created_at<=?
        ORDER BY p.checked_at,p.observed_at,p.job_id"""
    if repository_id is not None:
        sql = sql.replace("ORDER BY p.checked_at,p.observed_at,p.job_id", "AND EXISTS(SELECT 1 FROM discovery_page_hits h WHERE h.job_id=p.job_id AND h.github_id=?) ORDER BY p.checked_at,p.observed_at,p.job_id")
    items = {}
    for row in state.execute(sql, (as_of, as_of, as_of) + ((repository_id,) if repository_id is not None else ())):
        body = json.loads(row["body_json"])
        if hashlib.sha256(encode_json(body).encode()).hexdigest() != row["evidence_hash"]:
            raise ValueError("discovery body differs from its evidence hash")
        allowed = {hit[0] for hit in state.execute("SELECT github_id FROM discovery_page_hits WHERE job_id=?", (row["job_id"],))}
        for raw in body.get("items", []):
            if raw.get("id") not in allowed or repository_id is not None and raw.get("id") != repository_id:
                continue
            metadata = normalize_repository(raw)
            identity = metadata["githubRepoId"]
            if identity not in items:
                items[identity] = {"first": row["observed_at"], "sources": {}}
            known = items[identity]
            known.update(metadata=metadata, last=row["checked_at"])
            known["first"] = min(known["first"], row["observed_at"])
            evidence = _search_evidence(row)
            known["sources"][evidence["sourceId"]] = evidence
    for identity, known in sorted(items.items()):
        metadata = known["metadata"]
        item = _base("github-repository:" + str(identity), "repository", metadata["fullName"],
            known["first"], known["last"], metadata)
        item["keywords"] = metadata["topics"] or []
        evidence = [known["sources"][key] for key in sorted(known["sources"])]
        item.update(sourceEvidence=evidence[:MAX_SOURCES], sourceCount=len(evidence), evidenceTruncated=len(evidence) > MAX_SOURCES)
        yield item


def _manifests(state, as_of, *, candidate_id=None):
    sql = "SELECT * FROM manifest_candidates WHERE created_at<=?" + (" AND id=?" if candidate_id is not None else "") + " ORDER BY id"
    for row in state.execute(sql, (as_of,) + ((candidate_id,) if candidate_id is not None else ())):
        record, evidence = json.loads(row["record_json"]), json.loads(row["evidence_json"])
        full_name = evidence["repositoryNameAtRead"]
        GitHubRequest.repository(full_name).validate()
        from .declarations import declaration_path, declaration_association
        declaration_path(row["path"], row["format"])
        declaration_association(row["format"], row["agent"])
        _sha(row["commit_sha"], 40); _sha(row["blob_sha"], 40); _sha(row["content_hash"])
        # A pinned declaration identifies the profile; it never establishes compatibility.
        repository = {key: None for key in REPOSITORY_FIELDS}
        repository.update(githubRepoId=row["github_id"], fullName=full_name, url="https://github.com/" + full_name, topics=[])
        first = row["created_at"]
        item = _base("manifest:" + row["id"], "manifest", _public_text(record.get("name"), 4096).strip() or full_name + "/" + row["path"], first, first, repository)
        item["type"] = "plugin" if row["format"] == "claude_plugin" else "skill"
        item["agents"] = [row["agent"]]
        item["declaredMetadata"] = {key: record.get(key) for key in ("name", "description", "version", "license")}
        item["summary"] = _public_text(record.get("description"), 400)
        item["issues"] = [{key: value for key, value in issue.items() if key in {"code", "field", "path", "validator", "canonicalId"}}
            for issue in json.loads(row["issues_json"])]
        item["lifecycle"] = [dict(state=event["state"], observedAt=event["observed_at"]) for event in state.execute(
            "SELECT state,observed_at FROM manifest_candidate_events WHERE candidate_id=? AND observed_at<=? ORDER BY observed_at,state", (row["id"], as_of))]
        terminal = [event for event in item["lifecycle"] if event["state"] in {"needs_metadata", "identity_conflict", "review_pending"}]
        if not terminal:
            raise ValueError("manifest candidate has no dated review state")
        item["reviewStatus"] = max(terminal, key=lambda event: (event["observedAt"], event["state"]))["state"]
        item["classification"] = "declared_profile"
        url = "https://github.com/" + full_name + "/blob/" + row["commit_sha"] + "/" + quote(row["path"], safe="/")
        item["manifestEvidence"] = {"path": row["path"], "format": row["format"], "agentAssociation": row["agent"],
            "commitSha": row["commit_sha"], "blobSha": row["blob_sha"], "contentSha256": row["content_hash"],
            "url": url, "association": "explicit-profile-not-compatibility-verification"}
        # Content hashes describe declaration bytes; search evidence hashes describe API bodies.
        item["sourceEvidence"] = [{"sourceId": "github.com", "kind": "manifest_declaration", "url": url,
            "evidenceHash": row["content_hash"], "observedAt": first, "checkedAt": first}]
        item["sourceCount"] = 1
        yield item


def _source_health(state, connection, as_of):
    groups = {}
    for row in state.execute("SELECT * FROM discovery_runs WHERE created_at<=? ORDER BY created_at,id", (as_of,)):
        identity = _source_id(row["topic"], row["time_field"])
        group = groups.setdefault(identity, {"latest": None, "success": None, "observed": None})
        group["latest"] = row
        if row["state"] == "complete" and row["finished_at"] and row["finished_at"] <= as_of:
            group["success"] = max(group["success"] or row["finished_at"], row["finished_at"])
        observed = state.execute("SELECT max(p.checked_at) FROM discovery_pages p JOIN discovery_tasks t ON t.job_id=p.job_id WHERE t.run_id=? AND p.checked_at<=? AND p.observed_at<=?", (row["id"], as_of, as_of)).fetchone()[0]
        if observed:
            group["observed"] = max(group["observed"] or observed, observed)
    for identity, group in sorted(groups.items()):
        row = group["latest"]
        complete = row["state"] == "complete" and row["finished_at"] is not None and row["finished_at"] <= as_of
        data = {"id": identity, "kind": "github_search", "provider": "github.com", "topic": row["topic"],
            "timeField": row["time_field"], "windowStart": row["window_start"], "windowEnd": row["window_end"],
            "lastSuccessAt": group["success"], "lastObservationAt": group["observed"],
            "status": "complete" if complete else "partial" if group["observed"] else "pending",
            "partial": not complete, "asOf": as_of, "staleAfterSeconds": STALE_SECONDS,
            "coverage": "query_window_only_not_global"}
        connection.execute("INSERT INTO sources VALUES (?,?)", (identity, encode_json(data)))
    return len(groups)


def _row_values(item):
    search = " ".join(item["keywords"])
    return (item["id"], item["kind"], item["name"], item["displayName"], item["summary"], item["type"], item["subtype"],
        item["firstSeenAt"], item["lastSeenAt"], encode_json(item["keywords"]), search, encode_json(item))


def project_discoveries(state, connection, *, as_of, trigram):
    create_schema(connection, trigram=trigram)
    from .candidate_review_projection import project_reviews,summary
    review_data=project_reviews(state,connection,as_of=as_of)
    for item in chain(_repositories(state, as_of), _manifests(state, as_of)):
        if item["kind"] == "repository" and connection.execute("SELECT 1 FROM packages WHERE repository_id=? LIMIT 1", (item["repository"]["githubRepoId"],)).fetchone():
            continue
        if item["kind"] == "manifest":
            declared_id = state.execute("SELECT record_json FROM manifest_candidates WHERE id=?", (item["id"].removeprefix("manifest:"),)).fetchone()[0]
            if connection.execute("SELECT 1 FROM packages WHERE id=?", (json.loads(declared_id)["id"],)).fetchone():
                continue
        if review_data is not None:item["metadataReview"]=summary(connection,item)
        validate_item(item, as_of)
        connection.execute("INSERT INTO discoveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", _row_values(item))
        connection.executemany("INSERT INTO discovery_agents VALUES (?,?)", ((item["id"], agent) for agent in item["agents"]))
    connection.execute("INSERT INTO discoveries_fts(discoveries_fts) VALUES ('rebuild')")
    if trigram:
        connection.execute("INSERT INTO discoveries_trigram(discoveries_trigram) VALUES ('rebuild')")
    sources = _source_health(state, connection, as_of)
    return coverage(connection, as_of, sources)


def coverage(connection, as_of, sources):
    kinds = {row[0]: row[1] for row in connection.execute("SELECT kind,count(*) FROM discoveries GROUP BY kind")}
    bounds = connection.execute("SELECT min(first_seen_at),max(last_seen_at),min(last_seen_at) FROM discoveries").fetchone()
    return {"policyVersion": POLICY, "asOf": as_of, "repositories": kinds.get("repository", 0),
        "manifests": kinds.get("manifest", 0), "sources": sources, "firstSeenAt": bounds[0], "lastObservationAt": bounds[1],
        "coverage": "not_guaranteed", "adoption": "review_required", "staleAfterSeconds": STALE_SECONDS,
        "oldestObservationAt": bounds[2]}


def validate_item(item, as_of):
    try:
        _validate_item(item, as_of)
    except (AttributeError, KeyError, TypeError, RecursionError, OverflowError) as error:
        raise ValueError("invalid public discovery evidence shape") from error


def _validate_item(item, as_of):
    if not isinstance(item, dict) or set(item) not in (ITEM_FIELDS,ITEM_FIELDS | {"metadataReview"}) or len(encode_json(item).encode()) > MAX_ITEM_BYTES:
        raise ValueError("public discovery item shape/byte budget is invalid")
    if "metadataReview" in item:
        from .candidate_review_projection import validate_summary
        validate_summary(item["metadataReview"],item,as_of)
    if item["scope"] != "discovery" or item["kind"] not in ("repository", "manifest"):
        raise ValueError("invalid discovery scope/kind")
    _string(item["id"]); _string(item["name"])
    if not item["name"] or item["displayName"] is not None or item["subtype"] is not None:
        raise ValueError("unsupported discovery naming fields")
    _string(item["summary"], 400)
    first, last = _date(item["firstSeenAt"], as_of), _date(item["lastSeenAt"], as_of)
    if first > last or item["facets"] != {} or item["customFacets"] != {}:
        raise ValueError("invalid discovery dates/facets")
    repo = item["repository"]
    if not isinstance(repo, dict) or set(repo) != REPOSITORY_FIELDS:
        raise ValueError("invalid public discovery repository fields")
    GitHubRequest.repository(repo["fullName"]).validate(); GitHubRequest.repository(repo["githubRepoId"]).validate()
    if type(repo["githubRepoId"]) is not int or repo["url"] != "https://github.com/" + repo["fullName"]:
        raise ValueError("invalid public discovery repository identity")
    for key in ("archived", "disabled"):
        if repo[key] is not None and type(repo[key]) is not bool:
            raise ValueError("invalid discovery repository flag")
    for key in ("stars", "forks"):
        if repo[key] is not None and (type(repo[key]) is not int or not 0 <= repo[key] <= 2**63 - 1):
            raise ValueError("invalid discovery repository counter")
    for key in ("createdAt", "updatedAt", "pushedAt"):
        if repo[key] is not None and timestamp(repo[key]) != repo[key]:
            raise ValueError("invalid repository timestamp")
    if repo["defaultBranch"] is not None:
        _string(repo["defaultBranch"], 255)
    for values in (item["keywords"], repo["topics"] or []):
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError("invalid discovery keywords")
        for value in values:
            _string(value, 50)
    evidence = item["sourceEvidence"]
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_SOURCES or type(item["sourceCount"]) is not int or item["sourceCount"] < len(evidence) or type(item["evidenceTruncated"]) is not bool or item["evidenceTruncated"] != (item["sourceCount"] > len(evidence)):
        raise ValueError("invalid discovery source evidence coverage")
    if len({entry["sourceId"] for entry in evidence}) != len(evidence):
        raise ValueError("duplicate discovery source identity")
    for entry in evidence:
        _sha(entry["evidenceHash"])
        observed, checked = _date(entry["observedAt"], as_of), _date(entry["checkedAt"], as_of)
        if observed > checked or checked > last or observed < first:
            raise ValueError("discovery source chronology mismatch")
        if item["kind"] == "repository":
            required = {"sourceId", "kind", "url", "evidenceHash", "observedAt", "checkedAt", "topic", "timeField", "windowStart", "windowEnd", "page", "incomplete", "totalCount", "resultCount"}
            if set(entry) != required or entry["kind"] != "github_search":
                raise ValueError("invalid public search evidence")
            request = GitHubRequest.search(topic=entry["topic"], field=entry["timeField"], start=entry["windowStart"], end=entry["windowEnd"], page=entry["page"]).validate()
            if entry["sourceId"] != _source_id(entry["topic"], entry["timeField"]) or entry["url"] != "https://api.github.com/" + request.endpoint + "?" + urlencode(request.params):
                raise ValueError("search evidence identity/URL mismatch")
            if type(entry["incomplete"]) is not bool or type(entry["resultCount"]) is not int or not 0 <= entry["resultCount"] <= 100 or type(entry["totalCount"]) is not int or not entry["resultCount"] <= entry["totalCount"] <= 2**63 - 1:
                raise ValueError("invalid public search counts")
        elif set(entry) != {"sourceId", "kind", "url", "evidenceHash", "observedAt", "checkedAt"} or entry["sourceId"] != "github.com" or entry["kind"] != "manifest_declaration":
            raise ValueError("invalid public declaration evidence")
    if item["kind"] == "repository":
        if item["id"] != "github-repository:" + str(repo["githubRepoId"]) or item["name"] != repo["fullName"] or item["agents"] != [] or item["type"] is not None or item["declaredMetadata"] is not None or item["manifestEvidence"] is not None or item["issues"] != [] or item["reviewStatus"] != "unreviewed" or item["classification"] != "discovered" or item["keywords"] != (repo["topics"] or []) or item["lifecycle"] != [{"state": "discovered", "observedAt": first}]:
            raise ValueError("repository search cannot imply a package/profile")
    else:
        manifest = item["manifestEvidence"]
        if not isinstance(manifest, dict) or set(manifest) != {"path", "format", "agentAssociation", "commitSha", "blobSha", "contentSha256", "url", "association"}:
            raise ValueError("invalid public manifest evidence")
        from .declarations import declaration_path, declaration_association
        declaration_path(manifest["path"], manifest["format"]); declaration_association(manifest["format"], manifest["agentAssociation"])
        _sha(manifest["commitSha"], 40); _sha(manifest["blobSha"], 40); _sha(manifest["contentSha256"])
        identity = hashlib.sha256(encode_json(["manifest-candidate-v1", repo["githubRepoId"], manifest["path"], manifest["format"], manifest["agentAssociation"], manifest["commitSha"]]).encode()).hexdigest()
        url = "https://github.com/" + repo["fullName"] + "/blob/" + manifest["commitSha"] + "/" + quote(manifest["path"], safe="/")
        if item["id"] != "manifest:" + identity or manifest["url"] != url or manifest["association"] != "explicit-profile-not-compatibility-verification" or item["agents"] != [manifest["agentAssociation"]] or item["type"] != ("plugin" if manifest["format"] == "claude_plugin" else "skill") or item["classification"] != "declared_profile" or item["reviewStatus"] not in {"needs_metadata", "identity_conflict", "review_pending"}:
            raise ValueError("manifest identity/profile mismatch")
        if evidence != [{"sourceId": "github.com", "kind": "manifest_declaration", "url": url, "evidenceHash": manifest["contentSha256"], "observedAt": first, "checkedAt": first}] or first != last:
            raise ValueError("manifest source evidence mismatch")
        declared = item["declaredMetadata"]
        if not isinstance(declared, dict) or set(declared) != {"name", "description", "version", "license"}:
            raise ValueError("invalid declared metadata")
        for value in declared.values():
            if value is not None:
                _string(value, 128 * 1024, allow_controls=True)
        if item["name"] != (_public_text(declared["name"], 4096).strip() or repo["fullName"] + "/" + manifest["path"]) or item["summary"] != _public_text(declared["description"], 400) or item["keywords"] != []:
            raise ValueError("manifest summary differs from declared metadata")
        if not isinstance(item["issues"], list) or len(item["issues"]) > 33:
            raise ValueError("invalid manifest issues")
        for issue in item["issues"]:
            if not isinstance(issue, dict) or not {"code"} <= set(issue) <= {"code", "field", "path", "validator", "canonicalId"} or issue["code"] not in ISSUE_CODES:
                raise ValueError("invalid public manifest issue")
            for key in ("field", "validator", "canonicalId"):
                if key in issue:
                    _string(issue[key])
            if "path" in issue and (not isinstance(issue["path"], list) or len(issue["path"]) > 20 or any(type(part) not in (str, int) for part in issue["path"])):
                raise ValueError("invalid public manifest issue path")
        lifecycle = item["lifecycle"]
        if not isinstance(lifecycle, list) or not 1 <= len(lifecycle) <= len(MANIFEST_STATES) or len({event["state"] for event in lifecycle}) != len(lifecycle):
            raise ValueError("invalid manifest lifecycle")
        for event in lifecycle:
            if set(event) != {"state", "observedAt"} or event["state"] not in MANIFEST_STATES or not first <= _date(event["observedAt"], as_of):
                raise ValueError("invalid dated manifest lifecycle")
        terminal = [event for event in lifecycle if event["state"] in {"needs_metadata", "identity_conflict", "review_pending"}]
        if not terminal or item["reviewStatus"] != max(terminal, key=lambda event: (event["observedAt"], event["state"]))["state"]:
            raise ValueError("manifest review state lacks latest dated evidence")


def validate_projection(connection, manifest):
    reference = sqlite3.connect(":memory:")
    try:
        create_schema(reference, trigram=manifest["features"]["trigram"])
        expected_schema = {row[0]: row[1] for row in reference.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
        actual_schema = {row[0]: row[1] for row in connection.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
        if any(actual_schema.get(name) != sql for name, sql in expected_schema.items()) or any(name.startswith("discoveries_trigram") for name in actual_schema) and not manifest["features"]["trigram"]:
            raise ValueError("discovery SQL schema differs from the declared read model")
    finally:
        reference.close()
    as_of = manifest["generatedAt"]
    for row in connection.execute("SELECT * FROM discoveries ORDER BY id"):
        item = json.loads(row["data_json"])
        validate_item(item, as_of)
        if ("metadataReview" in item) != bool(manifest["features"].get("discoveryReviews")):
            raise ValueError("discovery review feature/DTO differ")
        if item["kind"] == "repository" and connection.execute("SELECT 1 FROM packages WHERE repository_id=? LIMIT 1", (item["repository"]["githubRepoId"],)).fetchone():
            raise ValueError("discovery repository already has an adopted canonical mapping")
        if item["kind"] == "manifest":
            canonical_id = item["type"] + ".github-" + str(item["repository"]["githubRepoId"]) + "/" + quote(item["manifestEvidence"]["path"], safe="._/-")
            if connection.execute("SELECT 1 FROM packages WHERE id=?", (canonical_id,)).fetchone():
                raise ValueError("discovery declaration already has an adopted canonical identity")
        if tuple(row) != _row_values(item):
            raise ValueError("discovery query columns differ from public evidence")
        agents = [value[0] for value in connection.execute("SELECT agent FROM discovery_agents WHERE discovery_id=? ORDER BY agent", (item["id"],))]
        if agents != sorted(item["agents"]):
            raise ValueError("discovery Agent associations differ from explicit declaration")
    if connection.execute("SELECT count(*) FROM discovery_facets").fetchone()[0] != 0:
        raise ValueError("unsupported inferred discovery facets")
    sources = 0
    for row in connection.execute("SELECT id,data_json FROM sources"):
        data = json.loads(row["data_json"])
        if data.get("kind") != "github_search":
            if row["id"].startswith("github-search:"):
                raise ValueError("discovery source provider differs from its identity")
            continue
        sources += 1
        if set(data) != {"id", "kind", "provider", "topic", "timeField", "windowStart", "windowEnd", "lastSuccessAt", "lastObservationAt", "status", "partial", "asOf", "staleAfterSeconds", "coverage"} or row["id"] != data["id"] or data["id"] != _source_id(data["topic"], data["timeField"]):
            raise ValueError("invalid public discovery source identity/fields")
        GitHubRequest.search(topic=data["topic"], field=data["timeField"], start=data["windowStart"], end=data["windowEnd"]).validate()
        if data["provider"] != "github.com" or data["asOf"] != as_of or data["status"] not in {"complete", "partial", "pending"} or type(data["partial"]) is not bool or data["partial"] != (data["status"] != "complete") or data["staleAfterSeconds"] != STALE_SECONDS or data["coverage"] != "query_window_only_not_global":
            raise ValueError("invalid discovery source status")
        for key in ("lastSuccessAt", "lastObservationAt"):
            if data[key] is not None:
                _date(data[key], as_of)
        if data["status"] == "complete" and data["lastSuccessAt"] is None:
            raise ValueError("complete discovery source lacks success evidence")
    expected = coverage(connection, as_of, sources)
    source_ids = {row[0] for row in connection.execute("SELECT id FROM sources WHERE id LIKE 'github-search:%'")}
    for row in connection.execute("SELECT data_json FROM discoveries WHERE kind='repository'"):
        item = json.loads(row[0])
        if any(evidence["sourceId"] not in source_ids for evidence in item["sourceEvidence"]) or item["sourceCount"] > len(source_ids):
            raise ValueError("discovery evidence source is absent from source status projection")
    if manifest.get("discoveryCoverage") != expected:
        raise ValueError("discovery coverage differs from public snapshot")
