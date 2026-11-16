"""Phase 5 route-change decision rules."""
import unittest

from app.modules.recommendation.change import RouteOption, decide_change


class ChangeDecisionTest(unittest.TestCase):
    def option(self, candidate_id="old", cost=1200, **overrides):
        values = dict(candidate_id=candidate_id, ranking_mode="nominal", cost_seconds=cost,
                      comparison_key="same-data-v1", startable=True, coverage_complete=True)
        values.update(overrides)
        return RouteOption(**values)

    def test_proposes_only_when_improvement_exceeds_threshold(self):
        result = decide_change(self.option(), [self.option("new", 900)], min_improvement_seconds=120)
        self.assertEqual((result.status, result.proposed_candidate_id, result.improvement_seconds),
                         ("propose", "new", 300))
        self.assertTrue(result.requires_user_confirmation)

    def test_keeps_route_at_threshold_and_for_slower_options(self):
        for cost in (1080, 1200, 1300):
            with self.subTest(cost=cost):
                result = decide_change(self.option(), [self.option("new", cost)], min_improvement_seconds=120)
                self.assertEqual(result.status, "keep")

    def test_never_compares_different_modes_or_information_snapshots(self):
        options = [self.option("expected", 10, ranking_mode="expected"),
                   self.option("other", 10, comparison_key="other-data")]
        result = decide_change(self.option(), options, min_improvement_seconds=0)
        self.assertEqual(result.status, "data_limited")

    def test_incomplete_or_infeasible_routes_cannot_be_proposed(self):
        options = [self.option("unavailable", None), self.option("blocked", 10, startable=False),
                   self.option("partial", 10, coverage_complete=False), self.option("nan", float("nan"))]
        result = decide_change(self.option(), options, min_improvement_seconds=10)
        self.assertEqual(result.status, "data_limited")

    def test_missing_current_route_never_suggests(self):
        result = decide_change(self.option(cost=None), [self.option("new", 50)], min_improvement_seconds=0)
        self.assertEqual(result.status, "data_limited")

    def test_ties_are_deterministic_and_current_candidate_is_ignored(self):
        result = decide_change(self.option(), [self.option("z", 800), self.option("old", 1),
                                               self.option("a", 800)], min_improvement_seconds=0)
        self.assertEqual(result.proposed_candidate_id, "a")

    def test_invalid_threshold_is_rejected(self):
        for value in (-1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decide_change(self.option(), [], min_improvement_seconds=value)


if __name__ == "__main__":
    unittest.main()
