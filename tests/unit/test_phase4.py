"""Phase 4 단위 시험(DB·네트워크 없음): 토큰 검증, 이동 상태 전이, 형상 구간, 장소 검색 계약, 응답 규칙."""

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import jwt
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.dependencies import active_user
from app.core.security import AuthConfigError, AuthError, AuthUser, TokenVerifier, bearer_token, load_verifier
from app.integrations import places_api
from app.integrations import route_shapes_api as shapes
from app.modules.journeys import state
from app.modules.journeys.schemas import EventBody
from app.modules.journeys.service import request_fingerprint
from app.modules.routing import service_area, views
from app.modules.users.schemas import ProfilePatch
from app.workflows import guidance

SECRET = "unit-test-secret-not-a-real-key-000000000000"
ISSUER = "http://auth.test/auth/v1"
UTC = timezone.utc


def token(**overrides):
    claims = {"sub": str(uuid4()), "aud": "authenticated", "iss": ISSUER, "role": "authenticated",
              "exp": int(time.time()) + 600, "email": "tester@example.invalid"}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, SECRET, algorithm="HS256")


class TokenVerifierTest(unittest.TestCase):
    def setUp(self):
        self.verifier = TokenVerifier(SECRET, issuer=ISSUER, audience="authenticated")

    def assertRejected(self, raw, code="INVALID_TOKEN"):
        with self.assertRaises(AuthError) as ctx:
            self.verifier.verify(raw)
        self.assertEqual(ctx.exception.code, code)

    def test_valid_token_gives_user_from_sub(self):
        sub = str(uuid4())
        user = self.verifier.verify(token(sub=sub))
        self.assertEqual(str(user.user_id), sub)
        self.assertEqual(user.role, "authenticated")
        self.assertGreater(user.expires_at, datetime.now(UTC))

    def test_rejections(self):
        self.assertRejected(token(exp=int(time.time()) - 5), "TOKEN_EXPIRED")
        self.assertRejected(token(aud="other"))
        self.assertRejected(token(iss="http://evil.test"))
        self.assertRejected(token(sub="not-a-uuid"))
        self.assertRejected(token(sub=None))
        self.assertRejected(token(exp=None))
        self.assertRejected(token(role="anon"))
        self.assertRejected(token(role="service_role"))
        self.assertRejected(jwt.encode({"sub": str(uuid4()), "aud": "authenticated", "iss": ISSUER,
                                        "role": "authenticated", "exp": int(time.time()) + 60},
                                       "another-secret-value-000000000000000000", algorithm="HS256"))
        self.assertRejected(jwt.encode({"sub": str(uuid4()), "aud": "authenticated", "iss": ISSUER,
                                        "role": "authenticated", "exp": int(time.time()) + 60},
                                       SECRET + SECRET, algorithm="HS512"))
        unsigned = jwt.encode({"sub": str(uuid4()), "aud": "authenticated", "iss": ISSUER, "role": "authenticated",
                               "exp": int(time.time()) + 60}, None, algorithm="none")
        self.assertRejected(unsigned)
        self.assertRejected("abc")
        self.assertRejected(token()[:-3] + "AAA")

    def test_bearer_parsing_and_repr_hides_secret(self):
        self.assertEqual(bearer_token("Bearer abc.def.ghi"), "abc.def.ghi")
        for bad in (None, "", "Basic xyz", "Bearer   "):
            with self.assertRaises(AuthError):
                bearer_token(bad)
        self.assertNotIn(SECRET, repr(self.verifier))

    def test_load_verifier_uses_settings_then_root_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(f"JWT_SECRET={SECRET}\nAPI_EXTERNAL_URL={ISSUER}\n", encoding="utf-8")
            verifier = load_verifier(Settings(_env_file=None), env_file=env)
            self.assertEqual(verifier.issuer, ISSUER)
            override = load_verifier(Settings(_env_file=None, auth_jwt_issuer="http://x.test",
                                              auth_jwt_secret="s" * 40), env_file=env)
            self.assertEqual(override.issuer, "http://x.test")
            empty = Path(tmp) / "empty.env"
            empty.write_text("", encoding="utf-8")
            with self.assertRaises(AuthConfigError) as ctx:
                load_verifier(Settings(_env_file=None), env_file=empty)
            self.assertIn("JWT_SECRET", str(ctx.exception))


