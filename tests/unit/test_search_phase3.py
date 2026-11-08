"""Phase 3 검색 연동의 DB 없는 시험. 정적·실시간·도보 응답은 tests/fixtures/phase3_world.py의 합성 데이터다.

기존 핵심 함수(generate_candidates·evaluate_candidates·recommend_candidates)는 그대로 호출하고, 이 파일은 그 입력을
만드는 어댑터와 저장 형식·표본 규칙을 검증한다.
"""

import json
import sys
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))

import phase3_world as w  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.integrations.walking_api import TmapWalkingClient, WalkQuery, parse_pedestrian_response  # noqa: E402
from app.modules.evaluation import samples as sample_rules  # noqa: E402
from app.modules.evaluation.schemas import EtaReport, EvaluationInputs, WalkResult  # noqa: E402
from app.modules.evaluation.service import evaluate_candidates  # noqa: E402
from app.modules.recommendation.service import recommend_candidates  # noqa: E402
from app.modules.routing.schemas import Coordinate, SearchContext  # noqa: E402
from app.modules.routing.service import build_search_index, generate_candidates  # noqa: E402
from app.modules.transit.schemas import StaticNetwork, StaticPattern, StaticPatternStop, StaticRoute, StaticStop  # noqa
from app.modules.transit.service import pattern_key  # noqa: E402
from app.workflows import search as flow  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 7, 3, 0, tzinfo=UTC)

SETTINGS = dict(
    search_max_boardings=3, search_access_radius_meters=300, search_transfer_radius_meters=150,
    search_egress_radius_meters=300, search_max_candidates=50, search_max_states=100000, search_budget_ms=60000,
    eval_search_horizon_seconds=3600, eval_budget_ms=60000, eval_max_timed_candidates=1000,
    eval_boarding_buffer_seconds=60, eval_max_data_age_seconds=900, eval_min_segment_samples=1,
    eval_min_probability_samples=1, eval_min_joint_scenes=1, eval_min_improvement_seconds=120,
    eval_max_horizon_difference_seconds=300, eval_max_recovery_attempts=2, eval_recovery_search_enabled=False,
    eval_timing_policy_version="timing-v1", eval_sample_policy_version="sample-v1",
    eval_recovery_policy_version="recovery-v1", eval_segment_model_version="segment-v1",
    search_expires_seconds=1800, tmap_search_option="0", tmap_max_lookups_per_search=60,
    sample_label_deadline_seconds=7200,
)


def config(**overrides):
    return flow.load_search_config(Settings(_env_file=None, **{**SETTINGS, **overrides}))


def network() -> StaticNetwork:
    stops = tuple(StaticStop(i, sid, name, lat, lon) for i, (sid, (name, lat, lon)) in enumerate(w.STOPS.items(), 1))
    patterns, routes = [], []
    for i, (route_id, (no, seq)) in enumerate(w.ROUTES.items(), 1):
        directions = ["0" if order <= 3 else "1" for order in range(1, len(seq) + 1)]
        key = pattern_key(route_id, list(zip(seq, directions)))
        patterns.append(StaticPattern(route_id, key, tuple(StaticPatternStop(o, s, d) for o, (s, d)
                                                            in enumerate(zip(seq, directions), 1))))
        routes.append(StaticRoute(i, route_id, no, "가상버스"))
    return StaticNetwork(data_version=__import__("uuid").UUID(int=7), stops=stops, patterns=tuple(patterns),
                         routes=tuple(routes))


def search(cfg=None, net=None):
    cfg, net = cfg or config(), net or network()
    context = SearchContext(origin=Coordinate(lat=w.ORIGIN[0], lon=w.ORIGIN[1]),
                            destination=Coordinate(lat=w.DESTINATION[0], lon=w.DESTINATION[1]),
                            start_at=NOW, evaluated_at=NOW, static_snapshot_id=str(net.data_version))
    index = build_search_index(flow.routing_snapshot(net), cfg.search_policy.transfer_radius_meters)
    return context, index, generate_candidates(index, context, cfg.search_policy)


