import unittest
from datetime import datetime, timedelta, timezone

from agent_forge_service.scheduling import RefreshPolicy, RefreshResource, RankedResource, select_refresh, utc_slot
from agent_forge_service.storage import timestamp

NOW = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
POLICY = RefreshPolicy("trial-explicit", 2, 7 * 24, 8, 24, 0.25)


def resource(identity, *, bootstrap=True, age=10, last=25, memberships=(("dsh", "plugin"),)):
    return RefreshResource(identity, memberships, timestamp(NOW - timedelta(days=age)), bootstrap,
                           timestamp(NOW - timedelta(hours=last)) if last is not None else None)


def rank(identity, metric="github-stars", n=1, agent="dsh", type="plugin", eligible=True):
    return RankedResource(identity, agent, type, metric, n, "revision-1", eligible)


class SchedulingTests(unittest.TestCase):
    def plan(self, resources, rankings=(), budget=10):
        return select_refresh(resources, list(rankings), policy=POLICY, ranking_revision="revision-1", now=NOW, max_jobs=budget)

    def test_utc_slots_are_equal_for_plus_eight_timezone(self):
        local = NOW.astimezone(timezone(timedelta(hours=8)))
        self.assertEqual(timestamp(NOW), utc_slot(local))
        self.assertEqual(timestamp(NOW.replace(hour=12)), utc_slot(NOW.replace(hour=15), hours=4))

    def test_hot_groups_and_agents_are_unioned_by_resource(self):
        r = resource("shared", last=9, memberships=(("dsh", "plugin"), ("claude-code", "skill")))
        plan = self.plan([r], [rank("shared"), rank("shared", metric="registry-downloads"),
                               rank("shared", agent="claude-code", type="skill")])
        self.assertEqual(1, len(plan.selected))
        self.assertEqual(2, len(plan.selected[0].reasons))

    def test_bootstrap_is_not_new_discovery_and_unknown_ranks_are_not_zero(self):
        plan = self.plan([resource("old", age=0, last=9), resource("new", bootstrap=False, age=0, last=9)],
                         [rank("old", eligible=False)])
        self.assertEqual(["new"], [r.identity for r in plan.selected])
        self.assertEqual(("new_discovery",), plan.selected[0].reasons)

    def test_longtail_has_reserved_capacity_during_hot_surge(self):
        ordinary = resource("tail", last=48)
        fast = [resource(f"hot{i}", last=9) for i in range(8)]
        plan = self.plan([ordinary] + fast, [rank(r.identity) for r in fast], budget=4)
        self.assertIn("tail", [r.identity for r in plan.selected])
        self.assertEqual(4, len(plan.selected))
        self.assertTrue(plan.partial)
        self.assertEqual(5, len(plan.deferred))
        self.assertEqual(["tail"], [r.identity for r in self.plan([ordinary] + fast, [rank(r.identity) for r in fast], budget=1).selected])

    def test_unused_reserved_capacity_is_lent_to_other_group(self):
        tail = [resource(str(i), last=48) for i in range(5)]
        self.assertEqual(4, len(self.plan(tail, budget=4).selected))
        fast = [resource(str(i), last=9) for i in range(5)]
        self.assertEqual(4, len(self.plan(fast, [rank(r.identity) for r in fast], budget=4).selected))

    def test_overdue_orders_longtail_and_zero_budget_reports_partial(self):
        values = [resource("younger", last=25), resource("older", last=49)]
        self.assertEqual("older", self.plan(values, budget=1).selected[0].identity)
        plan = self.plan(values, budget=0)
        self.assertFalse(plan.selected)
        self.assertEqual(2, len(plan.deferred))
        self.assertTrue(plan.partial)

    def test_demoting_resource_does_not_delay_already_overdue_daily_refresh(self):
        plan = self.plan([resource("demoted", last=49)], [rank("demoted", n=3)])
        self.assertEqual("ordinary", plan.selected[0].tier)
        self.assertEqual(25, plan.selected[0].overdue_hours)

    def test_mixed_revisions_identity_conflicts_and_invalid_policy_are_rejected(self):
        r = resource("a")
        with self.assertRaises(ValueError):
            self.plan([r, r])
        with self.assertRaises(ValueError):
            self.plan([r], [RankedResource("a", "dsh", "plugin", "stars", 1, "different")])
        with self.assertRaises(ValueError):
            self.plan([r], [rank("a", agent="wrong")])
        with self.assertRaises(ValueError):
            RefreshPolicy("hidden", 2, 168, 8, 24, 0)
