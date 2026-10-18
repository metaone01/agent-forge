"""Source-scoped growth calculations. No combined popularity or user-count inference."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .storage import timestamp


def _time(value: str) -> datetime:
    return datetime.fromisoformat(timestamp(value).replace("Z", "+00:00"))


@dataclass(frozen=True)
class MetricSample:
    source_id: str
    subject_kind: str
    subject_id: str
    metric: str
    unit: str
    counter_identity: str
    distribution_scope: str
    observed_at: str
    value: int | None
    measurement_kind: str = "cumulative"
    measurement_at: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    availability: str = "available"
    discontinuity: str | None = None

    def __post_init__(self):
        for field in ("source_id", "subject_kind", "subject_id", "metric", "unit", "counter_identity", "distribution_scope"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ValueError("metric identity fields must be explicit non-empty strings")
        if self.value is not None and (type(self.value) is not int or self.value < 0):
            raise ValueError("public count observations must be nonnegative integers or unknown")
        if self.measurement_kind not in ("cumulative", "interval", "rolling_window"):
            raise ValueError("unsupported measurement kind")
        for value in (self.observed_at, self.measurement_at, self.window_start, self.window_end):
            if value is not None:
                _time(value)

    @property
    def identity(self) -> tuple:
        return (self.source_id, self.subject_kind, self.subject_id, self.metric, self.unit,
                self.counter_identity, self.distribution_scope, self.measurement_kind)


@dataclass(frozen=True)
class Growth:
    value_per_hour: float | None
    status: str
    reason: str | None
    source_id: str
    subject_kind: str
    subject_id: str
    metric: str
    unit: str
    counter_identity: str
    distribution_scope: str
    window_start: str | None = None
    window_end: str | None = None
    elapsed_hours: float | None = None
    time_basis: str | None = None


def _result(sample: MetricSample, reason: str | None, *, value=None, start=None, end=None, hours=None, basis=None) -> Growth:
    return Growth(value, "unknown" if reason else "available", reason, sample.source_id, sample.subject_kind,
                  sample.subject_id, sample.metric, sample.unit + "/hour", sample.counter_identity,
                  sample.distribution_scope, start, end, hours, basis)


def rate_between(previous: MetricSample | None, current: MetricSample) -> Growth:
    if previous is None:
        return _result(current, "insufficient_samples")
    if previous.identity != current.identity:
        return _result(current, "metric_scope_changed")
    if current.measurement_kind != "cumulative":
        return _result(current, "not_cumulative")
    if current.value is None or previous.value is None or current.availability != "available" or previous.availability != "available":
        return _result(current, "sample_unavailable")
    if current.discontinuity:
        return _result(current, "counter_discontinuity")
    if bool(current.measurement_at) != bool(previous.measurement_at):
        return _result(current, "clock_basis_changed")
    basis = "source_measurement" if current.measurement_at else "observation_estimate"
    start = timestamp(previous.measurement_at or previous.observed_at)
    end = timestamp(current.measurement_at or current.observed_at)
    hours = (_time(end) - _time(start)).total_seconds() / 3600
    if hours <= 0:
        return _result(current, "nonpositive_interval", start=start, end=end, hours=hours, basis=basis)
    difference = current.value - previous.value
    if difference < 0 and current.metric != "stars":
        return _result(current, "counter_decreased", start=start, end=end, hours=hours, basis=basis)
    # A previous reset observation can be the first baseline of the new segment.
    return _result(current, None, value=difference / hours, start=start, end=end, hours=hours, basis=basis)


def rate_for_interval(sample: MetricSample) -> Growth:
    if sample.measurement_kind not in ("interval", "rolling_window"):
        return _result(sample, "not_an_interval")
    if sample.value is None or sample.availability != "available":
        return _result(sample, "sample_unavailable")
    if sample.discontinuity:
        return _result(sample, "counter_discontinuity")
    if sample.window_start is None or sample.window_end is None:
        return _result(sample, "unknown_window")
    start, end = timestamp(sample.window_start), timestamp(sample.window_end)
    hours = (_time(end) - _time(start)).total_seconds() / 3600
    if hours <= 0:
        return _result(sample, "nonpositive_interval", start=start, end=end, hours=hours)
    return _result(sample, None, value=sample.value / hours, start=start, end=end, hours=hours, basis="source_interval")


@dataclass(frozen=True)
class WindowPolicy:
    policy_id: str
    version: str
    target_hours: float
    tolerance_hours: float
    minimum_hours: float
    maximum_age_hours: float

    def __post_init__(self):
        import math
        values = (self.target_hours, self.tolerance_hours, self.minimum_hours, self.maximum_age_hours)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            raise ValueError("window policy values must be finite numbers")
        if not self.policy_id or not self.version or self.target_hours <= 0 or self.tolerance_hours < 0 or self.minimum_hours <= 0 or self.maximum_age_hours < 0:
            raise ValueError("invalid window policy")
        if self.minimum_hours > self.target_hours + self.tolerance_hours:
            raise ValueError("minimum interval exceeds the target window")

    def qualification(self, growth: Growth, *, as_of: str) -> dict:
        result = {"eligible": False, "reason": growth.reason, "policyId": self.policy_id,
                  "policyVersion": self.version, "targetHours": self.target_hours,
                  "actualHours": growth.elapsed_hours, "ageHours": None}
        if growth.status != "available":
            return result
        age = (_time(as_of) - _time(growth.window_end)).total_seconds() / 3600
        result["ageHours"] = age
        if age < 0:
            result["reason"] = "measurement_in_future"
        elif age > self.maximum_age_hours:
            result["reason"] = "stale"
        elif growth.elapsed_hours < self.minimum_hours:
            result["reason"] = "interval_too_short"
        elif abs(growth.elapsed_hours - self.target_hours) > self.tolerance_hours:
            result["reason"] = "window_mismatch"
        else:
            result.update(eligible=True, reason=None)
        return result
