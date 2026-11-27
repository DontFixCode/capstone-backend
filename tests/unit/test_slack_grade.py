"""환승 여유시간 등급(slackGrade)의 DB 없는 시험. 입력은 tests/fixtures/phase3_world.py의 합성 데이터다."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))

import phase3_world as w  # noqa: E402
import test_search_phase3 as base  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.integrations.walking_api import TmapWalkingClient  # noqa: E402
from app.modules.evaluation.schemas import VehicleEta  # noqa: E402
from app.modules.evaluation.transfer import slack_estimated, slack_grade  # noqa: E402
from app.modules.routing import views  # noqa: E402
from app.workflows import search as flow  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 7, 3, 0, tzinfo=UTC)


def eta(source: str) -> VehicleEta:
    return VehicleEta(pattern_id="P", stop_id="S", stop_order=1, vehicle_id="V", run_key=None,
                      predicted_arrival_at=NOW + timedelta(seconds=300), eta_source=source, model_version=None,
                      horizon_seconds=300, basis_at=NOW, source_generated_at=None, collected_at=NOW)


def rows_with_transfers(cfg):
    """합성 검색의 환승이 있는 후보 저장 행과, 같은 순서의 평가 결과."""
    net = base.network()
    context, _, result = base.search(cfg, net)
    evaluation, recommendation = base.evaluate(result, context, cfg, etas=base.provider_etas(net))
    stops = {s.external_id: s for s in net.stops}
    request = flow.SearchRequest(flow.PlaceInput(*w.ORIGIN), flow.PlaceInput(*w.DESTINATION))
    points = {ref: flow._point(ref, stops, request) for ref in {flow.ORIGIN_REF, flow.DESTINATION_REF, *w.STOPS}}
    fake = w.FakeTmap()
    walks, _ = flow.lookup_walks(flow.walk_pairs(result.candidates), points,
                                 TmapWalkingClient(fake.app_key, transport=fake.transport()),
                                 search_option="0", max_lookups=60)
    info = {"routes": {r.external_id: r for r in net.routes},
            "directions": {(p.pattern_key, s.seq): s.direction_code for p in net.patterns for s in p.stops}}
    statics = {c.static_candidate_id: c for c in result.candidates}
    items = [c for c in [*evaluation.candidates, *evaluation.limited] if c.transfer_count]
    rows = [flow.candidate_row(statics[item.static_candidate_id], item, recommendation, walks, points, info, {}, {},
                               None, policy=cfg.evaluation_policy) for item in items]
    return rows, items


class SlackGradeRuleTest(unittest.TestCase):
    THRESHOLDS = (120.0, 300.0)

    def test_boundaries(self):
        cases = [(-0.001, "missed"), (-600, "missed"), (0, "tight"), (119.999, "tight"), (120, "moderate"),
                 (299.999, "moderate"), (300, "comfortable"), (3600, "comfortable")]
        for slack, expected in cases:
            self.assertEqual(slack_grade(slack, self.THRESHOLDS), expected, slack)

    def test_unknown_time_or_missing_setting_gives_no_grade(self):
        self.assertIsNone(slack_grade(None, self.THRESHOLDS), "시각 미확인")
        self.assertIsNone(slack_grade(150, None), "경계 설정 없음")
        self.assertIsNone(slack_grade(150, ()), "경계 설정 없음")
        self.assertIsNone(slack_grade(-30, None), "설정이 없으면 음수 여유도 등급을 만들지 않는다")

    def test_estimated_when_any_eta_is_not_from_the_provider(self):
        provider = eta("provider")
        self.assertIs(slack_estimated(60, provider, provider), False)
        for source in ("leading_vehicle_gap", "headway_estimate", "historical_segments"):
            self.assertIs(slack_estimated(60, eta(source), provider), True, source)
            self.assertIs(slack_estimated(60, provider, eta(source)), True, source)
        self.assertIsNone(slack_estimated(None, provider, provider), "여유를 계산하지 못하면 미확인")
        self.assertIsNone(slack_estimated(60, None, provider))


class SlackGradeSettingTest(unittest.TestCase):
    def test_setting_is_two_increasing_positive_numbers(self):
        self.assertEqual(Settings(_env_file=None).eval_slack_grade_thresholds_seconds, (), "코드 기본값 없음")
        self.assertEqual(Settings(_env_file=None, eval_slack_grade_thresholds_seconds=" ")
                         .eval_slack_grade_thresholds_seconds, ())
        self.assertEqual(Settings(_env_file=None, eval_slack_grade_thresholds_seconds="120, 300")
                         .eval_slack_grade_thresholds_seconds, (120.0, 300.0))
        for bad in ("120", "300,120", "0,300", "120,120", "60,120,300", "-10,300"):
            with self.assertRaises(ValidationError, msg=bad):
                Settings(_env_file=None, eval_slack_grade_thresholds_seconds=bad)

    def test_unset_keeps_the_search_config_version(self):
        plain = base.config()
        self.assertIsNone(plain.evaluation_policy.slack_grade_thresholds_seconds)
        self.assertNotIn("slack_grade_thresholds_seconds", plain.params["evaluation_policy"],
                         "끄면 적용 설정에 남기지 않아 기존 설정 버전이 그대로다")
        graded = base.config(eval_slack_grade_thresholds_seconds="120,300")
        self.assertEqual(graded.params["evaluation_policy"]["slack_grade_thresholds_seconds"], (120.0, 300.0))
        self.assertNotEqual(plain.config_version, graded.config_version)


class SlackGradeRowTest(unittest.TestCase):
    def test_every_timed_transfer_gets_a_grade_without_probability(self):
        cfg = base.config(eval_slack_grade_thresholds_seconds="120,300")
        rows, items = rows_with_transfers(cfg)
        seen = set()
        for row, item in zip(rows, items):
            for transfer in row["evaluation"]["transfers"]:
                index = transfer["transfer_index"]
                connection = item.connections[index] if index < len(item.connections) else None
                slack = connection.slack_seconds if connection else None
                self.assertEqual(transfer["slack_seconds"], slack)
                self.assertEqual(transfer["slack_grade"], slack_grade(slack, (120.0, 300.0)))
                self.assertIsNone(transfer["probability"], "확률 표본이 없어도 등급은 나온다")
                if slack is None:
                    self.assertIsNone(transfer["slack_estimated"])
                else:
                    self.assertIs(transfer["slack_estimated"], False, "합성 ETA는 모두 제공처 예측이다")
                seen.add(transfer["slack_grade"])
        self.assertTrue(seen - {None}, "시간이 계산된 환승이 있다")

        view = views.evaluation_view(next(r for r in rows if r["evaluation"]["transfers"][0]["slack_grade"]))
        shown = view["transfers"][0]
        self.assertIn(shown["slackGrade"], ("missed", "tight", "moderate", "comfortable"))
        self.assertIs(shown["slackEstimated"], False)
        self.assertEqual({"transferIndex", "frontLegIndex", "nextLegIndex", "slackSeconds", "status", "probability",
                          "reasonCodes"} - set(shown), set(), "기존 필드는 그대로 둔다")

    def test_without_the_setting_the_grade_is_null(self):
        rows, _ = rows_with_transfers(base.config())
        transfers = [t for row in rows for t in row["evaluation"]["transfers"]]
        self.assertTrue(transfers)
        self.assertEqual({t["slack_grade"] for t in transfers}, {None})
        self.assertTrue(any(t["slack_seconds"] is not None for t in transfers))


if __name__ == "__main__":
    unittest.main()