def legs_two_rides():
    def walk(i, status="available"):
        return {"leg_index": i, "kind": "walk", "status": status, "duration_seconds": 100.0, "distance_m": 120.0}

    def bus(i, route, board, alight):
        return {"leg_index": i, "kind": "bus", "route_id": f"R{route}", "route_internal_id": route,
                "pattern_key": f"P{route}", "board": {"stop_id": board, "stop_internal_id": 1, "seq": 1},
                "alight": {"stop_id": alight, "stop_internal_id": 2, "seq": 3}, "vehicle": None}

    return [walk(0), bus(1, 10, "A", "C"), walk(2, "same_stop"), bus(3, 50, "C", "F"), walk(4)]


class JourneyStateTest(unittest.TestCase):
    NOW = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)

    def apply(self, current, event, payload, **resolved):
        return state.apply(lifecycle_status=current["lifecycle"], movement_state=current["movement"],
                           progress=current["progress"], legs=self.legs, event_type=event, payload=payload,
                           occurred_at=None, received_at=self.NOW, resolved=resolved)

    def setUp(self):
        self.legs = legs_two_rides()
        self.current = {"lifecycle": "active", "movement": state.initial_movement(self.legs),
                        "progress": state.initial_progress(self.legs)}

    def step(self, event, payload, **resolved):
        t = self.apply(self.current, event, payload, **resolved)
        self.current = {"lifecycle": t.lifecycle_status, "movement": t.movement_state, "progress": t.progress}
        return t

    def test_board_alight_transfer_then_final_walk_and_complete(self):
        self.assertEqual(self.current["movement"], "walking")
        self.assertEqual(self.current["progress"]["next_bus_leg_index"], 1)
        t = self.step("board", {"legIndex": 1, "vehicleId": None})
        self.assertEqual(t.movement_state, "onboard")
        self.assertIsNone(t.progress["boarded"]["vehicle_no"], "차량 미확인 허용, 계획 차량으로 채우지 않음")
        self.assertTrue(t.progress["boarded"]["matches_planned_route"])
        t = self.step("alight", {"legIndex": 1})
        self.assertEqual((t.movement_state, t.progress["next_bus_leg_index"]), ("transferring", 3))
        t = self.step("board", {"legIndex": 3, "vehicleId": "대전70자1234"})
        self.assertEqual(t.progress["boarded"]["vehicle_no"], "대전70자1234")
        t = self.step("alight", {"legIndex": 3})
        self.assertEqual((t.movement_state, t.progress["next_bus_leg_index"]), ("walking", None))
        self.assertEqual(t.progress["completed_bus_legs"], [1, 3])
        t = self.step("completed", {})
        self.assertEqual((t.lifecycle_status, t.movement_state, t.ended), ("completed", "arrived", True))

    def test_invalid_transitions(self):
        for event, payload, code in [("alight", {"legIndex": 1}, "INVALID_TRANSITION"),
                                     ("board", {"legIndex": 3}, "LEG_NOT_NEXT"),
                                     ("board", {"legIndex": 0}, "INVALID_LEG_INDEX"),
                                     ("board", {"legIndex": 99}, "INVALID_LEG_INDEX"),
                                     ("missed_bus", {}, "UNSUPPORTED_EVENT_TYPE")]:
            with self.subTest(event=event, payload=payload):
                with self.assertRaises(state.TransitionError) as ctx:
                    self.apply(self.current, event, payload)
                self.assertEqual(ctx.exception.code, code)
        self.step("board", {"legIndex": 1})
        with self.assertRaises(state.TransitionError) as ctx:
            self.apply(self.current, "board", {"legIndex": 1})
        self.assertEqual(ctx.exception.code, "INVALID_TRANSITION")
        with self.assertRaises(state.TransitionError) as ctx:
            self.apply(self.current, "alight", {"legIndex": 3})
        self.assertEqual(ctx.exception.code, "LEG_NOT_CURRENT")

    def test_ended_journey_accepts_only_correction(self):
        board = self.step("board", {"legIndex": 1})
        progress = state.stamp_event_id("board", board.progress, 11)
        self.current["progress"] = progress
        self.step("cancelled", {"reason": "사용자 취소"})
        with self.assertRaises(state.TransitionError) as ctx:
            self.apply(self.current, "alight", {"legIndex": 1})
        self.assertEqual(ctx.exception.code, "JOURNEY_ENDED")
        t = self.apply(self.current, "correction", {"supersedesEventId": "11", "vehicleId": "V-9"},
                       target={"id": 11, "event_type": "board"}, event_id=None)
        self.assertEqual(t.lifecycle_status, "cancelled")
        self.assertEqual(t.progress["boarded"]["vehicle_no"], "V-9")
        self.assertEqual(t.effect["applied_to"], "current_boarding")
        with self.assertRaises(state.TransitionError):
            self.apply(self.current, "correction", {"supersedesEventId": "11", "stopId": "5"},
                       target={"id": 11, "event_type": "board"})

    def test_board_with_other_route_is_recorded_as_not_planned(self):
        t = self.step("board", {"legIndex": 1, "routeId": "77"}, route_internal_id=77, route_external_id="R77")
        self.assertFalse(t.progress["boarded"]["matches_planned_route"])
        self.assertEqual(t.progress["boarded"]["route_external_id"], "R77")


