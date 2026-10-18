"""Public Release metadata: identity, bounded traversal, evidence and no version guessing."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from agent_forge_service.errors import ServiceError
from agent_forge_service.github import GhTransport
from agent_forge_service.github_client import GitHubClient, RequestPolicy
from agent_forge_service.public_git import ANONYMOUS_IDENTITY, AnonymousGitClient, AnonymousGitTransport, PublicGitRequest, sanitize_releases
from agent_forge_service.releases import SOURCE, enqueue_releases, plan_release_jobs, read_release_status, release_status, run_release_jobs
from agent_forge_service.storage import open_state, timestamp
from agent_forge_service.worker import main
from service_tests.temporary import cleanup_temporary
from service_tests.test_collector import NOW
from service_tests.test_manifests import Opener, Response


def item(identity=1, *, prerelease=False, downloads=10):
    return {"id": identity, "tag_name": "v1.0.0", "name": "", "draft": False,
            "prerelease": prerelease, "target_commitish": "main", "created_at": "2026-10-09T00:00:00Z",
            "published_at": "2026-10-10T00:00:00Z", "body": "never store release prose or scripts",
            "author": {"login": "not-retained"}, "assets": [{"id": identity + 1000, "name": "plugin.zip",
            "state": "uploaded", "size": 123, "download_count": downloads,
            "created_at": "2026-10-10T00:00:00Z", "updated_at": "2026-10-10T01:00:00Z",
            "browser_download_url": "https://example.invalid/do-not-fetch"}]}


class ReleaseMetadataTests(unittest.TestCase):
    def test_selected_public_fields_preserve_zero_empty_name_and_prerelease(self):
        row = sanitize_releases([item(prerelease=True, downloads=0)])["releases"][0]
        self.assertEqual("", row["name"]); self.assertTrue(row["prerelease"])
        self.assertEqual(0, row["assets"][0]["download_count"])
        self.assertNotIn("body", row); self.assertNotIn("author", row)
        self.assertNotIn("browser_download_url", row["assets"][0])
        self.assertEqual({"releases": [row]}, sanitize_releases({"releases": [row]}))

    def test_invalid_missing_unknown_counts_and_unpublished_are_not_zero_filled(self):
        for field, value in [("draft", True), ("draft", None), ("prerelease", 1), ("id", True),
                             ("id", 0), ("tag_name", "x\n"), ("published_at", None)]:
            body=item(); body[field]=value
            with self.subTest(field=field,value=value), self.assertRaises((ValueError,TypeError,AttributeError)):
                sanitize_releases([body])
        for value in [None, -1, True, 2**63]:
            body=item(); body["assets"][0]["download_count"]=value
            with self.subTest(value=value),self.assertRaises(ValueError):sanitize_releases([body])

    def test_duplicate_release_and_asset_identities_are_rejected(self):
        with self.assertRaises(ValueError):sanitize_releases([item(),item()])
        other=item(2); other["assets"][0]["id"]=1001
        with self.assertRaises(ValueError):sanitize_releases([item(),other])

    def test_page_and_asset_metadata_are_bounded(self):
        with self.assertRaises(ValueError):sanitize_releases([item(i+1) for i in range(101)])
        body=item();body["assets"]=[body["assets"][0]]*1001
        with self.assertRaises(ValueError):sanitize_releases([body])
        with self.assertRaises(ValueError):sanitize_releases({"releases": [],"extra":True})

    def test_stable_id_requests_and_pages_are_strict_and_auth_transport_rejects(self):
        request=PublicGitRequest("releases",123,page=2).validate()
        self.assertEqual("repositories/123/releases?per_page=100&page=2",request.endpoint)
        for repo in ["owner/repo", "https://evil.test", True, 0]:
            with self.assertRaises(ValueError):PublicGitRequest("releases",repo).validate()
        for page in [True,0,11,"1"]:
            with self.assertRaises(ValueError):PublicGitRequest("releases",123,page=page).validate()
        with self.assertRaises(ValueError):PublicGitRequest("releases",123,ref="main").validate()
        with self.assertRaises(ValueError):PublicGitRequest("repository",123,page=2).validate()
        with self.assertRaises(ValueError):request.cache_key("authenticated-label")
        with self.assertRaises(ValueError):GhTransport().get(request)
        self.assertNotEqual(request.cache_key(ANONYMOUS_IDENTITY),PublicGitRequest("releases",123).cache_key(ANONYMOUS_IDENTITY))

    def test_anonymous_transport_list_response_has_no_token_cookie_or_download(self):
        opener=Opener(lambda request:[item()])
        with patch.dict("os.environ",{"GH_TOKEN":"never-send","GITHUB_TOKEN":"never-send"}):
            result=AnonymousGitTransport(opener=opener).get(PublicGitRequest("releases",123))
        self.assertEqual(1,len(opener.requests));request=opener.requests[0]
        self.assertEqual("GET",request.get_method())
        self.assertEqual("https://api.github.com/repositories/123/releases?per_page=100&page=1",request.full_url)
        self.assertFalse(any(k.lower() in ("authorization","cookie") for k in request.headers))
        self.assertEqual(sanitize_releases([item()]),result.body)

    def test_redirect_size_and_malformed_payload_fail_closed(self):
        request=PublicGitRequest("releases",123)
        for response in [Response([],"https://evil.test"),Response([],"https://api.github.com/elsewhere",302),
                         Response({"message":"not an array"},"https://api.github.com/"+request.endpoint)]:
            with self.assertRaises(ServiceError):AnonymousGitTransport(opener=Opener(lambda r:response)).get(request)
        with self.assertRaises(ServiceError):
            AnonymousGitTransport(max_response_bytes=1024,opener=Opener(lambda r:[dict(item(),body="x"*2000)])).get(request)


class ReleasePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve();self.path=self.root/"state.db"
        self.addCleanup(cleanup_temporary,self.temp,self.root)
        self.db=open_state(self.path);self.addCleanup(self.db.close)
        self.now=NOW;self.body=[item()];self.code=200;self.headers={"ETag":'"release-test"'}
        self.opener=Opener(self.respond)
        self.client=AnonymousGitClient(self.db,transport=AnonymousGitTransport(opener=self.opener),
                                       policy=RequestPolicy(jitter_fraction=0),clock=lambda:self.now)
        self.seed_repo(123)

    def seed_repo(self,repo):
        with self.db:
            self.db.execute("INSERT INTO repositories VALUES (?,?,?,?,?,?,?)",(repo,f"owner/repo{repo}",timestamp(NOW),1,timestamp(NOW),timestamp(NOW),"{}"))

    def respond(self,request):
        self.now+=timedelta(seconds=2)
        return Response(self.body,request.full_url,self.code,self.headers)

    def enqueue(self,repo=123,slot=None,pages=3):
        with self.db:return enqueue_releases(self.db,github_id=repo,slot=slot or timestamp(self.now),max_pages=pages,now=self.now)

    def run_jobs(self,budget=1):
        return run_release_jobs(self.db,self.client,owner="release-test",max_requests=budget,clock=lambda:self.now)

    def scan(self,identity):return self.db.execute("SELECT * FROM release_scans WHERE id=?",(identity,)).fetchone()

    def link(self,repo=123):
        with self.db:
            self.db.execute("INSERT INTO canonical_mapping_records VALUES ('package','1','plugin','name','path','hash',?,?,1,'{}')",(timestamp(NOW),timestamp(NOW)))
            self.db.execute("INSERT INTO canonical_repository_links VALUES ('link','package','1',?,NULL,NULL,'root',?,?,1,'resolved','{}',?,?,NULL)",
                            (f"owner/repo{repo}",timestamp(NOW),timestamp(NOW),repo,timestamp(NOW)))

    def test_idempotent_enqueue_explicit_budget_and_known_identity(self):
        scan=self.enqueue();self.assertEqual(scan,self.enqueue(slot=timestamp(NOW)))
        with self.assertRaises(ServiceError):self.enqueue(slot=timestamp(NOW),pages=2)
        with self.assertRaises(ValueError):self.enqueue(repo=999)
        for pages in [True,0,11]:
            with self.assertRaises(ValueError):self.enqueue(pages=pages)
        self.assertEqual(1,self.db.execute("SELECT count(*) FROM jobs").fetchone()[0])

    def test_success_is_atomic_evidence_and_repository_scope_only(self):
        scan=self.enqueue();result=self.run_jobs();status=release_status(self.db)
        self.assertEqual(1,result["succeeded"]);self.assertEqual("traversal_complete",self.scan(scan)["state"])
        self.assertEqual(1,status["observations"]);self.assertIsNone(status["observedLatest"])
        for table in ["package_units","change_events","metric_observations","npm_download_observations"]:
            self.assertEqual(0,self.db.execute("SELECT count(*) FROM "+table).fetchone()[0])
        row=self.db.execute("SELECT p.*,e.body_json FROM release_pages p JOIN collection_evidence e ON e.hash=p.evidence_hash").fetchone()
        self.assertNotIn("never store",row["body_json"])
        self.assertEqual(timestamp(NOW),self.db.execute("SELECT watermark FROM checkpoints WHERE source_id=?",(SOURCE,)).fetchone()[0])

    def test_empty_scan_preserves_previous_release_observations_not_deletion(self):
        self.enqueue();self.run_jobs();self.body=[]
        self.enqueue();self.run_jobs()
        status=release_status(self.db);self.assertEqual(1,status["observations"])
        self.assertEqual("not_supported",status["deletionInference"])

    def test_page_budget_stops_partial_without_success_watermark(self):
        scan=self.enqueue(pages=1);self.body=[item(i+1) for i in range(100)]
        self.run_jobs();row=self.scan(scan)
        self.assertEqual("partial",row["state"]);self.assertEqual("page_budget_exhausted",row["reason"])
        mark=self.db.execute("SELECT * FROM checkpoints WHERE source_id=?",(SOURCE,)).fetchone()
        self.assertIsNone(mark["watermark"]);self.assertEqual(1,len(self.opener.requests))

    def test_next_page_is_enqueued_with_ack_and_resumes_without_repeating_first(self):
        scan=self.enqueue();self.body=[item(i+1) for i in range(100)]
        self.run_jobs();self.assertEqual(2,self.scan(scan)["next_page"])
        self.assertEqual("running",self.scan(scan)["state"])
        self.body=[item(101)];self.run_jobs()
        self.assertEqual("traversal_complete",self.scan(scan)["state"])
        self.assertEqual(101,release_status(self.db)["observations"])
        self.assertIn("page=2",self.opener.requests[-1].full_url)
        self.assertEqual(0,self.run_jobs()["requests"])

    def test_mutating_pagination_overlap_is_partial_and_retains_both_evidences(self):
        scan=self.enqueue();self.body=[item(i+1) for i in range(100)];self.run_jobs()
        changed=item(1,downloads=5);self.body=[changed];self.run_jobs()
        self.assertEqual("partial",self.scan(scan)["state"])
        self.assertEqual("pagination_overlap",self.scan(scan)["reason"])
        self.assertEqual(101,release_status(self.db)["observations"])
        self.assertIsNone(self.db.execute("SELECT watermark FROM checkpoints WHERE source_id=?",(SOURCE,)).fetchone()[0])

    def test_edit_prerelease_tag_and_asset_set_changes_keep_raw_history_without_counter_diff(self):
        self.enqueue();self.run_jobs()
        changed=item(prerelease=True,downloads=1);changed["tag_name"]="renamed-tag";changed["assets"][0]["id"]=2001
        self.body=[changed];self.enqueue();self.run_jobs()
        history=[json.loads(r[0]) for r in self.db.execute("SELECT metadata_json FROM release_observations")]
        self.assertEqual(2,len(history));self.assertEqual({"v1.0.0","renamed-tag"},{x["tag_name"] for x in history})
        self.assertIsNone(release_status(self.db)["observedLatest"])

    def test_304_reuses_observed_time_but_advances_checked_time(self):
        self.enqueue();self.run_jobs();first=self.db.execute("SELECT observed_at FROM release_pages").fetchone()[0]
        self.now+=timedelta(hours=8);self.code=304;self.body=None;self.enqueue();self.run_jobs()
        row=self.db.execute("SELECT * FROM release_pages ORDER BY checked_at DESC LIMIT 1").fetchone()
        self.assertEqual(first,row["observed_at"]);self.assertGreater(row["checked_at"],first)
        self.assertEqual(304,row["response_status"]);self.assertIn("If-none-match",self.opener.requests[-1].headers)

    def test_missing_private_or_permission_failure_is_not_empty_release_data(self):
        self.enqueue();self.code=404;self.body={"message":"Not Found"};result=self.run_jobs()
        self.assertEqual(1,result["deferred"]);self.assertEqual(0,release_status(self.db)["observations"])
        self.assertEqual(0,release_status(self.db)["pages"])

    def test_primary_limit_then_wait_does_not_reset_attempts_or_send_another_request(self):
        self.enqueue();self.code=403;self.body={"message":"API rate limit exceeded"}
        self.headers={"x-ratelimit-remaining":"0","x-ratelimit-limit":"60","x-ratelimit-reset":str(int((NOW+timedelta(hours=1)).timestamp()))}
        self.run_jobs();self.assertEqual(1,len(self.opener.requests));self.now+=timedelta(minutes=1)
        self.assertEqual(0,self.run_jobs()["requests"]);self.assertEqual(1,len(self.opener.requests))
        self.assertEqual(1,self.db.execute("SELECT attempts FROM jobs").fetchone()[0])

    def test_lost_lease_never_persists_old_response(self):
        self.enqueue()
        def expire(request):
            self.now+=timedelta(minutes=2)
            return Response([item()],request.full_url)
        self.opener=Opener(expire);self.client.transport=AnonymousGitTransport(opener=self.opener)
        result=self.run_jobs();self.assertEqual(1,result["leaseLost"])
        self.assertEqual(0,release_status(self.db)["pages"])

    def test_checkpoint_failure_rolls_back_page_observation_followup_and_ack(self):
        self.enqueue();self.body=[item(i+1) for i in range(100)]
        with patch("agent_forge_service.releases.save_checkpoint",side_effect=ServiceError("injected","stop",503)):
            self.run_jobs()
        self.assertEqual(0,release_status(self.db)["pages"])
        self.assertEqual(1,self.db.execute("SELECT count(*) FROM jobs").fetchone()[0])
        self.assertEqual(1,self.db.execute("SELECT next_page FROM release_scans").fetchone()[0])

    def test_historical_scan_does_not_regress_success_watermark(self):
        self.enqueue();self.run_jobs();mark=timestamp(NOW)
        self.enqueue(slot=timestamp(NOW-timedelta(days=1)));self.run_jobs()
        self.assertEqual(mark,self.db.execute("SELECT watermark FROM checkpoints WHERE source_id=?",(SOURCE,)).fetchone()[0])

    def test_older_scan_does_not_replace_newer_partial_cursor(self):
        current=self.enqueue();self.body=[item(i+1) for i in range(100)];self.run_jobs()
        checkpoint=self.db.execute("SELECT cursor_json FROM checkpoints WHERE source_id=?",(SOURCE,)).fetchone()[0]
        older=self.enqueue(slot=timestamp(NOW-timedelta(days=1)))
        # Finish only the older page while current page two stays queued.
        old_job=self.db.execute("SELECT job_id FROM release_tasks WHERE scan_id=?",(older,)).fetchone()[0]
        self.body=[]
        run_release_jobs(self.db,self.client,owner="historical-test",max_requests=1,clock=lambda:self.now,job_ids=(old_job,))
        self.assertEqual(checkpoint,self.db.execute("SELECT cursor_json FROM checkpoints WHERE source_id=?",(SOURCE,)).fetchone()[0])
        self.assertEqual("running",self.scan(current)["state"])

    def test_planner_uses_only_resolved_canonical_and_deduplicates_inflight(self):
        self.seed_repo(456);self.link()
        with patch.object(AnonymousGitTransport,"get",side_effect=AssertionError("planning must be offline")):
            result=plan_release_jobs(self.db,slot=timestamp(NOW),limit=10,max_pages=2,now=NOW)
        self.assertEqual(1,result["selected"])
        self.assertEqual(0,plan_release_jobs(self.db,slot=timestamp(NOW+timedelta(hours=8)),limit=10,max_pages=2,now=NOW)["selected"])
        self.assertEqual(123,self.db.execute("SELECT github_id FROM release_scans").fetchone()[0])

    def test_authenticated_client_and_wrong_connection_are_rejected(self):
        with self.assertRaises(ValueError):run_release_jobs(self.db,GitHubClient(self.db,identity="local"),owner="test",max_requests=1)
        other=open_state(self.root/"other.db")
        try:
            with self.assertRaises(ValueError):run_release_jobs(other,self.client,owner="test",max_requests=1)
        finally:other.close()

    def test_readonly_status_old_schema_does_not_migrate(self):
        path=self.root/"old.db";db=sqlite3.connect(path);db.execute("PRAGMA user_version=8");db.close()
        self.assertEqual("upgrade_required",read_release_status(path)["releaseSchema"])
        db=sqlite3.connect(path)
        try:self.assertEqual(8,db.execute("PRAGMA user_version").fetchone()[0])
        finally:db.close()


    def test_dead_task_is_reported_failed_not_traversal_complete(self):
        self.enqueue();self.code=401;self.body={"message":"Unauthorized"};self.run_jobs()
        self.assertEqual("failed",release_status(self.db)["recentScans"][0]["state"])
        self.assertIsNone(release_status(self.db)["observedLatest"])

    def test_cli_enqueue_run_and_status_need_no_gh_identity(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=main(["release-enqueue","--runtime",str(self.root),"--repository-id","123","--slot",timestamp(NOW),"--max-pages","1"])
        self.assertEqual(0,code);self.assertIn("scanId",json.loads(output.getvalue()))
        with patch("agent_forge_service.worker.AnonymousGitClient",side_effect=lambda connection: AnonymousGitClient(connection,transport=self.client.transport,clock=lambda:self.now)):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):code=main(["release-run","--runtime",str(self.root),"--max-requests","1"])
        self.assertEqual(0,code)
        output=io.StringIO()
        with contextlib.redirect_stdout(output):code=main(["release-status","--runtime",str(self.root)])
        self.assertEqual(0,code);self.assertIsNone(json.loads(output.getvalue())["observedLatest"])


if __name__ == "__main__":unittest.main()
