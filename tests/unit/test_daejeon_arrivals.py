"""대전시 도착정보 수집 해석·대상 키·같은 차량 하차 ETA 연결 단위 시험(DB·네트워크 없음).

XML_OK는 2026-11-09 사용자가 확인한 실응답(도마네거리, BusStopID 8001488)의 형식이다.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app.integrations.daejeon_bus_api import RawResponse, parse_arrivals
from app.modules.evaluation.eta import (
    ALIGHT_ETA_BY_LEADING_VEHICLE_GAP,
    ALIGHT_LINKED_WITHOUT_RUN_KEY,
    BOARD_ETA_BY_LEADING_VEHICLE_GAP,
    EtaTable,
    build_ride_legs,
    resolve_alight_eta,
)
from app.modules.evaluation.schemas import EtaReport, EvaluationPolicy, GapAnchor, VehicleEta
from app.modules.routing.schemas import Coordinate, SearchContext, StaticRide
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
        self.assertIn("alight_eta_progress_mismatch", self.resolve(board, self.eta("B", 15, 480, stops_away=4))[1],
                      "앞서 가는 같은 번호 차량의 다음 회차 등 진행 위치가 맞지 않으면 잇지 않음")
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
        self.assertIn("leading_gap_leading_eta_absent", self.gap(board, [], [self.eta("X", 12, 200, stops_away=4)])[1])
        self.assertIn("leading_gap_eta_before_evaluation",
                      self.gap(board, [self.eta("B", 15, 240, self.A, 3)], [self.eta("X", 12, 400, stops_away=4)],
                               after_seconds=300)[1], "평가 시각에 A의 예상 도착이 이미 지남")
        found, reasons = self.gap(board, lead, [self.eta("X", 12, 200, stops_away=4)], anchor=None)
        self.assertIsNone(found)
        self.assertFalse(any(r.startswith("leading_gap") for r in reasons), "차량 조합이 없으면 기존 결과 그대로")


class FollowingBoardGapTest(unittest.TestCase):
    """Phase 3B 승차 정류소 적용: 승차 D(10)에 선두 A만 나올 때 뒤 차량을 한 대씩 잇는다.

    B1 = R_A(D) + R_B1(X1), X1(7)은 A 위치. B2 = R_B1(D) + R_B2(X2), X2(4)는 B1 위치. 하차는 앞 차량 하차 + 같은 간격.
    """

    AT, RIDE, POLICY, eta = (SameVehicleAlightTest.AT, SameVehicleAlightTest.RIDE, SameVehicleAlightTest.POLICY,
                             SameVehicleAlightTest.eta)
    A, B1, B2 = "대전75자0002", "대전75자0001", "대전75자0003"

    def legs(self, at_x1, at_x2=()):
        first = GapAnchor(pattern_id="P", target_stop_id="A", target_stop_order=10, trailing_vehicle_id=self.B1,
                          leading_vehicle_id=self.A, anchor_stop_id="X1", anchor_stop_order=7, source_ref="pos")
        second = first.model_copy(update={"trailing_vehicle_id": self.B2, "leading_vehicle_id": self.B1,
                                          "anchor_stop_id": "X2", "anchor_stop_order": 4})
        table = EtaTable(by_position={("P", 10, "A"): (self.eta("A", 10, 120, self.A, 3),),
                                      ("P", 15, "B"): (self.eta("B", 15, 480, self.A, 8),),
                                      ("P", 7, "X1"): tuple(at_x1), ("P", 4, "X2"): tuple(at_x2)},
                         rejected_by_stop={}, rejected_counts={})
        anchors = {(a.pattern_id, a.target_stop_order, a.trailing_vehicle_id): a for a in (first, second)}
        return build_ride_legs(self.RIDE, table, {}, start_at=self.AT, queried_stop_ids=frozenset({"A", "B"}),
                               policy=self.POLICY, anchors=anchors, evaluated_at=self.AT)

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


class LeadingGapAnchorTest(unittest.TestCase):
    """하차 ETA가 없는 차량만, 노선 위치로 B 위치 < X(A 위치) < D를 확인해 기준점을 고른다."""

    AT, RIDE, POLICY = SameVehicleAlightTest.AT, SameVehicleAlightTest.RIDE, SameVehicleAlightTest.POLICY

    def report(self, stop, order, seconds, vehicle, stops_away):
        return EtaReport(pattern_id="P", stop_id=stop, stop_order=order, vehicle_id=vehicle, run_key=None,
                         remaining_seconds=seconds, basis_at=self.AT, collected_at=self.AT, stops_away=stops_away)

    def position(self, obs_id, seq, vehicle):
        return {"id": obs_id, "pattern_key": "P", "stop_seq": seq, "vehicle_no": vehicle, "stop_external_id": f"S{seq}"}

    def select(self, reports, positions, board_rides=None):
        realtime = flow.RealtimeInputs(reports, frozenset({"A", "B"}), {}, [], {}, positions)
        candidate = build_static_candidate([self.RIDE], transfer_meters=[], access_straight_line_meters=None,
                                           egress_straight_line_meters=None)
        context = SearchContext(origin=Coordinate(lat=36.3, lon=127.4), destination=Coordinate(lat=36.4, lon=127.4),
                                start_at=self.AT, evaluated_at=self.AT, static_snapshot_id="v")
        return flow.leading_gap_anchors([candidate], realtime, [], cohort="c", context=context, policy=self.POLICY,
                                        board_rides=board_rides)

    def anchors(self, b_seq, a_seq):
        return self.select([self.report("A", 10, 120, "대전75자0001", 2), self.report("B", 15, 480, "대전75자0002", 3)],
                        [self.position(1, a_seq, "대전75자0002"), self.position(2, b_seq, "대전75자0001")])

    def test_selects_last_passed_stop_of_leading_vehicle(self):
        [anchor] = self.anchors(b_seq=8, a_seq=12)
        self.assertEqual((anchor.trailing_vehicle_id, anchor.leading_vehicle_id, anchor.anchor_stop_id,
                          anchor.anchor_stop_order), ("대전75자0001", "대전75자0002", "S12", 12))
        self.assertEqual(self.anchors(b_seq=12, a_seq=12), [], "두 차량이 같은 정류장 사이면 쓰지 않음")
        self.assertEqual(self.anchors(b_seq=8, a_seq=15), [], "A가 이미 D에 있으면 X–D 구간이 없음")

    def test_chains_vehicles_behind_leading_until_ready_time(self):
        a, b, c = "대전75자0002", "대전75자0001", "대전75자0003"
        reports = [self.report("A", 10, 120, a, 2), self.report("B", 15, 480, a, 7)]
        positions = [self.position(1, 8, a), self.position(2, 5, b), self.position(3, 3, c)]
        missed = {("P", 10): self.AT + timedelta(seconds=600)}

        def chain(found):
            return [(x.target_stop_order, x.trailing_vehicle_id, x.leading_vehicle_id, x.anchor_stop_id) for x in found]

        self.assertEqual(self.select(reports, positions), [], "선두 차량을 놓치지 않는 구간은 고르지 않음")
        self.assertEqual(chain(self.select(reports, positions, missed)), [(10, b, a, "S8")],
                         "X(S8) 조회 전에는 바로 뒤 차량까지만")
        at_x = [*reports, self.report("S8", 8, 200, b, 3)]
        self.assertEqual(chain(self.select(at_x, positions, missed)), [(10, b, a, "S8"), (10, c, b, "S5")],
                         "B 승차(320초)가 준비 시각(600초) 전이면 그 뒤 차량을 B 위치 기준으로 이음")
        self.assertEqual(chain(self.select(at_x, positions, {("P", 10): self.AT + timedelta(seconds=300)})),
                         [(10, b, a, "S8")], "준비 시각 뒤에 오는 차량이 나오면 멈춤")
        self.assertEqual(self.select(reports, [*positions, self.position(4, 8, "대전75자0004")], missed), [],
                         "선두 차량과 같은 위치에 다른 차량이 있으면 순서가 모호")


if __name__ == "__main__":
    unittest.main()
