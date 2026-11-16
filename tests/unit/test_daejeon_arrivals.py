"""대전시 도착정보 수집 해석·대상 키·같은 차량 하차 ETA 연결 단위 시험(DB·네트워크 없음).

XML_OK는 2026-11-09 사용자가 확인한 실응답(도마네거리, BusStopID 8001488)의 형식이다.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.integrations.daejeon_bus_api import RawResponse, parse_arrivals
from app.modules.evaluation.eta import (
    ALIGHT_ETA_BY_HEADWAY_ESTIMATE,
    ALIGHT_ETA_BY_LEADING_VEHICLE_GAP,
    ALIGHT_LINKED_WITHOUT_RUN_KEY,
    BOARD_ETA_BY_HEADWAY_ESTIMATE,
    BOARD_ETA_BY_LEADING_VEHICLE_GAP,
    NEXT_VEHICLE_NOT_DEPARTED,
    EtaTable,
    build_ride_legs,
    index_etas,
    resolve_alight_eta,
)
from app.modules.evaluation.schemas import (
    EtaReport,
    EvaluationInputs,
    EvaluationPolicy,
    GapAnchor,
    RouteHeadway,
    VehicleEta,
    WalkResult,
)
from app.modules.evaluation.service import evaluate_candidates
from app.modules.recommendation.service import recommend_candidates
from app.modules.routing.schemas import (
    CandidateSearchResult,
    Coordinate,
    SearchContext,
    SearchCoverage,
    StaticRide,
)
from app.modules.routing.service import build_static_candidate
from app.modules.transit import service as transit_service
from app.modules.transit.schemas import RealtimeTarget
from app.workflows import search as flow

UTC = timezone.utc

XML_OK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ServiceResult><msgHeader><currentPage>1</currentPage><headerCd>0</headerCd><headerMsg>정상적으로 처리되었습니다.</headerMsg>
<itemCnt>2</itemCnt><itemPageCnt>1</itemPageCnt></msgHeader><msgBody>
<itemList><BUS_NODE_ID>8001488</BUS_NODE_ID><BUS_STOP_ID>30720</BUS_STOP_ID><CAR_REG_NO>대전75자2718</CAR_REG_NO>
<DESTINATION>서남부터미널종점</DESTINATION><EXTIME_MIN>16</EXTIME_MIN><EXTIME_SEC>922</EXTIME_SEC>
<INFO_OFFER_TM>2026-11-09 23:12:13</INFO_OFFER_TM><LAST_CAT>2</LAST_CAT><LAST_STOP_ID>20300</LAST_STOP_ID><MSG_TP>03</MSG_TP>
<ROUTE_CD>30300130</ROUTE_CD><ROUTE_NO>608</ROUTE_NO><ROUTE_TP>2  </ROUTE_TP><STATUS_POS>11</STATUS_POS>
<STOP_NAME>도마네거리</STOP_NAME></itemList>
<itemList><BUS_NODE_ID>8001488</BUS_NODE_ID><BUS_STOP_ID>30720</BUS_STOP_ID><CAR_REG_NO>대전75자3319</CAR_REG_NO>
<DESTINATION>뿌리공원</DESTINATION><EXTIME_MIN>2</EXTIME_MIN><EXTIME_SEC>73</EXTIME_SEC>
<INFO_OFFER_TM>2026-11-09 23:12:04</INFO_OFFER_TM><LAST_CAT>2</LAST_CAT><LAST_STOP_ID>30680</LAST_STOP_ID><MSG_TP>03</MSG_TP>
<ROUTE_CD>30300056</ROUTE_CD><ROUTE_NO>312</ROUTE_NO><ROUTE_TP>2  </ROUTE_TP><STATUS_POS>2</STATUS_POS>
<STOP_NAME>도마네거리</STOP_NAME></itemList></msgBody></ServiceResult>"""


def raw(body: str, status: int = 200) -> RawResponse:
    return RawResponse(requested_at="2026-11-09T14:12:20+00:00", received_at="2026-11-09T14:12:21+00:00",
                       http_status=status, content_type="application/xml;charset=UTF-8", body=body.encode("utf-8"))


class ParseArrivalsTest(unittest.TestCase):
    def test_real_response_shape(self):
        page = parse_arrivals(raw(XML_OK), {"BusStopID": "8001488"})
        self.assertEqual((page.status, page.provider_code, page.item_count, page.error), ("success", "0", 2, None))
        self.assertEqual(page.items[0]["CAR_REG_NO"], "대전75자2718")
        self.assertEqual((page.items[1]["EXTIME_SEC"], page.items[1]["STATUS_POS"]), ("73", "2"))
        self.assertEqual(page.items[0]["ROUTE_TP"], "2", "값 앞뒤 공백은 정리")

    def test_empty_partial_and_errors(self):
        empty = XML_OK.split("<msgBody>")[0].replace("<itemCnt>2</itemCnt>", "<itemCnt>0</itemCnt>") + \
            "<msgBody></msgBody></ServiceResult>"
        self.assertEqual(parse_arrivals(raw(empty), {}).status, "empty")
        partial = XML_OK.replace("<itemPageCnt>1</itemPageCnt>", "<itemPageCnt>2</itemPageCnt>")
        self.assertEqual(parse_arrivals(raw(partial), {}).status, "partial", "남은 페이지가 있으면 공개하지 않음")
        failed = XML_OK.replace("<headerCd>0</headerCd>", "<headerCd>4</headerCd>")
        page = parse_arrivals(raw(failed), {})
        self.assertEqual((page.status, page.provider_code), ("error", "4"))
        gateway = ("<OpenAPI_ServiceResponse><cmmMsgHeader><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR"
                   "</returnAuthMsg><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")
        page = parse_arrivals(raw(gateway, 401), {})
        self.assertEqual((page.status, page.provider_code), ("error", "30"))
        self.assertTrue(page.error.startswith("gateway"))
        self.assertTrue(parse_arrivals(raw("oops", 500), {}).error.startswith("http"))
        timeout = RawResponse(requested_at="t", error={"category": "timeout", "message": "slow"})
        self.assertEqual(parse_arrivals(timeout, {}).status, "timeout")

    def test_info_offer_time_is_kst(self):
        self.assertEqual(transit_service.parse_kst("2026-11-09 23:12:13"), datetime(2026, 11, 9, 14, 12, 13, tzinfo=UTC))
        self.assertIsNone(transit_service.parse_kst("20261109231213"))