class ShapeSegmentTest(unittest.TestCase):
    def test_missing_provider_and_pattern(self):
        entry = shapes.pattern_entry({}, "P1")
        self.assertEqual((entry["status"], entry["reason"]), ("unavailable", shapes.NOT_CONFIGURED_REASON))
        entry = shapes.pattern_entry({"patterns": {"P2": {"status": "available"}}}, "P1")
        self.assertEqual(entry["reason"], shapes.PATTERN_MISSING_REASON)
        seg = shapes.segment_geometry(entry, 1, 3)
        self.assertEqual((seg["geometry"], seg["geometry_status"]), (None, "unavailable"))

    def test_slice_uses_verified_stop_mapping_only(self):
        line = {"type": "LineString", "coordinates": [[127.0, 36.0], [127.1, 36.1], [127.2, 36.2], [127.3, 36.3]]}
        info = {"patterns": {"P1": {"status": "available", "geometry": line, "source": "verified-provider",
                                    "version": "v1", "fetched_at": "2026-11-08T00:00:00+00:00",
                                    "stop_vertex_index": {"1": 0, "2": 2, "3": 3}}}}
        seg = shapes.segment_geometry(shapes.pattern_entry(info, "P1"), 2, 3)
        self.assertEqual(seg["geometry_status"], "available")
        self.assertEqual(seg["geometry"]["coordinates"], [[127.2, 36.2], [127.3, 36.3]])
        no_map = shapes.segment_geometry(shapes.pattern_entry(info, "P1"), 1, 4)
        self.assertEqual((no_map["geometry_status"], no_map["geometry_reason"]),
                         ("unavailable", shapes.STOP_MAPPING_MISSING_REASON))
        reversed_ = shapes.segment_geometry(shapes.pattern_entry(info, "P1"), 3, 2)
        self.assertEqual(reversed_["geometry_reason"], shapes.SEGMENT_INVALID_REASON)
        broken = shapes.pattern_entry({"patterns": {"P1": {"status": "available", "geometry": {"type": "Point"}}}},
                                      "P1")
        self.assertEqual((broken["status"], broken["reason"]), ("error", "shape_geometry_invalid"))


