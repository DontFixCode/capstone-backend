"""이동 중 재탐색(버스 놓침·목적지 변경) 규칙의 단위 시험(DB·네트워크 없음).

재탐색 종류 판정, 검색이 저장한 평가를 다시 쓸 수 있는 조건, 제안 스냅샷의 환승 구간 번호, 수락 뒤 이동 상태,
같은 좌표 도보를 확인한다. 값은 모두 합성이다.
"""

import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.modules.journeys import service, state
from app.workflows import guidance
from app.workflows import search as flow

UTC = timezone.utc
NOW = datetime(2026, 11, 27, 5, 0, tzinfo=UTC)


def minutes(value: float) -> timedelta:
    return timedelta(minutes=value)


def walk(index, seconds=60.0):
    return {"leg_index": index, "kind": "walk", "status": "available", "duration_seconds": seconds, "distance_m": 70.0}


def bus(index, route, board, alight, *, vehicle=None, board_at=None):
    return {"leg_index": index, "kind": "bus", "route_id": f"R{route}", "route_internal_id": route,
            "pattern_key": f"P{route}", "board": {"stop_id": board, "seq": 1}, "alight": {"stop_id": alight, "seq": 4},
            "vehicle": {"vehicle_id": vehicle, "run_key": None},
            "board_eta": None if board_at is None else {"predicted_arrival_at": board_at.isoformat()}}


def candidate(legs, *, arrival, feasible=True, transfers=()):
    """재탐색 검색이 저장한 후보 행(평가는 검색 시점 값)."""
    return {"id": "cand-1", "static_route_key": "route-1", "option_key": "opt-1", "legs": legs,
            "nominally_feasible": feasible, "rank_no": 1, "walking_seconds": 60.0, "nominal_total_seconds": 900.0,
            "expected_total_seconds": None, "journey_probability": None, "time_status": "available",
            "evaluated_at": NOW,
            "evaluation": {"times": {"planned_arrival_at": arrival.isoformat()}, "transfers": list(transfers)}}


def never_missed(pattern, stop, vehicle_id, board_at):
    return False


class RerouteKindTest(unittest.TestCase):
    def test_kind_and_clear(self):
        self.assertIsNone(state.reroute_kind({"legs": []}))
        self.assertEqual(state.reroute_kind({"reroute_kind": "missed_bus", "reroute_search_id": "s"}), "missed_bus")
        changed = {"destination_changed_at": NOW.isoformat(), "reroute_kind": "missed_bus", "reroute_search_id": "s",
                   "reroute_status": "queued", "destination": {"lat": 1.0, "lon": 2.0}}
        self.assertEqual(state.reroute_kind(changed), "destination", "목적지 변경이 걸려 있으면 항상 destination")
        cleared = state.clear_reroute(changed)
        self.assertEqual(set(cleared), {"destination_changed_at", "destination"},
                         "재탐색 값만 지우고 목적지 변경 표시는 수락 전까지 남긴다")


class StoredArrivalTest(unittest.TestCase):
    """이동 중 재평가로 연결을 구하지 못한 재탐색 후보에 검색 시점의 평가를 쓰는 조건."""

    def stored(self, row, *, ready_at=NOW, excluded_vehicles=frozenset(), excluded_runs=frozenset(),
               missed_estimate=never_missed):
        return guidance._stored_arrival(row, ready_at=ready_at, now=NOW, excluded_runs=set(excluded_runs),
                                        excluded_vehicles=set(excluded_vehicles), missed_estimate=missed_estimate)

    def row(self, **extra):
        legs = [walk(0), bus(1, 40, "A", "C", vehicle="V40", board_at=NOW + minutes(3)), walk(2, 0.0),
                bus(3, 50, "C", "F", vehicle="V50", board_at=NOW + minutes(9)), walk(4)]
        return candidate(legs, arrival=NOW + minutes(15), **extra)

    def test_usable_while_first_bus_is_still_catchable(self):
        self.assertEqual(self.stored(self.row()), NOW + minutes(15))
        self.assertEqual(self.stored(self.row(), ready_at=NOW + minutes(3)), NOW + minutes(15), "준비 시각과 같으면 탈 수 있다")

    def test_not_usable_after_first_bus_or_when_search_found_it_infeasible(self):
        self.assertIsNone(self.stored(self.row(), ready_at=NOW + minutes(4)), "첫 차량이 준비 시각보다 먼저 온다")
        self.assertIsNone(self.stored(self.row(feasible=False)), "검색이 연결되지 않는다고 본 후보")
        passed = self.row()
        passed["legs"][1]["board_eta"] = {"predicted_arrival_at": (NOW - minutes(1)).isoformat()}
        self.assertIsNone(self.stored(passed, ready_at=NOW - minutes(5)), "첫 차량이 이미 지나갔다")
        no_time = self.row()
        no_time["evaluation"]["times"]["planned_arrival_at"] = None
        self.assertIsNone(self.stored(no_time), "도착 예정이 없는 후보")

    def test_missed_vehicle_is_not_used_on_any_leg(self):
        self.assertIsNone(self.stored(self.row(), excluded_vehicles={("P40", "V40")}))
        self.assertIsNone(self.stored(self.row(), excluded_vehicles={("P50", "V50")}),
                          "놓친 차량을 뒤 구간에서 다시 타는 후보도 쓰지 않는다")
        self.assertEqual(self.stored(self.row(), excluded_vehicles={("P40", "V50")}), NOW + minutes(15),
                         "다른 노선 패턴의 같은 번호는 관계없다")

    def test_missed_estimated_vehicle_rule_is_applied_to_first_leg(self):
        seen = []

        def missed(pattern, stop, vehicle_id, board_at):
            seen.append((pattern, stop, vehicle_id, board_at))
            return True

        self.assertIsNone(self.stored(self.row(), missed_estimate=missed))
        self.assertEqual(seen, [("P40", "A", "V40", NOW + minutes(3))])


