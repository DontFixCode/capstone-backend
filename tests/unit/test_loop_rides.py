"""순환·왕복 패턴에서 되돌아오는 구간을 타지 않는 후보 생성 단위 시험(DB·네트워크 없음).

한 패턴이 목적지(또는 출발지) 근처를 갈 때와 돌아올 때 두 번 지나는데 검색 반경에는 한쪽 정류소만 드는 경우다
(2026-11-12 실검색: 102번 유성온천역7번출구 → 정부청사역, 7정류장 대신 58정류장을 돌아 건너편에서 하차).
"""

import unittest
from datetime import datetime, timezone

from app.modules.routing.schemas import (
    Coordinate,
    PatternStop,
    RoutePattern,
    SearchContext,
    SearchPolicy,
    StaticSnapshot,
    StopRecord,
)
from app.modules.routing.service import build_search_index, generate_candidates

AT = datetime(2026, 11, 12, 11, 0, tzinfo=timezone.utc)


def policy(**overrides) -> SearchPolicy:
    values = dict(max_boardings=1, access_radius_meters=100, transfer_radius_meters=150, egress_radius_meters=500,
                  max_candidates=10, round_pool_size=50, walk_cost_weight=1, max_search_states=100000,
                  search_budget_ms=5000)
    return SearchPolicy(**{**values, **overrides})


def search(stops, patterns, origin, destination, **overrides):
    snapshot = StaticSnapshot(
        snapshot_id="v",
        stops=tuple(StopRecord(stop_id=stop_id, name=stop_id, lat=lat, lon=lon) for stop_id, (lat, lon) in stops.items()),
        patterns=tuple(RoutePattern(pattern_id=pattern_id, route_id=pattern_id, stops=tuple(
            PatternStop(stop_id=stop_id, stop_order=order) for order, stop_id in enumerate(sequence, start=1)))
            for pattern_id, sequence in patterns.items()))
    settings = policy(**overrides)
    index = build_search_index(snapshot, settings.transfer_radius_meters)
    context = SearchContext(origin=Coordinate(lat=origin[0], lon=origin[1]),
                            destination=Coordinate(lat=destination[0], lon=destination[1]),
                            start_at=AT, evaluated_at=AT, static_snapshot_id="v")
    return generate_candidates(index, context, settings)


def rides(result):
    return sorted(tuple((ride.pattern_id, ride.board_stop_id, ride.board_position, ride.alight_stop_id,
                         ride.alight_position) for ride in candidate.rides) for candidate in result.candidates)


class LoopRideTest(unittest.TestCase):
    # 위도 0.001도는 약 111 m. 건너편 정류소 쌍(GOAL_OPPOSITE·GOAL, START·START_OPPOSITE)은 111 m 떨어져 있다.
    STOPS = {
        "START": (36.3500, 127.3400), "MID": (36.3500, 127.3500), "GOAL_OPPOSITE": (36.3500, 127.3800),
        "FAR1": (36.3500, 127.4000), "TURN": (36.3500, 127.4200), "FAR2": (36.3510, 127.4000),
        "GOAL": (36.3510, 127.3800), "BACK": (36.3510, 127.3400),
    }
    LOOP = ["START", "MID", "GOAL_OPPOSITE", "FAR1", "TURN", "FAR2", "GOAL", "BACK"]
    ORIGIN = (36.3496, 127.3400)
    # GOAL까지 약 445 m(반경 안), GOAL_OPPOSITE까지 약 556 m(반경 밖).
    DESTINATION = (36.3550, 127.3800)

    def test_alights_at_first_pass_instead_of_riding_the_loop_back(self):
        result = search(self.STOPS, {"L": self.LOOP}, self.ORIGIN, self.DESTINATION)
        self.assertEqual(rides(result), [(("L", "START", 1, "GOAL_OPPOSITE", 3),)],
                         "돌아와서 내리는 탑승(7번째) 대신 먼저 지날 때(3번째) 내린다")
        [candidate] = result.candidates
        self.assertAlmostEqual(candidate.egress_straight_line_meters, 556, delta=3)
        self.assertIn("GOAL_OPPOSITE", [stop.stop_id for stop in result.egress_stops])

    def test_far_stop_is_kept_when_the_pattern_does_not_pass_nearby_earlier(self):
        one_way = ["START", "MID", "FAR1", "TURN", "FAR2", "GOAL", "BACK"]
        result = search(self.STOPS, {"L": one_way}, self.ORIGIN, self.DESTINATION)
        self.assertEqual(rides(result), [(("L", "START", 1, "GOAL", 6),)], "근처를 먼저 지나지 않으면 그대로")

    def test_last_ride_of_a_transfer_route_is_trimmed_too(self):
        stops = {**self.STOPS, "FEED": (36.3496, 127.3300)}
        result = search(stops, {"F": ["FEED", "START"], "L": self.LOOP}, (36.3496, 127.3300), self.DESTINATION,
                        max_boardings=2)
        self.assertIn((("F", "FEED", 1, "START", 2), ("L", "START", 1, "GOAL_OPPOSITE", 3)), rides(result))
        self.assertFalse(any(route[-1][3] == "GOAL" for route in rides(result)))

    def test_boards_at_last_pass_near_the_start_stop(self):
        stops = {"START": (36.3500, 127.3400), "LOOP1": (36.3500, 127.3300), "LOOP2": (36.3510, 127.3300),
                 "START_OPPOSITE": (36.3510, 127.3400), "MID": (36.3510, 127.3600), "GOAL": (36.3510, 127.3800)}
        result = search(stops, {"M": list(stops)}, self.ORIGIN, (36.3512, 127.3800))
        self.assertEqual(rides(result), [(("M", "START_OPPOSITE", 4, "GOAL", 6),)],
                         "가장 가까운 정류소에서 타면 한 바퀴 돌아 건너편을 다시 지나므로 건너편에서 탄다")
        self.assertAlmostEqual(result.candidates[0].access_straight_line_meters, 156, delta=3)


if __name__ == "__main__":
    unittest.main()