def walks_for(result):
    """FakeTmap과 같은 규칙의 도보 결과(평가 입력)."""
    stops = {sid: (lat, lon) for sid, (_, lat, lon) in w.STOPS.items()}
    points = {flow.ORIGIN_REF: w.ORIGIN, flow.DESTINATION_REF: w.DESTINATION, **stops}
    walks = []
    for (src, dst) in flow.walk_pairs(result.candidates):
        meters = round(w.haversine(*points[src], *points[dst]) * 1.25)
        walks.append(WalkResult(from_ref=src, to_ref=dst, status="available", duration_seconds=round(meters / 1.2),
                                distance_meters=meters))
    return tuple(walks)


def evaluate(result, context, cfg, *, etas=(), samples=(), scenes=()):
    inputs = EvaluationInputs(data_snapshot_id="snap", sample_condition_key=sample_rules.condition_key(
        "tago", cfg.evaluation_policy), walks=walks_for(result), eta_reports=tuple(etas),
        queried_stop_ids=frozenset(w.STOPS), transfer_error_samples=tuple(samples), joint_scenes=tuple(scenes))
    evaluation = evaluate_candidates(result, context, inputs, cfg.evaluation_policy)
    return evaluation, recommend_candidates(evaluation)


def provider_etas(net, **kwargs):
    return [EtaReport(**row) for row in w.synthetic_eta_rows(net.patterns, basis_at=NOW - timedelta(seconds=30),
                                                              **kwargs)]


class ConfigTest(unittest.TestCase):
    def test_missing_settings_are_listed_by_env_name(self):
        with self.assertRaises(flow.SearchConfigError) as raised:
            flow.load_search_config(Settings(_env_file=None, search_max_boardings=2))
        message = str(raised.exception)
        self.assertIn("BACKEND_SEARCH_ACCESS_RADIUS_METERS", message)
        self.assertIn("BACKEND_TMAP_MAX_LOOKUPS_PER_SEARCH", message)
        self.assertNotIn("BACKEND_SEARCH_MAX_BOARDINGS", message)

    def test_out_of_range_and_version_is_stable(self):
        with self.assertRaises(flow.SearchConfigError):
            config(search_access_radius_meters=-1)
        self.assertEqual(config().config_version, config().config_version)
        self.assertNotEqual(config().config_version, config(search_max_candidates=10).config_version)
        self.assertIsNone(config().evaluation_policy.recovery_search)
        self.assertEqual(config().params["bias"], {"user_seconds": 0.0, "equilibrium_seconds": 0.0})


class TmapClientTest(unittest.TestCase):
    def test_request_uses_header_key_and_lon_lat_order(self):
        fake = w.FakeTmap()
        client = TmapWalkingClient(fake.app_key, transport=fake.transport())
        response = client.route(WalkQuery(36.35, 127.38, 36.36, 127.38, "0"))
        body = fake.requests[0]
        self.assertEqual((body["startX"], body["startY"], body["searchOption"], body["reqCoordType"]),
                         (127.38, 36.35, "0", "WGS84GEO"))
        self.assertEqual(response.status, "available")
        self.assertEqual(response.geometry["type"], "LineString")
        self.assertGreater(response.distance_m, 1100)
        self.assertEqual(client.request_count, 1)

    def test_failures_are_errors_never_no_path(self):
        t0 = NOW
        cases = [
            (403, json.dumps({"error": {"code": "INVALID_API_KEY", "message": "Forbidden"}}), "INVALID_API_KEY"),
            (400, json.dumps({"error": {"id": "400", "code": "3102", "message": "x"}}), "3102"),
            (500, "oops", "http_500"),
            (200, "not json", "unexpected_response"),
            (200, json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {
                "type": "Point", "coordinates": [1, 2]}, "properties": {}}]}), "unexpected_response"),
            (200, json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {
                "type": "Point", "coordinates": [1, 2]}, "properties": {"totalDistance": -1, "totalTime": 3}}]}),
             "unexpected_response"),
        ]
        for status, text, code in cases:
            response = parse_pedestrian_response(status, text, t0, t0, {})
            self.assertEqual((response.status, response.error_code), ("error", code), text)

    def test_timeout_and_key_redaction(self):
        key = "synthetic-key-should-not-leak-000000000"

        def boom(request):
            raise httpx.ConnectError(f"failed for {request.headers['appKey']}")

        response = TmapWalkingClient(key, transport=httpx.MockTransport(boom)).route(WalkQuery(1, 1, 2, 2, "0"))
        self.assertEqual(response.status, "error")
        self.assertNotIn(key, response.error_message)
        self.assertIsNone(response.received_at)