class ProposalSnapshotTest(unittest.TestCase):
    def test_stored_evaluation_keeps_values_and_renumbers_transfers(self):
        """완료 구간(1) 뒤에 재탐색 후보를 붙이면 환승의 구간 번호는 합친 경로의 번호가 된다."""
        selected = [walk(0), bus(1, 10, "A", "C"), walk(2), bus(3, 20, "C", "E"), walk(4)]
        tail = [walk(0, 0.0), bus(1, 50, "C", "F", vehicle="V50", board_at=NOW + minutes(2)), walk(2, 0.0),
                bus(3, 60, "F", "G", vehicle="V60", board_at=NOW + minutes(9)), walk(4)]
        row = candidate(tail, arrival=NOW + minutes(14),
                        transfers=[{"transfer_index": 1, "front_leg_index": 1, "next_leg_index": 3, "status": "feasible",
                                    "sample_id": "sample-7"}])
        merged, progress = guidance._merge_fixed_prefix(selected, row["legs"], 1, alternative_has_prefix=False)
        self.assertEqual([leg.get("route_id") for leg in merged if leg["kind"] == "bus"], ["R10", "R50", "R60"])
        proposal = guidance._proposal_snapshot(
            {"destination": {"lat": 1.0, "lon": 2.0}, "search_id": "reroute-search"}, row, None, legs=merged,
            accepted_progress=progress, completed_count=1, mode="nominal", policy=None, remaining_seconds=840.0,
            state_version=4, selection_version=1, decision={"kind": "destination", "reason_codes": ["x"]})
        self.assertEqual((proposal["candidate_id"], proposal["search_id"]), ("cand-1", "reroute-search"))
        self.assertEqual(proposal["evaluation"]["transfers"],
                         [{"transfer_index": 1, "front_leg_index": 3, "next_leg_index": 5, "status": "feasible",
                           "sample_id": "sample-7"}])
        self.assertEqual((proposal["nominal_total_seconds"], proposal["ranking_mode"]), (840.0, "nominal"),
                         "기준 이동시간은 지금부터 도착까지")
        self.assertEqual((proposal["boarding_count"], proposal["base_state_version"], proposal["proposal_version"]),
                         (3, 4, 1))
        self.assertEqual(proposal["legs"][3]["vehicle"]["vehicle_id"], "V50", "검색이 정한 차량을 그대로 둔다")
        self.assertEqual((proposal["accepted_progress"]["completed_bus_legs"],
                          proposal["accepted_progress"]["next_bus_leg_index"]), ([1], 3))

    def test_time_unconfirmed_proposal_keeps_search_values(self):
        row = candidate([walk(0), bus(1, 50, "C", "F"), walk(2)], arrival=NOW + minutes(14))
        merged, progress = guidance._merge_fixed_prefix([], row["legs"], 0, alternative_has_prefix=False)
        proposal = guidance._proposal_snapshot({}, row, None, legs=merged, accepted_progress=progress,
                                               completed_count=0, mode="nominal", policy=None, remaining_seconds=None,
                                               state_version=2, selection_version=1, decision={})
        self.assertEqual(proposal["nominal_total_seconds"], 900.0)


class MovementAfterAcceptTest(unittest.TestCase):
    OLD = [walk(0), bus(1, 10, "A", "E"), walk(2)]

    def journey(self, movement):
        return {"movement_state": movement, "current_progress": {"next_bus_leg_index": 1}}

    def after(self, movement, new_legs, **progress):
        return service._movement_after_accept(self.journey(movement), self.OLD, new_legs,
                                              {"next_bus_leg_index": 1, **progress})

    def test_waiting_at_the_same_stop_stays_waiting(self):
        self.assertEqual(self.after("waiting", [walk(0, 0.0), bus(1, 40, "A", "C"), walk(2)]), "waiting")

    def test_waiting_then_other_stop_walks_again(self):
        other = [walk(0, 200.0), bus(1, 30, "C2", "E"), walk(2)]
        self.assertEqual(self.after("waiting", other), "walking", "새 정류소까지 걷는 시간을 안내에 다시 넣는다")
        self.assertEqual(self.after("waiting", other, completed_bus_legs=[1]), "transferring")

    def test_other_states_are_kept(self):
        other = [walk(0, 200.0), bus(1, 30, "C2", "E"), walk(2)]
        for movement in ("walking", "transferring"):
            self.assertEqual(self.after(movement, other), movement)


class SameCoordinateWalkTest(unittest.TestCase):
    def test_origin_on_the_stop_needs_no_lookup(self):
        """정류소를 출발점으로 한 재탐색: 출발점→그 정류소는 0초 도보로 두고 도보 API를 부르지 않는다."""
        points = {flow.ORIGIN_REF: {"ref": flow.ORIGIN_REF, "kind": "origin", "lat": 36.35, "lon": 127.38},
                  "STOP-A": {"ref": "STOP-A", "kind": "stop", "lat": 36.35, "lon": 127.38}}
        walks, stats = flow.lookup_walks(Counter({(flow.ORIGIN_REF, "STOP-A"): 2}), points, None, search_option="0",
                                         max_lookups=60)
        found = walks[(flow.ORIGIN_REF, "STOP-A")]
        self.assertEqual((found.status, found.duration_seconds, found.distance_m, found.provider),
                         ("available", 0.0, 0.0, None))
        self.assertEqual(found.as_walk_result().duration_seconds, 0.0)
        self.assertEqual(stats.get("same_coordinates"), 1)
        self.assertNotIn("tmap_calls", stats)


if __name__ == "__main__":
    unittest.main()
