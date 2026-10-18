import unittest
from dataclasses import replace

from agent_forge_service.metrics import MetricSample, WindowPolicy, rate_between, rate_for_interval


def sample(value, time, **kwargs):
    fields = {"source_id": "github", "subject_kind": "repo", "subject_id": "123", "metric": "stars",
              "unit": "stars", "counter_identity": "repo:123:stars", "distribution_scope": "repository",
              "observed_at": time, "value": value}
    fields.update(kwargs)
    return MetricSample(**fields)


class MetricsTests(unittest.TestCase):
    def test_hourly_rates_use_actual_successful_interval(self):
        first = sample(100, "2026-10-10T00:00:00Z")
        self.assertEqual(3, rate_between(first, sample(124, "2026-10-10T08:00:00Z")).value_per_hour)
        self.assertEqual(2, rate_between(first, sample(148, "2026-10-11T00:00:00Z")).value_per_hour)
        # A failed intermediate attempt is not zero and does not shorten the interval.
        self.assertEqual(1, rate_between(first, sample(148, "2026-10-12T00:00:00Z")).value_per_hour)

    def test_missing_and_single_sample_are_unknown(self):
        first = sample(100, "2026-10-10T00:00:00Z")
        self.assertEqual("insufficient_samples", rate_between(None, first).reason)
        failed = sample(None, "2026-10-10T08:00:00Z", availability="failed")
        self.assertEqual("sample_unavailable", rate_between(first, failed).reason)

    def test_zero_and_negative_star_net_growth_remain_valid(self):
        first = sample(100, "2026-10-10T00:00:00Z")
        self.assertEqual(0, rate_between(first, sample(100, "2026-10-10T08:00:00Z")).value_per_hour)
        decreased = rate_between(first, sample(92, "2026-10-10T08:00:00Z"))
        self.assertEqual("available", decreased.status)
        self.assertEqual(-1, decreased.value_per_hour)

    def test_download_decrease_is_not_negative_usage(self):
        first = sample(100, "2026-10-10T00:00:00Z", metric="downloads", unit="downloads")
        second = replace(first, value=90, observed_at="2026-10-10T08:00:00Z")
        self.assertEqual("counter_decreased", rate_between(first, second).reason)

    def test_counter_reset_starts_a_new_segment(self):
        first = sample(100, "2026-10-10T00:00:00Z", metric="downloads", unit="downloads")
        reset = replace(first, value=10, observed_at="2026-10-10T08:00:00Z", discontinuity="upstream_reset")
        self.assertEqual("counter_discontinuity", rate_between(first, reset).reason)
        next_sample = replace(reset, value=18, observed_at="2026-10-10T16:00:00Z", discontinuity=None)
        self.assertEqual(1, rate_between(reset, next_sample).value_per_hour)

    def test_scope_and_asset_set_changes_are_not_differenced(self):
        first = sample(100, "2026-10-10T00:00:00Z")
        for field in ("source_id", "subject_id", "subject_kind", "metric", "unit", "counter_identity", "distribution_scope"):
            changed = replace(first, **{field: "changed"}, observed_at="2026-10-10T08:00:00Z")
            self.assertEqual("metric_scope_changed", rate_between(first, changed).reason)

    def test_effective_source_time_not_collection_time(self):
        first = sample(100, "2026-10-10T00:30:00Z", measurement_at="2026-10-10T00:00:00Z")
        second = sample(124, "2026-10-10T10:00:00Z", measurement_at="2026-10-10T08:00:00Z")
        self.assertEqual(3, rate_between(first, second).value_per_hour)
        self.assertEqual("source_measurement", rate_between(first, second).time_basis)
        self.assertEqual("clock_basis_changed", rate_between(first, replace(second, measurement_at=None)).reason)
        self.assertEqual("nonpositive_interval", rate_between(first, replace(second, measurement_at=first.measurement_at)).reason)

    def test_same_or_reversed_timestamps_are_invalid(self):
        first = sample(100, "2026-10-10T00:00:00Z")
        self.assertEqual("nonpositive_interval", rate_between(first, first).reason)
        self.assertEqual("nonpositive_interval", rate_between(first, replace(first, observed_at="2026-10-09T00:00:00Z")).reason)

    def test_rolling_window_is_not_cumulative_difference(self):
        first = sample(1680, "2026-10-10T00:00:00Z", metric="downloads", unit="downloads", measurement_kind="rolling_window",
                       window_start="2026-10-03T00:00:00Z", window_end="2026-10-10T00:00:00Z")
        second = replace(first, value=1700, observed_at="2026-10-11T00:00:00Z", window_start="2026-10-04T00:00:00Z", window_end="2026-10-11T00:00:00Z")
        self.assertEqual("not_cumulative", rate_between(first, second).reason)
        self.assertEqual(10, rate_for_interval(first).value_per_hour)
        self.assertEqual(168, rate_for_interval(first).elapsed_hours)

    def test_interval_missing_or_bad_window_is_unknown(self):
        value = sample(24, "2026-10-10T08:00:00Z", measurement_kind="interval")
        self.assertEqual("unknown_window", rate_for_interval(value).reason)
        value = replace(value, window_start="2026-10-10T08:00:00Z", window_end="2026-10-10T08:00:00Z")
        self.assertEqual("nonpositive_interval", rate_for_interval(value).reason)

    def test_timezone_offsets_are_normalized(self):
        first = sample(100, "2026-10-10T08:00:00+08:00")
        second = sample(124, "2026-10-10T08:00:00Z")
        self.assertEqual(3, rate_between(first, second).value_per_hour)
        self.assertEqual("2026-10-10T00:00:00.000000Z", rate_between(first, second).window_start)

    def test_invalid_values_and_naive_times_are_rejected(self):
        for value in (True, -1, 1.2, float("nan")):
            with self.assertRaises(ValueError):
                sample(value, "2026-10-10T00:00:00Z")
        with self.assertRaises(ValueError):
            sample(1, "2026-10-10T00:00:00")

    def test_explicit_window_policy_distinguishes_window_and_age(self):
        policy = WindowPolicy("test-only", "1", 24, 2, 1, 12)
        first = sample(100, "2026-10-10T00:00:00Z")
        growth = rate_between(first, sample(148, "2026-10-11T00:00:00Z"))
        self.assertTrue(policy.qualification(growth, as_of="2026-10-11T08:00:00Z")["eligible"])
        self.assertEqual("stale", policy.qualification(growth, as_of="2026-10-11T13:00:00Z")["reason"])
        short = rate_between(first, sample(124, "2026-10-10T08:00:00Z"))
        self.assertEqual("window_mismatch", policy.qualification(short, as_of="2026-10-10T08:00:00Z")["reason"])
        self.assertEqual("measurement_in_future", policy.qualification(growth, as_of="2026-10-10T00:00:00Z")["reason"])

    def test_policy_has_no_hidden_default_thresholds(self):
        with self.assertRaises(TypeError):
            WindowPolicy("test", "1")
        for values in ((24, -1, 1, 12), (24, 2, 30, 12), (24, 2, 1, float("nan"))):
            with self.assertRaises(ValueError):
                WindowPolicy("test", "1", *values)


if __name__ == "__main__":
    unittest.main()
