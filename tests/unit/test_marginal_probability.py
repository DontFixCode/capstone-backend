"""단일 차량 ETA 오차 결합 확률과 단계 선택(짝 표본 → 오차 결합 → 없음)의 DB 없는 시험. 입력은 합성 데이터다."""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))

import phase3_world as w  # noqa: E402
import test_search_phase3 as base  # noqa: E402

from pydantic import ValidationError  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.integrations.walking_api import TmapWalkingClient  # noqa: E402
from app.modules.evaluation import samples as sample_rules  # noqa: E402
from app.modules.evaluation import service  # noqa: E402
from app.modules.evaluation.probability import (  # noqa: E402
    marginal_transfer_probability_bounds,
    select_marginal_errors,
)
from app.modules.evaluation.schemas import (  # noqa: E402
    EtaErrorSample,
    EvaluationInputs,
    ObservedEvent,
    PredictionOutcome,
    TransferErrorSample,
    VehicleEta,
)
from app.modules.evaluation.service import evaluate_candidates  # noqa: E402
from app.modules.recommendation.service import recommend_candidates  # noqa: E402
from app.modules.routing import views  # noqa: E402
from app.workflows import guidance  # noqa: E402
from app.workflows import search as flow  # noqa: E402

UTC = timezone.utc
NOW = base.NOW
MARGINAL = dict(eval_marginal_probability_enabled=True, eval_marginal_window_days=14, eval_min_marginal_samples=3,
                eta_error_horizon_edges_seconds="120,300,600")
BUCKETS = ("0-120", "120-300", "300-600", "600+")


def eta(source: str = "provider", horizon: float = 200, *, model: str | None = None, basis: str | None = None,
        run_key: str | None = None) -> VehicleEta:
    return VehicleEta(pattern_id="P", stop_id="S", stop_order=1, vehicle_id="V", run_key=run_key,
                      predicted_arrival_at=NOW + timedelta(seconds=horizon), eta_source=source, model_version=model,
                      horizon_seconds=horizon, basis_at=NOW, source_generated_at=None, collected_at=NOW,
                      basis_source=basis)


def sample(number: int, lower: float, upper: float | None = None, *, bucket: str = "120-300",
           source: str = "provider", model: str | None = None, basis: str | None = None,
           run: str | None = None) -> EtaErrorSample:
    return EtaErrorSample(sample_id=f"{number:04d}", run_group_key=run or f"run-{number}", eta_source=source,
                          model_version=model, basis_source=basis, horizon_bucket=bucket, error_lower_seconds=lower,
                          error_upper_seconds=lower if upper is None else upper)


def samples_for_every_bucket(count: int = 5) -> list[EtaErrorSample]:
    """모든 잔여시간 구간에 점 시각 표본을 넣는다(오차 −60, 0, 60, …초)."""
    rows, number = [], 0
    for bucket in BUCKETS:
        for i in range(count):
            number += 1
            rows.append(sample(number, (i - 1) * 60.0, bucket=bucket))
    return rows