class WalkLookupTest(unittest.TestCase):
    def setUp(self):
        self.context, self.index, self.result = search()
        stops = {s.external_id: s for s in network().stops}
        request = flow.SearchRequest(flow.PlaceInput(*w.ORIGIN), flow.PlaceInput(*w.DESTINATION))
        refs = {flow.ORIGIN_REF, flow.DESTINATION_REF, *w.STOPS}
        self.points = {ref: flow._point(ref, stops, request) for ref in refs}

    def test_pairs_exclude_same_stop_and_dedupe(self):
        pairs = flow.walk_pairs(self.result.candidates)
        self.assertTrue(all(src != dst for src, dst in pairs))
        self.assertIn((w.C, w.C2), pairs, "다른 정류소 환승 도보")
        self.assertIn((flow.ORIGIN_REF, w.A), pairs)
        fake = w.FakeTmap()
        client = TmapWalkingClient(fake.app_key, transport=fake.transport())
        walks, stats = flow.lookup_walks(pairs, self.points, client, search_option="0", max_lookups=60)
        self.assertEqual(stats["tmap_calls"], len(pairs), "방향 있는 구간마다 한 번만 호출")
        self.assertEqual(len(fake.requests), len(pairs))
        self.assertTrue(all(lookup.status == "available" for lookup in walks.values()))

    def test_same_coordinates_share_one_call_and_limit_marks_unknown(self):
        points = dict(self.points)
        points["DUPLICATE"] = dict(points[w.A], ref="DUPLICATE")
        pairs = Counter({(flow.ORIGIN_REF, w.A): 3, (flow.ORIGIN_REF, "DUPLICATE"): 1, (w.C, w.C2): 2,
                         (w.E, flow.DESTINATION_REF): 1})
        fake = w.FakeTmap()
        client = TmapWalkingClient(fake.app_key, transport=fake.transport())
        walks, stats = flow.lookup_walks(pairs, points, client, search_option="0", max_lookups=2)
        self.assertEqual((len(fake.requests), stats["reused_same_coordinates"]), (2, 1))
        self.assertEqual(walks[(w.E, flow.DESTINATION_REF)].status, "unknown")
        self.assertIsNone(walks[(w.E, flow.DESTINATION_REF)].as_walk_result(), "미조회는 시간 미확인(결과 없음)")
        self.assertIn("per_search_limit", walks[(w.E, flow.DESTINATION_REF)].reason)

    def test_failure_and_missing_coordinate(self):
        fake = w.FakeTmap(fail_pairs={(w.STOPS[w.C][1:], w.STOPS[w.C2][1:])})
        client = TmapWalkingClient(fake.app_key, transport=fake.transport())
        points = dict(self.points)
        points[w.F] = {"ref": w.F, "kind": "stop", "lat": None, "lon": None}
        pairs = Counter({(w.C, w.C2): 1, (w.D, w.F): 1})
        walks, _ = flow.lookup_walks(pairs, points, client, search_option="0", max_lookups=60)
        failed = walks[(w.C, w.C2)]
        self.assertEqual((failed.status, failed.provider_code), ("error", "9999"))
        self.assertEqual(failed.as_walk_result().status, "lookup_failed")
        self.assertEqual((walks[(w.D, w.F)].status, walks[(w.D, w.F)].reason), ("unknown", "coordinate_unavailable"))


