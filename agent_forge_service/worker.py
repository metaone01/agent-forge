"""Local worker/query CLI. No command writes to GitHub or canonical metadata."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .errors import ServiceError
from .operational_log import OperationalLog, add_log_arguments, error_kind, from_arguments, server_logging
from .publishing import SnapshotStore, build_and_publish
from .query import Query, QueryService
from .settings import Settings
from .storage import backup_state, open_state, utc_now, writer_lock
from .collector import collection_status, enqueue_repository, run_repository_jobs
from .github_client import GitHubClient
from .jobs import JobQueue
from .discovery import DiscoveryPolicy, enqueue_window, enqueue_incremental, enqueue_history, run_discovery_jobs
from .canonical_mapping import sync_canonical_mappings, enqueue_mapping_repositories
from .batches import load_policy, plan_batch, run_batch, read_batch_status
from .scheduler import ScheduleOptions, run_schedule, scheduled_tick, scheduler_status
from .public_git import AnonymousGitClient
from .manifests import enqueue_manifest, plan_discovered_manifests, run_manifest_jobs, read_manifest_status
from .downloads import DownloadsClient, enqueue_downloads, plan_download_jobs, run_download_jobs, read_download_status
from .releases import enqueue_releases, plan_release_jobs, run_release_jobs, read_release_status
from .candidate_reviews import read_review,record_review,ACTIONS as REVIEW_ACTIONS
from .source_controls import read_control,record_control,ACTIONS as CONTROL_ACTIONS
from .recommendations import RecommendationClient, load_source, enqueue_recommendation, run_recommendation_jobs, read_recommendation_status
from .bundles import export_bundle, import_bundle, activate_snapshot, snapshot_status, DEFAULT_MAX_DATABASE_BYTES
from .state_recovery import create_state_backup, inspect_state_backup, restore_state_backup, recovery_status, DEFAULT_MAX_STATE_BYTES


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("build", "verify", "query", "detail", "backup", "collect-enqueue", "collect-run", "collect-status", "discover-enqueue", "discover-incremental", "discover-history", "discover-run", "map-canonical", "map-enqueue", "batch-plan", "batch-run", "batch-status", "manifest-enqueue", "manifest-plan", "manifest-run", "manifest-status", "metrics", "downloads-enqueue", "downloads-plan", "downloads-run", "downloads-status", "release-enqueue", "release-plan", "release-run", "release-status", "release-query", "candidate-review-inspect", "candidate-review-record", "source-control-inspect", "source-control-record", "source-controls-query", "recommendation-enqueue", "recommendation-run", "recommendation-status", "recommendation-query", "recommendation-sources", "offline-export", "offline-verify", "snapshot-export", "snapshot-import", "snapshot-activate", "snapshot-status", "retention-plan", "retention-apply", "retention-run", "state-backup", "state-backup-verify", "state-restore", "state-recovery-status", "schedule-tick", "schedule-run", "schedule-status", "storage-status", "refresh-status"):
        command = commands.add_parser(name)
        add_log_arguments(command)
        command.add_argument("--runtime", type=Path, required=True)
        command.add_argument("--snapshot-cursor-ttl-seconds", type=int, default=86400)
        if name in ("schedule-tick", "schedule-run", "storage-status"):
            command.add_argument("--storage-max-state-bytes", type=int, default=1024**3)
            command.add_argument("--min-free-bytes", type=int, default=512*1024**2)
            command.add_argument("--cache-max-entries", type=int, default=1024)
            command.add_argument("--cache-max-bytes", type=int, default=32*1024**2)
            command.add_argument("--cache-max-age-seconds", type=int, default=7*86400)
        if name in ("schedule-tick", "schedule-run"):
            command.add_argument("--catalog", type=Path, required=True)
            command.add_argument("--policy", type=Path, required=True)
            command.add_argument("--identity", help="non-secret local gh identity, required for GitHub lanes")
            command.add_argument("--max-requests", type=int, required=True)
            command.add_argument("--max-batch-seconds", type=int, required=True)
            command.add_argument("--poll-seconds", type=int, default=60)
            command.add_argument("--include-undated", action="store_true")
        elif name == "storage-status":
            pass
        elif name == "refresh-status":
            command.add_argument("--policy", type=Path, required=True)
            command.add_argument("--at", help="Check-history cutoff only; assignments use current private state")
        elif name == "schedule-status":
            command.add_argument("--policy-id", required=True)
        elif name in ("state-backup", "state-backup-verify", "state-restore", "state-recovery-status"):
            command.add_argument("--max-state-bytes", type=int, default=DEFAULT_MAX_STATE_BYTES)
            if name == "state-backup":
                command.add_argument("--output", type=Path, required=True)
            elif name in ("state-backup-verify", "state-restore"):
                command.add_argument("--backup", type=Path, required=True)
                command.add_argument("--expected-backup-sha256", required=True)
        elif name == "retention-run":
            command.add_argument("--keep-last", type=int, default=3)
            command.add_argument("--max-remove", type=int, default=3)
            command.add_argument("--apply", action="store_true")
        elif name == "retention-plan":
            command.add_argument("--keep-last", type=int, default=3)
            command.add_argument("--max-remove", type=int, default=3)
            command.add_argument("--output", type=Path, required=True)
        elif name == "retention-apply":
            command.add_argument("--plan", type=Path, required=True)
            command.add_argument("--expected-plan-sha256", required=True)
        elif name == "offline-export":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--agent", required=True)
            command.add_argument("--type", required=True, choices=("mcp", "plugin", "skill", "general", "bundle"))
            command.add_argument("--revision")
        elif name == "offline-verify":
            command.add_argument("--directory", type=Path, required=True)
            command.add_argument("--expected-export-sha256", required=True)
        elif name in ("snapshot-export", "snapshot-import", "snapshot-activate"):
            command.add_argument("--max-database-bytes", type=int, default=DEFAULT_MAX_DATABASE_BYTES)
            if name == "snapshot-export":
                command.add_argument("--output", type=Path, required=True)
                command.add_argument("--revision")
            elif name == "snapshot-import":
                command.add_argument("--bundle", type=Path, required=True)
                command.add_argument("--expected-bundle-sha256", required=True)
            else:
                command.add_argument("--revision", required=True)
                command.add_argument("--expected-manifest-sha256", required=True)
                command.add_argument("--expected-pointer-sha256", required=True, help="current pointer SHA-256, or absent for initial publication")
        elif name == "snapshot-status":
            command.add_argument("--limit", type=int, default=20)
        elif name in ("source-control-inspect","source-control-record"):
            command.add_argument("--target-json",required=True,help="Explicit public collection target JSON; no credentials")
            if name == "source-control-inspect":
                command.add_argument("--limit",type=int,default=20)
            else:
                command.add_argument("--expected-head",required=True)
                command.add_argument("--action",choices=CONTROL_ACTIONS,required=True)
                command.add_argument("--reason-code",required=True)
                command.add_argument("--request-key",required=True)
                command.add_argument("--operator-label",required=True)
        elif name == "source-controls-query":
            command.add_argument("--revision")
            command.add_argument("--limit",type=int,default=20)
            command.add_argument("--cursor")
        elif name in ("candidate-review-inspect","candidate-review-record"):
            command.add_argument("--candidate-id",required=True)
            if name == "candidate-review-inspect":
                command.add_argument("--limit",type=int,default=20)
            else:
                command.add_argument("--expected-evidence-sha256",required=True)
                command.add_argument("--expected-head",required=True)
                command.add_argument("--action",choices=tuple(REVIEW_ACTIONS),required=True)
                command.add_argument("--reason-code",required=True)
                command.add_argument("--request-key",required=True)
                command.add_argument("--operator-label",required=True,help="non-secret local operator label; no GitHub role assertion")
        elif name == "recommendation-enqueue":
            command.add_argument("--source-definition", type=Path, required=True)
            command.add_argument("--slot", required=True)
        elif name == "recommendation-run":
            command.add_argument("--identity", required=True, help="non-secret local gh identity; shares the account core budget")
            command.add_argument("--max-requests", type=int, required=True)
        elif name == "recommendation-status":
            command.add_argument("--limit", type=int, default=20)
        elif name in ("recommendation-query", "recommendation-sources"):
            command.add_argument("--revision")
            command.add_argument("--limit", type=int, default=20)
            command.add_argument("--cursor")
            if name == "recommendation-query":
                command.add_argument("--id")
                command.add_argument("--source")
                command.add_argument("--version")
                command.add_argument("--status", choices=("all", "active", "withdrawn"), default="all")
        elif name in ("release-enqueue", "release-plan"):
            command.add_argument("--slot", required=True)
            command.add_argument("--max-pages", type=int, required=True)
            if name == "release-enqueue":
                command.add_argument("--repository-id", type=int, required=True)
            else:
                command.add_argument("--limit", type=int, required=True)
        elif name == "release-run":
            command.add_argument("--max-requests", type=int, required=True)
        elif name == "release-query":
            command.add_argument("--id")
            command.add_argument("--repository-id")
            command.add_argument("--version")
            command.add_argument("--association",choices=("all","declared","repository_context"),default="all")
            command.add_argument("--revision")
            command.add_argument("--cursor")
            command.add_argument("--limit",type=int,default=20)
        elif name == "release-status":
            command.add_argument("--limit", type=int, default=20)
        elif name in ("downloads-enqueue", "downloads-plan"):
            command.add_argument("--slot", required=True)
            if name == "downloads-enqueue":
                command.add_argument("--package", required=True)
            else:
                command.add_argument("--limit", type=int, required=True)
        elif name == "downloads-run":
            command.add_argument("--max-requests", type=int, required=True)
        elif name == "downloads-status":
            command.add_argument("--limit", type=int, default=20)
        elif name in ("manifest-enqueue", "manifest-plan"):

            command.add_argument("--path", required=True)
            command.add_argument("--format", choices=("claude_plugin", "agent_skill"), required=True)
            command.add_argument("--agent", required=True, help="explicit association only, not compatibility evidence")
            command.add_argument("--slot", required=True)
            if name == "manifest-enqueue":
                command.add_argument("--repository-id", type=int, required=True)
            else:
                command.add_argument("--limit", type=int, required=True)
        elif name == "manifest-run":
            command.add_argument("--max-requests", type=int, required=True)
        elif name == "manifest-status":
            command.add_argument("--limit", type=int, default=20)
        elif name == "build":
            command.add_argument("--catalog", type=Path, default=Path("."))
            command.add_argument("--as-of", help="ISO 8601 publication time with timezone")
            command.add_argument("--revision")
            command.add_argument("--include-undated", action="store_true")
        elif name == "map-canonical":
            command.add_argument("--catalog", type=Path, required=True)
        elif name == "map-enqueue":
            command.add_argument("--slot", required=True)
            command.add_argument("--limit", type=int, required=True)
        elif name == "batch-plan":
            command.add_argument("--policy", type=Path, required=True)
        elif name == "batch-run":
            command.add_argument("--batch-id", required=True)
            command.add_argument("--identity", help="required when the planned batch has GitHub lanes")
            command.add_argument("--max-requests", type=int, required=True)
        elif name == "batch-status":
            command.add_argument("--batch-id")
            command.add_argument("--limit", type=int, default=20)
        elif name == "verify":
            command.add_argument("--revision")
        elif name == "query":
            command.add_argument("--q")
            command.add_argument("--agent")
            command.add_argument("--type")
            command.add_argument("--scope", choices=("catalog", "discovery"), default="catalog")
            command.add_argument("--revision")
            command.add_argument("--sort")
            command.add_argument("--metric-source")
            command.add_argument("--recommendation-source")
            command.add_argument("--review-disposition")
            command.add_argument("--limit", type=int, default=20)
            command.add_argument("--cursor")
        elif name == "detail":
            command.add_argument("--scope", choices=("catalog", "discovery"), default="catalog")
            command.add_argument("id")
            command.add_argument("--revision")
        elif name == "metrics":
            command.add_argument("id")
            command.add_argument("--source")
            command.add_argument("--limit", type=int, default=20)
            command.add_argument("--cursor")
        elif name == "backup":
            command.add_argument("--output", type=Path, required=True)
        elif name == "collect-enqueue":
            command.add_argument("--repository", required=True, help="public owner/name or stable GitHub numeric ID")
            command.add_argument("--slot", required=True, help="explicit ISO 8601 batch slot")
            command.add_argument("--bootstrap", action="store_true", required=True, help="known seeds are not new discoveries")
        elif name in ("collect-run", "discover-run"):
            command.add_argument("--identity", required=True, help="opaque credential-profile label; never pass a token")
            command.add_argument("--max-requests", type=int, required=True)
        elif name == "collect-status":
            command.add_argument("--limit", type=int, default=20)
        elif name in ("discover-enqueue", "discover-incremental", "discover-history"):
            command.add_argument("--topic", required=True)
            command.add_argument("--min-shard-seconds", type=int, default=60)
            command.add_argument("--max-shards", type=int, default=255)
            if name == "discover-history":
                command.add_argument("--start", required=True)
                command.add_argument("--end", required=True)
                command.add_argument("--cycle", required=True)
                command.add_argument("--chunk-hours", type=int, required=True)
            else:
                command.add_argument("--field", choices=("created", "pushed"), required=True)
                command.add_argument("--generation", required=True)
                if name == "discover-enqueue":
                    command.add_argument("--start", required=True)
                    command.add_argument("--end", required=True)
                else:
                    command.add_argument("--bootstrap-start", required=True)
                    command.add_argument("--cutoff", required=True)
                    command.add_argument("--overlap-hours", type=int, required=True)
    return root


def _run(args, events) -> int:
    try:
        if args.command in ("state-backup", "state-restore", "state-recovery-status", "schedule-tick", "schedule-run", "schedule-status", "refresh-status") and args.runtime.absolute() != args.runtime.resolve():
            raise ValueError("private state runtime must not be a link or contain path traversal")
        settings = Settings(catalog_root=getattr(args, "catalog", Path(".")), runtime_root=args.runtime, snapshot_cursor_ttl_seconds=args.snapshot_cursor_ttl_seconds)
        if args.command in ("schedule-tick","schedule-run","storage-status"):
            from .resource_budget import ResourceLimits,CachePolicy
            resource_limits=ResourceLimits(args.storage_max_state_bytes,args.min_free_bytes,
                CachePolicy(args.cache_max_entries,args.cache_max_bytes,args.cache_max_age_seconds))
        if args.command == "storage-status":
            from .resource_budget import storage_status
            result=storage_status(args.runtime,resource_limits)
        elif args.command == "refresh-status":
            from .refresh_status import read_refresh_status
            result = read_refresh_status(settings.state_path, load_policy(args.policy), now=args.at)
        elif args.command == "schedule-status":
            result = scheduler_status(settings.state_path,args.policy_id)
        elif args.command in ("schedule-tick", "schedule-run"):
            import os
            options = ScheduleOptions(args.max_requests,args.max_batch_seconds,args.poll_seconds,args.include_undated,resource_limits)
            policy = load_policy(args.policy)
            values = {"identity":args.identity,"owner":"scheduled-worker:"+str(os.getpid())}
            if args.command == "schedule-tick":
                result = scheduled_tick(settings,policy,options,**values)
            else:
                import signal
                import threading
                stop = threading.Event()
                handlers = {}
                if threading.current_thread() is threading.main_thread():
                    for name in ("SIGINT", "SIGTERM"):
                        kind = getattr(signal,name,None)
                        if kind is not None:
                            handlers[kind] = signal.getsignal(kind)
                            signal.signal(kind,lambda *_:stop.set())
                try:
                    result = run_schedule(settings,policy,options,stop=stop,emit=events.worker_event,**values)
                finally:
                    for kind,handler in handlers.items():
                        signal.signal(kind,handler)
        elif args.command == "state-backup":
            result = create_state_backup(settings, args.output, max_state_bytes=args.max_state_bytes)
        elif args.command == "state-backup-verify":
            result = inspect_state_backup(args.backup, expected_backup_sha256=args.expected_backup_sha256, max_state_bytes=args.max_state_bytes)
        elif args.command == "state-restore":
            result = restore_state_backup(settings, args.backup, expected_backup_sha256=args.expected_backup_sha256, max_state_bytes=args.max_state_bytes)
        elif args.command == "state-recovery-status":
            result = recovery_status(settings, max_state_bytes=args.max_state_bytes)
        elif args.command == "retention-run":
            from .retention import run_cleanup
            result = run_cleanup(settings, keep_last=args.keep_last, max_remove=args.max_remove, apply=args.apply)
        elif args.command == "retention-plan":
            from .retention import plan_cleanup
            from .storage import encode_json
            output = args.output.absolute()
            if output != output.resolve() or output.exists() or output.is_symlink() or output.is_relative_to(settings.runtime_root) or output.is_relative_to(settings.catalog_root):
                raise ValueError("cleanup plan output must be a new file outside runtime/canonical sources")
            result = plan_cleanup(settings, keep_last=args.keep_last, max_remove=args.max_remove)
            with output.open("x", encoding="utf-8") as stream:
                stream.write(encode_json(result["plan"]))
            result = {"planPath": str(output), "planSha256": result["planSha256"], "candidateCount": len(result["plan"]["candidates"]), "deleted": False}
        elif args.command == "retention-apply":
            from .retention import apply_cleanup
            from .bundles import _json
            supplied, _ = _json(args.plan, 2 * 1024 ** 2)
            result = apply_cleanup(settings, supplied, expected_plan_sha256=args.expected_plan_sha256)
        elif args.command == "offline-export":
            from .offline import export_offline
            result = export_offline(settings, args.output, agent=args.agent, package_type=args.type, revision=args.revision)
        elif args.command == "offline-verify":
            from .offline import verify_offline
            result = verify_offline(args.directory, expected_export_sha256=args.expected_export_sha256)
        elif args.command == "snapshot-export":
            result = export_bundle(settings, args.output, revision=args.revision, max_database_bytes=args.max_database_bytes)
        elif args.command == "snapshot-import":
            result = import_bundle(settings, args.bundle, expected_bundle_sha256=args.expected_bundle_sha256, max_database_bytes=args.max_database_bytes)
        elif args.command == "snapshot-activate":
            result = activate_snapshot(settings, args.revision, expected_manifest_sha256=args.expected_manifest_sha256, expected_pointer_sha256=args.expected_pointer_sha256, max_database_bytes=args.max_database_bytes)
        elif args.command == "snapshot-status":
            result = snapshot_status(settings, args.limit)
        elif args.command == "source-control-inspect":
            from .declarations import strict_json
            result = read_control(settings.state_path,strict_json(args.target_json.encode("utf-8"),maximum=2048),limit=args.limit)
        elif args.command == "release-query":
            result=QueryService(settings).releases(args.id,repository_id=args.repository_id,version=args.version,association=args.association,limit=args.limit,cursor=args.cursor,revision=args.revision)
        elif args.command == "source-controls-query":
            result = QueryService(settings).source_controls(limit=args.limit,cursor=args.cursor,revision=args.revision)
        elif args.command == "candidate-review-inspect":
            result = read_review(settings.state_path,args.candidate_id,now=utc_now(),limit=args.limit)
        elif args.command == "build":
            now = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of else None
            result = build_and_publish(settings, now=now, revision=args.revision, include_undated=args.include_undated)
        elif args.command == "verify":
            store = SnapshotStore(settings.snapshots)
            result = store.load(args.revision) if args.revision else store.current()
        elif args.command == "query":
            result = QueryService(settings).packages(Query(q=args.q, agent=args.agent, type=args.type, scope=args.scope, sort=args.sort, limit=args.limit, metric_source=args.metric_source, recommendation_source=args.recommendation_source, review_disposition=args.review_disposition), cursor=args.cursor, revision=args.revision)
        elif args.command == "detail":
            service = QueryService(settings)
            result = service.discovery(args.id, revision=args.revision) if args.scope == "discovery" else service.package(args.id, revision=args.revision)
        elif args.command == "metrics":
            result = QueryService(settings).metrics(args.id, limit=args.limit, cursor=args.cursor, source=args.source)
        elif args.command == "batch-status":
            result = read_batch_status(settings.state_path, args.batch_id, args.limit)
        elif args.command == "manifest-status":
            result = read_manifest_status(settings.state_path, args.limit)
        elif args.command == "recommendation-query":
            result = QueryService(settings).recommendations(args.id, source=args.source, version=args.version, status=args.status, limit=args.limit, cursor=args.cursor, revision=args.revision)
        elif args.command == "recommendation-sources":
            result = QueryService(settings).recommendation_sources(limit=args.limit, cursor=args.cursor, revision=args.revision)
        elif args.command == "recommendation-status":
            result = read_recommendation_status(settings.state_path, limit=args.limit)
        elif args.command == "release-status":
            result = read_release_status(settings.state_path, args.limit)
        elif args.command == "downloads-status":
            result = read_download_status(settings.state_path, args.limit)
        elif args.command == "collect-status":
            result = collection_status(settings.state_path, args.limit)
        elif args.command in ("collect-enqueue", "collect-run", "discover-enqueue", "discover-incremental", "discover-history", "discover-run", "map-canonical", "map-enqueue", "batch-plan", "batch-run", "manifest-enqueue", "manifest-plan", "manifest-run", "downloads-enqueue", "downloads-plan", "downloads-run", "release-enqueue", "release-plan", "release-run", "recommendation-enqueue", "recommendation-run", "candidate-review-record", "source-control-record"):
            if args.command in ("candidate-review-record","source-control-record") and (settings.state_path.is_symlink() or not settings.state_path.is_file()):
                raise ValueError("review record needs an existing unlinked private state")
            with writer_lock(settings.runtime_root):
                connection = open_state(settings.state_path)
                try:
                    if args.command == "source-control-record":
                        from .declarations import strict_json
                        result = record_control(connection,value=strict_json(args.target_json.encode("utf-8"),maximum=2048),action=args.action,reason_code=args.reason_code,
                            expected_head=args.expected_head,request_key=args.request_key,operator_label=args.operator_label,now=utc_now())
                    elif args.command == "candidate-review-record":
                        result = record_review(connection,candidate_id=args.candidate_id,expected_fingerprint=args.expected_evidence_sha256,
                            expected_head=args.expected_head,action=args.action,reason_code=args.reason_code,request_key=args.request_key,
                            operator_label=args.operator_label,now=utc_now())
                    elif args.command == "recommendation-enqueue":
                        with connection:
                            result = {"scanId": enqueue_recommendation(connection, definition=load_source(args.source_definition), slot=args.slot, now=utc_now()), "queued": True, "canonicalWrites": 0}
                    elif args.command == "recommendation-run":
                        import os
                        result = run_recommendation_jobs(connection, RecommendationClient(connection, identity=args.identity), owner="local-worker:" + str(os.getpid()), max_requests=args.max_requests)
                    elif args.command == "release-enqueue":
                        with connection:
                            result = {"scanId": enqueue_releases(connection, github_id=args.repository_id, slot=args.slot, max_pages=args.max_pages, now=utc_now()), "queued": True}
                    elif args.command == "release-plan":
                        result = plan_release_jobs(connection, slot=args.slot, limit=args.limit, max_pages=args.max_pages, now=utc_now())
                    elif args.command == "release-run":
                        import os
                        result = run_release_jobs(connection, AnonymousGitClient(connection), owner="local-worker:" + str(os.getpid()), max_requests=args.max_requests)
                    elif args.command == "downloads-enqueue":
                        with connection:
                            result = {"jobId": enqueue_downloads(connection, package=args.package, slot=args.slot, now=utc_now()), "queued": True}
                    elif args.command == "downloads-plan":
                        result = plan_download_jobs(connection, SnapshotStore(settings.snapshots), slot=args.slot, limit=args.limit, now=utc_now())
                    elif args.command == "downloads-run":
                        import os
                        result = run_download_jobs(connection, DownloadsClient(connection), owner="local-worker:" + str(os.getpid()), max_requests=args.max_requests)
                    elif args.command in ("manifest-enqueue", "manifest-plan"):

                        with connection:
                            values = dict(path=args.path, kind=args.format, agent=args.agent, slot=args.slot, now=utc_now())
                            if args.command == "manifest-enqueue":
                                result = {"scanId": enqueue_manifest(connection, github_id=args.repository_id, **values), "queued": True}
                            else:
                                result = plan_discovered_manifests(connection, limit=args.limit, **values)
                    elif args.command == "manifest-run":
                        import os
                        result = run_manifest_jobs(connection, AnonymousGitClient(connection),
                            owner="local-worker:" + str(os.getpid()), max_requests=args.max_requests)
                    elif args.command == "batch-plan":
                        result = plan_batch(connection,load_policy(args.policy),now=utc_now(),ranking_store=SnapshotStore(settings.snapshots))
                    elif args.command == "batch-run":
                        import os
                        result = run_batch(connection,GitHubClient(connection,identity=args.identity) if args.identity else None,args.batch_id,
                                           owner="local-worker:"+str(os.getpid()),max_requests=args.max_requests)
                    elif args.command == "map-canonical":
                        result = sync_canonical_mappings(connection, settings.catalog_root, now=utc_now())
                    elif args.command == "map-enqueue":
                        result = enqueue_mapping_repositories(connection, slot=args.slot, limit=args.limit, now=utc_now())
                    elif args.command == "collect-enqueue":
                        repository = int(args.repository) if args.repository.isascii() and args.repository.isdigit() else args.repository
                        with connection:
                            job_id = enqueue_repository(JobQueue(connection), repository=repository,
                                                        slot=args.slot, bootstrap=args.bootstrap, now=utc_now())
                        result = {"jobId": job_id, "queued": True}
                    elif args.command in ("collect-run", "discover-run"):
                        import os
                        runner = run_repository_jobs if args.command == "collect-run" else run_discovery_jobs
                        result = runner(connection, GitHubClient(connection, identity=args.identity),
                                        owner="local-worker:" + str(os.getpid()), max_requests=args.max_requests)
                    else:
                        policy = DiscoveryPolicy(args.min_shard_seconds, args.max_shards)
                        with connection:
                            if args.command == "discover-enqueue":
                                run_id = enqueue_window(connection, topic=args.topic, field=args.field, start=args.start,
                                                        end=args.end, generation=args.generation, now=utc_now(), policy=policy)
                            elif args.command == "discover-incremental":
                                run_id = enqueue_incremental(connection, topic=args.topic, field=args.field,
                                    bootstrap_start=args.bootstrap_start, cutoff=args.cutoff, generation=args.generation,
                                    overlap_seconds=args.overlap_hours * 3600, now=utc_now(), policy=policy)
                            else:
                                run_id = enqueue_history(connection, topic=args.topic, start=args.start, end=args.end,
                                    cycle=args.cycle, chunk_seconds=args.chunk_hours * 3600, now=utc_now(), policy=policy)
                        result = {"runId": run_id, "queued": run_id is not None}
                finally:
                    connection.close()
        else:
            if not settings.state_path.is_file():
                raise ValueError("no state database to back up")
            with writer_lock(settings.runtime_root):
                backup_state(settings.state_path, args.output)
            result = {"backup": str(args.output.resolve())}
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if args.command == "storage-status" and not result["allowed"]:
            return 3
        if (args.command == "retention-apply" or args.command == "retention-run" and args.apply) and not result["complete"]:
            return 3
        return 0
    except ServiceError as error:
        events.emit("worker_error", command=args.command, code=error.code, error_kind=error_kind(error))
        print(json.dumps({"error": {"code": error.code, "message": "Operation rejected; inspect command parameters and local status."}}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        events.emit("worker_error", command=args.command, code="worker_failed", error_kind=error_kind(error))
        print(json.dumps({"error": {"code": "worker_failed", "message": "Local operation failed.", "kind": error_kind(error)}}, ensure_ascii=False), file=sys.stderr)
        return 2


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        with from_arguments("worker", args, protected=(Path(__file__).resolve().parents[1],
                            getattr(args, "catalog", Path.cwd()), args.runtime),
                            console_enabled=args.log_directory is not None or args.command == "schedule-run") as events:
            events.emit("service_start", command=args.command)
            with server_logging(events):
                try:
                    result = _run(args, events)
                except Exception as error:
                    events.emit("worker_error", command=args.command, code="worker_failed", error_kind=error_kind(error))
                    print(json.dumps({"error": {"code": "worker_failed", "message": "Local operation failed.", "kind": error_kind(error)}}), file=sys.stderr)
                    result = 2
            events.emit("worker_complete", command=args.command, exit_code=result)
            return result
    except (OSError, ValueError, RuntimeError) as error:
        with OperationalLog("worker") as events:
            events.emit("worker_error", command=args.command, code="log_configuration_invalid", error_kind=error_kind(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