class BoundsTest(unittest.TestCase):
    def test_worked_example_is_five_sixths(self):
        """앞 오차 [0, 60, 180], 다음 오차 [0, 120], 여유 120초 → 6개 조합 중 5개가 연결된다."""
        front = [(0, 0), (60, 60), (180, 180)]
        following = [(0, 0), (120, 120)]
        self.assertEqual(marginal_transfer_probability_bounds(120, front, following), (5 / 6, 5 / 6))

    def test_point_samples_give_equal_bounds(self):
        front = [(value, value) for value in (-90, -20, 0, 45, 130, 400)]
        following = [(value, value) for value in (-60, 10, 75)]
        for slack in (-300, -1, 0, 60, 119.5, 600):
            lower, upper = marginal_transfer_probability_bounds(slack, front, following)
            self.assertEqual(lower, upper, slack)
            expected = sum(f - n <= slack for f, _ in front for n, _ in following) / 18
            self.assertAlmostEqual(lower, expected)

    def test_range_samples_give_lower_at_most_upper(self):
        front = [(0, 60), (60, 200), (180, 180)]
        following = [(-30, 30), (100, 140)]
        lower, upper = marginal_transfer_probability_bounds(120, front, following)
        # 하한: 앞 상한 − 다음 하한 ≤ 120인 조합(60−(−30), 60−100, 200−100, 180−100) 4개.
        # 상한: 앞 하한 − 다음 상한 ≤ 120인 조합은 (180 − 30)만 빼고 5개.
        self.assertEqual((lower, upper), (4 / 6, 5 / 6))
        for slack in range(-400, 401, 40):
            low, high = marginal_transfer_probability_bounds(slack, front, following)
            self.assertLessEqual(low, high)
            self.assertTrue(0 <= low <= high <= 1)
        self.assertEqual(marginal_transfer_probability_bounds(-1000, front, following), (0.0, 0.0))
        self.assertEqual(marginal_transfer_probability_bounds(1000, front, following), (1.0, 1.0))

    def test_no_samples_give_no_bounds(self):
        self.assertIsNone(marginal_transfer_probability_bounds(120, [], [(0, 0)]))
        self.assertIsNone(marginal_transfer_probability_bounds(120, [(0, 0)], []))


class SelectionTest(unittest.TestCase):
    EDGES = (120.0, 300.0, 600.0)

    def test_only_the_same_generation_method_and_horizon_bucket_is_selected(self):
        rows = [
            sample(1, 10),
            sample(2, 20, source="leading_vehicle_gap", model="leading-vehicle-gap/1;passage-uncorrected"),
            sample(3, 30, source="headway_estimate", model="headway-estimate/1"),
            sample(4, 40, model="other-model"),
            sample(5, 50, basis="server_received_at_approx"),
            sample(6, 60, bucket="300-600"),
            sample(7, 70, 95),
        ]
        chosen = select_marginal_errors(rows, eta=eta(horizon=200), horizon_edges_seconds=self.EDGES)
        self.assertEqual(chosen, ((10.0, 10.0), (70.0, 95.0)), "생성 방식·모델·기준 출처·구간이 다른 표본은 섞지 않는다")
        gap = select_marginal_errors(rows, eta=eta("leading_vehicle_gap", 200,
                                                   model="leading-vehicle-gap/1;passage-uncorrected"),
                                     horizon_edges_seconds=self.EDGES)
        self.assertEqual(gap, ((20.0, 20.0),))
        approx = select_marginal_errors(rows, eta=eta(horizon=299, basis="server_received_at_approx"),
                                        horizon_edges_seconds=self.EDGES)
        self.assertEqual(approx, ((50.0, 50.0),))
        self.assertEqual(select_marginal_errors(rows, eta=eta(horizon=300), horizon_edges_seconds=self.EDGES),
                         ((60.0, 60.0),), "잔여시간 300초는 300-600 구간이다")
        self.assertEqual(select_marginal_errors(rows, eta=eta(horizon=50), horizon_edges_seconds=self.EDGES), ())

    def test_one_run_is_counted_once(self):
        rows = [sample(2, 20, run="same-run"), sample(1, 10, run="same-run"), sample(3, 30)]
        chosen = select_marginal_errors(rows, eta=eta(horizon=200), horizon_edges_seconds=self.EDGES)
        self.assertEqual(sorted(chosen), [(10.0, 10.0), (30.0, 30.0)])

    def test_borrowing_takes_the_named_methods_samples_of_the_same_bucket(self):
        """표본을 쌓지 않는 생성 방식의 근사: 제공처 예측 표본 가운데 잔여시간 구간만 같은 것을 고른다."""
        rows = [
            sample(1, 10),
            sample(2, 20, source="leading_vehicle_gap", model="m"),
            sample(3, 30, basis="daejeon_info_offer_tm"),
            sample(4, 40, bucket="300-600"),
            sample(5, 50, run="run-1"),
        ]
        gap_eta = eta("leading_vehicle_gap", 200, model="leading-vehicle-gap/1", basis="daejeon_info_offer_tm")
        borrowed = select_marginal_errors(rows, eta=gap_eta, horizon_edges_seconds=self.EDGES,
                                          borrow_from={"provider"})
        self.assertEqual(sorted(borrowed), [(10.0, 10.0), (30.0, 30.0)],
                         "모델 버전·기준 출처는 보지 않고, 다른 구간·다른 방식의 표본은 빼며, 같은 운행은 하나만 센다")
        self.assertEqual(select_marginal_errors(rows, eta=gap_eta, horizon_edges_seconds=self.EDGES), (),
                         "빌리지 않으면 그 방식의 표본만 고른다")
        far = eta("headway_estimate", 700, model="headway-estimate/1")
        self.assertEqual(select_marginal_errors(rows, eta=far, horizon_edges_seconds=self.EDGES,
                                                borrow_from={"provider"}), ())


