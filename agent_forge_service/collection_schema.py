"""Atomic initial Worker schema; no unreleased development migrations."""
from __future__ import annotations
import sqlite3


def initialize_state(connection: sqlite3.Connection):
    try:
        connection.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE package_units (
                id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, bootstrap INTEGER NOT NULL,
                active INTEGER NOT NULL, canonical_hash TEXT NOT NULL,
                UNIQUE(type, name)
            );
            CREATE TABLE change_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE,
                unit_id TEXT NOT NULL, event_type TEXT NOT NULL,
                observed_at TEXT NOT NULL, evidence_json TEXT NOT NULL
            );

            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL,
                payload_json TEXT NOT NULL, state TEXT NOT NULL
                    CHECK(state IN ('pending','leased','retry_wait','succeeded','dead')),
                attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
                next_run TEXT NOT NULL, lease_owner TEXT, lease_token TEXT, lease_until TEXT,
                last_error TEXT, created_at TEXT NOT NULL, finished_at TEXT,
                CHECK(attempts >= 0 AND max_attempts BETWEEN 1 AND 100)
            );
            CREATE INDEX jobs_due ON jobs(state, next_run, created_at, id);
            CREATE INDEX jobs_kind_scope ON jobs(kind,scope,created_at);
            CREATE TABLE checkpoints (
                source_id TEXT NOT NULL, shard_key TEXT NOT NULL,
                cursor_json TEXT, watermark TEXT, last_success_at TEXT,
                completeness TEXT NOT NULL, details_json TEXT NOT NULL,
                PRIMARY KEY(source_id, shard_key)
            );
            CREATE TABLE repositories (
                github_id INTEGER PRIMARY KEY, full_name TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, bootstrap INTEGER NOT NULL,
                last_seen_at TEXT NOT NULL, last_success_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL, CHECK(github_id > 0)
            );
            CREATE TABLE collection_evidence (
                hash TEXT PRIMARY KEY, representation TEXT NOT NULL, body_json TEXT NOT NULL
            );
            CREATE TABLE repository_observations (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
                github_id INTEGER NOT NULL REFERENCES repositories(github_id),
                observed_at TEXT NOT NULL, checked_at TEXT NOT NULL,
                response_status INTEGER NOT NULL, evidence_hash TEXT NOT NULL REFERENCES collection_evidence(hash),
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX repository_history
                ON repository_observations(github_id, observed_at);
            CREATE TABLE metric_observations (
                id TEXT PRIMARY KEY, observation_id TEXT NOT NULL
                    REFERENCES repository_observations(id),
                subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
                metric TEXT NOT NULL, observed_at TEXT NOT NULL, sample_json TEXT NOT NULL,
                UNIQUE(observation_id, metric)
            );
            CREATE INDEX metric_history
                ON metric_observations(subject_kind, subject_id, metric, observed_at);
            CREATE TABLE http_cache (
                request_key TEXT PRIMARY KEY, credential_identity TEXT NOT NULL,
                etag TEXT, last_modified TEXT, body_json TEXT NOT NULL,
                body_hash TEXT NOT NULL, observed_at TEXT NOT NULL, checked_at TEXT NOT NULL
            );
            CREATE TABLE rate_limits (
                credential_identity TEXT NOT NULL, resource TEXT NOT NULL,
                remaining INTEGER, quota_limit INTEGER, reset_at TEXT,
                blocked_until TEXT, next_request_at TEXT,
                PRIMARY KEY(credential_identity, resource)
            );
            CREATE TABLE discovery_streams (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, mode TEXT NOT NULL,
                topic TEXT NOT NULL, time_field TEXT NOT NULL,
                range_start TEXT NOT NULL, range_end TEXT, watermark TEXT,
                active_run_id TEXT, config_json TEXT NOT NULL, last_success_at TEXT
            );
            CREATE TABLE discovery_runs (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
                topic TEXT NOT NULL, time_field TEXT NOT NULL,
                window_start TEXT NOT NULL, window_end TEXT NOT NULL,
                generation TEXT NOT NULL, stream_id TEXT REFERENCES discovery_streams(id),
                policy_json TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL, finished_at TEXT
            );
            CREATE UNIQUE INDEX discovery_stream_generation
                ON discovery_runs(stream_id,generation) WHERE stream_id IS NOT NULL;
            CREATE TABLE discovery_shards (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES discovery_runs(id),
                parent_id TEXT REFERENCES discovery_shards(id),
                window_start TEXT NOT NULL, window_end TEXT NOT NULL,
                state TEXT NOT NULL, expected_total INTEGER,
                next_page INTEGER NOT NULL DEFAULT 1, reason TEXT
            );
            CREATE INDEX discovery_shard_parent ON discovery_shards(parent_id);
            CREATE INDEX discovery_shards_run_state ON discovery_shards(run_id,state);
            CREATE INDEX discovery_runs_created ON discovery_runs(created_at DESC,id);
            CREATE TABLE discovery_tasks (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                run_id TEXT NOT NULL REFERENCES discovery_runs(id),
                shard_id TEXT NOT NULL REFERENCES discovery_shards(id),
                page INTEGER NOT NULL, UNIQUE(shard_id,page)
            );
            CREATE INDEX discovery_tasks_run ON discovery_tasks(run_id,job_id);
            CREATE TABLE discovery_pages (
                job_id TEXT PRIMARY KEY REFERENCES discovery_tasks(job_id),
                evidence_hash TEXT NOT NULL REFERENCES collection_evidence(hash),
                observed_at TEXT NOT NULL, checked_at TEXT NOT NULL,
                response_status INTEGER NOT NULL, total_count INTEGER NOT NULL,
                incomplete INTEGER NOT NULL, result_count INTEGER NOT NULL
            );
            CREATE TABLE discovery_candidates (
                github_id INTEGER PRIMARY KEY, full_name TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                bootstrap INTEGER NOT NULL, classification TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE discovery_hits (
                github_id INTEGER NOT NULL REFERENCES discovery_candidates(github_id),
                source_id TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                PRIMARY KEY(github_id,source_id)
            );
            CREATE TABLE discovery_page_hits (
                job_id TEXT NOT NULL REFERENCES discovery_pages(job_id),
                github_id INTEGER NOT NULL REFERENCES discovery_candidates(github_id),
                PRIMARY KEY(job_id,github_id)
            );
            CREATE TABLE canonical_mapping_records (
                package_id TEXT NOT NULL, version TEXT NOT NULL,
                package_type TEXT NOT NULL, package_name TEXT NOT NULL,
                record_path TEXT NOT NULL, canonical_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                active INTEGER NOT NULL CHECK(active IN (0,1)), diagnostics_json TEXT NOT NULL,
                PRIMARY KEY(package_id,version)
            );
            CREATE TABLE canonical_repository_links (
                id TEXT PRIMARY KEY, package_id TEXT NOT NULL, version TEXT NOT NULL,
                repository TEXT NOT NULL, ref TEXT, subpath TEXT,
                path_status TEXT NOT NULL CHECK(path_status IN ('context','root','explicit','ambiguous')),
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                active INTEGER NOT NULL CHECK(active IN (0,1)),
                resolution TEXT NOT NULL CHECK(resolution IN ('pending','resolved','conflict')),
                evidence_json TEXT NOT NULL,
                github_id INTEGER REFERENCES repositories(github_id), resolved_at TEXT, conflict_reason TEXT,
                FOREIGN KEY(package_id,version) REFERENCES canonical_mapping_records(package_id,version)
            );
            CREATE INDEX canonical_links_alias ON canonical_repository_links(repository,active,resolution);
            CREATE INDEX canonical_links_pending ON canonical_repository_links(resolution,active,repository);
            CREATE INDEX canonical_links_identity ON canonical_repository_links(github_id,active,resolution);
            CREATE TABLE repository_aliases (
                alias TEXT NOT NULL, github_id INTEGER NOT NULL REFERENCES repositories(github_id),
                first_observed_at TEXT NOT NULL, last_checked_at TEXT NOT NULL,
                observation_id TEXT REFERENCES repository_observations(id), PRIMARY KEY(alias,github_id)
            );

            CREATE TABLE canonical_mapping_targets (
                package_id TEXT NOT NULL, version TEXT NOT NULL, agent_id TEXT NOT NULL,
                PRIMARY KEY(package_id,version,agent_id),
                FOREIGN KEY(package_id,version) REFERENCES canonical_mapping_records(package_id,version)
            );
            CREATE TABLE batch_policies (id TEXT PRIMARY KEY, config_json TEXT NOT NULL);
            CREATE TABLE refresh_resources (
                id TEXT PRIMARY KEY, github_id INTEGER NOT NULL UNIQUE,
                memberships_json TEXT NOT NULL, first_seen_at TEXT NOT NULL,
                bootstrap INTEGER NOT NULL, active INTEGER NOT NULL,
                last_success_at TEXT, next_due_at TEXT NOT NULL,
                tier TEXT NOT NULL, reasons_json TEXT NOT NULL, policy_id TEXT NOT NULL
            );
            CREATE INDEX refresh_due ON refresh_resources(active,next_due_at,id);
            CREATE TABLE refresh_cohorts (
                id TEXT PRIMARY KEY, policy_id TEXT NOT NULL REFERENCES batch_policies(id),
                utc_day TEXT NOT NULL, ranking_revision TEXT NOT NULL,
                ranking_status TEXT NOT NULL, rankings_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(policy_id,utc_day)
            );
            CREATE TABLE batch_runs (
                id TEXT PRIMARY KEY, policy_id TEXT NOT NULL REFERENCES batch_policies(id),
                slot TEXT NOT NULL, cohort_id TEXT NOT NULL REFERENCES refresh_cohorts(id),
                planned_at TEXT NOT NULL, summary_json TEXT NOT NULL, quotas_json TEXT NOT NULL,
                next_lane INTEGER NOT NULL DEFAULT 0, UNIQUE(policy_id,slot)
            );
            CREATE TABLE batch_jobs (
                batch_id TEXT NOT NULL REFERENCES batch_runs(id), job_id TEXT NOT NULL REFERENCES jobs(id),
                lane TEXT NOT NULL, resource_id TEXT REFERENCES refresh_resources(id),
                PRIMARY KEY(batch_id,job_id)
            );
            CREATE INDEX batch_jobs_lane ON batch_jobs(batch_id,lane,job_id);
            CREATE TABLE batch_streams (
                batch_id TEXT NOT NULL REFERENCES batch_runs(id), stream_id TEXT NOT NULL REFERENCES discovery_streams(id),
                lane TEXT NOT NULL, initial_run_id TEXT REFERENCES discovery_runs(id),
                PRIMARY KEY(batch_id,stream_id)
            );
            CREATE TABLE batch_history_cycles (
                policy_id TEXT NOT NULL REFERENCES batch_policies(id), topic TEXT NOT NULL, cycle INTEGER NOT NULL,
                range_end TEXT NOT NULL, stream_id TEXT NOT NULL REFERENCES discovery_streams(id),
                PRIMARY KEY(policy_id,topic)
            );
            CREATE TABLE batch_attempts (
                id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batch_runs(id), lane TEXT NOT NULL,
                reserved_at TEXT NOT NULL, requests INTEGER CHECK(requests IN (0,1)), result_json TEXT
            );
            CREATE INDEX batch_attempts_lane ON batch_attempts(batch_id,lane);
            CREATE TABLE manifest_scans (
                id TEXT PRIMARY KEY, github_id INTEGER NOT NULL, path TEXT NOT NULL,
                format TEXT NOT NULL, agent TEXT NOT NULL, slot TEXT NOT NULL,
                state TEXT NOT NULL, step INTEGER NOT NULL, progress_json TEXT NOT NULL,
                created_at TEXT NOT NULL, finished_at TEXT, reason TEXT
            );
            CREATE INDEX manifest_scans_created ON manifest_scans(created_at DESC,id);
            CREATE TABLE manifest_tasks (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                scan_id TEXT NOT NULL REFERENCES manifest_scans(id), step INTEGER NOT NULL,
                UNIQUE(scan_id,step)
            );
            CREATE TABLE manifest_fetches (
                job_id TEXT PRIMARY KEY REFERENCES manifest_tasks(job_id),
                evidence_hash TEXT NOT NULL REFERENCES collection_evidence(hash),
                endpoint TEXT NOT NULL, observed_at TEXT NOT NULL, checked_at TEXT NOT NULL,
                response_status INTEGER NOT NULL
            );
            CREATE TABLE manifest_candidates (
                id TEXT PRIMARY KEY, github_id INTEGER NOT NULL, path TEXT NOT NULL,
                format TEXT NOT NULL, agent TEXT NOT NULL, commit_sha TEXT NOT NULL,
                blob_sha TEXT NOT NULL, content_hash TEXT NOT NULL, state TEXT NOT NULL,
                record_json TEXT NOT NULL, issues_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL, UNIQUE(github_id,path,format,agent,commit_sha)
            );
            CREATE INDEX manifest_candidates_created ON manifest_candidates(created_at DESC,id);
            CREATE TABLE manifest_candidate_events (
                candidate_id TEXT NOT NULL REFERENCES manifest_candidates(id),
                state TEXT NOT NULL, observed_at TEXT NOT NULL,
                PRIMARY KEY(candidate_id,state)
            );
            CREATE TABLE npm_download_observations (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
                package TEXT NOT NULL, start_day TEXT NOT NULL, end_day TEXT NOT NULL,
                observed_at TEXT NOT NULL, checked_at TEXT NOT NULL, response_status INTEGER NOT NULL,
                evidence_hash TEXT NOT NULL REFERENCES collection_evidence(hash), body_json TEXT NOT NULL
            );
            CREATE INDEX npm_download_history ON npm_download_observations(package,end_day DESC,checked_at DESC,id);
            CREATE INDEX npm_download_window ON npm_download_observations(start_day,end_day,package);
            CREATE INDEX npm_job_window ON jobs(json_extract(payload_json,'$.start'),json_extract(payload_json,'$.end'),created_at,id,scope)
                WHERE kind='npm_downloads';
            CREATE TABLE download_refresh_cohorts (
                id TEXT PRIMARY KEY, policy_id TEXT NOT NULL REFERENCES batch_policies(id),
                utc_day TEXT NOT NULL, ranking_revision TEXT NOT NULL, ranking_status TEXT NOT NULL,
                rankings_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(policy_id,utc_day)
            );
            CREATE TABLE release_scans (
                id TEXT PRIMARY KEY, github_id INTEGER NOT NULL REFERENCES repositories(github_id),
                slot TEXT NOT NULL, max_pages INTEGER NOT NULL, next_page INTEGER NOT NULL,
                state TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT, reason TEXT,
                UNIQUE(github_id,slot)
            );
            CREATE INDEX release_scans_repo ON release_scans(github_id,slot DESC,id);
            CREATE TABLE release_tasks (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id), scan_id TEXT NOT NULL REFERENCES release_scans(id),
                page INTEGER NOT NULL, UNIQUE(scan_id,page)
            );
            CREATE TABLE release_pages (
                job_id TEXT PRIMARY KEY REFERENCES release_tasks(job_id),
                evidence_hash TEXT NOT NULL REFERENCES collection_evidence(hash),
                observed_at TEXT NOT NULL, checked_at TEXT NOT NULL, response_status INTEGER NOT NULL,
                item_count INTEGER NOT NULL
            );
            CREATE TABLE release_observations (
                job_id TEXT NOT NULL REFERENCES release_pages(job_id), release_id INTEGER NOT NULL,
                metadata_json TEXT NOT NULL, PRIMARY KEY(job_id,release_id)
            );
            CREATE INDEX release_observations_identity ON release_observations(release_id,job_id);
            CREATE TABLE recommendation_sources (
                id TEXT PRIMARY KEY, definition_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE recommendation_scans (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES recommendation_sources(id),
                slot TEXT NOT NULL, state TEXT NOT NULL, step INTEGER NOT NULL,
                progress_json TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT, reason TEXT,
                UNIQUE(source_id,slot)
            );
            CREATE TABLE recommendation_tasks (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                scan_id TEXT NOT NULL REFERENCES recommendation_scans(id), step INTEGER NOT NULL,
                UNIQUE(scan_id,step)
            );
            CREATE TABLE recommendation_fetches (
                job_id TEXT PRIMARY KEY REFERENCES recommendation_tasks(job_id),
                evidence_hash TEXT NOT NULL REFERENCES collection_evidence(hash), endpoint TEXT NOT NULL,
                observed_at TEXT NOT NULL, checked_at TEXT NOT NULL, response_status INTEGER NOT NULL
            );
            CREATE TABLE recommendation_snapshots (
                id TEXT PRIMARY KEY, scan_id TEXT NOT NULL UNIQUE REFERENCES recommendation_scans(id),
                source_id TEXT NOT NULL REFERENCES recommendation_sources(id), repository_name TEXT NOT NULL,
                commit_sha TEXT NOT NULL, blob_sha TEXT NOT NULL, content_hash TEXT NOT NULL,
                selection_at TEXT NOT NULL, observed_at TEXT NOT NULL, checked_at TEXT NOT NULL,
                completed_at TEXT NOT NULL, member_count INTEGER NOT NULL, entry_count INTEGER NOT NULL,
                previous_id TEXT REFERENCES recommendation_snapshots(id), applied INTEGER NOT NULL CHECK(applied IN (0,1))
            );
            CREATE INDEX recommendation_snapshot_history
                ON recommendation_snapshots(source_id,selection_at,completed_at,id);
            CREATE TABLE recommendation_members (
                snapshot_id TEXT NOT NULL REFERENCES recommendation_snapshots(id), target_url TEXT NOT NULL,
                name TEXT NOT NULL, positions_json TEXT NOT NULL, source_rank INTEGER CHECK(source_rank IS NULL),
                PRIMARY KEY(snapshot_id,target_url)
            );
            CREATE TABLE recommendation_observations (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES recommendation_sources(id), target_url TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','withdrawn')),
                snapshot_id TEXT NOT NULL REFERENCES recommendation_snapshots(id),
                previous_id TEXT REFERENCES recommendation_snapshots(id), reason TEXT NOT NULL,
                observed_at TEXT NOT NULL, evidence_json TEXT NOT NULL,
                UNIQUE(source_id,target_url,snapshot_id)
            );
            CREATE INDEX recommendation_history
                ON recommendation_observations(source_id,target_url,observed_at,id);
            CREATE TABLE candidate_review_contexts (
                fingerprint TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,data_json TEXT NOT NULL,
                UNIQUE(candidate_id,fingerprint)
            );
            CREATE TABLE candidate_review_decisions (
                seq INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,candidate_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,action TEXT NOT NULL CHECK(action IN ('reject','unsupported','request_metadata','reopen')),
                reason_code TEXT NOT NULL,reviewed_at TEXT NOT NULL,previous_id TEXT REFERENCES candidate_review_decisions(id),
                request_key TEXT NOT NULL UNIQUE,operator_label TEXT NOT NULL,
                FOREIGN KEY(candidate_id,fingerprint) REFERENCES candidate_review_contexts(candidate_id,fingerprint)
            );
            CREATE INDEX candidate_review_history ON candidate_review_decisions(candidate_id,seq);
            CREATE TABLE collection_control_events (
                seq INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,target_id TEXT NOT NULL,target_json TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('disable','enable')),reason_code TEXT NOT NULL,
                recorded_at TEXT NOT NULL,previous_id TEXT REFERENCES collection_control_events(id),
                request_key TEXT NOT NULL UNIQUE,operator_label TEXT NOT NULL
            );
            CREATE INDEX collection_control_history ON collection_control_events(target_id,seq);
            CREATE TABLE collection_job_sources (
                job_id TEXT NOT NULL REFERENCES jobs(id),target_id TEXT NOT NULL,target_json TEXT NOT NULL,
                PRIMARY KEY(job_id,target_id)
            );
            CREATE INDEX collection_source_jobs ON collection_job_sources(target_id,job_id);
            INSERT INTO metadata VALUES ('storageContract','"agent-forge-worker-state"');
            PRAGMA user_version=1;
            COMMIT;

        """)
    except sqlite3.Error:
        connection.rollback()
        raise
