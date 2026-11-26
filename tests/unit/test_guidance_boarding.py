"""이동 안내의 다음 승차 판단 단위 시험(DB·네트워크 없음).

도보 기준 시각, 이미 지난 예측 제외, 탈 수 있는 차량이 없을 때의 대체(계획 차량·배차간격 예상), 놓친 차량 기록,
재계산 직전 도착정보 새로 받기 대상을 확인한다. 값은 모두 합성이다.
"""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from app.core.config import Settings
from app.modules.evaluation.eta import index_etas
from app.modules.evaluation.schemas import EtaReport
from app.modules.journeys import state
from app.modules.routing.schemas import StaticRide
from app.workers import recalculation
from app.workflows import guidance

UTC = timezone.utc
NOW = datetime(2026, 11, 26, 5, 6, tzinfo=UTC)
PATTERN, BOARD, ALIGHT = "P613", ("S-board", 5), ("S-alight", 12)
RIDE = StaticRide(route_id="R613", pattern_id=PATTERN, direction=None, board_stop_id=BOARD[0],
                  board_position=BOARD[1], alight_stop_id=ALIGHT[0], alight_position=ALIGHT[1])


def minutes(value: float) -> timedelta:
    return timedelta(minutes=value)


def report(vehicle, stop, arrives_at, *, basis_at=None, **extra) -> EtaReport:
    basis = basis_at or NOW - timedelta(seconds=20)
    return EtaReport(pattern_id=PATTERN, stop_id=stop[0], stop_order=stop[1], vehicle_id=vehicle, run_key=None,
                     remaining_seconds=(arrives_at - basis).total_seconds(), basis_at=basis, collected_at=basis,
                     **extra)


def table(*reports):
    return index_etas(reports, evaluated_at=NOW, max_data_age_seconds=900)


def planned_bus_leg(vehicle, board_at, alight_at):
    """선택 스냅샷의 버스 구간(검색 시점 계획 차량의 승하차 예상)."""
    basis = NOW - minutes(3)
    etas = index_etas([report(vehicle, BOARD, board_at, basis_at=basis), report(vehicle, ALIGHT, alight_at, basis_at=basis)],
                      evaluated_at=NOW, max_data_age_seconds=900)
    board = etas.by_position[(PATTERN, BOARD[1], BOARD[0])][0]
    alight = etas.by_position[(PATTERN, ALIGHT[1], ALIGHT[0])][0]
    return {"leg_index": 1, "kind": "bus", "route_id": "R613", "pattern_key": PATTERN,
            "vehicle": {"vehicle_id": vehicle, "run_key": None},
            "board_eta": board.model_dump(mode="json"), "alight_eta": alight.model_dump(mode="json"),
            "ride_seconds": (alight_at - board_at).total_seconds()}


class BoardingStartTest(unittest.TestCase):
    """준비 시각 = 기준 시각 + 도보 600초 + 준비 60초."""

    def ready(self, started, movement):
        return guidance.boarding_start_at(started, movement, 600.0, 60.0, NOW) + timedelta(seconds=660)

    def test_walk_counts_from_when_it_started_not_from_now(self):
        started = NOW - minutes(4)
        self.assertEqual(self.ready(started, state.WALKING), started + timedelta(seconds=660),
                         "재계산할 때마다 도보 10분을 지금부터 다시 더하지 않는다")

    def test_ready_is_now_once_the_walk_time_has_passed_or_user_waits_at_stop(self):
        self.assertEqual(self.ready(NOW - minutes(20), state.TRANSFERRING), NOW)
        self.assertEqual(self.ready(NOW - minutes(1), state.WAITING), NOW, "버스를 놓친 뒤에는 정류소에 있다")

    def test_unknown_start_or_walk_time_falls_back_to_now(self):
        self.assertEqual(guidance.boarding_start_at(None, state.WALKING, 600.0, 60.0, NOW), NOW)
        self.assertEqual(guidance.boarding_start_at(NOW - minutes(4), state.WALKING, None, 60.0, NOW), NOW)
        self.assertEqual(guidance.boarding_start_at(NOW + minutes(1), state.WALKING, 600.0, 60.0, NOW), NOW,
                         "기준 시각을 미래로 두지 않는다")

    def test_walk_starts_at_journey_start_or_last_alight(self):
        journey = {"started_at": NOW - minutes(9)}
        self.assertEqual(guidance.walk_started_at(journey, {"completed_bus_legs": []}), NOW - minutes(9))
        alighted = {"completed_bus_legs": [1], "last_alight": {"received_at": (NOW - minutes(1)).isoformat()}}
        self.assertEqual(guidance.walk_started_at(journey, alighted), NOW - minutes(1))
        switched = {**journey, "selected_snapshot": {"selected_at": (NOW - minutes(2)).isoformat()}}
        self.assertEqual(guidance.walk_started_at(switched, {"completed_bus_legs": []}), NOW - minutes(2),
                         "경로를 바꾼 뒤에는 바꾼 시각부터 새 정류소로 걷는다")