class StageSelectionTest(unittest.TestCase):
    """환승 하나의 단계 선택. 여유 120초, 앞 차량 하차 예측과 다음 차량 승차 예측은 잔여시간 200초의 제공처 예측이다."""

    FRONT = [sample(1, 0), sample(2, 60), sample(3, 180)]
    NEXT = [sample(11, 0), sample(12, 120)]

    def decide(self, cfg, *, front=None, following=None, eta_samples=(), paired=(), slack=120.0):
        policy = cfg.evaluation_policy
        inputs = EvaluationInputs(data_snapshot_id="snap",
                                  sample_condition_key=sample_rules.condition_key("tago", policy),
                                  transfer_error_samples=tuple(paired), eta_error_samples=tuple(eta_samples))
        shared = service._SharedData(context=None, inputs=inputs, policy=policy, walks={}, etas=None, durations={})
        journey = SimpleNamespace(
            connections=[None, SimpleNamespace(slack_seconds=slack)],
            legs=[SimpleNamespace(alight_eta=front or eta()), SimpleNamespace(board_eta=following or eta())])
        detail, _, _ = service._transfer_probability(journey, 1, shared)
        return detail

    def paired_samples(self, cfg, count: int):
        """짝 표본: 앞 차량이 예측보다 i × 30초 늦고 다음 차량은 정시에 온 과거 연결."""
        policy = cfg.evaluation_policy

        def outcome(error: float) -> PredictionOutcome:
            predicted = NOW - timedelta(days=1)
            return PredictionOutcome(
                predicted_arrival_at=predicted, eta_source="provider", model_version=None, horizon_seconds=200,
                run_key="r", observed=ObservedEvent(observed_at=predicted + timedelta(seconds=error),
                                                    event_type="arrival", match_status="matched"))

        return [TransferErrorSample(sample_id=f"p{i}", run_group_key=f"pair-{i}",
                                    condition_key=sample_rules.condition_key("tago", policy),
                                    timing_policy_version=policy.timing_policy_version,
                                    front=outcome(i * 30.0), next=outcome(0.0)) for i in range(count)]

    def test_marginal_errors_give_a_range_when_pairs_are_missing(self):
        cfg = base.config(**{**MARGINAL, "eval_min_marginal_samples": 2})
        detail = self.decide(cfg, eta_samples=[*self.FRONT, *self.NEXT])
        # 앞·다음 예측이 같은 조건이라 다섯 표본이 양쪽 모두에 선정된다(25개 조합).
        errors = [0, 60, 180, 0, 120]
        expected = sum(f - n <= 120 for f in errors for n in errors) / 25
        self.assertEqual((detail.source, detail.probability, detail.probability_range),
                         ("marginal_ecdf", expected, (expected, expected)))
        self.assertEqual(detail.sample_counts, {"paired": 0, "front": 5, "next": 5})
        self.assertEqual(detail.reason_codes, ("transfer_probability_by_marginal_errors",))

    def test_worked_example_through_the_stage(self):
        """앞 차량은 120-300 구간(오차 0·60·180), 다음 차량은 300-600 구간(오차 0·120)의 표본을 쓴다."""
        cfg = base.config(**{**MARGINAL, "eval_min_marginal_samples": 2})
        rows = [*self.FRONT, sample(11, 0, bucket="300-600"), sample(12, 120, bucket="300-600")]
        detail = self.decide(cfg, front=eta(horizon=200), following=eta(horizon=450), eta_samples=rows)
        self.assertEqual((detail.source, detail.probability, detail.probability_range),
                         ("marginal_ecdf", 5 / 6, (5 / 6, 5 / 6)))
        self.assertEqual(detail.sample_counts, {"paired": 0, "front": 3, "next": 2})

    def test_bounded_samples_put_the_lower_bound_in_probability(self):
        cfg = base.config(**{**MARGINAL, "eval_min_marginal_samples": 2})
        rows = [sample(1, 0, 60), sample(2, 60, 200), sample(3, 180),
                sample(11, -30, 30, bucket="300-600"), sample(12, 100, 140, bucket="300-600")]
        detail = self.decide(cfg, following=eta(horizon=450), eta_samples=rows)
        self.assertEqual((detail.probability, detail.probability_range), (4 / 6, (4 / 6, 5 / 6)),
                         "기존 probability 필드에는 하한을 넣는다")

    def test_insufficient_samples_give_null_and_a_reason(self):
        cfg = base.config(**MARGINAL)  # 최소 3개
        rows = [*self.FRONT, sample(11, 0, bucket="300-600"), sample(12, 120, bucket="300-600")]
        detail = self.decide(cfg, following=eta(horizon=450), eta_samples=rows)
        self.assertEqual((detail.probability, detail.source, detail.probability_range), (None, None, None))
        self.assertEqual(detail.reason_codes, ("marginal_error_samples_insufficient",))
        self.assertEqual(detail.sample_counts, {"paired": 0, "front": 3, "next": 2})
        empty = self.decide(cfg)
        self.assertEqual((empty.probability, empty.reason_codes), (None, ("marginal_error_samples_insufficient",)))

    def test_generation_methods_without_samples_are_unsupported(self):
        cfg = base.config(**{**MARGINAL, "eval_min_marginal_samples": 1})
        rows = [*self.FRONT, sample(21, 0, source="leading_vehicle_gap", model="m"),
                sample(22, 0, source="headway_estimate", model="m")]
        for source in ("leading_vehicle_gap", "headway_estimate", "historical_segments"):
            for side in ("front", "following"):
                detail = self.decide(cfg, eta_samples=rows, **{side: eta(source, model="m")})
                self.assertEqual((detail.probability, detail.source), (None, None), (source, side))
                self.assertEqual(detail.reason_codes, ("marginal_error_source_unsupported",))
                self.assertEqual(detail.sample_counts, {"paired": 0})

    def test_allowed_generation_methods_borrow_the_provider_errors_and_say_so(self):
        """근사를 허용한 생성 방식은 같은 잔여시간 구간의 제공처 예측 표본으로 확률을 내고 사유 코드로 구분한다."""
        cfg = base.config(**{**MARGINAL, "eval_min_marginal_samples": 2,
                             "eval_marginal_approximate_sources": "leading_vehicle_gap, headway_estimate"})
        self.assertEqual(cfg.evaluation_policy.marginal_probability.approximate_sources,
                         ("leading_vehicle_gap", "headway_estimate"))
        rows = [*self.FRONT, sample(11, 0, bucket="300-600"), sample(12, 120, bucket="300-600"),
                sample(21, 999, source="leading_vehicle_gap", model="m")]
        exact = self.decide(cfg, front=eta(horizon=200), following=eta(horizon=450), eta_samples=rows)
        self.assertEqual(exact.reason_codes, ("transfer_probability_by_marginal_errors",),
                         "두 예측이 모두 제공처 예측이면 근사 표시가 없다")
        for front, following in ((eta("leading_vehicle_gap", 200, model="m"), eta(horizon=450)),
                                 (eta(horizon=200), eta("headway_estimate", 450, model="headway-estimate/1")),
                                 (eta("headway_estimate", 200, model="headway-estimate/1"),
                                  eta("leading_vehicle_gap", 450, model="m", basis="daejeon_info_offer_tm"))):
            detail = self.decide(cfg, front=front, following=following, eta_samples=rows)
            self.assertEqual((detail.source, detail.probability, detail.probability_range),
                             ("marginal_ecdf", 5 / 6, (5 / 6, 5 / 6)), (front.eta_source, following.eta_source))
            self.assertEqual(detail.reason_codes, ("transfer_probability_by_marginal_errors",
                                                   "marginal_error_source_approximated"))
            self.assertEqual(detail.sample_counts, {"paired": 0, "front": 3, "next": 2})
        # 허용하지 않은 방식은 그대로 확률을 내지 않는다.
        other = self.decide(cfg, front=eta("historical_segments", 200, model="m"), eta_samples=rows)
        self.assertEqual((other.probability, other.reason_codes), (None, ("marginal_error_source_unsupported",)))
        # 빌린 구간의 표본이 부족하면 확률이 없다.
        few = self.decide(cfg, front=eta("leading_vehicle_gap", 700, model="m"), eta_samples=rows)
        self.assertEqual((few.probability, few.reason_codes, few.sample_counts),
                         (None, ("marginal_error_samples_insufficient", "marginal_error_source_approximated"),
                          {"paired": 0, "front": 0, "next": 3}))

    def test_paired_samples_take_priority(self):
        cfg = base.config(**{**MARGINAL, "eval_min_marginal_samples": 2, "eval_min_probability_samples": 5})
        paired = self.paired_samples(cfg, 10)
        detail = self.decide(cfg, front=eta(run_key="r"), following=eta(run_key="r"),
                             eta_samples=[*self.FRONT, *self.NEXT], paired=paired)
        # 상대 오차 0·30·…·270초 가운데 120초 이하는 5개다.
        self.assertEqual((detail.source, detail.probability, detail.probability_range),
                         ("paired_ecdf", 0.5, (0.5, 0.5)))
        self.assertEqual((detail.sample_counts, detail.reason_codes), ({"paired": 10}, ()))
        few = self.decide(cfg, eta_samples=[*self.FRONT, *self.NEXT], paired=paired[:4])
        self.assertEqual((few.source, few.sample_counts["paired"]), ("marginal_ecdf", 4),
                         "짝 표본이 최소 수에 못 미치면 다음 단계로 간다")

    def test_flag_off_keeps_the_paired_only_result(self):
        cfg = base.config()
        self.assertIsNone(cfg.evaluation_policy.marginal_probability)
        detail = self.decide(cfg, eta_samples=[*self.FRONT, *self.NEXT])
        self.assertEqual((detail.probability, detail.source, detail.probability_range, detail.reason_codes),
                         (None, None, None, ()))
        self.assertEqual(detail.sample_counts, {"paired": 0})
        unknown = self.decide(base.config(**MARGINAL), slack=None, eta_samples=[*self.FRONT, *self.NEXT])
        self.assertEqual((unknown.probability, unknown.source, unknown.reason_codes, unknown.sample_counts),
                         (None, None, (), {}), "여유를 계산하지 못한 환승은 어느 단계도 시도하지 않는다")