class FakePlaces:
    name = "contract-test"

    def __init__(self, result):
        self.result = result
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class PlacesContractTest(unittest.TestCase):
    def setUp(self):
        from app import main

        self.app = main.app
        self.app.dependency_overrides[active_user] = lambda: AuthUser(uuid4(), "authenticated",
                                                                      datetime.now(UTC) + timedelta(hours=1))
        self.addCleanup(self.app.dependency_overrides.clear)
        self.addCleanup(setattr, self.app.state, "places_provider", None)
        self.client = TestClient(self.app)

    def get(self, provider, q="대전역"):
        self.app.state.places_provider = provider
        return self.client.get("/api/v1/places/search", params={"q": q})

    def test_items_empty_and_errors(self):
        item = places_api.PlaceItem("대전역", "대전 동구 중앙로 215", 36.3315, 127.4335, "contract-test", "P-1")
        provider = FakePlaces([item])
        r = self.get(provider, "  대전역 ")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(provider.queries, ["대전역"], "공백을 제거한 검색어")
        self.assertEqual(r.json()["items"][0], {"name": "대전역", "address": "대전 동구 중앙로 215",
                                                "latitude": 36.3315, "longitude": 127.4335,
                                                "provider": "contract-test", "externalId": "P-1"})
        self.assertEqual(self.get(FakePlaces([])).json(), {"items": []})
        self.assertEqual(self.get(FakePlaces([]), "   ").status_code, 422)
        for exc, status in [(places_api.PlacesRateLimited("x"), 429), (places_api.PlacesUpstreamError("x"), 502),
                            (places_api.PlacesUnavailable("x"), 503), (places_api.PlacesTimeout("x"), 504)]:
            self.assertEqual(self.get(FakePlaces(exc)).status_code, status)
        bad = places_api.PlaceItem("x", None, 91.0, 127.0, "contract-test", None)
        self.assertEqual(self.get(FakePlaces([bad])).status_code, 502)

    def test_unconfigured_provider_is_503(self):
        self.app.state.places_provider = None
        settings = self.app.state.settings
        self.addCleanup(setattr, self.app.state, "settings", settings)
        self.app.state.settings = Settings(_env_file=None)
        r = self.client.get("/api/v1/places/search", params={"q": "대전역"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (503, "PLACES_NOT_CONFIGURED"))


class ResponseRulesTest(unittest.TestCase):
    NOW = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)

    def test_startability(self):
        search = {"status": "ready", "expires_at": self.NOW + timedelta(minutes=5)}
        self.assertEqual(views.startability(search, {"rank_no": 1}, self.NOW), (True, None))
        self.assertEqual(views.start_mode(search, {"rank_no": 1}, self.NOW), ("ranked", None))
        self.assertEqual(views.startability({**search, "expires_at": self.NOW}, {"rank_no": 1}, self.NOW)[1],
                         "SEARCH_EXPIRED")
        self.assertEqual(views.startability({**search, "status": "processing"}, {"rank_no": 1}, self.NOW)[1],
                         "SEARCH_NOT_READY")
        self.assertEqual(views.startability({**search, "status": "error"}, {"rank_no": None}, self.NOW)[1],
                         "SEARCH_FAILED")

    def test_unranked_candidate_starts_only_when_limited_by_data(self):
        search = {"status": "data_limited", "expires_at": self.NOW + timedelta(minutes=5)}

        def unranked(status, reasons=("eta_basis_unconfirmed",), ranking=("time_unavailable",)):
            return {"rank_no": None, "evaluation": {"connection_status": status, "reason_codes": list(reasons),
                                                    "ranking": {"ranked": False, "reason_codes": list(ranking)}}}

        for status in ("time_unknown", None):
            self.assertEqual(views.start_mode(search, unranked(status), self.NOW), ("data_limited", None), status)
        for status in ("confirmed_infeasible", "negative_slack"):
            self.assertEqual(views.start_mode(search, unranked(status), self.NOW), (None, "CANDIDATE_NOT_STARTABLE"),
                             "자료 부족이 아니라 기준 ETA·도보 결과로 연결 불가가 판정된 후보")
        self.assertEqual(views.start_mode({**search, "expires_at": self.NOW}, unranked("time_unknown"), self.NOW),
                         (None, "SEARCH_EXPIRED"))
        self.assertEqual(views.start_limit_reasons(unranked("time_unknown", ("a", "b"), ("b", "c"))), ["a", "b", "c"])
        self.assertEqual(views.start_limit_reasons({"rank_no": 2, "evaluation": {"reason_codes": ["x"]}}), [])

    def test_profile_patch_distinguishes_omitted_and_null(self):
        self.assertFalse(ProfilePatch.model_validate({}).provided())
        self.assertTrue(ProfilePatch.model_validate({"displayName": None}).provided())
        with self.assertRaises(Exception):
            ProfilePatch.model_validate({"displayName": "a", "other": 1})

    def test_event_fingerprint_is_stable(self):
        a = EventBody(eventType="board", stateVersion=3, payload={"legIndex": 1, "vehicleId": None})
        b = EventBody(eventType="board", stateVersion=3, payload={"vehicleId": None, "legIndex": 1})
        c = EventBody(eventType="board", stateVersion=3, payload={"legIndex": 1, "vehicleId": "V"})
        self.assertEqual(request_fingerprint(a)[1], request_fingerprint(b)[1])
        self.assertNotEqual(request_fingerprint(a)[1], request_fingerprint(c)[1])

    def test_remaining_route_and_targets(self):
        legs = legs_two_rides()
        progress = state.initial_progress(legs)
        snapshot = {"legs": legs, "static_route_key": "S1"}
        self.assertEqual([leg["leg_index"] for leg in guidance.remaining_bus_legs(legs, progress, "walking")], [1, 3])
        targets = guidance.journey_targets(snapshot, progress, "walking", "journey:x")
        self.assertEqual({(t.kind, t.external_id) for t in targets},
                         {("arrivals", "A"), ("arrivals", "C"), ("arrivals", "F"), ("positions", "R10"),
                          ("positions", "R50")})
        with_via = {**snapshot, "legs": [{**leg, "via_stop_ids": ["V1", "V2"]} if leg["leg_index"] == 1 else leg
                                         for leg in legs]}
        arrivals = {t.external_id for t in guidance.journey_targets(with_via, progress, "walking", "j") if
                    t.kind == "arrivals"}
        self.assertTrue({"V1", "V2"} <= arrivals, "타기 전 구간은 경유 정류소도 수집")
        onboard = {**progress, "boarded": {"leg_index": 1}}
        arrivals = {t.external_id for t in guidance.journey_targets(with_via, onboard, "onboard", "j") if
                    t.kind == "arrivals"}
        self.assertFalse({"V1", "V2"} & arrivals, "탑승 중 구간은 경유 정류소를 넣지 않음")
        done = {**progress, "completed_bus_legs": [1], "next_bus_leg_index": 3}
        rides = guidance.remaining_bus_legs(legs, done, "transferring")
        candidate, walks = guidance.remaining_candidate(snapshot, rides, first_walk_leg=legs[2], static_version="v")
        self.assertEqual((candidate.access_stop_id, candidate.egress_stop_id, candidate.boarding_count), ("C", "F", 1))
        first = next(w for w in walks if w.to_ref == "C")
        self.assertEqual((first.status, first.duration_seconds), ("available", 0.0), "같은 정류소 환승은 0초 도보")


