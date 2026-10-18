"""Bounded read queries over immutable revisions, with filter-bound keyset cursors."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from .errors import ServiceError
from .publishing import SnapshotStore, validate_revision
from .settings import Settings
from .storage import encode_json, read_snapshot, timestamp, utc_now

FACET_KINDS = {"capabilities", "effects", "dataPractices", "permissions", "runtime", "integrations"}
TYPES = {"mcp", "plugin", "skill", "general", "bundle"}


def _bounded_string(value, label: str, maximum: int = 200):
    if value is not None and (not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value)):
        raise ServiceError("invalid_query", f"invalid {label}")
    return value


def _facets(value: dict, custom: bool) -> dict[str, list[str]]:
    if not isinstance(value, dict) or len(value) > 7 or any(not isinstance(key, str) for key in value):
        raise ServiceError("invalid_query", "invalid facets")
    output = {}
    allowed = FACET_KINDS | ({"other"} if custom else set())
    for kind, entries in sorted(value.items()):
        if kind not in allowed or not isinstance(entries, (list, tuple)) or not 1 <= len(entries) <= 10:
            raise ServiceError("invalid_query", "invalid facet kind or values")
        normalized = [_bounded_string(entry, "facet value") for entry in entries]
        if None in normalized:
            raise ServiceError("invalid_query", "null facet value")
        output[kind] = sorted(set(normalized))
    return output


@dataclass(frozen=True)
class Query:
    q: str | None = None
    agent: str | None = None
    type: str | None = None
    subtype: str | None = None
    scope: str = "catalog"
    sort: str | None = None
    limit: int = 20
    facets: dict = field(default_factory=dict)
    custom_facets: dict = field(default_factory=dict)
    metric_source: str | None = None
    recommendation_source: str | None = None
    review_disposition: str | None = None

    def normalized(self, settings: Settings) -> dict:
        if self.scope not in ("catalog", "discovery"):
            raise ServiceError("scope_unavailable", "unknown query scope")
        sort = self.sort if self.sort is not None else "first_seen" if self.scope == "discovery" else "canonical_updated"
        if self.scope == "discovery" and sort not in ("first_seen", "name", "relevance"):
            raise ServiceError("sort_unavailable", "discovery supports first_seen, name and relevance")
        if sort not in ("canonical_updated", "first_seen", "name", "relevance", "stars", "star_growth", "repo_pushed", "downloads", "download_growth", "latest_release"):
            raise ServiceError("sort_unavailable", "sort is not available in the canonical-only snapshot")
        if sort in ("downloads", "download_growth"):
            if self.metric_source != "npmjs.org":
                raise ServiceError("invalid_query", "download sorting requires explicit metric_source=npmjs.org")
        elif self.metric_source is not None:
            raise ServiceError("invalid_query", "metric_source only applies to source-scoped download sorts")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or not 1 <= self.limit <= settings.max_page_size:
            raise ServiceError("invalid_query", "page size is outside the allowed range")
        q = self.q.strip() if isinstance(self.q, str) else self.q
        if q == "":
            q = None
        _bounded_string(q, "search", settings.max_query_length)
        if sort == "relevance" and not q:
            raise ServiceError("invalid_query", "relevance requires a search query")
        _bounded_string(self.agent, "agent")
        _bounded_string(self.type, "type")
        _bounded_string(self.subtype, "subtype")
        if self.type is not None and self.type not in TYPES:
            raise ServiceError("invalid_query", "invalid package type")
        if self.recommendation_source is not None:
            _bounded_string(self.recommendation_source,"recommendation source",200)
            if self.scope != "catalog" or not re.fullmatch(r"curated-source:[0-9a-f]{64}",self.recommendation_source):
                raise ServiceError("invalid_query","recommendation filtering requires a canonical scope and stable source ID")
        if self.review_disposition is not None:
            from .candidate_reviews import STATUSES
            if self.scope != "discovery" or not isinstance(self.review_disposition,str) or self.review_disposition not in STATUSES:
                raise ServiceError("invalid_query","review disposition only applies to discovery metadata triage")
        parameters = {"q": q, "agent": self.agent, "type": self.type, "subtype": self.subtype,
                "scope": self.scope, "sort": sort, "limit": self.limit,
                "facets": _facets(self.facets, False), "customFacets": _facets(self.custom_facets, True),
                "policyVersion": "discovery-evidence-v1" if self.scope == "discovery" else "npm-download-signals-v1" if sort in ("downloads", "download_growth") else "repository-signals-v1" if sort in ("stars", "star_growth", "repo_pushed") else "canonical-declared-release-time-v1" if sort == "latest_release" else "canonical-v1"}
        if self.metric_source is not None:
            parameters["metricSource"] = self.metric_source
        if self.recommendation_source is not None:
            parameters["recommendationSource"] = self.recommendation_source
            parameters["recommendationFilterPolicy"] = "canonical-latest-active-source-v1"
        if self.review_disposition is not None:
            parameters["reviewDisposition"] = self.review_disposition
            parameters["reviewPolicyVersion"] = "candidate-metadata-review-v1"
        return parameters


def _query_hash(parameters: dict) -> str:
    return hashlib.sha256(encode_json(parameters).encode("utf-8")).hexdigest()


def _encode_cursor(revision: str, query_hash: str, last) -> str:
    raw = encode_json({"v": 1, "r": revision, "q": query_hash, "last": last}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> dict:
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 65536 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("invalid encoding")
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"v", "r", "q", "last"} or payload["v"] != 1 or isinstance(payload["v"], bool):
            raise ValueError("invalid cursor shape")
        validate_revision(payload["r"])
        if not isinstance(payload["q"], str) or not re.fullmatch(r"[0-9a-f]{64}", payload["q"]):
            raise ValueError("invalid cursor binding")
        return payload
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeDecodeError, RecursionError, ServiceError) as error:
        raise ServiceError("invalid_cursor", "invalid pagination cursor") from error


class QueryService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = SnapshotStore(settings.snapshots)

    def resolve_revision(self, *, revision: str | None = None, cursor: str | None = None) -> str:
        """Pin one published revision before constructing an HTTP cache key."""
        return self.resolve_snapshot(revision=revision, cursor=cursor)["revision"]

    def resolve_snapshot(self, *, revision: str | None = None, cursor: str | None = None) -> dict:
        """Resolve and verify a snapshot once when its manifest is also needed."""
        decoded = _decode_cursor(cursor) if cursor is not None else None
        if revision is not None:
            validate_revision(revision)
        if decoded and revision is not None and decoded["r"] != revision:
            raise ServiceError("cursor_mismatch", "cursor belongs to another revision")
        selected = decoded["r"] if decoded else revision
        if selected is None:
            return self.store.current()
        if not (self.store.root / selected).is_dir():
            code = "cursor_expired" if decoded else "revision_unavailable"
            raise ServiceError(code, "the requested snapshot has been retired", 410)
        manifest = self.store.load(selected)
        from .retention import assert_query_available
        deadline = assert_query_available(self.store.root, manifest, cursor=decoded is not None)
        return {**manifest, "_cursorDeadline": deadline} if deadline is not None else manifest

    def _context(self, parameters: dict, cursor: str | None, revision: str | None = None) -> tuple[dict, dict | None, str]:
        binding = _query_hash(parameters)
        decoded = _decode_cursor(cursor) if cursor is not None else None
        if decoded and decoded["q"] != binding:
            raise ServiceError("cursor_mismatch", "cursor belongs to another query")
        manifest = self.resolve_snapshot(revision=revision, cursor=cursor)
        return manifest, decoded, binding

    @contextmanager
    def _connection(self, revision: str):
        try:
            connection = read_snapshot(self.store.database(revision), self.settings.query_timeout_seconds)
        except FileNotFoundError as error:
            raise ServiceError("cursor_expired", "snapshot was retired before the query opened", 410) from error
        deadline = time.monotonic() + self.settings.query_timeout_seconds
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        try:
            yield connection
        except sqlite3.OperationalError as error:
            if "interrupted" in str(error).lower():
                raise ServiceError("query_timeout", "query exceeded its execution budget", 503) from error
            raise
        finally:
            connection.close()

    @staticmethod
    def _envelope(manifest: dict) -> dict:
        coverage = manifest.get("observationCoverage", {})
        has_observations = bool(coverage.get("sharedRepositories"))
        full = bool(coverage.get("packages")) and coverage.get("freshPackages") == coverage.get("packages")
        from datetime import datetime
        now = utc_now()
        generated = datetime.fromisoformat(timestamp(manifest["generatedAt"]).replace("Z", "+00:00"))
        snapshot_age = (now - generated).total_seconds()
        oldest = coverage.get("oldestMappedSuccessAt")
        expired = bool(oldest and (now - datetime.fromisoformat(oldest.replace("Z", "+00:00"))).total_seconds() > coverage["starWindowPolicy"]["maximum_age_hours"] * 3600)
        download_coverage = manifest.get("downloadCoverage", {})
        checked = download_coverage.get("oldestCheckedAt")
        download_expired = bool(checked and (now - datetime.fromisoformat(checked.replace("Z", "+00:00"))).total_seconds() > download_coverage["maximumCheckAgeHours"] * 3600)
        source_end = download_coverage.get("oldestWindowEnd")
        if source_end:
            download_expired = download_expired or (now - datetime.fromisoformat(source_end.replace("Z", "+00:00"))).total_seconds() > download_coverage["policy"]["maximum_age_hours"] * 3600
        return {**({"sourceControlCoverage":manifest["sourceControlCoverage"]} if manifest.get("features",{}).get("sourceControls") else {}),
                **({"releaseCoverage":manifest["releaseCoverage"]} if manifest.get("features",{}).get("releases") else {}),
                **({"releaseSortCoverage":manifest["releaseSortCoverage"]} if manifest.get("features",{}).get("releaseSort") else {}),
                "revision": manifest["revision"], "generatedAt": manifest["generatedAt"], "snapshotAgeSeconds": snapshot_age,
                "downloadFreshnessSummary": {"stale": download_expired or not bool(download_coverage.get("sharedNpmPackages")),
                    "reason": "snapshot_stale" if download_expired else None if download_coverage.get("sharedNpmPackages") else "upstream_not_collected",
                    "asOf": manifest["generatedAt"], "partialCoverage": download_coverage.get("eligiblePackages", 0) < manifest.get("counts", {}).get("packages", 0),
                    "coverage": download_coverage},
                "freshnessSummary": {"stale": expired or not full, "reason": "snapshot_stale" if expired else None if full else "partial_or_stale_coverage" if has_observations else "upstream_not_collected",
                                     "hasUpstreamObservations": has_observations, "asOf": manifest["generatedAt"], "coverage": coverage}}


    @staticmethod
    def _require_discovery(manifest):
        if manifest.get("features", {}).get("discovery") is not True:
            raise ServiceError("discovery_unavailable", "this revision has no discovery projection", 503)

    @staticmethod
    def _discovery_freshness(manifest):
        from datetime import datetime
        coverage = manifest["discoveryCoverage"]
        observed = coverage["oldestObservationAt"]
        stale = not observed or (utc_now() - datetime.fromisoformat(observed.replace("Z", "+00:00"))).total_seconds() > coverage["staleAfterSeconds"]
        return {"stale": bool(stale), "reason": "upstream_not_collected" if not observed else "discovery_stale" if stale else None,
                "coverage": coverage, "asOf": manifest["generatedAt"], "complete": False}

    @staticmethod
    def _discovery_item(row):
        from datetime import datetime
        from .discovery_projection import STALE_SECONDS
        item = json.loads(row["data_json"])
        observed = item["lastSeenAt"]
        stale = (utc_now() - datetime.fromisoformat(observed.replace("Z", "+00:00"))).total_seconds() > STALE_SECONDS
        item["freshness"] = {"lastObservationAt": observed, "stale": stale,
            "reason": "discovery_stale" if stale else None, "staleAfterSeconds": STALE_SECONDS}
        return item

    def _discovery_packages(self, parameters, manifest, decoded, binding):
        self._require_discovery(manifest)
        conditions, values, joins, search_table = self._conditions(parameters, manifest)
        if parameters.get("reviewDisposition") is not None:
            if not manifest.get("features",{}).get("discoveryReviews"):
                raise ServiceError("reviews_unavailable","this revision has no metadata review projection",503)
            conditions.append("json_extract(p.data_json,'$.metadataReview.status')=:review_disposition")
            values["review_disposition"] = parameters["reviewDisposition"]
        sort = parameters["sort"]
        ascending = sort in ("name", "relevance")
        expression = "p.name" if sort == "name" else "p.first_seen_at"
        if sort == "relevance":
            if search_table:
                expression = search_table + ".rank"
            else:
                expression = "CASE WHEN p.name=:exact OR p.display_name=:exact THEN 0 ELSE 1 END"
                values["exact"] = parameters["q"]
        if decoded:
            last = decoded["last"]
            if not isinstance(last, list) or len(last) != 2:
                raise ServiceError("invalid_cursor", "invalid discovery sort cursor")
            key, identity = last
            if not isinstance(identity, str) or not re.fullmatch(r"(?:github-repository:[1-9][0-9]{0,18}|manifest:[0-9a-f]{64})", identity):
                raise ServiceError("invalid_cursor", "invalid discovery cursor identity")
            if sort == "relevance":
                if type(key) not in (int, float) or not math.isfinite(key):
                    raise ServiceError("invalid_cursor", "invalid discovery rank")
            elif not isinstance(key, str) or not key or len(key) > 4096:
                raise ServiceError("invalid_cursor", "invalid discovery sort key")
            elif sort == "first_seen":
                try:
                    if timestamp(key) != key or key > manifest["generatedAt"]:
                        raise ValueError("invalid date")
                except ValueError as error:
                    raise ServiceError("invalid_cursor", "invalid discovery timestamp") from error
            comparator = ">" if ascending else "<"
            conditions.append(f"({expression}{comparator}:last_key OR ({expression}=:last_key AND p.id>:last_id))")
            values.update(last_key=key, last_id=identity)
        direction = "ASC" if ascending else "DESC"
        values["page_limit"] = parameters["limit"] + 1
        where = " AND ".join(conditions) or "1"
        sql = f"SELECT p.id,p.data_json,{expression} AS sort_value FROM discoveries p{joins} WHERE {where} ORDER BY {expression} {direction},p.id ASC LIMIT :page_limit"
        with self._connection(manifest["revision"]) as connection:
            rows = connection.execute(sql, values).fetchall()
        page = rows[:parameters["limit"]]
        cursor = _encode_cursor(manifest["revision"], binding, [page[-1]["sort_value"], page[-1]["id"]]) if len(rows) > len(page) else None
        return {**self._envelope(manifest), "scope": "discovery", "sort": sort,
                **({"discoveryReviewCoverage":manifest["discoveryReviewCoverage"],"reviewDisposition":parameters.get("reviewDisposition")} if manifest.get("features",{}).get("discoveryReviews") else {}),
                "sortPolicyVersion": "discovery-evidence-v1", "sortScope": "discovery_evidence",
                "sortAsOf": manifest["generatedAt"], "discoveryCoverage": manifest["discoveryCoverage"],
                "freshnessSummary": self._discovery_freshness(manifest),
                "items": [self._discovery_item(row) for row in page], "nextCursor": cursor}

    def discovery(self, identity, *, revision=None):
        manifest = self.resolve_snapshot(revision=revision)
        self._require_discovery(manifest)
        if not isinstance(identity, str) or not re.fullmatch(r"(?:github-repository:[1-9][0-9]{0,18}|manifest:[0-9a-f]{64})", identity):
            raise ServiceError("invalid_query", "invalid discovery identity")
        with self._connection(manifest["revision"]) as connection:
            row = connection.execute("SELECT data_json FROM discoveries WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ServiceError("discovery_not_found", "discovery candidate was not found", 404)
        return {**self._envelope(manifest), "scope": "discovery", "item": self._discovery_item(row),
                **({"discoveryReviewCoverage":manifest["discoveryReviewCoverage"]} if manifest.get("features",{}).get("discoveryReviews") else {}),
                "discoveryCoverage": manifest["discoveryCoverage"], "freshnessSummary": self._discovery_freshness(manifest)}

    @staticmethod
    def _repository_columns(manifest, *, include_keys=True):
        return (", s.stars_current AS repository_stars, s.star_observed_at, s.growth_json AS repository_growth, "
                "s.freshness_json AS repository_freshness", " LEFT JOIN repository_signals s ON s.github_id=p.repository_id" +
                (" JOIN package_sort_keys k ON k.package_id=p.id" if include_keys else ""))

    @staticmethod
    def _download_columns(manifest, *, include_keys=True):
        return (", d.mapping_status AS download_mapping_status, d.npm_package, ns.signal_json AS npm_download_signal",
                (" JOIN package_download_sort_keys d ON d.package_id=p.id" if include_keys else "") +
                " LEFT JOIN npm_download_signals ns ON ns.package=d.npm_package")

    @staticmethod
    def _release_sort_columns(manifest, *, include_keys=True):
        if not manifest.get("features", {}).get("releaseSort"):
            return "", ""
        return ", rs.data_json AS latest_release_json", " JOIN public_release_sort_keys rs ON rs.package_id=p.id" if include_keys else ""

    @staticmethod
    def _summary(row) -> dict:
        item = {"id": row["id"], "name": row["name"], "type": row["type"],
                "displayName": row["display_name"], "summary": row["summary"], "subtype": row["subtype"],
                "canonicalLatest": row["canonical_latest"], "observedLatest": None,
                "selectionStatus": row["selection_status"], "updatedAt": row["canonical_updated_at"],
                "firstSeenAt": row["first_seen_at"], "bootstrap": bool(row["bootstrap"]),
                "keywords": json.loads(row["keywords_json"]), "facets": json.loads(row["facets_json"]),
                "customFacets": json.loads(row["custom_facets_json"]), "targets": json.loads(row["targets_json"]),
                "freshness": {"lastSuccessAt": None, "stale": True, "reason": "upstream_not_collected"}}
        if "repository_mapping_status" in row.keys():
            item["repositoryMappingStatus"] = row["repository_mapping_status"]
            item["repositoryId"] = row["repository_id"]
            if "repository_freshness" in row.keys() and row["repository_freshness"]:
                item["freshness"] = json.loads(row["repository_freshness"])
                item["repositoryMetrics"] = {"sourceId": "github.com", "subjectKind": "repository", "subjectId": str(row["repository_id"]),
                    "scope": "github_repository", "sharedRepositorySignal": True,
                    "stars": {"value": row["repository_stars"], "observedAt": row["star_observed_at"], "unit": "stars"},
                    "starGrowth": json.loads(row["repository_growth"])}
            else:
                item["freshness"]["reason"] = "upstream_not_collected" if row["repository_mapping_status"] == "unmapped" else row["repository_mapping_status"]
        if "download_mapping_status" in row.keys():
            item["downloadMappingStatus"] = row["download_mapping_status"]
            item["downloadMetrics"] = json.loads(row["npm_download_signal"]) if row["npm_download_signal"] else None
        if "latest_release_json" in row.keys():
            item["latestRelease"] = json.loads(row["latest_release_json"])
        media = json.loads(row["media_json"])
        if media:
            item["media"] = media
        if "recommendation_canonical_hash" in row.keys():
            item["recommendationAssociation"] = {"sourceId": row["recommendation_source"], "version": row["canonical_latest"],
                "canonicalHash": row["recommendation_canonical_hash"], "status": "active", "association": "exact-declared-link",
                "endorsement": "not-inferred", "sourceRank": None}
        return item

    @staticmethod
    def _conditions(parameters: dict, manifest: dict) -> tuple[list[str], dict, str, str | None]:
        conditions, values, joins, search_table = [], {}, "", None
        discovered = parameters.get("scope") == "discovery"
        agents_table = "discovery_agents" if discovered else "package_agents"
        facets_table = "discovery_facets" if discovered else "package_facets"
        identity_column = "discovery_id" if discovered else "package_id"
        search_prefix = "discoveries" if discovered else "packages"
        for key in ("type", "subtype"):
            if parameters.get(key):
                conditions.append("p." + key + "=:" + key)
                values[key] = parameters[key]
        if parameters.get("agent"):
            conditions.append(f"EXISTS(SELECT 1 FROM {agents_table} a WHERE a.{identity_column}=p.id AND a.agent=:agent)")
            values["agent"] = parameters["agent"]
        for custom, field in ((0, "facets"), (1, "customFacets")):
            for kind, entries in parameters.get(field, {}).items():
                for entry in entries:
                    index = len(values)
                    kind_key, value_key = f"fk{index}", f"fv{index}"
                    conditions.append(f"EXISTS(SELECT 1 FROM {facets_table} f WHERE f.{identity_column}=p.id AND f.custom={custom} AND f.kind=:{kind_key} AND f.value=:{value_key})")
                    values[kind_key], values[value_key] = kind, entry
        q = parameters.get("q")
        if q:
            if re.search(r"[\u3400-\u9fff]", q):
                if len(q) < 3:
                    literal = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    values["like"] = "%" + literal + "%"
                    conditions.append("(p.name LIKE :like ESCAPE '\\' OR p.display_name LIKE :like ESCAPE '\\' OR p.keywords_json LIKE :like ESCAPE '\\')")
                else:
                    if not manifest["features"]["trigram"]:
                        raise ServiceError("search_unavailable", "this snapshot has no Chinese trigram index", 503)
                    search_table = search_prefix + "_trigram"
                    values["match"] = '"' + q.replace('"', '""') + '"'
            else:
                tokens = re.findall(r"[^\W_]+", q, flags=re.UNICODE)
                if not tokens or len(tokens) > 12:
                    raise ServiceError("invalid_query", "search requires between one and twelve tokens")
                search_table = search_prefix + "_fts"
                values["match"] = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
            if search_table:
                joins = f" JOIN {search_table} ON {search_table}.rowid=p.rowid"
                conditions.append(f"{search_table} MATCH :match")
        return conditions, values, joins, search_table

    def packages(self, query: Query | None = None, *, cursor: str | None = None, revision: str | None = None) -> dict:
        parameters = (query or Query()).normalized(self.settings)
        manifest, decoded, binding = self._context(parameters, cursor, revision)
        if parameters["scope"] == "discovery":
            return self._discovery_packages(parameters, manifest, decoded, binding)
        conditions, values, joins, search_table = self._conditions(parameters, manifest)
        source_id = parameters.get("recommendationSource")
        recommendation_filter = None
        if source_id:
            self._require_recommendations(manifest)
            values["recommendation_source"] = source_id
            conditions.append("EXISTS(SELECT 1 FROM curated_package_links l JOIN curated_claims c ON c.source_id=l.source_id AND c.target_url=l.target_url WHERE l.package_id=p.id AND l.version=p.canonical_latest AND l.source_id=:recommendation_source AND c.status='active')")
        sort = parameters["sort"]
        download_sort = sort in ("downloads", "download_growth")
        release_sort = sort == "latest_release"
        if release_sort and not manifest.get("features", {}).get("releaseSort"):
            raise ServiceError("sort_unavailable", "this revision has no explicit canonical Release sorting projection")
        repository_sort = sort in ("stars", "star_growth", "repo_pushed")
        repository_columns, repository_join = self._repository_columns(manifest, include_keys=not repository_sort or bool(parameters["q"]))
        joins += repository_join
        download_columns, download_join = self._download_columns(manifest, include_keys=not download_sort or bool(parameters["q"]))
        repository_columns += download_columns
        joins += download_join
        release_columns, release_join = self._release_sort_columns(manifest, include_keys=not release_sort or bool(parameters["q"]))
        repository_columns += release_columns
        joins += release_join
        if source_id:
            joins += " JOIN package_versions selected_version ON selected_version.package_id=p.id AND selected_version.version=p.canonical_latest"
            repository_columns += ",selected_version.payload_hash AS recommendation_canonical_hash,:recommendation_source AS recommendation_source"
        from_clause = "packages p"
        if repository_sort and not parameters["q"]:
            index = {"stars": "package_stars_rank", "star_growth": "package_star_growth_rank", "repo_pushed": "package_repo_pushed_rank"}[sort]
            # CROSS JOIN preserves the index-driven order on large, mostly-unknown catalogs.
            from_clause = "package_sort_keys k INDEXED BY " + index + " CROSS JOIN packages p ON p.id=k.package_id"
        if download_sort and not parameters["q"]:
            index = {"downloads": "package_downloads_rank", "download_growth": "package_download_growth_rank"}[sort]
            from_clause = "package_download_sort_keys d INDEXED BY " + index + " CROSS JOIN packages p ON p.id=d.package_id"
        if release_sort and not parameters["q"]:
            from_clause = "public_release_sort_keys rs INDEXED BY public_release_package_rank CROSS JOIN packages p ON p.id=rs.package_id"
        ascending = sort in ("name", "relevance")
        expression = {"canonical_updated": "p.canonical_updated_at", "first_seen": "p.first_seen_at", "name": "p.name", "stars": "k.stars_value", "star_growth": "k.growth_value", "repo_pushed": "k.pushed_at", "downloads": "d.downloads_value", "download_growth": "d.growth_value", "latest_release": "rs.published_at"}.get(sort)
        if sort == "relevance":
            if search_table:
                # Keep FTS5's native rank order. LIMIT -1 prevents flattening;
                # every filter and eligibility join still precedes the page limit.
                joins = joins.replace(f" JOIN {search_table} ON {search_table}.rowid=p.rowid", "", 1)
                conditions.remove(f"{search_table} MATCH :match")
                from_clause = (f"(SELECT rowid,rank FROM {search_table} WHERE {search_table} MATCH :match "
                               "ORDER BY rank LIMIT -1) ranked_fts CROSS JOIN packages p ON p.rowid=ranked_fts.rowid")
                expression = "ranked_fts.rank"
            else:
                # Short Chinese search is name/tag-only, not an unbounded description scan.
                expression = "CASE WHEN p.name=:exact OR p.display_name=:exact THEN 0 ELSE 1 END"
                values["exact"] = parameters["q"]
        if decoded:
            last = decoded["last"]
            if not isinstance(last, list) or len(last) != 3 or type(last[0]) is not int or last[0] not in (0, 1):
                raise ServiceError("invalid_cursor", "invalid sort cursor")
            null, key, package_id = last
            _bounded_string(package_id, "cursor id", 4096)
            if package_id is None:
                raise ServiceError("invalid_cursor", "missing cursor id")
            if null:
                if key is not None:
                    raise ServiceError("invalid_cursor", "invalid null cursor")
                conditions.append(f"({expression} IS NULL AND p.id>:last_id)")
            else:
                if sort in ("relevance", "star_growth", "stars", "downloads", "download_growth"):
                    if isinstance(key, bool) or not isinstance(key, (int, float)) or not math.isfinite(key):
                        raise ServiceError("invalid_cursor", "invalid numeric sort cursor")
                    if sort in ("stars", "downloads") and (type(key) is not int or not 0 <= key <= 2**63 - 1):
                        raise ServiceError("invalid_cursor", "invalid count cursor")
                elif not isinstance(key, str) or len(key) > 4096:
                    raise ServiceError("invalid_cursor", "invalid sort value")
                operator = ">" if ascending else "<"
                conditions.append(f"({expression} IS NULL OR {expression}{operator}:last_value OR ({expression}=:last_value AND p.id>:last_id))")
                values["last_value"] = key
            values["last_id"] = package_id
        where = " AND ".join(conditions) if conditions else "1"
        direction = "ASC" if ascending else "DESC"
        values["page_limit"] = parameters["limit"] + 1
        # DESC already places NULL last; ascending keys here are non-null.
        # Avoid a computed null-order prefix that would defeat the B-tree index.
        tie_key = "rs.package_id" if release_sort else "d.package_id" if download_sort else "k.package_id" if repository_sort else "p.id"
        sql = f"SELECT p.*{repository_columns}, {expression} AS sort_value, ({expression} IS NULL) AS sort_null FROM {from_clause}{joins} WHERE {where} ORDER BY {expression} {direction}, {tie_key} ASC LIMIT :page_limit"
        with self._connection(manifest["revision"]) as connection:
            if source_id:
                source = connection.execute("SELECT data_json FROM curated_sources WHERE id=?",(source_id,)).fetchone()
                if source is None:
                    raise ServiceError("recommendation_source_not_found","recommendation source is absent from this revision",404)
                source = json.loads(source[0])
                recommendation_filter = {"sourceId": source_id,"labels": source["labels"],"status": "active",
                    "canonicalVersionPolicy": "canonical-latest-only","association": "exact-declared-link","endorsement": "not-inferred",
                    "sourceRank": None,"policyVersion": "canonical-latest-active-source-v1","freshness": self._recommendation_freshness(source)}
            rows = connection.execute(sql, values).fetchall()
        page = rows[:parameters["limit"]]
        next_cursor = None
        if len(rows) > len(page):
            last = page[-1]
            next_cursor = _encode_cursor(manifest["revision"], binding, [last["sort_null"], last["sort_value"], last["id"]])
        return {**self._envelope(manifest), "scope": "catalog", "sort": sort,
                "recommendationFilteringAvailable": bool(manifest.get("features",{}).get("recommendations")),
                "recommendationFilter": recommendation_filter,
                "sortPolicyVersion": "canonical-declared-release-time-v1" if release_sort else "npm-download-signals-v1" if download_sort else "repository-signals-v1" if repository_sort else "canonical-v1",
                "sortScope": "canonical-latest-explicit-distribution" if release_sort else "npm_public_registry_all_versions" if download_sort else "shared_repository_signal" if repository_sort else "canonical",
                "metricSource": parameters.get("metricSource"),
                "downloadCoverage": manifest.get("downloadCoverage"),
                "sortAsOf": manifest["generatedAt"],
                "items": [self._summary(row) for row in page],
                "nextCursor": next_cursor, "disclaimer": "来源声明未经安装、兼容性或安全验证"}

    def package(self, package_id: str, *, revision: str | None = None, version: str | None = None) -> dict:
        _bounded_string(package_id, "package id", 4096)
        _bounded_string(version, "version", 1024)
        manifest = self.resolve_snapshot(revision=revision)
        with self._connection(manifest["revision"]) as connection:
            repository_columns, repository_join = self._repository_columns(manifest)
            download_columns, download_join = self._download_columns(manifest)
            repository_columns += download_columns
            repository_join += download_join
            release_columns, release_join = self._release_sort_columns(manifest)
            repository_columns += release_columns
            repository_join += release_join
            row = connection.execute("SELECT p.*" + repository_columns + " FROM packages p" + repository_join + " WHERE p.id=?", (package_id,)).fetchone()
            if row is None:
                raise ServiceError("not_found", "package not found", 404)
            selected = connection.execute("SELECT canonical_json FROM package_versions WHERE package_id=? AND version=?", (package_id, version if version is not None else row["canonical_latest"])).fetchone()
            if version is not None and selected is None:
                raise ServiceError("version_not_found", "canonical version not found", 404)
            observations = [{"subjectKind": "repository", "subjectId": str(item["github_id"]), "sourceId": "github.com",
                "scope": "github_repository", "observedAt": item["observed_at"], "checkedAt": item["checked_at"],
                "evidenceHash": item["evidence_hash"], "metadata": json.loads(item["metadata_json"]),
                "associationEvidence": json.loads(item["association_evidence"]), "sharedRepositorySignal": True}
                for item in connection.execute("SELECT o.*,l.evidence_json AS association_evidence FROM package_repository_links l "
                    "JOIN repository_observations o ON o.github_id=l.github_id WHERE l.package_id=? ORDER BY o.github_id", (package_id,))]
        return {**self._envelope(manifest), **self._summary(row),
                **({"requestedVersion": version} if version is not None else {}),
                "canonicalRecord": json.loads(selected[0]) if selected else None,
                "observations": observations, "disclaimer": "仅公开元数据，不代表安装、兼容或安全认证"}

    def metrics(self, package_id: str, *, limit: int = 20, cursor: str | None = None, source: str | None = None, revision: str | None = None) -> dict:
        if source == "npmjs.org":
            return self.download_metrics(package_id, limit=limit, cursor=cursor, revision=revision)
        if source not in (None, "github.com"):
            raise ServiceError("invalid_query", "unsupported metric source")
        _bounded_string(package_id, "package id", 4096)
        Query(limit=limit).normalized(self.settings)
        manifest, decoded, binding = self._context({"endpoint": "repository-metrics", "id": package_id, "limit": limit}, cursor, revision)
        values = {"package": package_id, "limit": limit + 1}
        condition = ""
        if decoded:
            last = decoded["last"]
            if not isinstance(last, list) or len(last) != 2 or any(not isinstance(value, str) for value in last):
                raise ServiceError("invalid_cursor", "invalid metric history cursor")
            _bounded_string(last[0], "metric observation time", 100)
            _bounded_string(last[1], "metric sample id", 100)
            condition = " AND (m.observed_at>:after_time OR (m.observed_at=:after_time AND m.id>:after_id))"
            values.update(after_time=last[0], after_id=last[1])
        with self._connection(manifest["revision"]) as connection:
            if not connection.execute("SELECT 1 FROM packages WHERE id=?", (package_id,)).fetchone():
                raise ServiceError("not_found", "package not found", 404)
            rows = connection.execute(
                "SELECT m.* FROM repository_metric_samples m WHERE EXISTS (SELECT 1 FROM package_repository_links l "
                "WHERE l.package_id=:package AND l.github_id=m.github_id)" + condition + " ORDER BY m.observed_at,m.id LIMIT :limit", values).fetchall()
        page = rows[:limit]
        next_cursor = _encode_cursor(manifest["revision"], binding, [page[-1]["observed_at"], page[-1]["id"]]) if len(rows) > limit else None
        return {**self._envelope(manifest), "id": package_id, "metricScope": "shared_repository_signal",
                "items": [{"sampleId": row["id"], "sample": json.loads(row["sample_json"]), "evidenceHash": row["evidence_hash"]} for row in page],
                "nextCursor": next_cursor, "availability": "available" if page else "unknown",
                "disclaimer": "所属仓库 Star 历史，不是插件独立使用人数或下载量"}

    def download_metrics(self, package_id: str, *, limit: int = 20, cursor: str | None = None, revision: str | None = None) -> dict:
        _bounded_string(package_id, "package id", 4096)
        Query(limit=limit).normalized(self.settings)
        manifest, decoded, binding = self._context({"endpoint": "npm-daily-metrics", "id": package_id, "limit": limit}, cursor, revision)
        values = {"package": package_id, "limit": limit + 1}
        condition = ""
        if decoded:
            last = decoded["last"]
            if not isinstance(last, list) or len(last) != 2 or any(not isinstance(value, str) for value in last):
                raise ServiceError("invalid_cursor", "invalid daily metrics cursor")
            _bounded_string(last[0], "npm package cursor", 214)
            _bounded_string(last[1], "npm day cursor", 10)
            condition = " AND (o.package>:after_package OR (o.package=:after_package AND json_extract(day.value,'$.day')>:after_day))"
            values.update(after_package=last[0], after_day=last[1])
        with self._connection(manifest["revision"]) as connection:
            if not connection.execute("SELECT 1 FROM packages WHERE id=?", (package_id,)).fetchone():
                raise ServiceError("not_found", "package not found", 404)
            rows = connection.execute(
                "SELECT o.package,o.observed_at,o.checked_at,o.evidence_hash,l.evidence_json,day.value AS daily_json "
                "FROM package_npm_links l JOIN npm_download_observations o ON o.package=l.npm_package "
                "JOIN json_each(o.body_json,'$.downloads') day WHERE l.package_id=:package" + condition +
                " ORDER BY o.package,json_extract(day.value,'$.day') LIMIT :limit", values).fetchall()
        page = rows[:limit]
        next_cursor = _encode_cursor(manifest["revision"], binding, [page[-1]["package"], json.loads(page[-1]["daily_json"])["day"]]) if len(rows) > limit else None
        items = [{"subjectKind": "registry_package", "subjectId": row["package"], "sourceId": "npmjs.org",
                  "scope": "npm_public_registry_all_versions", "measurementKind": "utc_daily_interval", **json.loads(row["daily_json"]),
                  "observedAt": row["observed_at"], "checkedAt": row["checked_at"], "evidenceHash": row["evidence_hash"],
                  "associationEvidence": json.loads(row["evidence_json"])} for row in page]
        return {**self._envelope(manifest), "id": package_id, "sourceId": "npmjs.org", "items": items,
                "nextCursor": next_cursor, "availability": "available" if page else "unknown",
                "coverage": "latest collected 30 UTC days; not an all-time history or cumulative counter"}

    def versions(self, package_id: str, *, limit: int = 20, cursor: str | None = None, revision: str | None = None) -> dict:
        _bounded_string(package_id, "package id", 4096)
        Query(limit=limit).normalized(self.settings)
        manifest, decoded, binding = self._context({"endpoint": "versions", "id": package_id, "limit": limit}, cursor, revision)
        values = {"id": package_id, "limit": limit + 1}
        condition = ""
        if decoded:
            if not isinstance(decoded["last"], str) or len(decoded["last"]) > 1024:
                raise ServiceError("invalid_cursor", "invalid version cursor")
            condition = " AND version>:version"
            values["version"] = decoded["last"]
        with self._connection(manifest["revision"]) as connection:
            if connection.execute("SELECT 1 FROM packages WHERE id=?", (package_id,)).fetchone() is None:
                raise ServiceError("not_found", "package not found", 404)
            rows = connection.execute("SELECT version,canonical_time,payload_hash FROM package_versions WHERE package_id=:id" + condition + " ORDER BY version LIMIT :limit", values).fetchall()
        page = rows[:limit]
        next_cursor = _encode_cursor(manifest["revision"], binding, page[-1]["version"]) if len(rows) > limit else None
        return {**self._envelope(manifest), "items": [{"version": row["version"], "canonicalUpdatedAt": row["canonical_time"], "sha256": row["payload_hash"], "kind": "canonical"} for row in page], "nextCursor": next_cursor}

    def changes(self, *, since: int = 0, limit: int = 20, cursor: str | None = None, revision: str | None = None) -> dict:
        if type(since) is not int or not 0 <= since <= 9223372036854775807:
            raise ServiceError("invalid_query", "invalid change sequence")
        Query(limit=limit).normalized(self.settings)
        manifest, decoded, binding = self._context({"endpoint": "changes", "since": since, "limit": limit}, cursor, revision)
        after = decoded["last"] if decoded else since
        if type(after) is not int or not since <= after <= 9223372036854775807:
            raise ServiceError("invalid_cursor", "invalid event cursor")
        with self._connection(manifest["revision"]) as connection:
            rows = connection.execute("SELECT * FROM change_events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit + 1)).fetchall()
        page = rows[:limit]
        next_cursor = _encode_cursor(manifest["revision"], binding, page[-1]["seq"]) if len(rows) > limit else None
        return {**self._envelope(manifest), "items": [{"seq": row["seq"], "id": row["unit_id"], "eventType": row["event_type"], "observedAt": row["observed_at"], "evidence": json.loads(row["evidence_json"])} for row in page], "nextCursor": next_cursor, "highWatermark": manifest["eventCutoff"]}

    def facets(self, *, revision: str | None = None, scope: str = "catalog") -> dict:
        if scope not in ("catalog", "discovery"):
            raise ServiceError("scope_unavailable", "unknown facet scope")
        manifest = self.resolve_snapshot(revision=revision)
        if scope == "discovery":
            self._require_discovery(manifest)
            with self._connection(manifest["revision"]) as connection:
                agents = [row[0] for row in connection.execute("SELECT DISTINCT agent FROM discovery_agents ORDER BY agent")]
                types = [{"value": row[0], "count": row[1]} for row in connection.execute("SELECT type,count(*) FROM discoveries WHERE type IS NOT NULL GROUP BY type ORDER BY type")]
            return {**self._envelope(manifest), "scope": "discovery", "facets": {}, "customFacets": {},
                    "subtypes": [], "agents": agents, "types": types,
                    "freshnessSummary": self._discovery_freshness(manifest),
                    "disclaimer": "明确声明检查 profile 的 Agent 关联不是兼容性认证；Topic 不推断类型或 Agent"}
        with self._connection(manifest["revision"]) as connection:
            rows = connection.execute("SELECT custom,kind,value,count(*) AS count FROM package_facets GROUP BY custom,kind,value ORDER BY custom,kind,value").fetchall()
            subtypes = [{"value": row[0], "count": row[1]} for row in connection.execute("SELECT subtype,count(*) FROM packages WHERE subtype IS NOT NULL GROUP BY subtype ORDER BY subtype")]
        official, custom = {}, {}
        for row in rows:
            group = custom if row["custom"] else official
            group.setdefault(row["kind"], []).append({"value": row["value"], "count": row["count"]})
        return {**self._envelope(manifest), "scope": "catalog", "facets": official, "customFacets": custom, "subtypes": subtypes, "agents": manifest.get("agents", []),
                "disclaimer": "规范声明中的 facet 与 customFacet 分栏；不是后端认证"}

    @staticmethod
    def _require_recommendations(manifest):
        if not manifest.get("features", {}).get("recommendations"):
            raise ServiceError("recommendations_unavailable", "this revision has no public recommendation projection", 503)

    @staticmethod
    def _recommendation_freshness(source):
        from datetime import datetime
        age = max(0, (utc_now() - datetime.fromisoformat(source["lastSuccessAt"].replace("Z", "+00:00"))).total_seconds())
        return {"lastSuccessAt": source["lastSuccessAt"], "lastObservationAt": source["lastObservationAt"],
                "ageSeconds": age, "staleAfterSeconds": source["staleAfterSeconds"], "stale": age > source["staleAfterSeconds"],
                "coverage": "configured-scopes-only"}

    def recommendation_sources(self, *, limit=20, cursor=None, revision=None):
        Query(limit=limit).normalized(self.settings)
        parameters = {"endpoint": "recommendation-sources", "limit": limit, "policy": "public-curated-projection-v1"}
        manifest, decoded, binding = self._context(parameters, cursor, revision)
        self._require_recommendations(manifest)
        after = ""
        if decoded:
            if not isinstance(decoded["last"], str):
                raise ServiceError("invalid_cursor", "invalid recommendation source cursor")
            after = _bounded_string(decoded["last"], "source cursor", 200)
        with self._connection(manifest["revision"]) as connection:
            rows = connection.execute("SELECT * FROM curated_sources WHERE id>? ORDER BY id LIMIT ?", (after, limit + 1)).fetchall()
        items = [json.loads(row["data_json"]) for row in rows[:limit]]
        for item in items:
            item["freshness"] = self._recommendation_freshness(item)
        return {**self._envelope(manifest), "items": items,
                "nextCursor": _encode_cursor(manifest["revision"], binding, rows[limit-1]["id"]) if len(rows) > limit else None,
                "recommendationCoverage": manifest["recommendationCoverage"], "availability": "available" if items else "unknown",
                "disclaimer": "来源精选列表；不是本站推荐或插件背书；没有来源排名时不虚构排名"}

    @staticmethod
    def _recommendation_item(connection, row):
        claim = json.loads(row["data_json"])
        source = json.loads(connection.execute("SELECT data_json FROM curated_sources WHERE id=?", (claim["sourceId"],)).fetchone()[0])
        snapshot = json.loads(connection.execute("SELECT data_json FROM curated_snapshots WHERE id=?", (claim["snapshotId"],)).fetchone()[0])
        citation_snapshot = claim["snapshotId"] if claim["status"] == "active" else claim["previousSnapshotId"]
        member = json.loads(connection.execute("SELECT data_json FROM curated_members WHERE snapshot_id=? AND target_url=?", (citation_snapshot, claim["targetUrl"])).fetchone()[0])
        previous = None
        if claim["previousSnapshotId"]:
            previous = json.loads(connection.execute("SELECT data_json FROM curated_snapshots WHERE id=?", (claim["previousSnapshotId"],)).fetchone()[0])
        def proof(value):
            if value is None:
                return None
            return {key: value[key] for key in ("id", "commitSha", "blobSha", "contentSha256", "url", "selectionAt", "observedAt", "checkedAt", "completedAt", "scope", "memberCount", "entryCount", "steps")}
        return {**claim, "source": {"id": source["id"], "labels": source["labels"], "repositoryId": source["repositoryId"],
                "path": source["path"], "selectionStatement": source["selectionStatement"], "startHeading": source["startHeading"], "endHeading": source["endHeading"]},
                "snapshot": proof(snapshot), "previousSnapshot": proof(previous), "citations": member["positions"][:20],
                "citationCount": len(member["positions"]), "citationsTruncated": len(member["positions"]) > 20,
                "freshness": QueryService._recommendation_freshness(source)}

    def recommendations(self, package_id=None, *, source=None, status="all", version=None, limit=20, cursor=None, revision=None):
        Query(limit=limit).normalized(self.settings)
        _bounded_string(package_id, "package id", 4096); _bounded_string(source, "recommendation source", 200)
        _bounded_string(version, "version", 4096)
        if status not in ("all", "active", "withdrawn") or package_id is None and source is None or package_id is None and version is not None:
            raise ServiceError("invalid_query", "recommendations need a package or explicit source and a valid status")
        parameters = {"endpoint": "recommendations", "id": package_id, "source": source, "status": status,
                      "version": version, "limit": limit, "policy": "public-curated-projection-v1", "order": "source-target"}
        manifest, decoded, binding = self._context(parameters, cursor, revision)
        self._require_recommendations(manifest)
        values = {"limit": limit + 1}
        conditions, joins, selected_version = [], "", version
        if source:
            conditions.append("c.source_id=:source"); values["source"] = source
        if status != "all":
            conditions.append("c.status=:status"); values["status"] = status
        if decoded:
            last = decoded["last"]
            if not isinstance(last, list) or len(last) != 2 or any(not isinstance(item, str) for item in last):
                raise ServiceError("invalid_cursor", "invalid recommendations cursor")
            _bounded_string(last[0], "source cursor", 200); _bounded_string(last[1], "target cursor", 2000)
            conditions.append("(c.source_id>:after_source OR (c.source_id=:after_source AND c.target_url>:after_target))")
            values.update(after_source=last[0], after_target=last[1])
        with self._connection(manifest["revision"]) as connection:
            if source and not connection.execute("SELECT 1 FROM curated_sources WHERE id=?", (source,)).fetchone():
                raise ServiceError("recommendation_source_not_found", "recommendation source is absent from this revision", 404)
            if package_id is not None:
                package = connection.execute("SELECT canonical_latest FROM packages WHERE id=?", (package_id,)).fetchone()
                if package is None:
                    raise ServiceError("not_found", "package not found", 404)
                selected_version = version if version is not None else package["canonical_latest"]
                if version is not None and not connection.execute("SELECT 1 FROM package_versions WHERE package_id=? AND version=?", (package_id, version)).fetchone():
                    raise ServiceError("version_not_found", "canonical version not found", 404)
                joins = " JOIN curated_package_links l ON l.source_id=c.source_id AND l.target_url=c.target_url"
                conditions += ["l.package_id=:package", "l.version=:version"]
                values.update(package=package_id, version=selected_version)
            rows = connection.execute("SELECT c.*" + (",l.data_json AS association_json" if package_id is not None else "") +
                " FROM curated_claims c" + joins + (" WHERE " + " AND ".join(conditions) if conditions else "") +
                " ORDER BY c.source_id,c.target_url LIMIT :limit", values).fetchall()
            items = []
            for row in rows[:limit]:
                item = self._recommendation_item(connection, row)
                item["packageAssociation"] = json.loads(row["association_json"]) if package_id is not None else None
                items.append(item)
        return {**self._envelope(manifest), "id": package_id, "selectedVersion": selected_version, "sourceId": source,
                "statusFilter": status, "order": "source-target", "policyVersion": "public-curated-projection-v1",
                "items": items, "nextCursor": _encode_cursor(manifest["revision"], binding, [rows[limit-1]["source_id"], rows[limit-1]["target_url"]]) if len(rows) > limit else None,
                "availability": "available" if items else "unknown", "recommendationCoverage": manifest["recommendationCoverage"],
                "disclaimer": "来源列表成员关系与规范声明链接的精确关联；不推断包背书、质量、安全或使用人数；字母序不是推荐排名"}

    def releases(self,package_id=None,*,repository_id=None,version=None,association="all",limit=20,cursor=None,revision=None):
        from .release_projection import POLICY,STALE_SECONDS,_id,declared_scopes
        Query(limit=limit).normalized(self.settings)
        if association not in ("all","declared","repository_context"):raise ServiceError("invalid_query","unsupported Release association")
        if repository_id is not None:
            try:_id(repository_id)
            except ValueError as error:raise ServiceError("invalid_query","invalid Release repository ID") from error
        if package_id is None and (version is not None or association!="all"):raise ServiceError("invalid_query","version and association require a canonical package")
        parameters={"endpoint":"releases","id":package_id,"repositoryId":repository_id,"version":version,"association":association,"limit":limit,"policy":POLICY}
        manifest,decoded,binding=self._context(parameters,cursor,revision)
        if manifest.get("features",{}).get("releases") is not True:raise ServiceError("releases_unavailable","this revision has no Release observations",503)
        selected_version=None;where=[];args={};join="";columns="r.data_json";scope_repos=set()
        with self._connection(manifest["revision"]) as connection:
            if package_id is not None:
                _bounded_string(package_id,"package id",4096);_bounded_string(version,"version",1024)
                row=connection.execute('SELECT canonical_latest FROM packages WHERE id=?',(package_id,)).fetchone()
                if row is None:raise ServiceError("not_found","package not found",404)
                selected_version=version if version is not None else row['canonical_latest']
                row=connection.execute('SELECT canonical_json FROM package_versions WHERE package_id=? AND version=?',(package_id,selected_version)).fetchone()
                if row is None:raise ServiceError("version_not_found","canonical version not found",404)
                repositories={row['repository_id']:json.loads(row['data_json']) for row in connection.execute('SELECT * FROM public_release_repositories')}
                scope_repos={value['repositoryId'] for value in declared_scopes(json.loads(row['canonical_json']),repositories)}
                join=' JOIN public_release_package_links l ON l.repository_id=r.repository_id AND l.release_id=r.release_id'
                columns+=",l.data_json AS association_json"
                where += ['l.package_id=:package','l.version=:version'];args.update(package=package_id,version=selected_version)
                if association!="all":
                    expression="EXISTS(SELECT 1 FROM json_each(l.data_json,'$.assertions') a WHERE json_extract(a.value,'$.kind')!='repository_context')"
                    where.append(expression if association=="declared" else 'NOT '+expression)
            if repository_id is not None:where.append('r.repository_id=:repository');args['repository']=int(repository_id)
            if decoded:
                last=decoded['last']
                if not isinstance(last,list) or len(last)!=3 or not isinstance(last[0],str) or any(not isinstance(value,str) for value in last[1:]):raise ServiceError("invalid_cursor","invalid Release cursor")
                try:
                    if timestamp(last[0])!=last[0]:raise ValueError('invalid Release cursor clock')
                    _id(last[1]);_id(last[2])
                except ValueError as error:raise ServiceError("invalid_cursor","invalid Release cursor") from error
                where.append('(r.published_at<:after_time OR r.published_at=:after_time AND (r.repository_id>:after_repo OR r.repository_id=:after_repo AND r.release_id>:after_release))')
                args.update(after_time=last[0],after_repo=int(last[1]),after_release=int(last[2]))
            sql='SELECT '+columns+' FROM public_release_items r'+join+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY r.published_at DESC,r.repository_id,r.release_id LIMIT :limit'
            rows=connection.execute(sql,{**args,'limit':limit+1}).fetchall()
            items=[]
            from datetime import datetime
            now=utc_now()
            for row in rows[:limit]:
                release=json.loads(row['data_json']);checked=release['observation']['checkedAt']
                items.append({'release':release,'association':json.loads(row['association_json']) if package_id is not None else None,
                    'freshness':{'checkedAt':checked,'stale':(now-datetime.fromisoformat(checked.replace('Z','+00:00'))).total_seconds()>STALE_SECONDS,'staleAfterSeconds':STALE_SECONDS}})
            if repository_id is not None:scope_repos={int(repository_id)} if package_id is None else scope_repos&{int(repository_id)}
            elif package_id is None:scope_repos={int(item['release']['repositoryId']) for item in items}
            if package_id is None and repository_id is None and not items:
                scope_repos={row[0] for row in connection.execute('SELECT repository_id FROM public_release_checks ORDER BY repository_id LIMIT 20')}
            check_count=len(scope_repos)
            if package_id is None and repository_id is None and not items:check_count=connection.execute("SELECT count(*) FROM public_release_checks").fetchone()[0]
            checks=[]
            for repo in sorted(scope_repos)[:20]:
                row=connection.execute('SELECT data_json FROM public_release_checks WHERE repository_id=?',(repo,)).fetchone()
                if row:checks.append(json.loads(row[0]))
        last=items[-1]['release'] if items else None
        return {**self._envelope(manifest),"items":items,"id":package_id,"repositoryId":repository_id,"selectedVersion":selected_version,
            "associationFilter":association,"order":"published-desc-repository-id-release-id","policyVersion":POLICY,
            "nextCursor":_encode_cursor(manifest["revision"],binding,[last['publishedAt'],last['repositoryId'],last['releaseId']]) if len(rows)>limit else None,
            "releaseCoverage":manifest['releaseCoverage'],"repositoryChecks":checks,"repositoryCheckCount":check_count,"repositoryChecksTruncated":check_count>20,
            "availability":"available" if items else "unknown","disclaimer":"Observed repository Release facts; exact declared URLs do not verify plugin versions. Asset download counters are not users or npm downloads; missing pages do not prove deletion."}

    def source_controls(self, *, limit=20, cursor=None, revision=None):
        from .source_controls import POLICY
        from .source_control_projection import summary
        Query(limit=limit).normalized(self.settings)
        parameters = {"endpoint": "source-controls", "limit": limit, "policy": POLICY}
        manifest, decoded, binding = self._context(parameters,cursor,revision)
        if manifest.get("features",{}).get("sourceControls") is not True:
            raise ServiceError("source_controls_unavailable","this revision has no recorded collection controls",503)
        after = ""
        if decoded:
            after = decoded["last"]
            if not isinstance(after,str) or not re.fullmatch(r"collection-source:[0-9a-f]{64}",after):
                raise ServiceError("invalid_cursor","invalid source control cursor")
        with self._connection(manifest["revision"]) as connection:
            rows = connection.execute("SELECT DISTINCT target_id FROM source_control_events WHERE target_id>? ORDER BY target_id LIMIT ?",(after,limit+1)).fetchall()
            items = [summary(connection,row["target_id"]) for row in rows[:limit]]
        return {**self._envelope(manifest),"items":items,"sourceControlCoverage":manifest["sourceControlCoverage"],
            "nextCursor":_encode_cursor(manifest["revision"],binding,rows[limit-1]["target_id"]) if len(rows)>limit else None,
            "semantics":"Local controls apply to future collection; retained facts, recommendation membership and package visibility are unchanged"}

    def sources(self, *, revision: str | None = None) -> dict:
        manifest = self.resolve_snapshot(revision=revision)
        with self._connection(manifest["revision"]) as connection:
            items = [json.loads(row[0]) for row in connection.execute("SELECT data_json FROM sources ORDER BY id")]
        from datetime import datetime
        now = utc_now()
        for item in items:
            if item.get("kind") == "github_search":
                success = item["lastSuccessAt"]
                item["stale"] = not success or (now - datetime.fromisoformat(success.replace("Z", "+00:00"))).total_seconds() > item["staleAfterSeconds"]
        return {**self._envelope(manifest), "items": items, "nextCursor": None}

    def dashboard(self, *, agent: str | None = None, revision: str | None = None) -> dict:
        from .dashboard import aggregate_dashboard, dashboard_key
        _bounded_string(agent, "agent", 100)
        manifest = self.resolve_snapshot(revision=revision)
        if agent is not None and agent not in manifest.get("agents", []):
            raise ServiceError("agent_not_found", "Agent is not present in this revision", 404)
        with self._connection(manifest["revision"]) as connection:
            row = connection.execute("SELECT value FROM dashboard_summaries WHERE scope=?", (dashboard_key(agent),)).fetchone() if manifest.get("features", {}).get("dashboard") else None
            if row is not None:
                summary, mode = json.loads(row[0]), "precomputed"
            else:
                summary = aggregate_dashboard(connection, agent, deadline=time.monotonic() + self.settings.query_timeout_seconds)
                mode = "on-demand"
        return {**self._envelope(manifest), **summary, "projectionMode": mode}