class MarginalSettingTest(unittest.TestCase):
    def test_enabled_without_required_values_is_a_search_config_error(self):
        with self.assertRaises(flow.SearchConfigError) as raised:
            base.config(eval_marginal_probability_enabled=True)
        message = str(raised.exception)
        for name in ("BACKEND_EVAL_MARGINAL_WINDOW_DAYS", "BACKEND_EVAL_MIN_MARGINAL_SAMPLES",
                     "BACKEND_ETA_ERROR_HORIZON_EDGES_SECONDS"):
            self.assertIn(name, message)
        base.config(eval_marginal_window_days=14)  # 꺼져 있으면 나머지 값이 비어 있어도 된다

    def test_off_keeps_the_config_version_and_on_records_the_policy(self):
        plain, on = base.config(), base.config(**MARGINAL)
        self.assertFalse(Settings(_env_file=None).eval_marginal_probability_enabled)
        self.assertNotIn("marginal_probability", plain.params["evaluation_policy"])
        self.assertEqual(plain.config_version, base.config(eta_error_horizon_edges_seconds="120,300").config_version,
                         "꺼져 있으면 구간 경계가 있어도 검색 설정 버전이 그대로다")
        self.assertEqual(on.params["evaluation_policy"]["marginal_probability"],
                         {"min_samples": 3, "window_days": 14.0, "horizon_edges_seconds": (120.0, 300.0, 600.0),
                          "approximate_sources": ()})
        self.assertNotEqual(plain.config_version, on.config_version)

    def test_approximate_sources_are_off_by_default_and_checked(self):
        self.assertEqual(Settings(_env_file=None).eval_marginal_approximate_sources, ())
        self.assertEqual(Settings(_env_file=None, eval_marginal_approximate_sources="").eval_marginal_approximate_sources,
                         ())
        approx = base.config(**MARGINAL, eval_marginal_approximate_sources="headway_estimate,leading_vehicle_gap")
        self.assertEqual(approx.params["evaluation_policy"]["marginal_probability"]["approximate_sources"],
                         ("headway_estimate", "leading_vehicle_gap"))
        self.assertNotEqual(approx.config_version, base.config(**MARGINAL).config_version)
        for bad in ("provider", "gps", "leading_vehicle_gap,unknown"):
            with self.assertRaises(ValidationError, msg=bad):
                Settings(_env_file=None, eval_marginal_approximate_sources=bad)
        off = base.config(eval_marginal_approximate_sources="headway_estimate")
        self.assertIsNone(off.evaluation_policy.marginal_probability, "확률 결합이 꺼져 있으면 근사 설정만으로는 켜지지 않는다")