class TargetKeyTest(unittest.TestCase):
    def test_arrivals_use_daejeon_key_positions_stay_tago(self):
        arrivals = transit_service.realtime_target_key(RealtimeTarget("arrivals", "DJB8001488"))
        self.assertEqual(arrivals, "daejeon:getArrInfoByStopID?nodeId=DJB8001488")
        self.assertTrue(transit_service.realtime_target_key(RealtimeTarget("positions", "DJB30300130"))
                        .startswith("tago:getRouteAcctoBusLcList?"))
        back = transit_service.target_from_key(arrivals, source="realtime_view")
        self.assertEqual((back.kind, back.external_id), ("arrivals", "DJB8001488"))
        old = "tago:getSttnAcctoArvlPrearngeInfoList?cityCode=25&nodeId=DJB8001488"
        self.assertIsNone(transit_service.target_from_key(old), "이전 TAGO 도착정보 키는 더 이상 수집 대상이 아님")

    def test_msg_tp_07_keeps_time_but_drops_last_passed_vehicle(self):
        """MSG_TP=07(다음 차 출발 전): 시각은 유지하고 차량번호(직전에 지나간 차)는 쓰지 않는다. 03은 그대로."""
        items = parse_arrivals(raw(XML_OK), {"BusStopID": "8001488"}).items
        waiting = {**items[0], "MSG_TP": "07"}
        with mock.patch.object(transit_service.repository, "route_ids", return_value={}), \
                mock.patch.object(transit_service.repository, "route_visits", return_value={}), \
                mock.patch.object(transit_service.repository, "stop_ids", return_value={}):
            rows = transit_service.normalize_daejeon_arrivals(
                None, "DJB8001488", "8001488", 1, [waiting, items[1]], datetime(2026, 11, 9, 14, 12, 21, tzinfo=UTC),
                None)
        self.assertIsNone(rows[0].vehicle_no)
        self.assertEqual(rows[0].details["vehicle_match"]["reason"], "msg_tp_07_last_passed_vehicle")
        self.assertEqual(rows[0].details["source"]["CAR_REG_NO"], "대전75자2718", "원값은 보존")
        self.assertIsNotNone(rows[0].predicted_arrival_at, "시각은 유지")
        self.assertEqual(rows[1].vehicle_no, "대전75자3319")

    def test_bus_stop_id_is_node_id_without_prefix(self):
        """BusStopID는 정류소 번호(stop_no 30720)가 아니라 'DJB'를 뗀 정류소 ID다(번호로 보내면 headerCd=4)."""
        self.assertEqual(transit_service.daejeon_bus_stop_id("DJB8001488"), "8001488")
        for bad in ("8001488", "DJB", "DJBabc", "CCB8001488", ""):
            self.assertIsNone(transit_service.daejeon_bus_stop_id(bad), bad)


class SameVehicleAlightTest(unittest.TestCase):
    """회차 키 없는 대전시 예측: 같은 차량·남은 정류장 수 차이 = 경유 순번 차이일 때만 하차 ETA로 잇는다."""

    AT = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
    RIDE = StaticRide(route_id="R", pattern_id="P", direction=None, board_stop_id="A", board_position=10,
                      alight_stop_id="B", alight_position=15)
    POLICY = EvaluationPolicy(
        max_boardings=2, access_radius_meters=300, transfer_radius_meters=150, egress_radius_meters=300,
        max_candidates=10, max_search_states=1000, search_budget_ms=1000, search_horizon_seconds=3600,
        round_pool_size=50, walk_cost_weight=3,
        evaluation_budget_ms=1000, max_timed_candidates=10, boarding_buffer_seconds=60, max_data_age_seconds=900,
        min_segment_samples=5, min_probability_samples=30, min_joint_scenes=30, min_improvement_seconds=120,
        max_horizon_difference_seconds=300, max_recovery_attempts=2, recovery_search=None,
        timing_policy_version="t1", sample_policy_version="s1", recovery_policy_version="r1",
        segment_model_version="m1")

    def eta(self, stop, order, seconds, vehicle="대전75자0001", stops_away=None):
        return VehicleEta(pattern_id="P", stop_id=stop, stop_order=order, vehicle_id=vehicle, run_key=None,
                          predicted_arrival_at=self.AT + timedelta(seconds=seconds), eta_source="provider",
                          model_version=None, horizon_seconds=seconds, basis_at=self.AT, source_generated_at=None,
                          collected_at=self.AT, stops_away=stops_away, basis_source="daejeon_info_offer_tm")

    def resolve(self, board, *alights):
        table = EtaTable(by_position={("P", 15, "B"): tuple(alights)}, rejected_by_stop={}, rejected_counts={})
        return resolve_alight_eta(self.RIDE, board, table, {}, self.POLICY)

    def test_links_same_vehicle_with_consistent_progress(self):
        board = self.eta("A", 10, 120, stops_away=2)
        alight = self.eta("B", 15, 480, stops_away=7)
        found, reasons = self.resolve(board, alight)
        self.assertEqual(found, alight)
        self.assertEqual(reasons, (ALIGHT_LINKED_WITHOUT_RUN_KEY,), "검증 전 연결임을 표시")

    def test_rejects_other_vehicle_progress_and_order_mismatch(self):
        board = self.eta("A", 10, 120, stops_away=2)
        self.assertIn("alight_eta_vehicle_absent", self.resolve(board, self.eta("B", 15, 480, "대전75자9999", 7))[1])
        self.assertIn("alight_eta_progress_mismatch", self.resolve(board, self.eta("B", 15, 480, stops_away=1))[1],
                      "하차 정류소가 승차 정류소보다 가깝게 나오면(진행 방향 불일치) 잇지 않음")
        linked = self.eta("B", 15, 480, stops_away=6)
        self.assertEqual(self.resolve(board, linked)[0], linked,
                         "대전시 정류장 수 차이(4)가 경유 순번 차이(5)와 달라도 방향이 맞으면 같은 운행")
        self.assertIn("alight_eta_order_inconsistent", self.resolve(board, self.eta("B", 15, 60, stops_away=7))[1])
        self.assertIn("vehicle_run_unconfirmed", self.resolve(self.eta("A", 10, 120, vehicle=None),
                                                              self.eta("B", 15, 480))[1])


