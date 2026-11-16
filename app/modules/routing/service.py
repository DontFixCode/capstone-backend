"""정적 스냅샷의 탐색 인덱스 구성과 후보 생성(직행 + 반복 심화 DFS·분기 한정)을 담당한다. 장소 검색 연동은 후속 구현 위치다.

입력으로 받은 정적 정보만 사용하며 DB·TMAP·실시간 API를 호출하지 않는다. 좌표 근접은 도보 후보의 범위일 뿐
실제 보행 가능 여부가 아니고, 차량·탑승시각·도보시간은 평가 단계에서 정한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from bisect import bisect_left, bisect_right, insort
from collections.abc import Callable, Collection, Iterator, Sequence
from itertools import pairwise

from app.modules.routing.schemas import (
    CandidateSearchResult,
    Coordinate,
    IndexIssue,
    NearbyStop,
    RoutePattern,
    SearchContext,
    SearchCoverage,
    SearchIndex,
    SearchPolicy,
    StaticCandidate,
    StaticRide,
    StaticSnapshot,
    StopRecord,
    TransferPair,
)

# 구면 근사의 평균 지구 반지름.
EARTH_RADIUS_METERS = 6_371_008.8


def haversine_meters(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    """두 좌표 사이의 지표면 거리(m). 위도·경도의 도 단위 차이를 미터로 취급하지 않는다."""
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    half_phi = (phi_b - phi_a) / 2
    half_lambda = math.radians(lon_b - lon_a) / 2
    value = math.sin(half_phi) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(half_lambda) ** 2
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(min(1.0, value)))


def build_search_index(snapshot: StaticSnapshot, transfer_radius_meters: float) -> SearchIndex:
    """정적 스냅샷으로 stops·pattern_stops·boardings_by_stop·transfer_stops를 구성한다.

    운행 순서를 확인할 수 없는 패턴은 제외하고, 좌표 누락·이상값과 경유 순서 공백은 issues로 남긴다.
    """
    if transfer_radius_meters < 0:
        raise ValueError("환승 반경은 0 이상이어야 합니다.")

    stops: dict[str, StopRecord] = {}
    issues: list[IndexIssue] = []
    for stop in snapshot.stops:
        if stop.stop_id in stops:
            raise ValueError(f"정류소 ID가 중복되었습니다: {stop.stop_id}")
        stops[stop.stop_id] = stop
        if not _has_usable_coordinate(stop):
            issues.append(IndexIssue(code="stop_coordinate_unusable", ref=stop.stop_id))

    pattern_stops: dict[str, RoutePattern] = {}
    boardings: dict[str, list[tuple[str, int]]] = {}
    for pattern in sorted(snapshot.patterns, key=lambda item: item.pattern_id):
        if pattern.pattern_id in pattern_stops:
            raise ValueError(f"패턴 ID가 중복되었습니다: {pattern.pattern_id}")
        orders = [stop.stop_order for stop in pattern.stops]
        if any(after <= before for before, after in pairwise(orders)):
            issues.append(IndexIssue(code="pattern_order_unconfirmed", ref=pattern.pattern_id))
            continue
        if any(after != before + 1 for before, after in pairwise(orders)):
            issues.append(IndexIssue(code="pattern_order_gap", ref=pattern.pattern_id))
        pattern_stops[pattern.pattern_id] = pattern
        for position, stop in enumerate(pattern.stops):
            boardings.setdefault(stop.stop_id, []).append((pattern.pattern_id, position))
            if stop.stop_id not in stops:
                issues.append(IndexIssue(code="pattern_stop_unknown", ref=stop.stop_id))

    located = sorted(
        (stop for stop in stops.values() if _has_usable_coordinate(stop)),
        key=lambda stop: (stop.lat, stop.stop_id),
    )
    latitudes = tuple(stop.lat for stop in located)

    transfer_stops: dict[str, tuple[NearbyStop, ...]] = {}
    for stop_id in sorted(stops.keys() | boardings.keys()):
        same_stop = NearbyStop(stop_id=stop_id, straight_line_meters=0.0)
        stop = stops.get(stop_id)
        nearby = (
            _stops_within(latitudes, located, stop.lat, stop.lon, transfer_radius_meters)
            if stop is not None and _has_usable_coordinate(stop)
            else ()
        )
        transfer_stops[stop_id] = (same_stop, *(item for item in nearby if item.stop_id != stop_id))

    return SearchIndex(
        snapshot_id=snapshot.snapshot_id,
        transfer_radius_meters=transfer_radius_meters,
        stops=stops,
        pattern_stops=pattern_stops,
        boardings_by_stop={stop_id: tuple(items) for stop_id, items in boardings.items()},
        transfer_stops=transfer_stops,
        latitudes=latitudes,
        located_stops=tuple(located),
        issues=tuple(sorted(set(issues), key=lambda item: (item.code, item.ref))),
    )


def find_stops_within(index: SearchIndex, coordinate: Coordinate, radius_meters: float) -> tuple[NearbyStop, ...]:
    """저장 좌표에서 반경 안의 정류소를 찾아 거리·ID 순서로 반환한다."""
    return _stops_within(index.latitudes, index.located_stops, coordinate.lat, coordinate.lon, radius_meters)


def build_static_candidate(
    rides: Sequence[StaticRide],
    *,
    transfer_meters: Sequence[float | None],
    access_straight_line_meters: float | None,
    egress_straight_line_meters: float | None,
) -> StaticCandidate:
    """탑승 구간으로 후보를 구성한다. 식별자는 경로 내용으로 정해 발견 순서와 무관하게 안정적이다."""
    transfers = tuple(
        TransferPair(
            from_stop_id=front.alight_stop_id,
            to_stop_id=following.board_stop_id,
            same_stop=front.alight_stop_id == following.board_stop_id,
            straight_line_meters=meters,
        )
        for (front, following), meters in zip(pairwise(rides), transfer_meters, strict=True)
    )
    payload = json.dumps(
        [[ride.pattern_id, ride.board_stop_id, ride.board_position, ride.alight_stop_id, ride.alight_position]
         for ride in rides],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return StaticCandidate(
        static_candidate_id=hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32],
        access_stop_id=rides[0].board_stop_id,
        egress_stop_id=rides[-1].alight_stop_id,
        rides=tuple(rides),
        transfers=transfers,
        boarding_count=len(rides),
        transfer_count=len(transfers),
        access_straight_line_meters=access_straight_line_meters,
        egress_straight_line_meters=egress_straight_line_meters,
    )


def generate_candidates(
    index: SearchIndex,
    context: SearchContext,
    policy: SearchPolicy,
    *,
    from_stop_id: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> CandidateSearchResult:
    """직행을 먼저 찾고, 환승 후보는 탑승 횟수를 하나씩 늘리는 반복 심화 DFS와 라운드별 분기 한정법으로 찾는다.

    정적 비용은 승차 구간의 정류소 간 직선거리 합에 접근·환승·하차 도보 직선거리 × walk_cost_weight를 더한 값이다.
    탑승 2회부터 max_boardings까지 라운드마다 독립적인 탐색 예산과 round_pool_size개짜리 보관함을 두고, 보관함이 찬 뒤
    미완성 경로의 누적 비용이 보관함에서 가장 비싼 완성 경로보다 크면 그 분기만 자른다. 비용은 모두 0 이상이므로
    누적 비용은 완성 비용의 하한이다. 마지막에 탑승 횟수별 최저 비용 경로와 새로운 노선 패턴 조합을 우선해 최대
    max_candidates개를 ETA 평가 대상으로 고른다.

    전역 visited[stop_id]를 쓰지 않고 순환은 경로 내부에서 이미 승하차한 정류소로 되돌아가는 경우만 제거한다. 라운드
    예산을 다 쓰면 그때까지 찾은 후보를 보관하고 다음 라운드로 넘어가며, 잘린 범위를 반환한다.

    from_stop_id를 주면 출발지 좌표 대신 그 정류소와 환승 반경 안의 정류소에서 출발한다(실패 대응 재탐색).

    한 번의 탑승에서 버스가 같은 곳(환승 반경 안)을 두 번 지나면 되돌아오는 구간을 타지 않는다. 순환·왕복 패턴은
    목적지 근처를 갈 때(건너편 정류소)와 돌아올 때 두 번 지나는데, 목적지 반경에는 한쪽 정류소만 들 수 있다.
    - 하차: 목적지 후보 정류소의 환승 반경 안 정류소를 같은 패턴이 더 일찍 지나면 그 위치도 하차 후보로 보고
      (near_goals), 더 일찍 지나는 위치가 있는 뒤쪽 하차는 버린다(passes_earlier).
    - 승차: 승차 정류소의 환승 반경 안 정류소를 하차 전에 다시 지나면 가장 늦게 지나는 위치에서 탄다(later_board).
    """
    if context.static_snapshot_id != index.snapshot_id:
        raise ValueError("탐색 인덱스와 검색 입력의 정적 스냅샷이 다릅니다.")
    if policy.transfer_radius_meters != index.transfer_radius_meters:
        raise ValueError("탐색 인덱스와 정책의 환승 반경이 다릅니다.")

    start_stops = (
        find_stops_within(index, context.origin, policy.access_radius_meters)
        if from_stop_id is None
        else _transfer_options(index, from_stop_id)
    )
    goal_stops = {
        stop.stop_id: stop for stop in find_stops_within(index, context.destination, policy.egress_radius_meters)
    }
    reach = _boarding_reach(index, goal_stops, policy.max_boardings)
    walk_weight = policy.walk_cost_weight
    ride_meters: dict[str, tuple[float, ...]] = {}

    expanded = [0] * policy.max_boardings
    completed = [0] * policy.max_boardings
    pruned_cycles = 0
    pruned_splits = 0

    def cumulative_meters(pattern_id: str) -> tuple[float, ...]:
        if pattern_id not in ride_meters:
            ride_meters[pattern_id] = _cumulative_ride_meters(index, index.pattern_stops[pattern_id])
        return ride_meters[pattern_id]

    origin_stop = index.stops.get(from_stop_id) if from_stop_id is not None else None
    origin_point = (
        (context.origin.lat, context.origin.lon) if from_stop_id is None
        else (origin_stop.lat, origin_stop.lon) if origin_stop is not None and _has_usable_coordinate(origin_stop)
        else None
    )
    destination_point = (context.destination.lat, context.destination.lon)
    near_goal_positions: dict[str, dict[int, NearbyStop]] = {}
    used_near_goals: dict[str, NearbyStop] = {}

    def meters_from(point: tuple[float, float] | None, stop_id: str) -> float | None:
        stop = index.stops.get(stop_id)
        if point is None or stop is None or not _has_usable_coordinate(stop):
            return None
        return haversine_meters(point[0], point[1], stop.lat, stop.lon)

    def nearby_ids(stop_id: str) -> set[str]:
        return {option.stop_id for option in _transfer_options(index, stop_id)}

    def near_goals(pattern_id: str) -> dict[int, NearbyStop]:
        """목적지 후보 정류소의 환승 반경 안 정류소를 같은 패턴이 더 일찍 지나는 위치(건너편 정류소 등)와 목적지까지 거리."""
        if pattern_id not in near_goal_positions:
            stops = index.pattern_stops[pattern_id].stops
            found: dict[int, NearbyStop] = {}
            run_start = 0
            for later, item in enumerate(stops):
                if later > 0 and item.stop_order != stops[later - 1].stop_order + 1:
                    run_start = later
                if item.stop_id not in goal_stops:
                    continue
                near = nearby_ids(item.stop_id)
                for earlier in range(run_start, later):
                    stop_id = stops[earlier].stop_id
                    if stop_id in near and stop_id not in goal_stops and earlier not in found:
                        meters = meters_from(destination_point, stop_id)
                        if meters is not None:
                            found[earlier] = NearbyStop(stop_id=stop_id, straight_line_meters=meters)
            near_goal_positions[pattern_id] = found
        return near_goal_positions[pattern_id]

    def goal_at(pattern_id: str, position: int) -> NearbyStop | None:
        goal = goal_stops.get(index.pattern_stops[pattern_id].stops[position].stop_id)
        return goal if goal is not None else near_goals(pattern_id).get(position)

    def passes_earlier(pattern: RoutePattern, board_index: int, alight_index: int) -> bool:
        """이 탑승이 하차 정류소 근처(환승 반경)를 더 일찍 지나는지. 그렇다면 돌아와서 내리는 탑승이다."""
        near = nearby_ids(pattern.stops[alight_index].stop_id)
        return any(pattern.stops[position].stop_id in near for position in range(board_index + 1, alight_index))

    def later_board(
        start: NearbyStop, pattern: RoutePattern, board_index: int, alight_index: int
    ) -> tuple[int, NearbyStop]:
        """승차 정류소 근처(환승 반경)를 하차 전에 다시 지나면 가장 늦게 지나는 위치와 그 정류소까지의 접근 거리."""
        near = nearby_ids(pattern.stops[board_index].stop_id)
        for position in range(alight_index - 1, board_index, -1):
            stop_id = pattern.stops[position].stop_id
            if stop_id in near:
                meters = meters_from(origin_point, stop_id)
                if meters is not None:
                    return position, NearbyStop(stop_id=stop_id, straight_line_meters=meters)
        return board_index, start

    def keep(
        pool: _RoundPool,
        start: NearbyStop,
        rides: tuple[StaticRide, ...],
        links: tuple[NearbyStop, ...],
        score: float,
        goal: NearbyStop,
    ) -> None:
        if pool.above_cutoff(score):
            return
        candidate = build_static_candidate(
            rides,
            transfer_meters=[link.straight_line_meters for link in links],
            access_straight_line_meters=start.straight_line_meters,
            egress_straight_line_meters=goal.straight_line_meters,
        )
        if pool.keep_best_unique(score, candidate):
            completed[len(rides) - 1] += 1
            if goal.stop_id not in goal_stops:
                used_near_goals[goal.stop_id] = goal

    def direct(pool: _RoundPool) -> None:
        """같은 패턴에서 하차 경유 순번이 승차 경유 순번보다 뒤인 목적지 인근 하차 위치를 모두 직행 후보로 둔다."""
        nonlocal pruned_cycles
        for start in start_stops:
            start_cost = walk_weight * start.straight_line_meters
            for pattern_id, board_index in index.boardings_by_stop.get(start.stop_id, ()):
                pattern = index.pattern_stops[pattern_id]
                meters = cumulative_meters(pattern_id)
                for alight_index in _alight_positions(pattern, board_index):
                    expanded[0] += 1
                    alight = pattern.stops[alight_index]
                    if alight.stop_id == start.stop_id:
                        pruned_cycles += 1
                        continue
                    goal = goal_at(pattern_id, alight_index)
                    if goal is None:
                        continue
                    if passes_earlier(pattern, board_index, alight_index):
                        pruned_cycles += 1
                        continue
                    board_at, access = later_board(start, pattern, board_index, alight_index)
                    score = (walk_weight * access.straight_line_meters + meters[alight_index] - meters[board_at]
                             + walk_weight * goal.straight_line_meters)
                    keep(pool, access, (_ride(pattern, board_at, alight_index),), (), score, goal)

    def transfer_boardings(
        alight_stop_id: str,
        pattern_id: str,
        alight_position: int,
        visited: frozenset[str],
        remaining: int,
    ) -> list[tuple[NearbyStop, str, int]]:
        """하차 정류소에서 이어 탈 수 있는 (환승 정류소, 패턴, 위치). 순환·같은 탑승을 나눈 경우·목적지에 닿을 수 없는
        정류소는 뺀다."""
        nonlocal pruned_cycles, pruned_splits
        boardings: list[tuple[NearbyStop, str, int]] = []
        for option in _transfer_options(index, alight_stop_id):
            if option.stop_id in visited:
                pruned_cycles += 1
                continue
            if option.stop_id not in reach[remaining]:
                continue
            for next_pattern_id, board_index in index.boardings_by_stop.get(option.stop_id, ()):
                board_order = index.pattern_stops[next_pattern_id].stops[board_index].stop_order
                # 같은 경유 위치에서 내려 같은 패턴을 다시 타는 것은 한 탑승을 나눈 것과 같다.
                if next_pattern_id == pattern_id and board_order == alight_position:
                    pruned_splits += 1
                    continue
                boardings.append((option, next_pattern_id, board_index))
        return boardings

    def dfs(
        start: NearbyStop,
        pattern_id: str,
        board_index: int,
        rides: tuple[StaticRide, ...],
        links: tuple[NearbyStop, ...],
        cost: float,
        visited: frozenset[str],
        target: int,
        pool: _RoundPool,
        budget: _RoundBudget,
    ) -> None:
        # cost에는 앞서 탄 버스와 현재 승차 지점까지의 도보 비용이 포함된다.
        nonlocal pruned_cycles
        if budget.exhausted() or pool.above_cutoff(cost):
            return
        pattern = index.pattern_stops[pattern_id]
        meters = cumulative_meters(pattern_id)
        level = len(rides)
        remaining_after_ride = target - level - 1
        for alight_index in _alight_positions(pattern, board_index):
            if not budget.try_take_one():
                return
            expanded[level] += 1
            arrival_cost = cost + meters[alight_index] - meters[board_index]
            # 승차 비용은 경유 순번을 따라 줄지 않으므로 더 뒤의 하차 위치도 함께 건너뛴다.
            if pool.above_cutoff(arrival_cost):
                break
            alight = pattern.stops[alight_index]
            if alight.stop_id in visited:
                pruned_cycles += 1
                continue
            # 첫 탑승은 승차 정류소 근처를 다시 지나면 가장 늦게 지나는 위치에서 탄 것으로 본다(되돌아오는 구간 제외).
            board_at, access = (later_board(start, pattern, board_index, alight_index) if level == 0
                                else (board_index, start))
            if board_at != board_index:
                arrival_cost = (walk_weight * access.straight_line_meters + meters[alight_index] - meters[board_at])
            if remaining_after_ride == 0:
                goal = goal_at(pattern_id, alight_index)
                if goal is not None:
                    if passes_earlier(pattern, board_at, alight_index):
                        pruned_cycles += 1
                    else:
                        score = arrival_cost + walk_weight * goal.straight_line_meters
                        keep(pool, access, (*rides, _ride(pattern, board_at, alight_index)), links, score, goal)
                continue
            boardings = transfer_boardings(alight.stop_id, pattern_id, alight.stop_order, visited,
                                           remaining_after_ride)
            if not boardings:
                continue
            next_rides = (*rides, _ride(pattern, board_at, alight_index))
            next_visited = visited | {alight.stop_id}
            for option, next_pattern_id, next_board_index in boardings:
                if budget.exhausted():
                    return
                dfs(access, next_pattern_id, next_board_index, next_rides, (*links, option),
                    arrival_cost + walk_weight * option.straight_line_meters, next_visited | {option.stop_id},
                    target, pool, budget)

    direct_pool = _RoundPool(None)
    direct(direct_pool)
    pools: dict[int, _RoundPool] = {1: direct_pool}

    truncation_reasons: list[str] = []
    finished_by_round: list[set[str]] = []
    partial_start: str | None = None
    unexplored: set[str] = set()
    for target in range(2, policy.max_boardings + 1):
        pool = _RoundPool(policy.round_pool_size)
        budget = _RoundBudget(policy.max_search_states, policy.search_budget_ms, monotonic)
        finished: set[str] = set()
        for position, start in enumerate(start_stops):
            if start.stop_id in reach[target]:
                cost = walk_weight * start.straight_line_meters
                visited = frozenset({start.stop_id})
                for pattern_id, board_index in index.boardings_by_stop.get(start.stop_id, ()):
                    dfs(start, pattern_id, board_index, (), (), cost, visited, target, pool, budget)
            if budget.reason is not None:
                if budget.reason not in truncation_reasons:
                    truncation_reasons.append(budget.reason)
                if partial_start is None:
                    partial_start = start.stop_id
                unexplored.update(stop.stop_id for stop in start_stops[position + 1:])
                break
            finished.add(start.stop_id)
        pools[target] = pool
        finished_by_round.append(finished)

    selected = _select_for_eta(pools, policy.max_candidates)
    truncated = bool(truncation_reasons)
    completed_starts = [
        stop.stop_id for stop in start_stops if all(stop.stop_id in finished for finished in finished_by_round)
    ]
    unexplored.discard(partial_start)

    reasons: list[str] = []
    if not start_stops:
        reasons.append("no_access_stops")
    if not goal_stops:
        reasons.append("no_egress_stops")
    if start_stops and goal_stops and not selected and not truncated:
        reasons.append("no_candidate_within_limits")
    if truncated:
        reasons.append("search_truncated")

    return CandidateSearchResult(
        static_snapshot_id=index.snapshot_id,
        candidates=tuple(sorted(selected, key=lambda candidate: candidate.static_candidate_id)),
        access_stops=tuple(start_stops),
        egress_stops=(*goal_stops.values(), *(used_near_goals[key] for key in sorted(used_near_goals))),
        reason_codes=tuple(reasons),
        coverage=SearchCoverage(
            truncated=truncated,
            truncation_reasons=tuple(truncation_reasons),
            completed_start_stops=tuple(completed_starts),
            partial_start_stop=partial_start,
            unexplored_start_stops=tuple(stop.stop_id for stop in start_stops if stop.stop_id in unexplored),
            expanded_states_by_level=tuple(expanded),
            completed_candidates_by_level=tuple(completed),
            pruned_cycle_count=pruned_cycles,
            pruned_split_ride_count=pruned_splits,
        ),
        index_issues=index.issues,
    )


class _RoundPool:
    """라운드에서 찾은 완성 경로를 (정적 비용, 후보 ID) 오름차순으로 보관한다. capacity가 None이면 모두 보관한다."""

    def __init__(self, capacity: int | None) -> None:
        self.capacity = capacity
        self.items: list[tuple[float, str, StaticCandidate]] = []
        self._ids: set[str] = set()

    def is_full(self) -> bool:
        return self.capacity is not None and len(self.items) >= self.capacity

    def above_cutoff(self, lower_bound: float) -> bool:
        """보관함이 찼고 하한이 가장 비싼 완성 경로의 비용보다 크면 그 분기의 경로는 보관함에 들어갈 수 없다."""
        return self.is_full() and lower_bound > self.items[-1][0]

    def keep_best_unique(self, score: float, candidate: StaticCandidate) -> bool:
        """같은 경로는 한 번만 보관한다. 가득 찼으면 (비용, 후보 ID)가 가장 큰 경로보다 작을 때만 바꿔 넣는다."""
        candidate_id = candidate.static_candidate_id
        if candidate_id in self._ids:
            return False
        if self.is_full():
            if (score, candidate_id) >= self.items[-1][:2]:
                return False
            self._ids.discard(self.items.pop()[1])
        insort(self.items, (score, candidate_id, candidate), key=lambda item: item[:2])
        self._ids.add(candidate_id)
        return True


class _RoundBudget:
    """라운드 하나의 탐색 예산. 하차 위치 하나를 확인할 때마다 한 단위를 쓰고, 다 쓰면 이유를 남긴다."""

    def __init__(self, max_states: int, budget_ms: int, monotonic: Callable[[], float]) -> None:
        self._remaining = max_states
        self._monotonic = monotonic
        self._deadline = monotonic() + budget_ms / 1000
        self.reason: str | None = None

    def exhausted(self) -> bool:
        if self.reason is None and self._monotonic() >= self._deadline:
            self.reason = "search_budget"
        return self.reason is not None

    def try_take_one(self) -> bool:
        if self.exhausted():
            return False
        if self._remaining <= 0:
            self.reason = "max_search_states"
            return False
        self._remaining -= 1
        return True


def _select_for_eta(pools: dict[int, _RoundPool], limit: int) -> list[StaticCandidate]:
    """ETA 평가 대상을 최대 limit개 고른다.

    후보가 있는 탑승 횟수마다 최저 비용 경로를 하나씩 확보하고, 나머지는 탑승 횟수 → 비용 순으로 살피면서 새로운
    노선 패턴 조합을 먼저 고른 뒤 남은 자리를 같은 순서로 채운다. 조합의 범위는 라운드별 보관함에 남은 후보로 한정된다.
    """
    selected: dict[str, StaticCandidate] = {}
    for boardings in sorted(pools):
        if len(selected) == limit:
            return list(selected.values())
        items = pools[boardings].items
        if items:
            selected[items[0][1]] = items[0][2]

    # 보관함은 이미 (비용, 후보 ID) 순이므로 이어 붙이면 탑승 횟수 → 비용 순서가 된다.
    remaining = [item for boardings in sorted(pools) for item in pools[boardings].items if item[1] not in selected]
    sequences = {_pattern_sequence(candidate) for candidate in selected.values()}
    for _, candidate_id, candidate in remaining:
        if len(selected) == limit:
            return list(selected.values())
        sequence = _pattern_sequence(candidate)
        if sequence not in sequences:
            selected[candidate_id] = candidate
            sequences.add(sequence)
    for _, candidate_id, candidate in remaining:
        if len(selected) == limit:
            break
        selected.setdefault(candidate_id, candidate)
    return list(selected.values())


def _pattern_sequence(candidate: StaticCandidate) -> tuple[str, ...]:
    return tuple(ride.pattern_id for ride in candidate.rides)


def _ride(pattern: RoutePattern, board_index: int, alight_index: int) -> StaticRide:
    board, alight = pattern.stops[board_index], pattern.stops[alight_index]
    return StaticRide(
        route_id=pattern.route_id,
        pattern_id=pattern.pattern_id,
        direction=pattern.direction,
        board_stop_id=board.stop_id,
        board_position=board.stop_order,
        alight_stop_id=alight.stop_id,
        alight_position=alight.stop_order,
    )


def _cumulative_ride_meters(index: SearchIndex, pattern: RoutePattern) -> tuple[float, ...]:
    """패턴 위치별 누적 승차 직선거리. 좌표를 쓸 수 없는 정류소는 건너뛰고 앞뒤 정류소를 이어 누적값이 줄지 않게 한다."""
    totals: list[float] = []
    total = 0.0
    previous: StopRecord | None = None
    for item in pattern.stops:
        stop = index.stops.get(item.stop_id)
        if stop is not None and _has_usable_coordinate(stop):
            if previous is not None:
                total += haversine_meters(previous.lat, previous.lon, stop.lat, stop.lon)
            previous = stop
        totals.append(total)
    return tuple(totals)


def _has_usable_coordinate(stop: StopRecord) -> bool:
    """누락·0·범위 밖 좌표를 거리 판정에서 제외한다."""
    if stop.lat is None or stop.lon is None or stop.lat == 0 or stop.lon == 0:
        return False
    return -90 <= stop.lat <= 90 and -180 <= stop.lon <= 180


def _stops_within(
    latitudes: Sequence[float],
    located: Sequence[StopRecord],
    lat: float,
    lon: float,
    radius_meters: float,
) -> tuple[NearbyStop, ...]:
    # 지표면 거리는 위도 차이의 호 길이보다 짧을 수 없으므로 위도 범위로 후보를 줄여도 누락이 없다.
    latitude_margin = math.degrees(radius_meters / EARTH_RADIUS_METERS)
    first = bisect_left(latitudes, lat - latitude_margin)
    last = bisect_right(latitudes, lat + latitude_margin)
    nearby = []
    for stop in located[first:last]:
        meters = haversine_meters(lat, lon, stop.lat, stop.lon)
        if meters <= radius_meters:
            nearby.append(NearbyStop(stop_id=stop.stop_id, straight_line_meters=meters))
    nearby.sort(key=lambda item: (item.straight_line_meters, item.stop_id))
    return tuple(nearby)


def _transfer_options(index: SearchIndex, stop_id: str) -> tuple[NearbyStop, ...]:
    options = index.transfer_stops.get(stop_id)
    return options if options is not None else (NearbyStop(stop_id=stop_id, straight_line_meters=0.0),)


def _alight_positions(pattern: RoutePattern, board_index: int) -> Iterator[int]:
    """승차 위치 뒤의 하차 위치. 경유 순서가 끊긴 지점 너머는 운행 순서를 확인할 수 없어 연결하지 않는다."""
    stops = pattern.stops
    for position in range(board_index + 1, len(stops)):
        if stops[position].stop_order != stops[position - 1].stop_order + 1:
            return
        yield position


def _boarding_reach(index: SearchIndex, goal_stops: Collection[str], max_boardings: int) -> list[frozenset[str]]:
    """reach[k]: k회 이내 탑승으로 목적지 후보에 닿을 수 있는 승차 정류소. 완성될 수 없는 분기만 잘라낸다."""
    reach: list[frozenset[str]] = [frozenset()]
    for _ in range(max_boardings):
        previous = reach[-1]
        current = set(previous)
        for pattern in index.pattern_stops.values():
            stops = pattern.stops
            can_alight_later = False
            for position in range(len(stops) - 1, -1, -1):
                stop_id = stops[position].stop_id
                if can_alight_later:
                    current.add(stop_id)
                if stop_id in goal_stops or any(
                    option.stop_id in previous for option in _transfer_options(index, stop_id)
                ):
                    can_alight_later = True
                if position > 0 and stops[position].stop_order != stops[position - 1].stop_order + 1:
                    can_alight_later = False
        reach.append(frozenset(current))
    return reach