class EvaluationFlowTest(unittest.TestCase):
    """합성 검색 전체를 평가해 후보 저장 행·응답·표본 기록·예측 스냅샷에 근거가 남는지 본다."""

    def evaluate(self, cfg, eta_samples=()):
        net = base.network()
        context, _, result = base.search(cfg, net)
        policy = cfg.evaluation_policy
        inputs = EvaluationInputs(
            data_snapshot_id="snap", sample_condition_key=sample_rules.condition_key("tago", policy),
            walks=base.walks_for(result), eta_reports=tuple(base.provider_etas(net)),
            queried_stop_ids=frozenset(w.STOPS), eta_error_samples=tuple(eta_samples))
        evaluation = evaluate_candidates(result, context, inputs, policy)
        return net, result, evaluation, recommend_candidates(evaluation)

    def rows(self, cfg, net, result, evaluation, recommendation):
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
        return items, [flow.candidate_row(statics[item.static_candidate_id], item, recommendation, walks, points, info,
                                          {}, {}, None, policy=cfg.evaluation_policy) for item in items]

    def test_flag_off_gives_the_same_probabilities_and_reasons(self):
        plain = base.config()
        _, _, without, _ = self.evaluate(plain)
        _, _, with_samples, _ = self.evaluate(plain, samples_for_every_bucket())
        key = lambda item: item.timed_candidate_id  # noqa: E731
        before = sorted([*without.candidates, *without.limited], key=key)
        after = sorted([*with_samples.candidates, *with_samples.limited], key=key)
        self.assertEqual(len(before), len(after))
        for old, new in zip(before, after):
            self.assertEqual((old.timed_candidate_id, old.transfer_probabilities, old.reason_codes,
                              old.journey_probability, old.expected_total_seconds),
                             (new.timed_candidate_id, new.transfer_probabilities, new.reason_codes,
                              new.journey_probability, new.expected_total_seconds))
            self.assertTrue(all(value is None for value in new.transfer_probabilities))
            self.assertTrue(all(detail.source is None and not detail.reason_codes
                                for detail in new.transfer_probability_details))

    def test_rows_views_sample_records_and_snapshots_carry_the_source(self):
        cfg = base.config(**MARGINAL)
        net, result, evaluation, recommendation = self.evaluate(cfg, samples_for_every_bucket())
        items, rows = self.rows(cfg, net, result, evaluation, recommendation)
        timed = [(item, row, transfer) for item, row in zip(items, rows) for transfer in row["evaluation"]["transfers"]
                 if transfer["slack_seconds"] is not None]
        self.assertTrue(timed)
        for item, row, transfer in timed:
            detail = item.transfer_probability_details[transfer["transfer_index"] - 1]
            self.assertEqual(transfer["probability_source"], "marginal_ecdf")
            self.assertEqual(transfer["probability"], transfer["probability_range"][0])
            self.assertEqual(transfer["probability_range"], list(detail.probability_range))
            self.assertEqual(transfer["probability_sample_counts"], {"paired": 0, "front": 5, "next": 5})
            self.assertIn("transfer_probability_by_marginal_errors", transfer["reason"])
            self.assertIn("transfer_probability_by_marginal_errors", row["evaluation"]["reason_codes"])
            self.assertIsNone(item.journey_probability, "전체 연결 확률 계산은 건드리지 않는다")
            self.assertIsNone(item.expected_total_seconds)
        complete = [(item, row) for item, row, _ in timed
                    if all(value is not None for value in item.transfer_probabilities)]
        self.assertTrue(complete)
        for item, _ in complete:
            self.assertNotIn("transfer_probability_unavailable", item.reason_codes)

        item, row, transfer = timed[0]
        shown = views.evaluation_view(row)["transfers"][transfer["transfer_index"] - 1]
        self.assertEqual((shown["probabilitySource"], shown["probabilityRange"], shown["probabilitySampleCounts"]),
                         ("marginal_ecdf", transfer["probability_range"], {"paired": 0, "front": 5, "next": 5}))
        self.assertEqual(shown["probability"], shown["probabilityRange"][0])
        self.assertIn("transfer_probability_by_marginal_errors", shown["reasonCodes"])

        candidate = {**row, "id": "c1", "search_id": "s1", "evaluated_at": NOW}
        snapshots = guidance.evaluation_snapshots("j1", candidate, selection_version=1, config_version="cfg",
                                                  fixed_at=NOW)
        payload = snapshots[transfer["transfer_index"] - 1][1]
        self.assertEqual((payload["probability_source"], payload["probability_range"], payload["predicted_probability"]),
                         ("marginal_ecdf", transfer["probability_range"], transfer["probability"]))

    def test_sample_record_context_keeps_the_probability_source(self):
        """회차가 확인된 예측이면 짝 표본 기록이 만들어지고, 그 확률 근거에 출처가 남는다."""
        cfg = base.config(**MARGINAL)
        _, _, evaluation, _ = self.evaluate(cfg, samples_for_every_bucket())
        records = []
        for item in [*evaluation.candidates, *evaluation.limited]:
            built, _ = sample_rules.build_sample_records(
                item, static_version="v", cohort=sample_rules.condition_key("tago", cfg.evaluation_policy),
                policy=cfg.evaluation_policy, evaluated_at=NOW, label_deadline_seconds=7200, config_version="cfg")
            records.extend(record for _, record in built)
        self.assertTrue(records)
        for record in records:
            context = record.probability_context
            self.assertEqual(context["probability_source"], "marginal_ecdf")
            self.assertEqual(record.predicted_probability, context["probability_range"][0])
            self.assertEqual(context["probability_sample_counts"], {"paired": 0, "front": 5, "next": 5})
            self.assertIsNone(context["reason"])
            self.assertEqual(context["sample_count"], 0, "기존 값(짝 표본 수)은 그대로다")

    def test_no_samples_leave_the_probability_null_with_a_reason(self):
        cfg = base.config(**MARGINAL)
        net, result, evaluation, recommendation = self.evaluate(cfg)
        items, rows = self.rows(cfg, net, result, evaluation, recommendation)
        transfers = [t for row in rows for t in row["evaluation"]["transfers"] if t["slack_seconds"] is not None]
        self.assertTrue(transfers)
        for transfer in transfers:
            self.assertEqual((transfer["probability"], transfer["probability_source"], transfer["probability_range"]),
                             (None, None, None))
            self.assertIn("marginal_error_samples_insufficient", transfer["reason"])
        self.assertTrue(all("transfer_probability_unavailable" in item.reason_codes for item in items))


if __name__ == "__main__":
    unittest.main()