class LeadingVehicleGapTest(unittest.TestCase):
    """Phase 3B: 하차 정류소 D(15)에 앞차 A만 나올 때 R_B(D) = R_A(D) + R_B(X), X(12)는 A가 마지막으로 통과한 위치."""

    AT, RIDE, POLICY, eta = (SameVehicleAlightTest.AT, SameVehicleAlightTest.RIDE, SameVehicleAlightTest.POLICY,
                             SameVehicleAlightTest.eta)
    B, A = "대전75자0001", "대전75자0002"
    ANCHOR = GapAnchor(pattern_id="P", target_stop_id="B", target_stop_order=15, trailing_vehicle_id=B,
                       leading_vehicle_id=A, anchor_stop_id="X", anchor_stop_order=12, source_ref="pos")

    def gap(self, board, at_target, at_anchor, anchor=ANCHOR, after_seconds=0):
        table = EtaTable(by_position={("P", 15, "B"): tuple(at_target), ("P", 12, "X"): tuple(at_anchor)},
                         rejected_by_stop={}, rejected_counts={})
        anchors = {("P", 15, self.B): anchor} if anchor else None
        return resolve_alight_eta(self.RIDE, board, table, {}, self.POLICY, anchors,
                                  self.AT + timedelta(seconds=after_seconds))

    def test_adds_leading_remaining_to_trailing_remaining_at_anchor(self):
        board = self.eta("A", 10, 120, stops_away=2)
        found, reasons = self.gap(board, [self.eta("B", 15, 480, self.A, 3)], [self.eta("X", 12, 200, stops_away=4)])
        self.assertEqual(found.predicted_arrival_at, self.AT + timedelta(seconds=680), "8분 + 3분20초, 경과분 0")
        self.assertEqual((found.eta_source, found.vehicle_id, found.horizon_seconds), ("leading_vehicle_gap", self.B, 680))
        self.assertIn("passage-uncorrected", found.model_version)
        self.assertIn(ALIGHT_ETA_BY_LEADING_VEHICLE_GAP, reasons)
        self.assertIn("alight_eta_vehicle_absent", reasons, "사용하지 못한 직접 ETA 사유도 남김")

    def test_limits_when_trailing_vehicle_or_progress_or_order_unconfirmed(self):
        board = self.eta("A", 10, 120, stops_away=2)
        lead = [self.eta("B", 15, 480, self.A, 3)]
        self.assertIn("leading_gap_trailing_eta_absent",
                      self.gap(board, lead, [self.eta("X", 12, 200, "대전75자9999", 4)])[1], "X의 다음 차량이 B가 아님")
        self.assertIn("leading_gap_progress_mismatch", self.gap(board, lead, [self.eta("X", 12, 200, stops_away=7)])[1])
        confirmed = self.ANCHOR.model_copy(update={"order_confirmed": True})
        self.assertEqual(self.gap(board, lead, [self.eta("X", 12, 200, stops_away=7)], anchor=confirmed)[0]
                         .predicted_arrival_at, self.AT + timedelta(seconds=680),
                         "노선 위치로 순서를 확인했으면 정류장 수 차이는 방향만 맞으면 됨")
        self.assertIn("leading_gap_progress_mismatch",
                      self.gap(board, lead, [self.eta("X", 12, 200, stops_away=1)], anchor=confirmed)[1])
        self.assertIn("leading_gap_leading_eta_absent", self.gap(board, [], [self.eta("X", 12, 200, stops_away=4)])[1])
        self.assertIn("leading_gap_eta_before_evaluation",
                      self.gap(board, [self.eta("B", 15, 240, self.A, 3)], [self.eta("X", 12, 400, stops_away=4)],
                               after_seconds=300)[1], "평가 시각에 A의 예상 도착이 이미 지남")
        found, reasons = self.gap(board, lead, [self.eta("X", 12, 200, stops_away=4)], anchor=None)
        self.assertIsNone(found)
        self.assertFalse(any(r.startswith("leading_gap") for r in reasons), "차량 조합이 없으면 기존 결과 그대로")

    def test_chains_through_vehicle_between_leading_and_trailing(self):
        v = "대전75자0003"
        first = GapAnchor(pattern_id="P", target_stop_id="B", target_stop_order=15, trailing_vehicle_id=v,
                          leading_vehicle_id=self.A, anchor_stop_id="X13", anchor_stop_order=13, order_confirmed=True)
        second = first.model_copy(update={"trailing_vehicle_id": self.B, "leading_vehicle_id": v,
                                          "anchor_stop_id": "X11", "anchor_stop_order": 11})
        table = EtaTable(by_position={("P", 15, "B"): (self.eta("B", 15, 480, self.A, 2),),
                                      ("P", 13, "X13"): (self.eta("X13", 13, 100, v, 1),),
                                      ("P", 11, "X11"): (self.eta("X11", 11, 150, self.B, 2),)},
                         rejected_by_stop={}, rejected_counts={})
        anchors = {(a.pattern_id, a.target_stop_order, a.trailing_vehicle_id): a for a in (first, second)}
        found, reasons = resolve_alight_eta(self.RIDE, self.eta("A", 10, 120, stops_away=1), table, {}, self.POLICY,
                                            anchors, self.AT)
        self.assertEqual(found.predicted_arrival_at, self.AT + timedelta(seconds=730), "A 8분 + V의 X13 100초 + B의 X11 150초")
        self.assertIn(ALIGHT_ETA_BY_LEADING_VEHICLE_GAP, reasons)


