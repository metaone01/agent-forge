"""Public, rebuildable repository observations over exact published canonical mappings."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import timedelta

from .metrics import MetricSample, WindowPolicy, rate_between
from .storage import encode_json, timestamp

STAR_POLICY = WindowPolicy("github-repository-star-growth", "pilot-24h-v1", 24, 8, 8, 26)
OBSERVATION_POLICY = "github-repository-observations-v1"


def create_observation_schema(connection):
    connection.executescript("""
        CREATE TABLE package_repository_links (
            package_id TEXT NOT NULL REFERENCES packages(id), github_id INTEGER NOT NULL,
            evidence_json TEXT NOT NULL, PRIMARY KEY(package_id,github_id)
        );
        CREATE INDEX package_repository_identity ON package_repository_links(github_id,package_id);
        CREATE TABLE repository_observations (
            github_id INTEGER PRIMARY KEY, observation_id TEXT NOT NULL,
            observed_at TEXT NOT NULL, checked_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL, evidence_hash TEXT NOT NULL
        );
        CREATE TABLE repository_metric_samples (
            id TEXT PRIMARY KEY, github_id INTEGER NOT NULL REFERENCES repository_observations(github_id),
            observed_at TEXT NOT NULL, value INTEGER, sample_json TEXT NOT NULL, evidence_hash TEXT NOT NULL
        );
        CREATE INDEX repository_metric_history ON repository_metric_samples(github_id,observed_at,id);
        CREATE TABLE repository_signals (
            github_id INTEGER PRIMARY KEY REFERENCES repository_observations(github_id),
            stars_current INTEGER, stars_rank_value INTEGER, growth_per_hour REAL, pushed_at TEXT,
            checked_at TEXT NOT NULL, star_observed_at TEXT, freshness_json TEXT NOT NULL, growth_json TEXT NOT NULL
        );
        CREATE INDEX repository_stars_rank ON repository_signals(stars_rank_value DESC,github_id);
        CREATE INDEX repository_growth_rank ON repository_signals(growth_per_hour DESC,github_id);
        CREATE TABLE package_sort_keys (
            package_id TEXT PRIMARY KEY REFERENCES packages(id),
            stars_value INTEGER, growth_value REAL, pushed_at TEXT
        );
        CREATE INDEX package_stars_rank ON package_sort_keys(stars_value DESC,package_id);
        CREATE INDEX package_star_growth_rank ON package_sort_keys(growth_value DESC,package_id);
        CREATE INDEX package_repo_pushed_rank ON package_sort_keys(pushed_at DESC,package_id);
    """)


def _time(value):
    from datetime import datetime
    return datetime.fromisoformat(timestamp(value).replace("Z", "+00:00"))


def _sample(row, repo_id):
    data = json.loads(row["sample_json"])
    sample = MetricSample(**data)
    expected = ("github.com", "repository", str(repo_id), "stars", "stars",
                f"github-repository:{repo_id}:stargazers", "github_repository", "cumulative")
    if sample.identity != expected or timestamp(sample.observed_at) != row["observed_at"] or sample.value != row["value"]:
        raise ValueError("public repository metric identity or value mismatch")
    return sample


def _growth(samples, *, policy, as_of):
    if not samples:
        return {"status": "unknown", "reason": "not_reported", "valuePerHour": None,
                "qualification": {"eligible": False, "policyId": policy.policy_id, "policyVersion": policy.version}}
    current = samples[-1][1]
    current_time = _time(current.measurement_at or current.observed_at)
    target = current_time - timedelta(hours=policy.target_hours)
    last_reset = max((index for index, pair in enumerate(samples[:-1]) if pair[1].discontinuity), default=0)
    older = [pair for pair in samples[last_reset:-1] if _time(pair[1].observed_at) < _time(current.observed_at)]
    previous = min(older, key=lambda pair: (abs((_time(pair[1].measurement_at or pair[1].observed_at) - target).total_seconds()), -_time(pair[1].observed_at).timestamp(), pair[0])) if older else None
    growth = rate_between(previous[1] if previous else None, current)
    qualification = policy.qualification(growth, as_of=as_of)
    return {**asdict(growth), "valuePerHour": growth.value_per_hour, "qualification": qualification,
            "baselineSampleId": previous[0] if previous else None, "latestSampleId": samples[-1][0],
            "sharedRepositorySignal": True, "interpretation": "repository_star_net_growth_not_plugin_users"}


def project_observations(state, snapshot, *, as_of, policy=STAR_POLICY):
    if not isinstance(policy, WindowPolicy):
        raise ValueError("an explicit Star growth window policy is required")
    as_of = timestamp(as_of)
    chosen = {row["id"]: (row["canonical_latest"], row["payload_hash"]) for row in snapshot.execute(
        "SELECT p.id,p.canonical_latest,v.payload_hash FROM packages p JOIN package_versions v "
        "ON v.package_id=p.id AND v.version=p.canonical_latest")}
    linked, rejected_hash = {}, set()
    for row in state.execute(
        "SELECT l.package_id,l.version,l.github_id,l.evidence_json,r.canonical_hash FROM canonical_repository_links l "
        "JOIN canonical_mapping_records r ON r.package_id=l.package_id AND r.version=l.version "
        "WHERE l.active=1 AND r.active=1 AND l.resolution='resolved' AND l.github_id IS NOT NULL ORDER BY l.id"):
        selected = chosen.get(row["package_id"])
        if selected is None or row["version"] != selected[0]:
            continue
        if row["canonical_hash"] != selected[1]:
            rejected_hash.add(row["package_id"])
            continue
        proof = json.loads(row["evidence_json"])
        if proof.get("canonicalHash") != selected[1]:
            raise ValueError("canonical repository mapping evidence is inconsistent")
        linked.setdefault(row["package_id"], {}).setdefault(row["github_id"], []).append(
            {key: proof[key] for key in ("field", "url", "canonicalHash") if key in proof})
    for package_id, repos in linked.items():
        snapshot.executemany("INSERT INTO package_repository_links VALUES (?,?,?)",
                             ((package_id, repo_id, encode_json(evidence)) for repo_id, evidence in repos.items()))
    known = set()
    for repo_id in sorted({repo_id for repos in linked.values() for repo_id in repos}):
        row = state.execute("SELECT * FROM repository_observations WHERE github_id=? AND checked_at<=? AND observed_at<=? "
                            "ORDER BY checked_at DESC,id DESC LIMIT 1", (repo_id, as_of, as_of)).fetchone()
        if row is None:
            continue
        metadata = {key: value for key, value in json.loads(row["metadata_json"]).items()
                    if key in {"githubRepoId", "fullName", "url", "topics", "archived", "disabled", "defaultBranch",
                               "createdAt", "updatedAt", "pushedAt", "stars", "forks"}}
        if metadata.get("githubRepoId") != repo_id:
            raise ValueError("repository observation identity mismatch")
        snapshot.execute("INSERT INTO repository_observations VALUES (?,?,?,?,?,?)", (repo_id, row["id"], row["observed_at"], row["checked_at"], encode_json(metadata), row["evidence_hash"]))
        # Only successful observations of this repository are valid numeric evidence.
        rows = state.execute(
            "SELECT m.id,m.observed_at,json_extract(m.sample_json,'$.value') AS value,m.sample_json,o.evidence_hash "
            "FROM metric_observations m JOIN repository_observations o ON o.id=m.observation_id "
            "WHERE o.github_id=? AND m.subject_kind='repository' AND m.subject_id=? AND m.metric='stars' "
            "AND m.observed_at<=? AND o.checked_at<=? ORDER BY m.observed_at,m.id", (repo_id, str(repo_id), as_of, as_of)).fetchall()
        samples = []
        for sample_row in rows:
            sample = _sample(sample_row, repo_id)
            snapshot.execute("INSERT INTO repository_metric_samples VALUES (?,?,?,?,?,?)", (sample_row["id"], repo_id, sample_row["observed_at"], sample.value, encode_json(asdict(sample)), sample_row["evidence_hash"]))
            samples.append((sample_row["id"], sample))
        growth = _growth(samples, policy=policy, as_of=as_of)
        latest = samples[-1][1] if samples else None
        age = (_time(as_of) - _time(row["checked_at"])).total_seconds() / 3600
        stale = age > policy.maximum_age_hours
        freshness = {"lastSuccessAt": row["checked_at"], "observedAt": row["observed_at"], "ageHours": age,
                     "stale": stale, "reason": "stale" if stale else None, "scope": "repository_metadata_only", "asOf": as_of}
        snapshot.execute("INSERT INTO repository_signals VALUES (?,?,?,?,?,?,?,?,?)", (repo_id,
            latest.value if latest and latest.availability == "available" else None,
            latest.value if latest and latest.availability == "available" and not stale else None,
            growth["valuePerHour"] if growth["qualification"]["eligible"] else None, metadata.get("pushedAt"),
            row["checked_at"], latest.observed_at if latest else None, encode_json(freshness), encode_json(growth)))
        known.add(repo_id)
    statuses = {}
    for row in snapshot.execute("SELECT id,canonical_latest FROM packages"):
        repos = linked.get(row["id"], {})
        identity = next(iter(repos)) if len(repos) == 1 else None
        status = ("selection_unknown" if row["canonical_latest"] is None else "mapping_stale" if row["id"] in rejected_hash
                  else "ambiguous_repositories" if len(repos) > 1 else "unmapped" if not repos
                  else "mapped" if identity in known else "not_collected")
        if status == "mapping_stale":
            identity = None
        snapshot.execute("UPDATE packages SET repository_id=?,repository_mapping_status=? WHERE id=?", (identity, status, row["id"]))
        statuses[status] = statuses.get(status, 0) + 1
    # Explicitly rebuildable sort cache; source counts remain shared repository facts above.
    snapshot.execute("INSERT INTO package_sort_keys SELECT p.id,s.stars_rank_value,s.growth_per_hour,s.pushed_at "
                     "FROM packages p LEFT JOIN repository_signals s ON s.github_id=p.repository_id")
    covered = statuses.get("mapped", 0)
    fresh = snapshot.execute("SELECT count(*) FROM packages p JOIN repository_signals s ON s.github_id=p.repository_id WHERE json_extract(s.freshness_json,'$.stale')=0").fetchone()[0]
    eligible = snapshot.execute("SELECT count(*) FROM packages p JOIN repository_signals s ON s.github_id=p.repository_id WHERE s.growth_per_hour IS NOT NULL").fetchone()[0]
    snapshot.execute("INSERT INTO sources VALUES (?,?)", ("github.com:repositories", encode_json({
        "id": "github.com:repositories", "kind": "repository_observations", "sourceId": "github.com",
        "status": "partial" if known else "not_collected", "mappedPackages": covered, "freshPackages": fresh,
        "repositories": len(known), "observationCutoff": as_of,
        "upstreamLastSuccessAt": snapshot.execute("SELECT max(checked_at) FROM repository_observations").fetchone()[0],
        "disclaimer": "仅明确映射仓库的公开观察；不是插件安全或兼容认证，也不保证全目录覆盖"})))
    return {"policyVersion": OBSERVATION_POLICY, "asOf": as_of, "starWindowPolicy": asdict(policy),
            "packages": snapshot.execute("SELECT count(*) FROM packages").fetchone()[0], "mappedPackages": covered, "freshPackages": fresh, "eligibleStarGrowthPackages": eligible,
            "sharedRepositories": len(known), "oldestMappedSuccessAt": snapshot.execute("SELECT min(s.checked_at) FROM packages p JOIN repository_signals s ON s.github_id=p.repository_id").fetchone()[0], "mappingStatuses": statuses, "coverage": "explicit_selected_version_github_links_only"}


def published_star_rankings(store, *, now):
    """Rank shared repositories from a verified public revision, never live worker counts."""
    from dataclasses import fields
    from .metrics import Growth
    from .scheduling import RankedResource
    from .storage import read_snapshot
    if not (store.root / "current.json").is_file():
        return None
    manifest = store.current()
    if _time(manifest["generatedAt"]) > _time(now):
        raise ValueError("ranking snapshot is in the future")
    policy = WindowPolicy(**manifest["observationCoverage"]["starWindowPolicy"])
    connection = read_snapshot(store.database(manifest["revision"]))
    try:
        rows = connection.execute(
            "SELECT p.id AS package_id,s.github_id,s.growth_json,a.agent,p.type FROM packages p "
            "JOIN package_agents a ON a.package_id=p.id JOIN repository_signals s ON s.github_id=p.repository_id "
            "WHERE s.growth_per_hour IS NOT NULL ORDER BY a.agent,p.type,s.growth_per_hour DESC,p.id LIMIT 100001").fetchall()
    finally:
        connection.close()
    if len(rows) > 100000:
        raise ValueError("published package ranking memberships exceed the planning bound")
    groups = {}
    for row in rows:
        data = json.loads(row["growth_json"])
        growth = Growth(**{field.name: data[field.name] for field in fields(Growth)})
        if policy.qualification(growth, as_of=timestamp(now))["eligible"]:
            groups.setdefault((row["agent"], row["type"]), []).append((row["package_id"], row["github_id"], growth.value_per_hour))
    metric_identity = encode_json(["github.com", "repository", "stars", "stars/hour", "github_repository", policy.policy_id, policy.version])
    result = []
    for (agent, category), items in sorted(groups.items()):
        # Top N belongs to the published package board. Deduplicate requests afterwards,
        # preserving each repository's earliest package rank (including rank gaps).
        repository_ranks = {}
        for rank, (_, repo_id, _) in enumerate(sorted(items, key=lambda item: (-item[2], item[0])), 1):
            repository_ranks.setdefault(repo_id, rank)
        for repo_id, rank in repository_ranks.items():
            result.append(RankedResource("github-repository:" + str(repo_id), agent, category, metric_identity, rank, manifest["revision"]))
            if len(result) > 100000:
                raise ValueError("published ranking membership exceeds the planning bound")
    return tuple(result), manifest["revision"], "published_snapshot" if result else "published_snapshot_no_eligible_samples"
