"""정적 스냅샷의 탐색 인덱스 구성과 깊이 제한 DFS 후보 생성을 담당한다. 장소 검색 연동은 후속 구현 위치다.

입력으로 받은 정적 정보만 사용하며 DB·TMAP·실시간 API를 호출하지 않는다. 좌표 근접은 도보 후보의 범위일 뿐
실제 보행 가능 여부가 아니고, 차량·탑승시각·도보시간은 평가 단계에서 정한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from bisect import bisect_left, bisect_right
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
    """최대 탑승 횟수 안의 직행·다중 환승 후보를 깊이 제한 DFS로 생성한다.

    첫 후보를 찾아도 탐색을 끝내지 않고 다른 노선·하차·환승 조합을 유지한다. 전역 visited[stop_id]를 쓰지 않고
    순환은 경로 내부에서 이미 승하차한 정류소로 되돌아가는 경우만 제거한다. 한도에 닿으면 멈추고 잘린 범위를
    반환하며, 결과는 식별자 순서라 발견 순서가 순위로 쓰이지 않는다.

    from_stop_id를 주면 출발지 좌표 대신 그 정류소와 환승 반경 안의 정류소에서 출발한다(실패 대응 재탐색).
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

    found: dict[str, StaticCandidate] = {}
    expanded = [0] * policy.max_boardings
    completed = [0] * policy.max_boardings
    pruned_cycles = 0
    pruned_splits = 0
    stop_reason: str | None = None
    deadline = monotonic() + policy.search_budget_ms / 1000

    def emit(start: NearbyStop, rides: tuple[StaticRide, ...], links: tuple[NearbyStop, ...]) -> None:
        nonlocal stop_reason
        candidate = build_static_candidate(
            rides,
            transfer_meters=[link.straight_line_meters for link in links],
            access_straight_line_meters=start.straight_line_meters,
            egress_straight_line_meters=goal_stops[rides[-1].alight_stop_id].straight_line_meters,
        )
        if candidate.static_candidate_id in found:
            return
        if len(found) >= policy.max_candidates:
            stop_reason = "max_candidates"
            return
        found[candidate.static_candidate_id] = candidate
        completed[len(rides) - 1] += 1

    def dfs(
        start: NearbyStop,
        board_stop_id: str,
        rides: tuple[StaticRide, ...],
        links: tuple[NearbyStop, ...],
        visited: frozenset[str],
    ) -> None:
        nonlocal stop_reason, pruned_cycles, pruned_splits
        if sum(expanded) >= policy.max_search_states:
            stop_reason = "max_search_states"
            return
        if monotonic() >= deadline:
            stop_reason = "search_budget"
            return
        level = len(rides)
        expanded[level] += 1
        remaining_after_ride = policy.max_boardings - level - 1

        for pattern_id, board_index in index.boardings_by_stop.get(board_stop_id, ()):
            pattern = index.pattern_stops[pattern_id]
            board_order = pattern.stops[board_index].stop_order
            # 같은 경유 위치에서 내려 같은 패턴을 다시 타는 것은 한 탑승을 나눈 것과 같다.
            if rides and rides[-1].pattern_id == pattern_id and rides[-1].alight_position == board_order:
                pruned_splits += 1
                continue
            for alight_index in _alight_positions(pattern, board_index):
                alight = pattern.stops[alight_index]
                if alight.stop_id in visited:
                    pruned_cycles += 1
                    continue
                next_stops: list[NearbyStop] = []
                if remaining_after_ride > 0:
                    for option in _transfer_options(index, alight.stop_id):
                        if option.stop_id in visited:
                            pruned_cycles += 1
                        elif option.stop_id in reach[remaining_after_ride]:
                            next_stops.append(option)
                if alight.stop_id not in goal_stops and not next_stops:
                    continue
                ride = StaticRide(
                    route_id=pattern.route_id,
                    pattern_id=pattern_id,
                    direction=pattern.direction,
                    board_stop_id=board_stop_id,
                    board_position=board_order,
                    alight_stop_id=alight.stop_id,
                    alight_position=alight.stop_order,
                )
                next_rides = (*rides, ride)
                if alight.stop_id in goal_stops:
                    emit(start, next_rides, links)
                    if stop_reason is not None:
                        return
                next_visited = visited | {alight.stop_id}
                for next_stop in next_stops:
                    dfs(start, next_stop.stop_id, next_rides, (*links, next_stop), next_visited | {next_stop.stop_id})
                    if stop_reason is not None:
                        return

    completed_starts: list[str] = []
    partial_start: str | None = None
    unexplored: list[str] = []
    for position, start in enumerate(start_stops):
        if start.stop_id in reach[policy.max_boardings]:
            dfs(start, start.stop_id, (), (), frozenset({start.stop_id}))
        if stop_reason is not None:
            partial_start = start.stop_id
            unexplored = [stop.stop_id for stop in start_stops[position + 1:]]
            break
        completed_starts.append(start.stop_id)

    reasons: list[str] = []
    if not start_stops:
        reasons.append("no_access_stops")
    if not goal_stops:
        reasons.append("no_egress_stops")
    if start_stops and goal_stops and not found and stop_reason is None:
        reasons.append("no_candidate_within_limits")
    if stop_reason is not None:
        reasons.append("search_truncated")

    return CandidateSearchResult(
        static_snapshot_id=index.snapshot_id,
        candidates=tuple(found[key] for key in sorted(found)),
        access_stops=tuple(start_stops),
        egress_stops=tuple(goal_stops.values()),
        reason_codes=tuple(reasons),
        coverage=SearchCoverage(
            truncated=stop_reason is not None,
            truncation_reasons=(stop_reason,) if stop_reason is not None else (),
            completed_start_stops=tuple(completed_starts),
            partial_start_stop=partial_start,
            unexplored_start_stops=tuple(unexplored),
            expanded_states_by_level=tuple(expanded),
            completed_candidates_by_level=tuple(completed),
            pruned_cycle_count=pruned_cycles,
            pruned_split_ride_count=pruned_splits,
        ),
        index_issues=index.issues,
    )


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