class UpcomingReportsTest(unittest.TestCase):
    def test_already_passed_predictions_are_dropped(self):
        stale = report("B1", BOARD, NOW - minutes(5), basis_at=NOW - minutes(8))
        coming = report("B2", BOARD, NOW + minutes(4))
        waiting = EtaReport(pattern_id=PATTERN, stop_id=BOARD[0], stop_order=BOARD[1], vehicle_id=None, run_key=None,
                            remaining_seconds=60.0, basis_at=NOW - minutes(12), collected_at=NOW, not_departed=True)
        unknown = EtaReport(pattern_id=PATTERN, stop_id=BOARD[0], stop_order=BOARD[1], vehicle_id="B3", run_key=None,
                            remaining_seconds=60.0, basis_at=None, collected_at=NOW)
        self.assertEqual(guidance.upcoming_reports([stale, coming, waiting, unknown], NOW), (coming, waiting, unknown))


class FallbackLegsTest(unittest.TestCase):
    READY = NOW + minutes(10)

    def legs(self, leg, etas, *, missed=(), headway=600.0, excluded=()):
        return guidance.fallback_board_legs(
            leg, RIDE, etas, missed=list(missed), excluded_vehicles=set(excluded), ready_at=self.READY, now=NOW,
            headway_seconds=headway, horizon_end_at=NOW + timedelta(hours=1))

    def test_planned_vehicle_is_kept_while_only_an_earlier_bus_is_listed(self):
        leg = planned_bus_leg("B2", NOW + minutes(21), NOW + minutes(35))
        result = self.legs(leg, table(report("B1", BOARD, NOW + minutes(3))))
        self.assertEqual([(l.vehicle_id, l.board_eta.predicted_arrival_at) for l in result],
                         [("B2", NOW + minutes(21))])
        self.assertEqual(result[0].reason_codes, (guidance.BOARD_ETA_FROM_SELECTION,))
        self.assertEqual(result[0].alight_eta.predicted_arrival_at, NOW + minutes(35))

    def test_plan_far_behind_the_listed_bus_gives_way_to_the_headway_estimate(self):
        # 계획(환승 뒤 45분)보다 훨씬 일찍 정류소에 온 경우: 앞차 3분 + 배차 10분 예상이 먼저다.
        leg = planned_bus_leg("B5", NOW + minutes(45), NOW + minutes(59))
        listed = table(report("B1", BOARD, NOW + minutes(3)))
        result = self.legs(leg, listed)
        self.assertEqual((result[0].vehicle_id, result[0].board_eta.predicted_arrival_at), (None, NOW + minutes(13)))
        kept = self.legs(leg, listed, headway=None)
        self.assertEqual([l.vehicle_id for l in kept], ["B5"], "배차간격을 모르면 계획 시각을 쓴다")

    def test_listed_planned_vehicle_is_not_revived_and_next_is_estimated_by_headway(self):
        leg = planned_bus_leg("B1", NOW + minutes(4), NOW + minutes(18))
        etas = table(report("B1", BOARD, NOW + minutes(3)), report("B1", ALIGHT, NOW + minutes(16)))
        result = self.legs(leg, etas)
        self.assertEqual(result[0].board_eta.predicted_arrival_at, NOW + minutes(13), "앞차 3분 + 배차 10분")
        self.assertEqual(result[0].alight_eta.predicted_arrival_at, NOW + minutes(26), "같은 차량의 하차 예상 + 배차")
        self.assertEqual((result[0].vehicle_id, result[0].board_eta.eta_source), (None, "headway_estimate"))
        self.assertIn(guidance.BOARD_ETA_AFTER_PASSED_VEHICLE, result[0].reason_codes)
        self.assertGreaterEqual(result[0].board_eta.predicted_arrival_at, self.READY)
        self.assertEqual(len(result), 3)

    def test_estimate_starts_from_the_first_bus_after_ready_time(self):
        leg = planned_bus_leg("B1", NOW - minutes(3), NOW + minutes(11))
        passed = table(report("B1", BOARD, NOW - minutes(4), basis_at=NOW - minutes(6)))
        result = self.legs(leg, passed)
        self.assertEqual(result[0].board_eta.predicted_arrival_at, NOW + minutes(16), "지나간 차 −4분 + 배차 10분 × 2")
        self.assertEqual(result[0].ride_seconds, 14 * 60, "하차 응답이 없으면 계획 탑승 시간을 쓴다")

    def test_missed_vehicle_is_the_reference_and_plan_is_not_reused(self):
        leg = planned_bus_leg("B2", NOW + minutes(21), NOW + minutes(35))
        missed = [{"leg_index": 1, "vehicle_id": "B2", "board_eta_at": (NOW + minutes(21)).isoformat()}]
        result = self.legs(leg, table(), missed=missed, excluded={(PATTERN, "B2")})
        self.assertEqual(result[0].board_eta.predicted_arrival_at, NOW + minutes(31), "놓친 차 시각 + 배차간격")
        self.assertIsNone(result[0].vehicle_id)

    def test_nothing_is_invented_without_a_reference_or_headway(self):
        leg = planned_bus_leg("B1", NOW + minutes(4), NOW + minutes(18))
        listed = table(report("B1", BOARD, NOW + minutes(3)))
        self.assertEqual(self.legs(leg, listed, headway=None), [])
        self.assertEqual(self.legs({"leg_index": 1}, table()), [], "기준 차량이 없으면 예상하지 않는다")


