"""경로 후보 생성의 모듈 간 데이터 형식을 정의한다. 장소 검색 요청·응답은 후속 구현 위치다.

시각은 시간대가 있는 datetime, 거리는 미터로 통일하고 식별자는 불투명한 문자열로 다룬다.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, NonNegativeFloat, PositiveFloat, PositiveInt


class FrozenSchema(BaseModel):
    """모듈 간 불변 데이터. 필수 값 누락·정의하지 않은 필드·NaN·무한대는 검증 오류로 처리한다."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Coordinate(FrozenSchema):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class StopRecord(FrozenSchema):
    """정류소 하나. 좌표 누락·이상값은 스냅샷 전체를 막지 않고 인덱스 구성에서 제약으로 남긴다."""

    stop_id: str
    name: str | None = None
    lat: float | None = None
    lon: float | None = None


class PatternStop(FrozenSchema):
    """패턴의 경유 항목. 같은 정류소가 여러 번 나와도 stop_order로 각 위치를 구분한다."""

    stop_id: str
    stop_order: int


class RoutePattern(FrozenSchema):
    """노선·방향·분기를 구분한 운행 순서열. 제공되지 않는 방향은 임의로 만들지 않고 None으로 둔다."""

    pattern_id: str
    route_id: str
    direction: str | None = None
    stops: tuple[PatternStop, ...]


class StaticSnapshot(FrozenSchema):
    """한 번의 검색 동안 사용할 정적 데이터 버전."""

    snapshot_id: str
    stops: tuple[StopRecord, ...]
    patterns: tuple[RoutePattern, ...]


class NearbyStop(FrozenSchema):
    """저장 좌표로 계산한 직선 거리 안의 정류소. 실제 보행 가능 여부나 도보시간이 아니다."""

    stop_id: str
    straight_line_meters: NonNegativeFloat


class IndexIssue(FrozenSchema):
    """인덱스 구성에서 확인한 정적 데이터 제약. ref는 해당 정류소 또는 패턴의 ID다."""

    code: str
    ref: str


@dataclass(frozen=True)
class SearchIndex:
    """정적 스냅샷에서 만든 탐색용 인덱스. 정적 데이터 갱신 후 만들어 검색 간에 재사용한다."""

    snapshot_id: str
    transfer_radius_meters: float
    stops: dict[str, StopRecord]
    pattern_stops: dict[str, RoutePattern]
    # 정류소에서 탈 수 있는 (pattern_id, pattern.stops의 위치). 반복 경유 위치도 각각 저장한다.
    boardings_by_stop: dict[str, tuple[tuple[str, int], ...]]
    # 동일 정류소(항상 첫 항목)와 환승 반경 안의 인근 정류소.
    transfer_stops: dict[str, tuple[NearbyStop, ...]]
    # 위도순으로 정렬한 좌표 보유 정류소. 범위 검색의 공간 인덱스다.
    latitudes: tuple[float, ...]
    located_stops: tuple[StopRecord, ...]
    issues: tuple[IndexIssue, ...]


class SearchPolicy(FrozenSchema):
    """후보 생성 설정. 자료에 근거한 값이 정해지지 않았으므로 기본값을 두지 않는다."""

    max_boardings: PositiveInt
    access_radius_meters: PositiveFloat
    # 0이면 동일 정류소 환승만 허용한다.
    transfer_radius_meters: NonNegativeFloat
    egress_radius_meters: PositiveFloat
    # ETA 평가에 넘길 최대 후보 수. 탐색 중단 조건이 아니다.
    max_candidates: PositiveInt
    # 환승 라운드(정확한 탑승 횟수)마다 정적 비용 순으로 보관하는 완성 경로 수. 분기 한정의 기준이다.
    round_pool_size: PositiveInt
    # 정적 비용에서 도보 직선거리 1 m를 승차 직선거리 몇 m로 볼지.
    walk_cost_weight: PositiveFloat
    # 라운드마다 새로 주는 탐색 한도. max_search_states는 확인한 하차 위치 수(재현 가능),
    # search_budget_ms는 실행 환경에 따라 달라지는 계산시간 한도다.
    max_search_states: PositiveInt
    search_budget_ms: PositiveInt


class SearchContext(FrozenSchema):
    """검색 입력. 이동 중 상태 버전·완료 구간·현재 탑승 제약은 이동 관리 구현 때 추가한다."""

    origin: Coordinate
    destination: Coordinate
    start_at: AwareDatetime
    evaluated_at: AwareDatetime
    static_snapshot_id: str


class StaticRide(FrozenSchema):
    """버스 탑승 구간 하나. board_position·alight_position은 패턴의 stop_order다."""

    route_id: str
    pattern_id: str
    direction: str | None
    board_stop_id: str
    board_position: int
    alight_stop_id: str
    alight_position: int


class TransferPair(FrozenSchema):
    """환승 정류소 쌍. 직선 거리만 기록하며 도보시간은 평가 단계에서 확인한다."""

    from_stop_id: str
    to_stop_id: str
    same_stop: bool
    straight_line_meters: NonNegativeFloat | None


class StaticCandidate(FrozenSchema):
    """경로의 뼈대. 특정 차량·운행 회차·탑승시각·도보시간은 평가 단계에서 정한다."""

    static_candidate_id: str
    access_stop_id: str
    egress_stop_id: str
    rides: tuple[StaticRide, ...]
    transfers: tuple[TransferPair, ...]
    boarding_count: int
    transfer_count: int
    access_straight_line_meters: NonNegativeFloat | None
    egress_straight_line_meters: NonNegativeFloat | None


class SearchCoverage(FrozenSchema):
    """탐색 범위와 잘림 정보. 잘린 결과는 탐색 전체의 최적 경로를 보장하지 않는다."""

    truncated: bool
    truncation_reasons: tuple[str, ...]
    completed_start_stops: tuple[str, ...]
    partial_start_stop: str | None
    unexplored_start_stops: tuple[str, ...]
    expanded_states_by_level: tuple[int, ...]
    completed_candidates_by_level: tuple[int, ...]
    pruned_cycle_count: int
    pruned_split_ride_count: int


class CandidateSearchResult(FrozenSchema):
    """후보 생성 결과. candidates는 ETA 평가 대상으로 고른 후보의 식별자 순서이며 발견 순서나 추천 순위가 아니다."""

    static_snapshot_id: str
    candidates: tuple[StaticCandidate, ...]
    access_stops: tuple[NearbyStop, ...]
    egress_stops: tuple[NearbyStop, ...]
    reason_codes: tuple[str, ...]
    coverage: SearchCoverage
    index_issues: tuple[IndexIssue, ...]
