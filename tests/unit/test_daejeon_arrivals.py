"""대전시 도착정보 수집 해석·대상 키·같은 차량 하차 ETA 연결 단위 시험(DB·네트워크 없음).

XML_OK는 2026-11-09 사용자가 확인한 실응답(도마네거리, BusStopID 8001488)의 형식이다.
"""

import unittest
from datetime import datetime, timedelta, timezone

from app.integrations.daejeon_bus_api import RawResponse, parse_arrivals
from app.modules.evaluation import eta as eta_module
from app.modules.evaluation.eta import ALIGHT_LINKED_WITHOUT_RUN_KEY, EtaTable, resolve_alight_eta
from app.modules.evaluation.schemas import EtaReport, EvaluationPolicy, VehicleEta
from app.modules.routing.schemas import StaticRide
from app.modules.transit import service as transit_service
from app.modules.transit.schemas import RealtimeTarget

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


class ProviderSegmentChainTest(unittest.TestCase):
    """하차 정류소에 승차 차량이 없을 때: 인접 정류소 쌍마다 같은 운행 차량의 ETA 차이를 이어 붙인다."""

    AT = SameVehicleAlightTest.AT
    POLICY = SameVehicleAlightTest.POLICY
    eta = SameVehicleAlightTest.eta
    RIDE = StaticRide(route_id="R", pattern_id="P", direction=None, board_stop_id="S10", board_position=10,
                      alight_stop_id="S13", alight_position=13)

    def table(self, *etas):
        by_position: dict = {}
        for e in etas:
            by_position.setdefault(("P", e.stop_order, e.stop_id), []).append(e)
        return EtaTable(by_position={k: tuple(v) for k, v in by_position.items()}, rejected_by_stop={},
                        rejected_counts={})

    def chain_world(self):
        """승차 차량 V1은 10·11에만, 앞 차량 V2는 11·12·13에 나온다(하차 쪽 정류소에는 가까운 V2만)."""
        board = self.eta("S10", 10, 100, "V1", stops_away=1)
        return board, [board, self.eta("S11", 11, 160, "V1", stops_away=2),
                       self.eta("S11", 11, 50, "V2", stops_away=1), self.eta("S12", 12, 130, "V2", stops_away=2),
                       self.eta("S13", 13, 210, "V2", stops_away=3)]

    def test_chains_segments_when_board_vehicle_absent_at_alight(self):
        board, etas = self.chain_world()
        # 10→11: V1 60초, 11→12: V2 80초, 12→13: V2 80초.
        found, reasons = resolve_alight_eta(self.RIDE, board, self.table(*etas), {}, self.POLICY)
        self.assertIsNotNone(found)
        self.assertEqual(found.predicted_arrival_at, board.predicted_arrival_at + timedelta(seconds=60 + 80 + 80))
        self.assertEqual((found.eta_source, found.vehicle_id, found.segment_sample_counts),
                         ("provider_segments", "V1", (1, 1, 1)))
        self.assertIn("alight_eta_by_provider_segments", reasons)
        self.assertIn("alight_eta_vehicle_absent", reasons, "같은 차량 연결 실패 이유는 남김")

    def test_missing_pair_or_inconsistent_progress_leaves_alight_unknown(self):
        board, etas = self.chain_world()
        without_pair = [e for e in etas if not (e.vehicle_id == "V2" and e.stop_order == 11)]
        found, reasons = resolve_alight_eta(self.RIDE, board, self.table(*without_pair), {}, self.POLICY)
        self.assertIsNone(found, "11→12 구간 근거가 없으면 합을 완성하지 않음")
        self.assertIn("provider_segment_missing", reasons)
        jumped = [e if e.stop_order != 13 else self.eta("S13", 13, 210, "V2", stops_away=5) for e in etas]
        self.assertIsNone(resolve_alight_eta(self.RIDE, board, self.table(*jumped), {}, self.POLICY)[0],
                          "남은 정류장 수 차이가 1이 아니면 같은 운행으로 보지 않음")

    def test_earlier_snapshot_fills_gap_where_another_bus_is_inside(self):
        """지금은 V2가 12와 13 사이에 있어 12(V1)·13(V2)이 다른 차량이다. 이전 스냅샷의 V2 쌍으로 12→13을 채운다."""
        board = self.eta("S10", 10, 100, "V1", stops_away=1)
        latest = [board, self.eta("S11", 11, 160, "V1", stops_away=2), self.eta("S12", 12, 240, "V1", stops_away=3),
                  self.eta("S13", 13, 30, "V2", stops_away=1)]
        table = self.table(*latest)
        self.assertIsNone(resolve_alight_eta(self.RIDE, board, table, {}, self.POLICY)[0], "최신 스냅샷만으로는 공백")
        earlier = [self.eta("S12", 12, 10, "V2", stops_away=1), self.eta("S13", 13, 100, "V2", stops_away=2),
                   self.eta("S13", 13, 400, "V9", stops_away=7)]  # V9: S12 쌍이 없어 쓰지 않음
        recent: dict = {}
        for e in [*latest, *earlier]:
            recent.setdefault(("P", e.stop_order), []).append(e)
        found, reasons = resolve_alight_eta(self.RIDE, board, table, {}, self.POLICY,
                                            {k: tuple(v) for k, v in recent.items()})
        self.assertEqual(found.predicted_arrival_at, board.predicted_arrival_at + timedelta(seconds=60 + 80 + 90))
        self.assertIn("alight_eta_by_provider_segments", reasons)

    def test_index_segment_etas_applies_age_rule(self):
        def report(stop, order, basis_age, vehicle="V1"):
            return EtaReport(pattern_id="P", stop_id=stop, stop_order=order, vehicle_id=vehicle, run_key=None,
                             remaining_seconds=60, basis_at=self.AT - timedelta(seconds=basis_age),
                             collected_at=self.AT, stops_away=1)
        index = eta_module.index_segment_etas([report("S1", 1, 100), report("S1", 1, 1000, "V2"),
                                               report("S2", None, 10)], evaluated_at=self.AT,
                                              max_data_age_seconds=900)
        self.assertEqual(list(index), [("P", 1)], "경유 위치 미확인 응답 제외")
        self.assertEqual([e.vehicle_id for e in index[("P", 1)]], ["V1"], "허용 나이를 넘은 응답 제외")

    def test_same_vehicle_alight_eta_still_preferred(self):
        board, etas = self.chain_world()
        direct = self.eta("S13", 13, 400, "V1", stops_away=4)
        found, _ = resolve_alight_eta(self.RIDE, board, self.table(*etas, direct), {}, self.POLICY)
        self.assertEqual((found.eta_source, found.predicted_arrival_at), ("provider", direct.predicted_arrival_at))


if __name__ == "__main__":
    unittest.main()