class PickOptionTest(unittest.TestCase):
    @staticmethod
    def option(key, status, board_at, arrival=None):
        leg = SimpleNamespace(run_key=None, board_eta=SimpleNamespace(predicted_arrival_at=board_at))
        return SimpleNamespace(timed_candidate_id=key, legs=(leg,), connections=(SimpleNamespace(status=status),),
                               nominally_feasible=status == "feasible", planned_arrival_at=arrival)

    def test_a_bus_that_leaves_before_the_user_arrives_is_never_picked(self):
        early = self.option("a", "negative_slack", NOW + minutes(3))
        self.assertIsNone(guidance._pick_option([early], None))
        self.assertIs(guidance._any_option([early]), early, "사유 표시용으로만 남긴다")
        later = self.option("b", "feasible", NOW + minutes(21), NOW + minutes(50))
        self.assertIs(guidance._pick_option([early, later], None), later)

    def test_boardable_bus_without_full_times_is_still_guided(self):
        partial = self.option("c", "feasible", NOW + minutes(12))
        self.assertIs(guidance._pick_option([partial], None), partial)


class MissedBusRecordTest(unittest.TestCase):
    LEGS = [{"leg_index": 0, "kind": "walk"},
            {"leg_index": 1, "kind": "bus", "pattern_key": PATTERN, "vehicle": {"vehicle_id": "PLAN", "run_key": None},
             "board_eta": {"predicted_arrival_at": "2026-11-26T05:27:00+00:00"}}]

    def missed(self, payload, **resolved):
        progress = state.initial_progress(self.LEGS)
        return state.apply(lifecycle_status="active", movement_state="walking", progress=progress, legs=self.LEGS,
                           event_type="missed_bus", payload=payload, occurred_at=None, received_at=NOW,
                           resolved=resolved).progress["missed_vehicles"][-1]

    def test_guided_vehicle_is_recorded_when_app_sends_no_vehicle(self):
        entry = self.missed({"legIndex": 1}, guided={"vehicle_id": "SHOWN", "predicted_arrival_at": "T1"})
        self.assertEqual((entry["vehicle_id"], entry["vehicle_basis"], entry["board_eta_at"]),
                         ("SHOWN", "guidance", "T1"))
        estimate = self.missed({"legIndex": 1}, guided={"vehicle_id": None, "predicted_arrival_at": "T2"})
        self.assertEqual((estimate["vehicle_id"], estimate["board_eta_at"]), (None, "T2"),
                         "차량번호 없는 예상 차량은 시각으로 남긴다(계획 차량으로 바꾸지 않음)")

    def test_planned_vehicle_is_used_without_guidance_and_user_value_wins(self):
        entry = self.missed({"legIndex": 1})
        self.assertEqual((entry["vehicle_id"], entry["vehicle_basis"], entry["board_eta_at"]),
                         ("PLAN", "selection", "2026-11-26T05:27:00+00:00"))
        told = self.missed({"legIndex": 1, "vehicleId": "USER"}, guided={"vehicle_id": "SHOWN",
                                                                         "predicted_arrival_at": "T1"})
        self.assertEqual((told["vehicle_id"], told["vehicle_basis"]), ("USER", "user"))