class FollowingBoardGapTest(unittest.TestCase):
    """Phase 3B 승차 정류소 적용: 승차 D(10)에 선두 A만 나올 때 뒤 차량을 한 대씩 잇는다.

    B1 = R_A(D) + R_B1(X1), X1(7)은 A 위치. B2 = R_B1(D) + R_B2(X2), X2(4)는 B1 위치. 하차는 앞 차량 하차 + 같은 간격.
    """

    AT, RIDE, POLICY, eta = (SameVehicleAlightTest.AT, SameVehicleAlightTest.RIDE, SameVehicleAlightTest.POLICY,
                             SameVehicleAlightTest.eta)
    A, B1, B2 = "대전75자0002", "대전75자0001", "대전75자0003"

    def legs(self, at_x1, at_x2=(), *, lead_seconds=120, confirmed=False, after_seconds=0):
        first = GapAnchor(pattern_id="P", target_stop_id="A", target_stop_order=10, trailing_vehicle_id=self.B1,
                          leading_vehicle_id=self.A, anchor_stop_id="X1", anchor_stop_order=7, source_ref="pos",
                          order_confirmed=confirmed)
        second = first.model_copy(update={"trailing_vehicle_id": self.B2, "leading_vehicle_id": self.B1,
                                          "anchor_stop_id": "X2", "anchor_stop_order": 4})
        table = EtaTable(by_position={("P", 10, "A"): (self.eta("A", 10, lead_seconds, self.A, 3),),
                                      ("P", 15, "B"): (self.eta("B", 15, 480, self.A, 8),),
                                      ("P", 7, "X1"): tuple(at_x1), ("P", 4, "X2"): tuple(at_x2)},
                         rejected_by_stop={}, rejected_counts={})
        anchors = {(a.pattern_id, a.target_stop_order, a.trailing_vehicle_id): a for a in (first, second)}
        return build_ride_legs(self.RIDE, table, {}, start_at=self.AT, queried_stop_ids=frozenset({"A", "B"}),
                               policy=self.POLICY, anchors=anchors,
                               evaluated_at=self.AT + timedelta(seconds=after_seconds))

    def test_chains_following_vehicles_and_their_alight(self):
        legs = self.legs([self.eta("X1", 7, 200, self.B1, 2)], [self.eta("X2", 4, 150, self.B2, 1)])
        self.assertEqual([leg.vehicle_id for leg in legs.legs], [self.A, self.B1, self.B2], "선두 옵션은 그대로")
        b1, b2 = legs.legs[1], legs.legs[2]
        self.assertEqual((b1.vehicle_rank, b1.board_eta.eta_source, b1.board_eta.stops_away),
                         (1, "leading_vehicle_gap", 5))
        self.assertEqual([leg.board_eta.predicted_arrival_at for leg in legs.legs],
                         [self.AT + timedelta(seconds=s) for s in (120, 320, 470)], "2분+3분20초, 그 뒤 +2분30초")
        self.assertEqual([leg.alight_eta.predicted_arrival_at for leg in legs.legs],
                         [self.AT + timedelta(seconds=s) for s in (480, 680, 830)], "앞 차량 하차 + 같은 간격")
        self.assertTrue({BOARD_ETA_BY_LEADING_VEHICLE_GAP, ALIGHT_ETA_BY_LEADING_VEHICLE_GAP} <= set(b2.reason_codes))

    def test_stops_chain_when_anchor_vehicle_differs(self):
        legs = self.legs([self.eta("X1", 7, 200, "대전75자9999", 2)], [self.eta("X2", 4, 150, self.B2, 1)])
        self.assertEqual([leg.vehicle_id for leg in legs.legs], [self.A])
        self.assertTrue({"leading_gap_trailing_eta_absent", "leading_gap_leading_eta_absent"} <= set(legs.reason_codes),
                        "B1을 못 만들면 B1 기준인 B2도 만들지 않음")

    def test_confirmed_order_treats_past_prediction_as_arriving_now(self):
        at_x1 = [self.eta("X1", 7, 200, self.B1, 2)]
        legs = self.legs(at_x1, lead_seconds=30, confirmed=True, after_seconds=60)
        self.assertEqual(legs.legs[1].board_eta.predicted_arrival_at, self.AT + timedelta(seconds=200),
                         "위치로 A가 D를 지나지 않음을 확인하면 R_A(D)=0, 평가 60초 + B1의 X까지 140초")
        legs = self.legs(at_x1, lead_seconds=30, after_seconds=60)
        self.assertEqual([leg.vehicle_id for leg in legs.legs], [self.A])
        self.assertIn("leading_gap_eta_before_evaluation", legs.reason_codes, "위치 확인이 없으면 지난 예측은 쓰지 않음")


