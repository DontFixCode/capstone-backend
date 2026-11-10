"""후보별 도착 예상·대기시간·환승 여유·성공 확률의 입력·출력 형식을 정의한다.

시각은 시간대가 있는 datetime, 소요시간은 초, 거리는 미터, 확률은 0~1이다. None은 미산출·미확인을 뜻하며
0이나 false로 대신하지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

from app.modules.routing.schemas import FrozenSchema, SearchCoverage, SearchPolicy, StaticCandidate

# 도보 결과의 from·to에서 정류소가 아닌 양 끝을 나타낸다.
ORIGIN_REF = "@origin"
DESTINATION_REF = "@destination"

# 거리 비례 추정(route_average_ratio)은 단위·운행 범위 검증 전이라 값에 포함하지 않는다.
EtaSource = Literal["provider", "historical_segments"]
TimeStatus = Literal["available", "partial", "unavailable"]
ConnectionStatus = Literal["feasible", "negative_slack", "time_unknown", "confirmed_infeasible"]
WalkStatus = Literal["available", "no_route", "lookup_failed"]
EventType = Literal["arrival", "departure", "passage", "unknown"]
MatchStatus = Literal["matched", "uncertain", "unmatched"]
Version = Annotated[str, Field(min_length=1)]
Probability = Annotated[float, Field(ge=0, le=1)]


class EvaluationPolicy(SearchPolicy):
    """평가 설정. 자료에 근거한 값이 정해지지 않았으므로 기본값을 두지 않으며 누락은 검증 오류다."""

    search_horizon_seconds: PositiveFloat
    evaluation_budget_ms: PositiveInt
    max_timed_candidates: PositiveInt
    boarding_buffer_seconds: NonNegativeFloat
    max_data_age_seconds: NonNegativeFloat
    min_segment_samples: PositiveInt
    min_probability_samples: PositiveInt
    min_joint_scenes: PositiveInt
    min_improvement_seconds: NonNegativeFloat
    # 예측 잔여시간 차이가 이 범위 안인 과거 표본만 비교 가능하다고 본다.
    max_horizon_difference_seconds: NonNegativeFloat
    # 한 시나리오에서 실패 대응을 다시 시도할 수 있는 횟수. 넘으면 미도착으로 구분한다.
    max_recovery_attempts: PositiveInt
    # 대체 경로 재탐색 설정. None이면 실패 대응을 같은 경로의 다음 차량 대기로 제한한다.
    recovery_search: SearchPolicy | None
    timing_policy_version: Version
    sample_policy_version: Version
    recovery_policy_version: Version
    segment_model_version: Version


class WalkResult(FrozenSchema):
    """방향을 구분한 도보 조회 결과. no_route는 확인된 경로 없음이고 lookup_failed는 조회 실패다."""

    from_ref: str
    to_ref: str
    status: WalkStatus
    duration_seconds: NonNegativeFloat | None = None
    distance_meters: NonNegativeFloat | None = None

    @model_validator(mode="after")
    def available_requires_duration(self) -> WalkResult:
        if self.status == "available" and self.duration_seconds is None:
            raise ValueError("조회에 성공한 도보 결과에는 duration_seconds가 필요합니다.")
        return self


class EtaReport(FrozenSchema):
    """정규화한 차량별 도착 정보 하나. 평가 모듈은 수집 시각을 기준시각으로 대신하지 않는다.

    basis_at은 remaining_seconds의 기준으로 확인된 시각, 또는 설정한 근사 정책이 정한 시각이다(basis_source로 구분,
    app.modules.transit.eta_basis). 둘 다 없으면 None으로 둔다.
    stop_order를 확인하지 못한 응답은 반복 경유 위치를 구분할 수 없어 탑승 구간에 연결하지 않는다.
    """

    pattern_id: str
    stop_id: str
    stop_order: int | None
    vehicle_id: str | None
    run_key: str | None
    remaining_seconds: NonNegativeFloat | None
    basis_at: AwareDatetime | None
    source_generated_at: AwareDatetime | None = None
    collected_at: AwareDatetime
    # 해당 차량이 이 정류소를 지난 것으로 확인된 시각. 예상 도착시각이 지났다는 사실과 구분한다.
    observed_passed_at: AwareDatetime | None = None
    source_ref: str | None = None
    # basis_at의 출처: provider(원천 확인) 또는 설정한 근사 정책의 출처(예: server_received_at_approx).
    basis_source: str | None = None
    # 원천이 준 남은 정류장 수(대전시 STATUS_POS, 검증 전). 회차 키가 없을 때 같은 차량의 승하차 예측 일관성 확인에 쓴다.
    stops_away: int | None = None


class VehicleEta(FrozenSchema):
    """차량·정류소별 절대 도착 예상시각과 생성 근거."""

    pattern_id: str
    stop_id: str
    stop_order: int
    vehicle_id: str | None
    run_key: str | None
    predicted_arrival_at: AwareDatetime
    eta_source: EtaSource
    model_version: str | None
    # 예측 기준시각부터 도착까지 남은 시간. 비교 가능한 표본을 고르는 기준이다.
    horizon_seconds: NonNegativeFloat
    basis_at: AwareDatetime
    source_generated_at: AwareDatetime | None
    collected_at: AwareDatetime
    observed_passed_at: AwareDatetime | None = None
    source_ref: str | None = None
    segment_sample_counts: tuple[int, ...] = ()
    basis_source: str | None = None
    stops_away: int | None = None


class ObservedEvent(FrozenSchema):
    """사후 관측 하나. 정확한 시각이 없으면 observed_at은 None이며 대표시각을 넣지 않는다."""

    observed_at: AwareDatetime | None
    event_type: EventType
    match_status: MatchStatus

    @property
    def confirmed_arrival_at(self) -> datetime | None:
        """같은 차량·정류소의 도착으로 확인된 점 시각만 반환한다."""
        if self.event_type == "arrival" and self.match_status == "matched":
            return self.observed_at
        return None


class SegmentObservation(FrozenSchema):
    """같은 운행에서 확인한 인접 경유 위치 사이의 도착 이벤트 쌍."""

    pattern_id: str
    from_stop_order: int
    to_stop_order: int
    run_key: str | None
    from_event: ObservedEvent
    to_event: ObservedEvent
    condition_key: str


class PredictionOutcome(FrozenSchema):
    """예측 당시 값과 이후 관측. 예측값은 이후 ETA 갱신으로 덮어쓰지 않는다."""

    predicted_arrival_at: AwareDatetime
    eta_source: EtaSource
    model_version: str | None
    horizon_seconds: NonNegativeFloat
    run_key: str | None
    observed: ObservedEvent | None = None

    @property
    def error_seconds(self) -> float | None:
        """관측 도착시각 − 예측 도착시각. 확인된 점 시각이 없으면 None이다."""
        observed_at = self.observed.confirmed_arrival_at if self.observed else None
        if observed_at is None:
            return None
        return (observed_at - self.predicted_arrival_at).total_seconds()


class TransferErrorSample(FrozenSchema):
    """환승 연결 1건의 상대 오차 표본. 같은 운행의 반복 예측은 run_group_key로 묶어 한 번만 센다."""

    sample_id: str
    run_group_key: str
    condition_key: str
    timing_policy_version: str
    front: PredictionOutcome
    next: PredictionOutcome


class SceneRide(FrozenSchema):
    """과거 관측 묶음의 탑승 구간 하나. vehicle_rank는 그 시점 해당 구간의 도착 예정 순서(0이 가장 빠름)다."""

    pattern_id: str
    board_position: int
    alight_position: int
    vehicle_rank: NonNegativeInt
    board: PredictionOutcome
    alight: PredictionOutcome


class JointScene(FrozenSchema):
    """scene_id로 연결한 과거 관측 묶음. 계획한 차량과 실패 대응에 쓸 차량의 관측을 함께 담는다."""

    scene_id: str
    run_group_key: str
    condition_key: str
    timing_policy_version: str
    rides: tuple[SceneRide, ...]


class EvaluationInputs(FrozenSchema):
    """호출자가 준비한 평가 자료. 같은 입력이면 같은 계산 결과를 재현한다."""

    data_snapshot_id: str
    # 비교 가능한 운행 조건 키. 표본·관측 묶음·구간 관측은 이 키가 같을 때만 사용한다.
    sample_condition_key: str
    walks: tuple[WalkResult, ...] = ()
    eta_reports: tuple[EtaReport, ...] = ()
    # 도착 정보를 조회한 정류소. 조회하지 않은 대상과 조회했지만 응답에 없는 대상을 구분한다.
    queried_stop_ids: frozenset[str] = frozenset()
    segment_observations: tuple[SegmentObservation, ...] = ()
    transfer_error_samples: tuple[TransferErrorSample, ...] = ()
    joint_scenes: tuple[JointScene, ...] = ()


class TimedLeg(FrozenSchema):
    """정적 탑승 구간에 차량 하나를 연결한 결과. 시각은 정류소 도착 예상이며 출발시각이 아니다."""

    route_id: str
    pattern_id: str
    direction: str | None
    vehicle_id: str | None
    run_key: str | None
    vehicle_rank: NonNegativeInt
    board_stop_id: str
    board_position: int
    alight_stop_id: str
    alight_position: int
    board_eta: VehicleEta
    alight_eta: VehicleEta | None
    ride_seconds: NonNegativeFloat | None
    reason_codes: tuple[str, ...] = ()


class RideLegs(FrozenSchema):
    """정적 탑승 구간 하나의 차량 옵션과, 옵션이 없거나 줄어든 이유."""

    legs: tuple[TimedLeg, ...]
    reason_codes: tuple[str, ...]
    horizon_excluded_count: int


class ConnectionAssessment(FrozenSchema):
    """첫 승차(connection_index=0) 또는 환승 하나의 기준 ETA 판정."""

    connection_index: int
    from_ref: str
    to_stop_id: str
    walk_seconds: NonNegativeFloat | None
    buffer_seconds: NonNegativeFloat
    ready_at: AwareDatetime | None
    next_arrival_at: AwareDatetime | None
    slack_seconds: float | None
    # 대기시간은 음수를 만들지 않는다. 기준 여유가 음수면 None이다.
    wait_seconds: NonNegativeFloat | None
    status: ConnectionStatus
    reason_codes: tuple[str, ...] = ()


class PlannedJourney(FrozenSchema):
    """차량·시각 조합 하나의 상태. 시간 자료가 끊긴 경로는 계산한 앞부분까지 보존한다."""

    candidate: StaticCandidate
    start_at: AwareDatetime
    legs: tuple[TimedLeg, ...]
    connections: tuple[ConnectionAssessment, ...]
    egress_walk_seconds: NonNegativeFloat | None = None
    # 시간 자료가 끊긴 탑승 구간 순번. 탑승 구간 수와 같으면 마지막 도보에서 끊긴 것이다.
    limited_at_ride_index: int | None = None
    reason_codes: tuple[str, ...] = ()

    @property
    def connections_complete(self) -> bool:
        """모든 탑승 구간의 승하차 ETA와 첫 승차·환승 판정이 시간 자료로 채워졌는지 반환한다."""
        return (
            len(self.legs) == len(self.candidate.rides)
            and len(self.connections) == len(self.candidate.rides)
            and all(leg.alight_eta is not None for leg in self.legs)
            and all(c.status in ("feasible", "negative_slack") for c in self.connections)
        )

    @property
    def planned_arrival_at(self) -> datetime | None:
        """기준 ETA로 계산한 목적지 도착시각. 시간 자료가 끊겼으면 None이다."""
        if not self.connections_complete or self.egress_walk_seconds is None:
            return None
        return self.legs[-1].alight_eta.predicted_arrival_at + timedelta(seconds=self.egress_walk_seconds)


class CandidateProvenance(FrozenSchema):
    """평가 근거. 관측 ETA와 모델 추정, 사용한 표본 수와 묶음을 구분해 남긴다."""

    data_snapshot_id: str
    start_at: AwareDatetime
    evaluated_at: AwareDatetime
    timing_policy_version: str
    sample_policy_version: str
    recovery_policy_version: str
    transfer_sample_counts: tuple[int, ...]
    transfer_sample_rejections: tuple[dict[str, int], ...]
    joint_scene_ids: tuple[str, ...]
    joint_scene_rejections: dict[str, int]
    expected_scenario_count: int | None


class EvaluatedCandidate(FrozenSchema):
    """차량·시각 조합별 평가 결과. 필드 의미는 Phase 3 Algorithms의 출력 계약을 따른다."""

    static_candidate_id: str
    timed_candidate_id: str
    legs: tuple[TimedLeg, ...]
    connections: tuple[ConnectionAssessment, ...]
    transfer_count: int
    egress_walk_seconds: NonNegativeFloat | None
    walk_seconds: NonNegativeFloat | None
    buffer_seconds: NonNegativeFloat | None
    wait_seconds: NonNegativeFloat | None
    ride_seconds: NonNegativeFloat | None
    planned_arrival_at: AwareDatetime | None
    nominal_total_seconds: NonNegativeFloat | None
    expected_total_seconds: NonNegativeFloat | None
    first_boarding_probability: Probability | None
    transfer_probabilities: tuple[Probability | None, ...]
    journey_probability: Probability | None
    time_status: TimeStatus
    nominally_feasible: bool | None
    connection_status: ConnectionStatus
    limited_at_ride_index: int | None
    # 기대시간을 계산한 정책·자료 범위. 같은 값끼리만 기대시간을 비교한다.
    expected_comparison_key: str | None
    reason_codes: tuple[str, ...]
    provenance: CandidateProvenance


class EvaluationCoverage(FrozenSchema):
    """평가 범위와 잘림 정보. 평가하지 못한 정적 후보를 함께 남긴다."""

    truncated: bool
    truncation_reasons: tuple[str, ...]
    evaluated_static_candidate_ids: tuple[str, ...]
    partial_static_candidate_id: str | None
    unevaluated_static_candidate_ids: tuple[str, ...]
    timed_combination_count: int
    horizon_end_at: AwareDatetime
    horizon_excluded_option_count: int
    rejected_eta_counts: dict[str, int]
    recovery_search_truncated: bool


class EvaluationResult(FrozenSchema):
    """평가 응답. 기준 시간이 완성된 후보와 평가 제한 목록을 구분한다.

    candidates는 기준 ETA상 연결되고 전체 시간이 계산된 후보다. limited는 시간 미확인·음수 기준 여유·확인된
    실행 불가 후보로, 계산한 부분 구간과 이유를 보존한다.
    """

    static_snapshot_id: str
    data_snapshot_id: str
    start_at: AwareDatetime
    evaluated_at: AwareDatetime
    candidates: tuple[EvaluatedCandidate, ...]
    limited: tuple[EvaluatedCandidate, ...]
    search_coverage: SearchCoverage
    coverage: EvaluationCoverage