class FocusRefreshTest(unittest.TestCase):
    LEGS = [{"leg_index": 0, "kind": "walk"},
            {"leg_index": 1, "kind": "bus", "route_id": "R1", "board": {"stop_id": "DJB1"},
             "alight": {"stop_id": "DJB2"}},
            {"leg_index": 2, "kind": "walk"},
            {"leg_index": 3, "kind": "bus", "route_id": "R2", "board": {"stop_id": "DJB3"},
             "alight": {"stop_id": "DJB4"}}]

    def journey(self, movement, progress, guidance_version=None, state_version=2, started_ago=5):
        return {"id": "j1", "movement_state": movement, "current_progress": progress, "state_version": state_version,
                "selected_snapshot": {"legs": self.LEGS}, "started_at": NOW - minutes(started_ago),
                "guidance": None if guidance_version is None else {"base_state_version": guidance_version}}

    def refreshed(self, journey):
        settings = Settings(_env_file=None, journey_realtime_refresh_seconds=45,
                            journey_realtime_refresh_idle_seconds=1800)
        with mock.patch.object(recalculation.search_flow, "collect_targets", return_value={}) as collect:
            recalculation.refresh_focus_targets(None, journey, settings, object(), clock=lambda: NOW)
        if not collect.called:
            return None
        (_db, _tago, targets, interval, _clock), options = collect.call_args
        return [(t.kind, t.external_id) for t in targets], interval, options["force"]

    def test_next_board_stop_before_boarding_and_alight_stop_onboard(self):
        walking = self.journey("walking", {"completed_bus_legs": []})
        self.assertEqual(self.refreshed(walking), ([("arrivals", "DJB1")], 45, True), "첫 계산은 항상 새로 받는다")
        onboard = self.journey("onboard", {"completed_bus_legs": [], "boarded": {"leg_index": 1}}, guidance_version=2)
        self.assertEqual(self.refreshed(onboard), ([("arrivals", "DJB2")], 45, False),
                         "같은 상태의 주기 재계산은 기준 시간 안의 값을 재사용")
        transferring = self.journey("transferring", {"completed_bus_legs": [1]}, guidance_version=1)
        self.assertEqual(self.refreshed(transferring), ([("arrivals", "DJB3")], 45, True),
                         "하차 직후(상태 변경)에는 다음 승차 정류소를 새로 받는다")

    def test_abandoned_journey_stops_periodic_refresh_until_new_input(self):
        idle = self.journey("walking", {"completed_bus_legs": []}, guidance_version=2, started_ago=45)
        self.assertIsNone(self.refreshed(idle), "30분 넘게 입력이 없는 이동은 주기 갱신을 하지 않는다")
        located = {**idle, "location_received_at": NOW - minutes(3)}
        self.assertEqual(self.refreshed(located), ([("arrivals", "DJB1")], 45, False), "위치가 들어오면 다시 받는다")
        pressed = self.journey("waiting", {"completed_bus_legs": [], "missed_vehicles": [
            {"leg_index": 1, "received_at": (NOW - minutes(2)).isoformat()}]}, guidance_version=2, started_ago=45)
        self.assertEqual(self.refreshed(pressed), ([("arrivals", "DJB1")], 45, False))
        changed = self.journey("walking", {"completed_bus_legs": []}, guidance_version=1, started_ago=45)
        self.assertEqual(self.refreshed(changed), ([("arrivals", "DJB1")], 45, True), "상태가 바뀌면 항상 새로 받는다")

    def test_no_target_after_the_last_bus(self):
        done = self.journey("walking", {"completed_bus_legs": [1, 3]})
        with mock.patch.object(recalculation.search_flow, "collect_targets") as collect:
            self.assertEqual(recalculation.refresh_focus_targets(None, done, Settings(_env_file=None), object(),
                                                                 clock=lambda: NOW), {})
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
