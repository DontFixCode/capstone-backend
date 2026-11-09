"""Phase 5 제안 판단: 외부 API/DB 없는 회귀 테스트."""
import unittest

from app.modules.recommendation.change import RouteOption, decide_change


class ChangeDecisionTest(unittest.TestCase):
    def option(self, candidate_id="old", cost=1200, **overrides):
        fields = dict(candidate_id=candidate_id, ranking_mode="nominal", cost_seconds=cost,
                      comparison_key="departure-v1", startable=True, coverage_complete=True)
        fields.update(overrides)
        return RouteOption(**fields)

    def test_propose_only_after_meaningful_improvement(self):
        result = decide_change(self.option(), [self.option("new", 900)], min_improvement_seconds=120)
        self.assertEqual(result.status, "propose")
        self.assertEqual(result.proposed_candidate_id, "new")
        self.assertEqual(result.improvement_seconds, 300)
        self.assertTrue(result.requires_user_confirmation)

    def test_do_not_propose_at_threshold_or_tie(self):
        for cost in (1080, 1200, 1300):
            result = decide_change(self.option(), [self.option("new", cost)], min_improvement_seconds=120)
            self.assertEqual(result.status, "keep")
            self.assertIsNone(result.proposed_candidate_id)

    def test_never_mix_modes_or_contexts(self):
        options = [self.option("expected", 10, ranking_mode="expected"),
                   self.option("other-departure", 10, comparison_key="departure-v2")]
        result = decide_change(self.option(), options, min_improvement_seconds=10)
        self.assertEqual(result.status, "data_limited")

    def test_unavailable_or_infeasible_candidates_excluded(self):
        options = [self.option("missing", None),
                   self.option("blocked", 10, startable=False),
                   self.option("partial", 10, coverage_complete=False),
                   self.option("nan", float("nan"))]
        result = decide_change(self.option(), options, min_improvement_seconds=10)
        self.assertEqual(result.status, "data_limited")

    def test_current_data_missing_never_suggests(self):
        result = decide_change(self.option(cost=None), [self.option("new", 50)], min_improvement_seconds=0)
        self.assertEqual(result.status, "data_limited")

    def test_deterministic_tie_and_ignore_current_candidate(self):
        result = decide_change(self.option(), [self.option("z", 800), self.option("old", 1),
                                                      self.option("a", 800)], min_improvement_seconds=0)
        self.assertEqual(result.proposed_candidate_id, "a")

    def test_invalid_threshold_rejected(self):
        for threshold in (-1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                decide_change(self.option(), [], min_improvement_seconds=threshold)


if __name__ == "__main__":
    unittest.main()