class ServiceAreaRulesTest(unittest.TestCase):
    def point(self, role, inside, served):
        return service_area.PointScope(role, 36.0, 127.0, 300.0, inside, served, None)

    def test_decision_matrix(self):
        S = service_area
        ok = S.decide([self.point("origin", True, 0), self.point("destination", None, 2)], {}, "v")
        self.assertEqual((ok.status, ok.reason_codes), (S.IN_SCOPE, []), "경계 안은 정류소가 없어도, 경계 자료 없이도 정류소로 범위 안")
        out = S.decide([self.point("origin", False, 0), self.point("destination", True, None)], {}, "v")
        self.assertEqual((out.status, out.reason_codes), (S.OUT_OF_SCOPE, ["origin_out_of_scope"]))
        both = S.decide([self.point("origin", False, 0), self.point("destination", False, 0)], {}, "v")
        self.assertEqual(both.reason_codes, ["origin_out_of_scope", "destination_out_of_scope"])
        unknown = S.decide([self.point("origin", None, 0), self.point("destination", True, 0)], {}, "v")
        self.assertEqual((unknown.status, unknown.reason_codes), (S.UNKNOWN, ["service_area_unavailable"]))
        no_static = S.decide([self.point("origin", False, None), self.point("destination", True, None)], {}, None)
        self.assertEqual(no_static.status, S.UNKNOWN, "경계 밖이지만 정적 공개본을 확인 못 하면 판단 불가")
        mixed = S.decide([self.point("origin", False, 0), self.point("destination", None, 0)], {}, "v")
        self.assertEqual((mixed.status, mixed.reason_codes), (S.OUT_OF_SCOPE, ["origin_out_of_scope"]),
                         "범위 밖이 확인된 지점이 있으면 out_of_scope가 우선")

    def test_boundary_file_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = service_area.load_boundary(Path(tmp) / "none.geojson")
            self.assertEqual((missing.boundary, missing.reason), (None, "boundary_file_missing"))
            bad = Path(tmp) / "bad.geojson"
            bad.write_text('{"type": "FeatureCollection", "features": [{"geometry": {"type": "Point"}}]}',
                           encoding="utf-8")
            self.assertEqual(service_area.load_boundary(bad).reason, "boundary_file_invalid")
            good = Path(tmp) / "good.geojson"
            good.write_text('{"type": "FeatureCollection", "features": [{"type": "Feature", "properties": '
                            '{"NAME": "x"}, "geometry": {"type": "Polygon", "coordinates": '
                            '[[[0,0],[1,0],[1,1],[0,0]]]}}]}', encoding="utf-8")
            state = service_area.load_boundary(good)
            self.assertEqual((state.boundary.name, state.reason), ("x", None))
        official = service_area.load_boundary(None)
        self.assertEqual(official.boundary.name, "대전광역시", "저장소 기본 경계 파일")
        self.assertIn('"MultiPolygon"', official.boundary.geometry_json)