class CandidateShapeTest(unittest.TestCase):
    def test_direct_same_stop_other_stop_multi_transfer_and_repeat(self):
        _, index, result = search()
        shapes = {tuple(r.route_id[-2:] for r in c.rides): c for c in result.candidates}
        self.assertIn(("10",), shapes, "직행")
        self.assertTrue(shapes[("40", "10")].transfers[0].same_stop, "같은 정류소 환승")
        self.assertFalse(shapes[("40", "30")].transfers[0].same_stop, "다른 정류소 도보 환승")
        self.assertIn(("40", "50", "60"), shapes, "환승 2회")
        r20 = next(p for p in index.pattern_stops.values() if p.route_id == "TSTR000020")
        self.assertEqual(sum(1 for pid, _ in index.boardings_by_stop[w.B] if pid == r20.pattern_id), 2,
                         "반복 경유 위치를 각각 탑승 위치로 보존")
        self.assertFalse(result.coverage.truncated)

    def test_truncation_is_reported(self):
        _, _, result = search(config(search_max_candidates=2))
        self.assertTrue(result.coverage.truncated)
        self.assertIn("search_truncated", result.reason_codes)

    def test_required_targets_cover_board_alight_and_routes(self):
        _, _, result = search()
        targets = flow.required_targets(result.candidates)
        arrivals = {t.external_id for t in targets if t.kind == "arrivals"}
        positions = {t.external_id for t in targets if t.kind == "positions"}
        self.assertTrue({w.A, w.C, w.C2, w.F, w.E} <= arrivals)
        self.assertEqual(positions, {r.route_id for c in result.candidates for r in c.rides})


class EvaluationStagesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.net = network()
        self.context, _, self.result = search(self.cfg, self.net)

    def test_tago_arrivals_without_basis_are_limited(self):
        etas = [EtaReport(**{**row, "basis_at": None, "vehicle_id": None, "run_key": None})
                for row in w.synthetic_eta_rows(self.net.patterns, basis_at=NOW)]
        evaluation, recommendation = evaluate(self.result, self.context, self.cfg, etas=etas)
        self.assertEqual(evaluation.candidates, ())
        self.assertTrue(all("eta_basis_unconfirmed" in c.reason_codes for c in evaluation.limited))
        self.assertEqual((recommendation.ranked, recommendation.ranking_mode), ((), "nominal"))
        self.assertEqual(flow.search_status(self.result, recommendation)[0], "data_limited")

    def test_provider_etas_rank_nominal_with_multiple_vehicle_combinations(self):
        evaluation, recommendation = evaluate(self.result, self.context, self.cfg, etas=provider_etas(self.net))
        self.assertTrue(recommendation.ranked)
        self.assertEqual(recommendation.ranking_mode, "nominal")
        per_static = Counter(c.static_candidate_id for c in evaluation.candidates)
        self.assertGreater(max(per_static.values()), 1, "정적 경로 하나에 복수 차량 조합")
        times = [r.time_seconds for r in recommendation.ranked]
        self.assertEqual(times, sorted(times))
        top = evaluation.candidates[0]
        self.assertIsNone(top.journey_probability)
        self.assertIn("joint_scenes_insufficient", top.reason_codes)
        self.assertIsNone(top.expected_total_seconds)

    def test_missing_alight_eta_keeps_partial_result(self):
        evaluation, recommendation = evaluate(self.result, self.context, self.cfg,
                                              etas=provider_etas(self.net, with_alight=False))
        direct = [c for c in evaluation.limited if c.transfer_count == 0]
        self.assertTrue(direct)
        self.assertTrue(all(c.time_status == "partial" and c.legs and c.legs[0].alight_eta is None for c in direct))
        self.assertTrue(any("segment_samples_insufficient" in c.reason_codes for c in direct))
        self.assertFalse(recommendation.ranked)

    def test_transfer_samples_only_give_transfer_probability_not_journey(self):
        evaluation, _ = evaluate(self.result, self.context, self.cfg, etas=provider_etas(self.net))
        candidate = next(c for c in evaluation.candidates if c.transfer_count == 1)
        rows = []
        for index, record in sample_rules.build_sample_records(
                candidate, static_version="v", cohort=sample_rules.condition_key("tago", self.cfg.evaluation_policy),
                policy=self.cfg.evaluation_policy, evaluated_at=NOW, label_deadline_seconds=7200,
                config_version="c")[0]:
            rows.append({"id": "s1", "label_status": "labeled", "independence_group": record.independence_group,
                         "cohort_key": record.cohort_key, "prediction_context": record.prediction_context,
                         "front_prediction_at": record.front_prediction_at,
                         "next_prediction_at": record.next_prediction_at,
                         "front_observed_at": record.front_prediction_at + timedelta(seconds=30),
                         "next_observed_at": record.next_prediction_at})
        samples = sample_rules.transfer_error_samples(rows)
        evaluation, recommendation = evaluate(self.result, self.context, self.cfg, etas=provider_etas(self.net),
                                              samples=samples)
        same = next(c for c in evaluation.candidates if c.timed_candidate_id == candidate.timed_candidate_id)
        self.assertIsNotNone(same.transfer_probabilities[0])
        self.assertIsNone(same.journey_probability, "개별 확률을 곱해 전체 확률을 만들지 않음")
        self.assertEqual(recommendation.ranking_mode, "nominal")

    def _scenes(self, evaluation, skip_static=None):
        rows = []
        cohort = sample_rules.condition_key("tago", self.cfg.evaluation_policy)
        timed = [*evaluation.candidates, *evaluation.limited]
        for static_id in sorted({c.static_candidate_id for c in timed}):
            if static_id == skip_static:
                continue
            record, reason = sample_rules.build_scene_record(static_id, timed, static_version="v", cohort=cohort,
                                                             policy=self.cfg.evaluation_policy, evaluated_at=NOW,
                                                             label_deadline_seconds=7200)
            if record is None:
                continue
            observations = [{"ride_index": p["ride_index"], "vehicle_rank": p["vehicle_rank"],
                             "board": {"observed_at": p["board"]["predicted_arrival_at"]},
                             "alight": {"observed_at": p["alight"]["predicted_arrival_at"]}} for p in record.predictions]
            rows.append({"id": static_id, "status": "complete", "independence_group": record.independence_group,
                         "cohort_key": cohort, "route_context": record.route_context,
                         "predictions": record.predictions, "observations": observations})
        return sample_rules.joint_scenes(rows)

    def test_expected_mode_only_when_whole_group_has_expected_time(self):
        evaluation, _ = evaluate(self.result, self.context, self.cfg, etas=provider_etas(self.net))
        scenes = self._scenes(evaluation)
        full, rec_full = evaluate(self.result, self.context, self.cfg, etas=provider_etas(self.net), scenes=scenes)
        self.assertEqual(rec_full.ranking_mode, "expected", rec_full.mode_reason_codes)
        self.assertTrue(all(c.journey_probability is not None for c in full.candidates))
        some_static = evaluation.candidates[0].static_candidate_id
        partial, rec_partial = evaluate(self.result, self.context, self.cfg, etas=provider_etas(self.net),
                                        scenes=self._scenes(evaluation, skip_static=some_static))
        self.assertEqual((rec_partial.ranking_mode, rec_partial.mode_reason_codes),
                         ("nominal", ("expected_time_missing_in_group",)))
        self.assertTrue(rec_partial.ranked, "확률·기대시간이 없는 후보도 nominal 순위에서 제외하지 않음")


class SampleRulesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        context, _, result = search(self.cfg)
        self.evaluation, _ = evaluate(result, context, self.cfg, etas=provider_etas(network()))
        self.candidate = next(c for c in self.evaluation.candidates if c.transfer_count == 1)

    def build(self, candidate, evaluated_at):
        return sample_rules.build_sample_records(candidate, static_version="v", cohort="k",
                                                 policy=self.cfg.evaluation_policy, evaluated_at=evaluated_at,
                                                 label_deadline_seconds=7200, config_version="c")

    def test_key_ignores_evaluation_time_and_requires_run(self):
        first, _ = self.build(self.candidate, NOW)
        later, _ = self.build(self.candidate, NOW + timedelta(minutes=1))
        self.assertEqual(first[0][1].sample_key, later[0][1].sample_key, "매분 재평가해도 같은 표본")
        self.assertNotIn("user", json.dumps(first[0][1].prediction_context, default=str))
        legs = tuple(leg.model_copy(update={"run_key": None, "board_eta": leg.board_eta.model_copy(
            update={"run_key": None})}) for leg in self.candidate.legs)
        no_run = self.candidate.model_copy(update={"legs": legs})
        records, skips = self.build(no_run, NOW)
        self.assertEqual((records, [s.reason for s in skips]), ([], ["run_unconfirmed"]))

    def test_label_rules(self):
        record = self.build(self.candidate, NOW)[0][0][1]
        row = {"walk_seconds": record.walk_seconds, "buffer_seconds": record.buffer_seconds,
               "label_deadline_at": record.label_deadline_at}
        front = sample_rules.EventMatch(1, record.front_prediction_at, "exact")
        late = sample_rules.EventMatch(2, record.next_prediction_at - timedelta(hours=1), "exact")
        on_time = sample_rules.EventMatch(3, record.next_prediction_at + timedelta(minutes=5), "exact")
        bounded = sample_rules.EventMatch(4, None, "bounded", NOW, NOW + timedelta(minutes=10))
        self.assertFalse(sample_rules.label_sample(row, front, late, NOW).time_feasible)
        self.assertTrue(sample_rules.label_sample(row, front, on_time, NOW).time_feasible)
        self.assertIsNone(sample_rules.label_sample(row, front, bounded, NOW), "범위만 있으면 기한 전 대기")
        expired = sample_rules.label_sample(row, front, bounded, record.label_deadline_at)
        self.assertEqual((expired.label_status, expired.time_feasible), ("insufficient", None))
        self.assertEqual(expired.observation_context["next"]["quality"], "bounded")


class LegsTest(unittest.TestCase):
    def test_legs_and_ranking_columns(self):
        cfg = config()
        net = network()
        context, _, result = search(cfg, net)
        evaluation, recommendation = evaluate(result, context, cfg, etas=provider_etas(net))
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
        ranked_id = recommendation.ranked[0].timed_candidate_id
        same_stop = next(c for c in evaluation.candidates
                         if c.transfer_count == 1 and statics[c.static_candidate_id].transfers[0].same_stop)
        for item, expect_rank in ((next(c for c in evaluation.candidates if c.timed_candidate_id == ranked_id), 1),
                                  (same_stop, None)):
            row = flow.candidate_row(statics[item.static_candidate_id], item, recommendation, walks, points, info,
                                     {}, {}, None)
            kinds = [leg["kind"] for leg in row["legs"]]
            self.assertEqual(kinds[0], "walk")
            self.assertEqual(kinds[-1], "walk")
            self.assertEqual([leg["leg_index"] for leg in row["legs"]], list(range(len(kinds))))
            for leg in row["legs"]:
                if leg["kind"] == "bus":
                    self.assertEqual(leg["geometry_status"], "unavailable")
                    self.assertIsNotNone(leg["board"]["name"])
                elif leg["status"] == "available":
                    self.assertEqual(leg["provider"], "TMAP")
                    self.assertGreater(leg["duration_seconds"], 0)
            json.dumps(row["legs"])
            if expect_rank:
                self.assertEqual((row["rank_no"], row["ranking_mode"]), (1, "nominal"))
            else:
                self.assertIn("same_stop", [leg.get("status") for leg in row["legs"]])
                transfer = row["evaluation"]["transfers"][0]
                self.assertEqual(row["legs"][transfer["front_leg_index"]]["kind"], "bus")


if __name__ == "__main__":
    unittest.main()
