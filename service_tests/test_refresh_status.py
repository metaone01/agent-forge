"""Coverage is measured from successful checks, never inferred from a budget or a ranking."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import timedelta, timezone
import hashlib
import io
import itertools
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from agent_forge_service import refresh_status as reports
from agent_forge_service.canonical_mapping import sync_canonical_mappings
from agent_forge_service.errors import ServiceError
from agent_forge_service.scheduling import refresh_capacity
from agent_forge_service.storage import open_state, timestamp
from agent_forge_service.worker import main
from service_tests import test_batches as fixture
from service_tests.test_publication_query import record


def external(policy):
    return {"schemaVersion": 1, "policyId": policy.policy_id, "slotHours": policy.slot_hours,
            "refresh": {"topN": policy.refresh.top_n, "protectionHours": policy.refresh.protection_hours,
                        "fastHours": policy.refresh.fast_hours, "ordinaryHours": policy.refresh.ordinary_hours,
                        "longtailFraction": policy.refresh.longtail_fraction},
            "budgets": {"refresh": policy.refresh_budget, "bootstrap": policy.bootstrap_budget,
                        "discovery": policy.discovery_budget, "history": policy.history_budget},
            "discovery": {"topics": list(policy.topics), "start": policy.discovery_start,
                          "overlapHours": policy.overlap_hours, "historyStart": policy.history_start,
                          "historyLagHours": policy.history_lag_hours, "historyChunkHours": policy.history_chunk_hours,
                          "minShardSeconds": policy.discovery.min_shard_seconds, "maxShards": policy.discovery.max_shards}}


class RefreshCapacityTests(unittest.TestCase):
    def capacity(self, ordinary=250, fast=100, budget=100, hours=8):
        policy = replace(fixture.POLICY.refresh, fast_hours=hours)
        return refresh_capacity(policy, slot_hours=hours, max_jobs=budget,
                                ordinary_resources=ordinary, fast_resources=fast)

    def test_fast_checks_cannot_be_omitted_from_daily_budget(self):
        value = self.capacity()
        self.assertEqual(250, value['ordinaryChecksPerDay'])
        self.assertEqual(300, value['fastChecksPerDay'])
        self.assertEqual(550, value['requiredChecksPerDayLowerBound'])
        self.assertEqual(300, value['refreshRequestBudgetPerDay'])
        self.assertEqual(250, value['minimumDailyShortfall'])
        self.assertEqual(184, value['minimumAverageBudgetPerSlot'])
        self.assertEqual(['combined_refresh_target_exceeds_budget'], value['warnings'])
        self.assertFalse(value['budgetCoversLowerBound'])

    def test_four_hour_policy_has_six_fast_checks(self):
        value = self.capacity(ordinary=100, fast=10, budget=50, hours=4)
        self.assertEqual(160, value['requiredChecksPerDayLowerBound'])
        self.assertEqual(300, value['refreshRequestBudgetPerDay'])
        self.assertEqual(27, value['minimumAverageBudgetPerSlot'])
        self.assertTrue(value['budgetCoversLowerBound'])
        self.assertEqual('one_get_per_check_necessary_not_sufficient', value['semantics'])
        self.assertIn('retries', value['excludedCosts'])

    def test_disabled_and_empty_population_do_not_fake_coverage(self):
        self.assertEqual(3, self.capacity(0, 1, 0)['minimumDailyShortfall'])
        self.assertEqual([], self.capacity(0, 0, 0)['warnings'])
        self.assertTrue(self.capacity(0, 0, 0)['budgetCoversLowerBound'])

    def test_large_resource_population_does_not_disable_the_existing_planner(self):
        self.assertEqual(100001, self.capacity(100001, 0, 100000)['ordinaryResources'])

    def test_invalid_counts_and_slot_alignment_are_rejected(self):
        for fields in ({'ordinary': True}, {'fast': -1}, {'budget': 100001}, {'budget': 1.5}, {'hours': 12}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.capacity(**fields)
        with self.assertRaises(ValueError):
            refresh_capacity(fixture.POLICY.refresh, slot_hours=4, max_jobs=10, ordinary_resources=1, fast_resources=1)


class RefreshReportTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.BatchTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.policy = fixture.POLICY

    def status(self, **kwargs):
        return reports.read_refresh_status(self.case.dbpath, self.policy, now=kwargs.pop('now', self.case.now), **kwargs)

    def test_read_only_status_measures_stale_checks_and_preserves_db_and_wal(self):
        self.case.seed(3)
        self.case.plan()
        files = [p for p in self.case.dbpath.parent.iterdir() if p.suffix == '.db' or p.name.endswith('-wal')]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        result = self.status()
        self.assertEqual(3, result['totals']['resources'])
        self.assertEqual(0, result['totals']['checkedWithinTarget'])
        self.assertEqual(3, result['totals']['notCheckedWithinTarget'])
        self.assertEqual(0, result['totals']['neverChecked'])
        self.assertEqual(0, result['stateWrites'])
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
        self.assertEqual('not_guaranteed', result['catalogCoverage'])

    def test_successful_304_checks_do_not_advance_observation_freshness(self):
        self.case.seed()
        first = self.case.plan()
        self.case.auto()
        self.case.run_batch(first)
        metrics = self.case.db.execute('SELECT count(*) FROM metric_observations').fetchone()[0]
        self.case.now = fixture.NOW + timedelta(days=2)
        second = self.case.plan()
        self.case.auto(status=304)
        result = self.case.run_batch(second)
        self.assertEqual(1, result['requestsThisInvocation'])
        report = self.status()
        self.assertEqual(1, report['totals']['checkedWithinTarget'])
        self.assertEqual(0, report['totals']['observationWithinTarget'])
        self.assertEqual(metrics, self.case.db.execute('SELECT count(*) FROM metric_observations').fetchone()[0])

    def test_failed_request_retains_missing_check_and_overdue_state(self):
        self.case.seed()
        batch = self.case.plan()
        self.case.auto(status=500)
        self.case.run_batch(batch)
        report = self.status()
        self.assertEqual(0, report['totals']['checkedWithinTarget'])
        self.assertEqual(1, report['totals']['overdue'])
        self.assertIn('planned_resources_outside_check_target', report['warnings'])

    def test_cutoff_ignores_future_check_without_reconstructing_assignments(self):
        self.case.seed()
        batch = self.case.plan()
        self.case.auto()
        self.case.run_batch(batch)
        result = self.status(now=fixture.NOW+timedelta(seconds=1))
        self.assertEqual(0, result['totals']['checkedWithinTarget'])
        self.assertEqual('current_state_not_historical_cohort_reconstruction', result['assignmentBasis'])
        local = (fixture.NOW+timedelta(seconds=1)).astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(result, self.status(now=local))

    def test_never_checked_candidate_is_missing_instead_of_zero_sample(self):
        self.case.seed()
        self.case.candidate(100)
        self.case.plan()
        result = self.status()
        self.assertEqual(1, result['totals']['neverChecked'])
        self.assertEqual(1, result['tiers']['fast']['resources'])
        self.assertEqual(0, result['tiers']['fast']['observationWithinTarget'])
        self.assertTrue(any(row['agent'] == 'unclassified' for row in result['groups']))

    def test_shared_agent_memberships_overlap_but_capacity_counts_one_repository(self):
        self.case.seed()
        self.case.write('shared', 'example/repo1', agents=('dsh', 'claude-code'))
        sync_canonical_mappings(self.case.db, self.case.catalog, now=self.case.now)
        self.case.plan()
        result = self.status()
        self.assertEqual(1, result['totals']['resources'])
        self.assertEqual(2, sum(row['resources'] for row in result['groups']))
        self.assertTrue(result['groupCountsOverlap'])
        self.assertEqual(1, result['capacity']['requiredChecksPerDayLowerBound'])
        self.assertEqual(2, result['mapping']['packagesWithResolvedRepositories'])

    def test_unresolved_and_absent_links_cannot_be_called_full_catalog_coverage(self):
        self.case.seed()
        self.case.write('unresolved', 'example/pending')
        path = self.case.packages/'no-repository.json'
        path.write_text(json.dumps(record('no-repository')), encoding='utf-8')
        sync_canonical_mappings(self.case.db, self.case.catalog, now=self.case.now)
        self.case.plan()
        result = self.status()
        self.assertEqual(3, result['mapping']['mappedCanonicalPackages'])
        self.assertEqual(1, result['mapping']['pendingRepositoryNames'])
        self.assertEqual(1, result['mapping']['packagesWithoutRepositoryLinks'])
        self.assertIn('canonical_repository_coverage_incomplete', result['warnings'])

    def test_unplanned_policy_has_unknown_coverage_and_no_mutation(self):
        result = self.status()
        self.assertFalse(result['policyPlanned'])
        self.assertIsNone(result['totals']['checkedCoverage'])
        self.assertIn('policy_not_planned', result['warnings'])
        self.assertEqual(0, self.case.db.execute('SELECT count(*) FROM batch_policies').fetchone()[0])

    def test_existing_index_without_mapping_is_not_an_empty_successful_catalog(self):
        with self.case.db:
            self.case.db.execute("INSERT INTO package_units VALUES (?,?,?,?,?,?,?)",
                                 ('plugin.indexed', 'plugin', 'indexed', timestamp(fixture.NOW), 1, 1, 'fixture-hash'))
        result = self.status()
        self.assertEqual(1, result['mapping']['indexedPackages'])
        self.assertEqual(1, result['mapping']['indexedPackagesNotMapped'])
        self.assertEqual(0, result['mapping']['mappedCanonicalPackages'])
        self.assertFalse(result['mapping']['mappingInitialized'])
        self.assertIsNone(result['totals']['checkedCoverage'])
        self.assertIn('canonical_mapping_not_initialized', result['warnings'])
        self.assertIn('canonical_repository_coverage_incomplete', result['warnings'])

    def test_paused_provider_remains_in_target_denominator(self):
        from agent_forge_service.source_controls import ACTIONS, record_control
        self.case.seed(2)
        self.case.plan()
        record_control(self.case.db, value={'kind': 'provider', 'provider': 'github.com'}, action='disable',
                       reason_code=ACTIONS['disable'], expected_head='none', request_key='coverage-pause',
                       operator_label='fixture', now=self.case.now)
        result = self.status()
        self.assertEqual(2, result['totals']['resources'])
        self.assertEqual(2, result['totals']['notCheckedWithinTarget'])
        self.assertEqual('paused_resources_remain_in_current_target_denominator', result['controlSemantics'])

    def test_check_window_includes_boundary_but_excludes_one_second_older(self):
        self.case.seed()
        self.case.plan()
        checked = self.case.db.execute('SELECT max(checked_at) FROM repository_observations').fetchone()[0]
        at = reports._time(checked) + timedelta(hours=24)
        self.assertEqual(1, self.status(now=at)['totals']['checkedWithinTarget'])
        self.assertEqual(0, self.status(now=at+timedelta(seconds=1))['totals']['checkedWithinTarget'])

    def test_same_numeric_version_without_contract_is_rejected_without_write(self):
        path = self.case.root/'unrecognized.db'
        connection = sqlite3.connect(path)
        connection.execute('PRAGMA user_version=1')
        connection.execute('CREATE TABLE metadata (key TEXT, value TEXT)')
        connection.execute("INSERT INTO metadata VALUES ('storageContract','invalid-json')")
        connection.commit()
        connection.close()
        before = path.read_bytes()
        with self.assertRaises(ServiceError) as caught:
            reports.read_refresh_status(path, self.policy)
        self.assertEqual('state_version', caught.exception.code)
        self.assertEqual(before, path.read_bytes())

    def test_changed_parameters_cannot_reuse_a_policy_identity(self):
        self.case.seed()
        self.case.plan()
        with self.assertRaises(ServiceError) as caught:
            reports.read_refresh_status(self.case.dbpath, replace(self.policy, refresh_budget=3), now=self.case.now)
        self.assertEqual('batch_policy_conflict', caught.exception.code)

    def test_other_current_policy_is_reported_without_inventing_its_population(self):
        self.case.seed()
        self.case.plan()
        self.policy = replace(self.policy, policy_id='other-explicit', refresh=replace(self.policy.refresh, policy_id='other-explicit'))
        result = self.status()
        self.assertEqual(1, result['otherPolicyActiveResources'])
        self.assertEqual(0, result['totals']['resources'])
        self.assertEqual(1, result['mapping']['resolvedRepositoriesNotPlanned'])

    def test_live_read_transaction_sees_committed_wal_with_consistent_mapping(self):
        self.case.seed(2)
        self.case.plan()
        self.assertGreater(Path(str(self.case.dbpath)+'-wal').stat().st_size, 0)
        original = reports._report
        def concurrent(connection, *args):
            other = open_state(self.case.dbpath)
            try:
                with other:
                    other.execute("UPDATE canonical_mapping_records SET active=0 WHERE package_id='plugin.package1'")
            finally:
                other.close()
            return original(connection, *args)
        with patch.object(reports, '_report', side_effect=concurrent):
            result = self.status()
        self.assertEqual(2, result['mapping']['mappedCanonicalPackages'])
        self.assertEqual(1, self.case.db.execute('SELECT count(*) FROM canonical_mapping_records WHERE active=1').fetchone()[0])

    def test_missing_state_and_unsupported_formats_are_not_created_or_migrated(self):
        missing = self.case.root/'missing'/'state.db'
        with self.assertRaises(ValueError):
            reports.read_refresh_status(missing, self.policy)
        self.assertFalse(missing.parent.exists())
        path = self.case.root/'future.db'
        connection = sqlite3.connect(path)
        connection.execute('PRAGMA user_version=2')
        connection.close()
        before = path.read_bytes()
        with self.assertRaises(ServiceError) as caught:
            reports.read_refresh_status(path, self.policy)
        self.assertEqual('state_version', caught.exception.code)
        self.assertEqual(before, path.read_bytes())

    def test_deadline_and_malformed_memberships_fail_instead_of_partial_success(self):
        self.case.seed()
        self.case.plan()
        ticks = itertools.chain([1000.0], itertools.repeat(1001.0))
        with patch.object(reports.time, 'monotonic', side_effect=lambda: next(ticks)), self.assertRaises(ServiceError) as caught:
            self.status(timeout_seconds=.01)
        self.assertEqual('refresh_status_timeout', caught.exception.code)
        with self.case.db:
            self.case.db.execute("UPDATE refresh_resources SET memberships_json='{}'")
        with self.assertRaises(ValueError):
            self.status()

    def test_cli_real_process_uses_same_policy_and_safe_single_json_output(self):
        from agent_forge_service.batches import load_policy
        self.case.seed()
        policy_path = self.case.root/'policy.json'
        policy_path.write_text(json.dumps(external(self.policy)), encoding='utf-8')
        self.policy = load_policy(policy_path)
        self.case.plan(self.policy)
        result = subprocess.run([sys.executable, '-m', 'agent_forge_service.worker', 'refresh-status',
                    '--runtime', str(self.case.dbpath.parent), '--policy', str(policy_path), '--at', timestamp(self.case.now)],
                    capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('', result.stderr)
        self.assertEqual(self.status(), json.loads(result.stdout))

    def test_combined_demand_reaches_persisted_batch_summary(self):
        self.case.seed(3)
        policy = replace(self.policy, refresh_budget=1)
        batch = self.case.plan(policy, rankings=(self.case.rank(1),), ranking_revision='rank-r1')
        capacity = batch['summary']['refreshCapacity']
        self.assertEqual(5, capacity['requiredChecksPerDayLowerBound'])
        self.assertEqual(3, capacity['refreshRequestBudgetPerDay'])
        self.assertEqual(['combined_refresh_target_exceeds_budget'], batch['summary']['capacityWarnings'])


class MultiCycleCoverageTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.BatchTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_eight_days_with_sufficient_budget_and_connection_reopen_cover_both_tiers(self):
        self.case.seed(8)
        self.case.auto()
        first_seen = dict(self.case.db.execute('SELECT github_id,first_seen_at FROM repositories'))
        policy = fixture.POLICY
        checks = []
        for slot in range(24):
            self.case.now = fixture.NOW + timedelta(hours=8*slot)
            hot = 1 if slot < 12 else 2
            revision = 'fixture-day-' + timestamp(self.case.now)[:10]
            rank = self.case.rank(hot, revision=revision)
            batch = self.case.plan(policy, rankings=(rank,), ranking_revision=revision)
            self.case.run_batch(batch)
            result = reports.read_refresh_status(self.case.dbpath, policy, now=self.case.now)
            self.assertTrue(result['capacity']['budgetCoversLowerBound'])
            if slot >= 3:
                self.assertEqual(8, result['totals']['checkedWithinTarget'], result)
                self.assertEqual(0, result['totals']['overdue'], result)
            checks.append(result['totals']['checkedWithinTarget'])
            if slot == 11:
                self.case.db.close()
                self.case.db = open_state(self.case.dbpath)
                self.case.addCleanup(self.case.db.close)
                self.case.client.connection = self.case.db
        self.assertEqual(first_seen, dict(self.case.db.execute('SELECT github_id,first_seen_at FROM repositories')))
        self.assertTrue(all(count == 8 for count in checks[3:]))
        self.assertGreater(len(self.case.calls), 50)

    def test_four_hour_slots_with_sufficient_budget_keep_daily_ordinary_and_fast_checks(self):
        self.case.seed(8)
        self.case.auto()
        policy = replace(fixture.POLICY, slot_hours=4, refresh_budget=3,
                         refresh=replace(fixture.POLICY.refresh, fast_hours=4))
        for slot in range(24):
            self.case.now = fixture.NOW + timedelta(hours=4*slot)
            batch = self.case.plan(policy, rankings=(self.case.rank(1),), ranking_revision='rank-r1')
            self.case.run_batch(batch)
            result = reports.read_refresh_status(self.case.dbpath, policy, now=self.case.now)
            if slot >= 6:
                self.assertEqual(8, result['totals']['checkedWithinTarget'])
                self.assertEqual(0, result['totals']['overdue'])
            self.assertEqual(6, result['capacity']['slotsPerDay'])
            self.assertTrue(result['capacity']['budgetCoversLowerBound'])

    def test_insufficient_budget_does_not_starve_ordinary_rotation_or_claim_daily_success(self):
        self.case.seed(8)
        self.case.auto()
        policy = replace(fixture.POLICY, refresh_budget=1)
        selected = set()
        for slot in range(12):
            self.case.now = fixture.NOW + timedelta(hours=8*slot)
            rank = self.case.rank(1)
            batch = self.case.plan(policy, rankings=(rank,), ranking_revision='rank-r1')
            selected.update(row[0] for row in self.case.db.execute("SELECT resource_id FROM batch_jobs WHERE batch_id=? AND lane='ordinary'", (batch['batchId'],)))
            self.case.run_batch(batch)
            result = reports.read_refresh_status(self.case.dbpath, policy, now=self.case.now)
            self.assertFalse(result['capacity']['budgetCoversLowerBound'])
            self.assertIn('combined_refresh_target_exceeds_budget', result['warnings'])
            self.assertLess(result['totals']['checkedWithinTarget'], 8)
        self.assertEqual({'github-repository:'+str(i) for i in range(2, 9)}, selected)


if __name__ == '__main__':
    unittest.main()