class LeadingGapAnchorTest(unittest.TestCase):
    """하차 ETA가 없는 차량만, 노선 위치로 B 위치 < X(A 위치) < D를 확인해 기준점을 고른다."""

    AT, RIDE, POLICY = SameVehicleAlightTest.AT, SameVehicleAlightTest.RIDE, SameVehicleAlightTest.POLICY

    def report(self, stop, order, seconds, vehicle, stops_away):
        return EtaReport(pattern_id="P", stop_id=stop, stop_order=order, vehicle_id=vehicle, run_key=None,
                         remaining_seconds=seconds, basis_at=self.AT, collected_at=self.AT, stops_away=stops_away)

    def position(self, obs_id, seq, vehicle):
        return {"id": obs_id, "pattern_key": "P", "stop_seq": seq, "vehicle_no": vehicle, "stop_external_id": f"S{seq}"}

    def select(self, reports, positions, board_rides=None, previous_stops=None):
        realtime = flow.RealtimeInputs(reports, frozenset({"A", "B"}), {}, [], {}, positions)
        candidate = build_static_candidate([self.RIDE], transfer_meters=[], access_straight_line_meters=None,
                                           egress_straight_line_meters=None)
        context = SearchContext(origin=Coordinate(lat=36.3, lon=127.4), destination=Coordinate(lat=36.4, lon=127.4),
                                start_at=self.AT, evaluated_at=self.AT, static_snapshot_id="v")
        return flow.leading_gap_anchors([candidate], realtime, [], cohort="c", context=context, policy=self.POLICY,
                                        board_rides=board_rides, previous_stops=previous_stops)

    def anchors(self, b_seq, a_seq):
        return self.select([self.report("A", 10, 120, "대전75자0001", 2), self.report("B", 15, 480, "대전75자0002", 3)],
                        [self.position(1, a_seq, "대전75자0002"), self.position(2, b_seq, "대전75자0001")])

    def test_selects_last_passed_stop_of_leading_vehicle(self):
        [anchor] = self.anchors(b_seq=8, a_seq=12)
        self.assertEqual((anchor.trailing_vehicle_id, anchor.leading_vehicle_id, anchor.anchor_stop_id,
                          anchor.anchor_stop_order), ("대전75자0001", "대전75자0002", "S12", 12))
        self.assertEqual(self.anchors(b_seq=12, a_seq=12), [], "두 차량이 같은 정류장 사이면 쓰지 않음")
        self.assertEqual(self.anchors(b_seq=8, a_seq=15), [], "A가 이미 D에 있고 D 앞 정류소를 모르면 쓰지 않음")

    def test_alight_chain_passes_through_vehicle_between(self):
        a2, v, b = "대전75자0002", "대전75자0003", "대전75자0001"
        reports = [self.report("A", 10, 120, b, 2), self.report("B", 15, 480, a2, 2)]
        found = self.select(reports, [self.position(1, 13, a2), self.position(2, 11, v), self.position(3, 8, b)])
        self.assertEqual([(x.target_stop_order, x.trailing_vehicle_id, x.leading_vehicle_id, x.anchor_stop_id)
                          for x in found], [(15, b, v, "S11"), (15, v, a2, "S13")],
                         "하차 정류소 선두 A부터 사이 차량 V를 거쳐 B까지 앞 차량 위치를 X로 이음")

    def test_chains_vehicles_behind_leading_until_ready_time(self):
        a, b, c = "대전75자0002", "대전75자0001", "대전75자0003"
        reports = [self.report("A", 10, 120, a, 2), self.report("B", 15, 480, a, 7)]
        positions = [self.position(1, 8, a), self.position(2, 5, b), self.position(3, 3, c)]
        missed = {("P", 10): self.AT + timedelta(seconds=600)}

        def chain(found):
            return [(x.target_stop_order, x.trailing_vehicle_id, x.leading_vehicle_id, x.anchor_stop_id) for x in found]

        self.assertEqual(self.select(reports, positions), [], "선두 차량을 놓치지 않는 구간은 고르지 않음")
        self.assertEqual(chain(self.select(reports, positions, missed)), [(10, b, a, "S8"), (10, c, b, "S5")],
                         "X 조회 전에도 뒤 차량 여러 대의 기준점을 미리 골라 한 묶음으로 조회")
        at_x = [*reports, self.report("S8", 8, 200, b, 3)]
        self.assertEqual(chain(self.select(at_x, positions, missed)), [(10, b, a, "S8"), (10, c, b, "S5")],
                         "B 승차(320초)가 준비 시각(600초) 전이면 그 뒤 차량을 B 위치 기준으로 이음")
        self.assertEqual(chain(self.select(at_x, positions, {("P", 10): self.AT + timedelta(seconds=300)})),
                         [(10, b, a, "S8")], "준비 시각 뒤에 오는 차량이 나오면 멈춤")
        self.assertEqual(self.select(reports, [*positions, self.position(4, 8, "대전75자0004")], missed), [],
                         "선두 차량과 같은 위치에 다른 차량이 있으면 순서가 모호")

    def test_uses_stop_before_board_when_leading_vehicle_is_there(self):
        a, b = "대전75자0002", "대전75자0001"
        reports = [self.report("A", 10, 0, a, 0), self.report("B", 15, 480, a, 5)]
        positions = [self.position(1, 10, a), self.position(2, 5, b)]
        missed = {("P", 10): self.AT + timedelta(seconds=600)}
        self.assertEqual(self.select(reports, positions, missed), [], "앞 정류소를 모르면 쓰지 않음")
        [anchor] = self.select(reports, positions, missed, previous_stops={("P", 10): (9, "S9")})
        self.assertEqual((anchor.target_stop_order, anchor.trailing_vehicle_id, anchor.anchor_stop_id,
                          anchor.anchor_stop_order, anchor.order_confirmed), (10, b, "S9", 9, True))
        self.assertEqual(self.select(reports, [self.position(1, 10, a), self.position(2, 9, b)], missed,
                                     previous_stops={("P", 10): (9, "S9")}), [], "B가 이미 X에 있으면 X 다음 차량이 B가 아님")

    def test_probe_stop_when_no_vehicle_behind_and_alight_chain_for_last_passed_vehicle(self):
        a, v = "대전75자0002", "대전75자0003"
        missed = {("P", 10): self.AT + timedelta(seconds=600)}
        probes: set[str] = set()
        realtime = flow.RealtimeInputs([self.report("A", 10, 120, a, 2), self.report("B", 15, 480, a, 7)],
                                       frozenset({"A", "B"}), {}, [], {}, [self.position(1, 8, a)])
        candidate = build_static_candidate([self.RIDE], transfer_meters=[], access_straight_line_meters=None,
                                           egress_straight_line_meters=None)
        context = SearchContext(origin=Coordinate(lat=36.3, lon=127.4), destination=Coordinate(lat=36.4, lon=127.4),
                                start_at=self.AT, evaluated_at=self.AT, static_snapshot_id="v")
        rear: set[tuple[str, str]] = set()
        found = flow.leading_gap_anchors([candidate], realtime, [], cohort="c", context=context, policy=self.POLICY,
                                         board_rides=missed, probe_stops=probes, rear_vehicles=rear)
        self.assertEqual((found, probes, rear), ([], {"S8"}, {("P", a)}),
                         "뒤 차량이 없으면 맨 뒤 차량이 지난 정류소로 출발 전 여부를 확인")

        waiting = EtaReport(pattern_id="P", stop_id="A", stop_order=10, vehicle_id=None, run_key=None,
                            remaining_seconds=1700, basis_at=self.AT - timedelta(seconds=300), collected_at=self.AT,
                            not_departed=True, last_passed_vehicle_id=a)
        [anchor] = self.select([waiting, self.report("B", 15, 480, v, 2)],
                               [self.position(1, 13, v), self.position(2, 11, a)])
        self.assertEqual((anchor.target_stop_order, anchor.trailing_vehicle_id, anchor.leading_vehicle_id,
                          anchor.anchor_stop_id), (15, a, v, "S13"),
                         "승차 정류소가 출발 전이면 직전 차량(A)의 하차 정류소 시각을 앞차 근사로 구할 조합을 고름")

    def test_alight_chain_by_arrivals_keeps_adding_until_the_boarded_vehicle(self):
        """노선 위치 없이 도착정보만으로: D(15)의 다음 차 A1(1정류장 전) → S14의 다음 차 V(3정류장 전) → S11의 다음 차 B."""
        a1, v, b = "대전75자0002", "대전75자0003", "대전75자0001"
        stops = {("P", order): f"S{order}" for order in range(1, 20)} | {("P", 10): "A", ("P", 15): "B"}
        reports = [self.report("A", 10, 120, b, 4), self.report("B", 15, 51, a1, 1),
                   self.report("S14", 14, 400, v, 3), self.report("S11", 11, 900, b, 5)]
        candidate = build_static_candidate([self.RIDE], transfer_meters=[], access_straight_line_meters=None,
                                           egress_straight_line_meters=None)
        context = SearchContext(origin=Coordinate(lat=36.3, lon=127.4), destination=Coordinate(lat=36.4, lon=127.4),
                                start_at=self.AT, evaluated_at=self.AT, static_snapshot_id="v")

        def select(found_reports):
            probes: set[str] = set()
            realtime = flow.RealtimeInputs(found_reports, frozenset({"A", "B"}), {}, [], {}, [])
            found = flow.leading_gap_anchors([candidate], realtime, [], cohort="c", context=context,
                                             policy=self.POLICY, pattern_stops=stops, probe_stops=probes)
            return found, probes

        found, probes = select(reports)
        self.assertEqual([(x.trailing_vehicle_id, x.leading_vehicle_id, x.anchor_stop_id) for x in found],
                         [(b, v, "S11"), (v, a1, "S14")])
        self.assertEqual(probes, set())
        table = index_etas(reports, evaluated_at=self.AT, max_data_age_seconds=900)
        anchors = {(x.pattern_id, x.target_stop_order, x.trailing_vehicle_id): x for x in found}
        legs = build_ride_legs(self.RIDE, table, {}, start_at=self.AT, queried_stop_ids=frozenset({"A", "B"}),
                               policy=self.POLICY, anchors=anchors, evaluated_at=self.AT)
        self.assertEqual(legs.legs[0].alight_eta.predicted_arrival_at, self.AT + timedelta(seconds=51 + 400 + 900),
                         "앞차들이 직전 정류소까지 남은 시간을 계속 더함")

        found, probes = select(reports[:3])
        self.assertEqual(([x.trailing_vehicle_id for x in found], probes), ([v], {"S11"}),
                         "아직 조회하지 않은 직전 정류소는 조회 대상으로 돌려주고, 조회 뒤 이어 감")
        same = [*reports[:2], self.report("S14", 14, 20, a1, 0), self.report("S13", 13, 400, v, 2),
                self.report("S11", 11, 900, b, 5)]
        found, _ = select(same)
        self.assertEqual([(x.trailing_vehicle_id, x.leading_vehicle_id, x.anchor_stop_id) for x in found],
                         [(b, v, "S11"), (v, a1, "S13")],
                         "직전 정류소 응답에 앞차가 그대로 나오면(아직 안 지남) 한 정류소 더 뒤에서 다음 차를 찾음")