class ViewTargetKeyTest(unittest.TestCase):
    def test_target_key_round_trip(self):
        from app.modules.transit import service as transit_service
        from app.modules.transit.schemas import RealtimeTarget

        for target in (RealtimeTarget("positions", "DJB30300004"), RealtimeTarget("arrivals", "DJB8001793")):
            key = transit_service.realtime_target_key(target)
            back = transit_service.target_from_key(key, source="realtime_view")
            self.assertEqual((back.kind, back.external_id, back.sources),
                             (target.kind, target.external_id, frozenset({"realtime_view"})))
        for bad in ("tago:getRouteAcctoBusLcList?cityCode=99&routeId=X", "other:x?y=1",
                    "tago:getSttnAcctoArvlPrearngeInfoList?cityCode=25"):
            self.assertIsNone(transit_service.target_from_key(bad))


class EtaBasisPolicyTest(unittest.TestCase):
    """도착정보 ETA 기준 시각 정책: 기본은 미산출, 근사 정책은 출처를 남기고 원천 시각 칸을 채우지 않는다."""

    AT = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)

    def obs(self, **kw):
        return {"eta_basis_at": None, "predicted_arrival_at": None, "remaining_seconds": 600, "collected_at": self.AT,
                "source_generated_at": None, "details": {}, **kw}

    def test_default_policy_leaves_basis_unconfirmed(self):
        from app.modules.transit import eta_basis

        self.assertEqual(eta_basis.resolve_basis(self.obs(), eta_basis.UNCONFIRMED), (None, None))
        self.assertIsNone(eta_basis.predicted_arrival(self.obs(), None))
        self.assertEqual(Settings(_env_file=None).tago_eta_basis_policy, "unconfirmed")

    def test_received_at_policy_counts_down_from_collection(self):
        from app.modules.transit import eta_basis

        basis, source = eta_basis.resolve_basis(self.obs(), eta_basis.SERVER_RECEIVED_AT)
        self.assertEqual((basis, source), (self.AT, "server_received_at_approx"))
        arrival = eta_basis.predicted_arrival(self.obs(), basis)
        self.assertEqual(arrival, self.AT + timedelta(seconds=600))
        # 5분 뒤 조회하면 남은 시간은 5분(잔여초를 조회 시각에 다시 더하지 않는다).
        self.assertEqual((arrival - (self.AT + timedelta(minutes=5))).total_seconds(), 300)
        self.assertEqual(eta_basis.resolve_basis(self.obs(remaining_seconds=None), eta_basis.SERVER_RECEIVED_AT),
                         (None, None), "잔여초가 없으면 근사하지 않음")
        confirmed = self.obs(eta_basis_at=self.AT - timedelta(seconds=20), details={"eta_basis_source": "x-provider"})
        self.assertEqual(eta_basis.resolve_basis(confirmed, eta_basis.SERVER_RECEIVED_AT),
                         (self.AT - timedelta(seconds=20), "x-provider"), "확인된 기준이 있으면 근사보다 우선")
        self.assertEqual(eta_basis.policy_version(eta_basis.SERVER_RECEIVED_AT), "tago-arrival-basis-server-received-at/1")

    def test_approximated_predictions_use_a_separate_sample_condition(self):
        from app.modules.evaluation import samples
        from app.modules.evaluation.schemas import EvaluationPolicy

        policy = EvaluationPolicy(
            max_boardings=2, access_radius_meters=300, transfer_radius_meters=150, egress_radius_meters=300,
            max_candidates=10, max_search_states=1000, search_budget_ms=1000, search_horizon_seconds=3600,
            round_pool_size=50, walk_cost_weight=3,
            evaluation_budget_ms=1000, max_timed_candidates=10, boarding_buffer_seconds=60, max_data_age_seconds=900,
            min_segment_samples=5, min_probability_samples=30, min_joint_scenes=30, min_improvement_seconds=120,
            max_horizon_difference_seconds=300, max_recovery_attempts=2, recovery_search=None, timing_policy_version="t1",
            sample_policy_version="s1", recovery_policy_version="r1", segment_model_version="m1")
        base = samples.condition_key("tago", policy)
        self.assertEqual(samples.condition_key("tago", policy, "unconfirmed"), base, "기본 정책 키는 기존과 같음")
        self.assertNotEqual(samples.condition_key("tago", policy, "server_received_at"), base)


if __name__ == "__main__":
    unittest.main()