class HeadwayEstimateTest(unittest.TestCase):
    """아직 출발하지 않은 다음 차량: 직전 차량 시각 + k × 배차간격(예상, 순위는 실시간 후보 뒤)."""

    AT, RIDE, POLICY, eta = (SameVehicleAlightTest.AT, SameVehicleAlightTest.RIDE, SameVehicleAlightTest.POLICY,
                             SameVehicleAlightTest.eta)
    A = "대전75자0002"
    H = 1200.0

    def waiting(self, stop, order, passed_seconds_ago, vehicle=A, collected_seconds_ago=0):
        return EtaReport(pattern_id="P", stop_id=stop, stop_order=order, vehicle_id=None, run_key=None,
                         remaining_seconds=1700, basis_at=self.AT - timedelta(seconds=passed_seconds_ago),
                         collected_at=self.AT - timedelta(seconds=collected_seconds_ago), not_departed=True,
                         last_passed_vehicle_id=vehicle, source_ref="w")

    def report(self, stop, order, seconds, vehicle, stops_away=None):
        return EtaReport(pattern_id="P", stop_id=stop, stop_order=order, vehicle_id=vehicle, run_key=None,
                         remaining_seconds=seconds, basis_at=self.AT, collected_at=self.AT, stops_away=stops_away)

    def legs(self, reports, headway=H, after_seconds=0, rear=()):
        at = self.AT + timedelta(seconds=after_seconds)
        table = index_etas(reports, evaluated_at=at, max_data_age_seconds=self.POLICY.max_data_age_seconds)
        return build_ride_legs(self.RIDE, table, {}, start_at=self.AT, queried_stop_ids=frozenset({"A", "B"}),
                               policy=self.POLICY, anchors={}, evaluated_at=at, headway_seconds=headway,
                               rear_vehicle_ids=frozenset(rear))

    def test_not_departed_response_is_a_passage_record_not_an_eta(self):
        table = index_etas([self.waiting("A", 10, 1500)], evaluated_at=self.AT, max_data_age_seconds=900)
        self.assertEqual(table.by_position, {}, "남은 시간(약 28분 고정값)은 ETA로 쓰지 않음")
        mark = table.not_departed[("P", 10, "A")]
        self.assertEqual((mark.vehicle_id, mark.passed_at), (self.A, self.AT - timedelta(seconds=1500)),
                         "직전 차량이 지난 시각은 오래돼도 응답이 새로 받은 것이면 사용")
        stale = index_etas([self.waiting("A", 10, 1500, collected_seconds_ago=1000)], evaluated_at=self.AT,
                           max_data_age_seconds=900)
        self.assertEqual((stale.not_departed, stale.rejected_counts), ({}, {"eta_stale": 1}))

    def test_adds_headway_after_last_passed_vehicle(self):
        legs = self.legs([self.waiting("A", 10, 300), self.report("B", 15, 480, self.A, 3)])
        self.assertEqual([(leg.vehicle_id, leg.board_eta.eta_source) for leg in legs.legs],
                         [(None, "headway_estimate")] * 3)
        self.assertEqual([leg.board_eta.predicted_arrival_at for leg in legs.legs],
                         [self.AT + timedelta(seconds=s) for s in (900, 2100, 3300)], "5분 전 통과 + 20분씩")
        self.assertEqual([leg.alight_eta.predicted_arrival_at for leg in legs.legs],
                         [self.AT + timedelta(seconds=s) for s in (1680, 2880, 4080)], "직전 차량 하차 8분 뒤 + 20분씩")
        self.assertEqual(legs.legs[0].ride_seconds, 780, "탑승 시간은 직전 차량과 같음")
        self.assertEqual(set(legs.legs[0].reason_codes), {BOARD_ETA_BY_HEADWAY_ESTIMATE, ALIGHT_ETA_BY_HEADWAY_ESTIMATE})
        self.assertEqual(legs.reason_codes, (NEXT_VEHICLE_NOT_DEPARTED,))

    def test_skips_overdue_estimate_and_needs_headway(self):
        late = self.legs([self.waiting("A", 10, 1000), self.report("B", 15, 2000, self.A, 3)], after_seconds=300)
        self.assertEqual([leg.board_eta.predicted_arrival_at for leg in late.legs],
                         [self.AT + timedelta(seconds=s) for s in (1400, 2600)],
                         "첫 예상(200초)은 평가 시각(300초)보다 일러 만들지 않음")
        none = self.legs([self.waiting("A", 10, 300), self.report("B", 15, 480, self.A, 3)], headway=None)
        self.assertEqual((none.legs, set(none.reason_codes)), ((), {NEXT_VEHICLE_NOT_DEPARTED, "boarding_eta_absent"}))

    def test_after_last_approaching_vehicle_only_with_upstream_evidence(self):
        listed = [self.report("A", 10, 120, self.A, 2), self.report("B", 15, 480, self.A, 7)]
        with_evidence = self.legs([*listed, self.waiting("X", 7, 60)])
        self.assertEqual([(leg.vehicle_id, leg.board_eta.predicted_arrival_at, leg.alight_eta.predicted_arrival_at)
                          for leg in with_evidence.legs],
                         [(self.A, self.AT + timedelta(seconds=120), self.AT + timedelta(seconds=480)),
                          (None, self.AT + timedelta(seconds=1320), self.AT + timedelta(seconds=1680)),
                          (None, self.AT + timedelta(seconds=2520), self.AT + timedelta(seconds=2880))],
                         "오는 차량 뒤가 출발 전이면 그 차량 시각 + 배차간격(검색 범위 1시간 안)")
        origin = self.waiting("O", 1, 60, vehicle=None)
        for other in ([], [self.waiting("X", 7, 60, vehicle="대전75자9999")], [self.waiting("Z", 12, 60)], [origin]):
            self.assertEqual([leg.vehicle_id for leg in self.legs([*listed, *other]).legs], [self.A],
                             "상류 정류소가 '그 차량이 마지막, 다음 차 출발 전'이라고 할 때만 예상")
        self.assertEqual([leg.vehicle_id for leg in self.legs([*listed, origin], rear=[self.A]).legs],
                         [self.A, None, None],
                         "기점 응답은 직전 차량번호가 없어, 노선 위치로 맨 뒤 차량임을 확인했을 때만 근거로 봄")

    def recommend(self, reports):
        candidate = build_static_candidate([self.RIDE], transfer_meters=[], access_straight_line_meters=None,
                                           egress_straight_line_meters=None)
        coverage = SearchCoverage(truncated=False, truncation_reasons=(), completed_start_stops=("A",),
                                  partial_start_stop=None, unexplored_start_stops=(), expanded_states_by_level=(),
                                  completed_candidates_by_level=(), pruned_cycle_count=0, pruned_split_ride_count=0)
        result = CandidateSearchResult(static_snapshot_id="v", candidates=(candidate,), access_stops=(),
                                       egress_stops=(), reason_codes=(), coverage=coverage, index_issues=())
        context = SearchContext(origin=Coordinate(lat=36.3, lon=127.4), destination=Coordinate(lat=36.4, lon=127.4),
                                start_at=self.AT, evaluated_at=self.AT, static_snapshot_id="v")
        inputs = EvaluationInputs(
            data_snapshot_id="s", sample_condition_key="c",
            walks=(WalkResult(from_ref="@origin", to_ref="A", status="available", duration_seconds=120),
                   WalkResult(from_ref="B", to_ref="@destination", status="available", duration_seconds=60)),
            eta_reports=tuple(reports), queried_stop_ids=frozenset({"A", "B"}),
            route_headways=(RouteHeadway(route_id="R", headway_seconds=self.H),))
        evaluation = evaluate_candidates(result, context, inputs, self.POLICY)
        return evaluation, recommend_candidates(evaluation)

    def test_fastest_estimate_per_route_is_ranked(self):
        evaluation, recommendation = self.recommend([self.waiting("A", 10, 300), self.report("B", 15, 480, self.A, 3)])
        self.assertEqual(sorted(item.nominal_total_seconds for item in evaluation.candidates), [1740, 2940, 4140],
                         "예상 차량으로 전체 시간이 채워짐(하차 1680초 + 도보 60초)")
        self.assertEqual([(item.rank, item.time_seconds) for item in recommendation.ranked], [(1, 1740)],
                         "같은 경로의 예상 조합은 가장 빠른 하나만 순위")
        self.assertIn("ranked_with_headway_estimate", recommendation.ranked[0].reason_codes)
        self.assertEqual(recommendation.mode_reason_codes, ("headway_estimates_only",))
        self.assertEqual([("later_headway_estimate_not_ranked" in item.reason_codes) for item in recommendation.excluded],
                         [True, True])

    def test_realtime_candidates_rank_before_estimates(self):
        _, recommendation = self.recommend([self.report("A", 10, 300, self.A, 2), self.report("B", 15, 480, self.A, 7),
                                            self.waiting("X", 7, 60)])
        self.assertEqual([(item.rank, item.time_seconds, "ranked_with_headway_estimate" in item.reason_codes)
                          for item in recommendation.ranked], [(1, 540, False), (2, 1740, True)],
                         "실시간 예측으로 계산된 조합이 먼저, 그 차 뒤 예상 조합은 그다음 순위")


class HeadwayValueTest(unittest.TestCase):
    def info(self, **values):
        return {"fields": {name: {"state": "present", "value": value} for name, value in values.items()}}

    def test_picks_day_type_and_service_hours_in_kst(self):
        info = self.info(intervaltime=20, intervalsattime=24, intervalsuntime=26, startvehicletime="0545",
                         endvehicletime=2230)
        thursday = datetime(2026, 11, 12, 8, 0, tzinfo=UTC)  # 한국 시각 목요일 17:00
        self.assertEqual(flow.applicable_headway_seconds(info, thursday), 1200)
        self.assertEqual(flow.applicable_headway_seconds(info, thursday + timedelta(days=2)), 1440, "토요일")
        self.assertEqual(flow.applicable_headway_seconds(info, thursday + timedelta(days=3)), 1560, "일요일")
        self.assertIsNone(flow.applicable_headway_seconds(info, datetime(2026, 11, 12, 15, 30, tzinfo=UTC)),
                          "한국 시각 00:30은 첫차 전")
        self.assertIsNone(flow.applicable_headway_seconds(info, datetime(2026, 11, 12, 14, 0, tzinfo=UTC)),
                          "한국 시각 23:00은 막차 뒤")
        self.assertIsNone(flow.applicable_headway_seconds(info, datetime(2026, 11, 11, 20, 0, tzinfo=UTC)),
                          "한국 시각 05:00은 첫차 전")
        self.assertIsNone(flow.applicable_headway_seconds(self.info(intervaltime=0), thursday))
        self.assertIsNone(flow.applicable_headway_seconds(None, thursday))


if __name__ == "__main__":
    unittest.main()
