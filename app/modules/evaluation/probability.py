"""상대 오차 ECDF·공동 시나리오·전체 연결 확률·실패 대응 후 기대시간 계산을 담당한다.

개별 환승 확률·전체 연결 확률·기대시간의 자료 요건을 각각 검사하고 부족한 값만 None과 이유로 반환한다. 표본은
결과를 보기 전에 조건으로 고르며, 개별 확률을 곱해 전체 확률을 만들지 않는다. 도보시간은 TMAP 예상값을 고정값으로
사용하므로 결과는 그 가정 아래의 추정값이다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import fmean

from app.modules.evaluation.schemas import (
    EvaluationPolicy,
    JointScene,
    PlannedJourney,
    PredictionOutcome,
    SceneRide,
    TimedLeg,
    TransferErrorSample,
    VehicleEta,
)

# 실패 지점의 경로와 실패한 탑승 구간 순번을 받아, 그 정류소에서 이어갈 수 있는 경로를 돌려준다.
RecoveryOptions = Callable[[PlannedJourney, int], Sequence[PlannedJourney]]
SceneRideKey = tuple[str, int, int, int]


@dataclass(frozen=True)
class SelectedScene:
    """후보에 적용하기로 고른 관측 묶음."""

    scene_id: str
    rides: Mapping[SceneRideKey, SceneRide]


@dataclass(frozen=True)
class SceneSelection:
    """적용 묶음과 묶음별 제외 이유의 개수."""

    scenes: tuple[SelectedScene, ...]
    rejected: Mapping[str, int]


@dataclass(frozen=True)
class _Replay:
    """관측 오차를 적용한 연결 결과. failed_ride_index가 None이면 계획한 연결이 모두 성공한 것이다."""

    failed_ride_index: int | None
    ready_at: datetime | None
    arrival_at: datetime | None


def estimate_transfer_probability(
    slack_seconds: float,
    eligible_error_pairs: Sequence[tuple[float, float]],
    min_samples: int,
) -> float | None:
    """상대 오차 z = 앞 차량 오차 − 다음 차량 오차가 여유 m 이하인 표본의 비율을 구한다. 표본이 부족하면 None이다."""
    if len(eligible_error_pairs) < max(1, min_samples):
        return None
    successes = sum(front_error - next_error <= slack_seconds for front_error, next_error in eligible_error_pairs)
    return successes / len(eligible_error_pairs)


def select_error_pairs(
    samples: Iterable[TransferErrorSample],
    *,
    front_eta: VehicleEta,
    next_eta: VehicleEta,
    condition_key: str,
    policy: EvaluationPolicy,
) -> tuple[tuple[tuple[float, float], ...], dict[str, int]]:
    """현재 예측과 비교 가능한 (앞 오차, 다음 오차) 쌍을 고른다. 같은 운행 묶음은 표본 하나로 센다."""
    tolerance = policy.max_horizon_difference_seconds
    selected: dict[str, tuple[tuple[float, str], tuple[float, float]]] = {}
    rejected: Counter[str] = Counter()
    for sample in samples:
        if sample.condition_key != condition_key or sample.timing_policy_version != policy.timing_policy_version:
            rejected["sample_condition_mismatch"] += 1
            continue
        if not (_comparable(sample.front, front_eta, tolerance) and _comparable(sample.next, next_eta, tolerance)):
            rejected["sample_not_comparable"] += 1
            continue
        front_error, next_error = sample.front.error_seconds, sample.next.error_seconds
        if front_error is None or next_error is None:
            rejected["sample_observation_missing"] += 1
            continue
        distance = abs(sample.front.horizon_seconds - front_eta.horizon_seconds) + abs(
            sample.next.horizon_seconds - next_eta.horizon_seconds
        )
        order = (distance, sample.sample_id)
        current = selected.get(sample.run_group_key)
        if current is not None:
            rejected["sample_same_run_group"] += 1
        if current is None or order < current[0]:
            selected[sample.run_group_key] = (order, (front_error, next_error))
    return tuple(selected[key][1] for key in sorted(selected)), dict(rejected)


def select_joint_scenes(
    journey: PlannedJourney,
    scenes: Iterable[JointScene],
    *,
    condition_key: str,
    policy: EvaluationPolicy,
) -> SceneSelection:
    """후보의 모든 구간을 잇는 비교 가능한 관측 묶음을 고른다. 성공·실패 결과는 선정 기준에 쓰지 않는다.

    중간 차량은 승차·하차 관측이 같은 과거 운행이어야 한다. 마지막 하차 관측은 연결 판정에 필요하지 않으므로
    없어도 확률 계산에 쓰고, 기대시간에서만 자료 부족으로 처리한다.
    """
    tolerance = policy.max_horizon_difference_seconds
    selected: dict[str, tuple[tuple[float, str], SelectedScene]] = {}
    rejected: Counter[str] = Counter()
    for scene in scenes:
        if scene.condition_key != condition_key or scene.timing_policy_version != policy.timing_policy_version:
            rejected["scene_condition_mismatch"] += 1
            continue
        rides = {_scene_ride_key(ride): ride for ride in scene.rides}
        problem = _scene_problem(journey, rides, tolerance)
        if problem is not None:
            rejected[problem] += 1
            continue
        distance = sum(
            abs(rides[_leg_key(leg)].board.horizon_seconds - leg.board_eta.horizon_seconds)
            + abs(rides[_leg_key(leg)].alight.horizon_seconds - leg.alight_eta.horizon_seconds)
            for leg in journey.legs
        )
        order = (distance, scene.scene_id)
        current = selected.get(scene.run_group_key)
        if current is not None:
            rejected["scene_same_run_group"] += 1
        if current is None or order < current[0]:
            selected[scene.run_group_key] = (order, SelectedScene(scene.scene_id, rides))
    chosen = sorted((item[1] for item in selected.values()), key=lambda scene: scene.scene_id)
    return SceneSelection(tuple(chosen), dict(rejected))


def estimate_journey_probability(
    journey: PlannedJourney,
    selection: SceneSelection,
    min_joint_scenes: int,
) -> tuple[float | None, float | None, tuple[str, ...]]:
    """(전체 연결 확률, 첫 승차 확률, 이유)를 반환한다. 실패한 연결도 분모에 포함한다."""
    scenes = selection.scenes
    if len(scenes) < max(1, min_joint_scenes):
        return None, None, ("joint_scenes_insufficient",)
    first_ready_at = journey.connections[0].ready_at
    replays = [_replay_connections(journey, scene.rides, first_ready_at) for scene in scenes]
    connected = sum(replay.failed_ride_index is None for replay in replays)
    boarded_first = sum(replay.failed_ride_index != 0 for replay in replays)
    return connected / len(scenes), boarded_first / len(scenes), ()


def estimate_expected_total_seconds(
    journey: PlannedJourney,
    selection: SceneSelection,
    *,
    recovery_options: RecoveryOptions,
    policy: EvaluationPolicy,
) -> tuple[float | None, tuple[str, ...]]:
    """시나리오별 전체 시간 T_s의 평균을 구한다. 실패 후 대기·우회가 T_s에 포함되므로 손실을 다시 더하지 않는다.

    미도착·자료 부족 시나리오를 빼고 평균내지 않고, 하나라도 있으면 None과 이유를 반환한다.
    """
    scenes = selection.scenes
    if len(scenes) < max(1, policy.min_joint_scenes):
        return None, ("joint_scenes_insufficient",)
    if journey.egress_walk_seconds is None:
        return None, ("egress_walk_unavailable",)
    totals: list[float] = []
    problems: set[str] = set()
    for scene in scenes:
        total, problem = _scenario_total_seconds(journey, scene, recovery_options, policy)
        if problem is not None:
            problems.add(problem)
        else:
            totals.append(total)
    if problems:
        return None, tuple(sorted(problems))
    return fmean(totals), ()


def _comparable(outcome: PredictionOutcome, eta: VehicleEta, tolerance: float) -> bool:
    """ETA 생성 방식·모델 버전이 같고 예측 잔여시간이 허용 범위 안일 때만 같은 분포로 다룬다."""
    return (
        outcome.eta_source == eta.eta_source
        and outcome.model_version == eta.model_version
        and abs(outcome.horizon_seconds - eta.horizon_seconds) <= tolerance
    )


def _scene_ride_key(ride: SceneRide) -> SceneRideKey:
    return ride.pattern_id, ride.board_position, ride.alight_position, ride.vehicle_rank


def _leg_key(leg: TimedLeg) -> SceneRideKey:
    return leg.pattern_id, leg.board_position, leg.alight_position, leg.vehicle_rank


def _scene_problem(journey: PlannedJourney, rides: Mapping[SceneRideKey, SceneRide], tolerance: float) -> str | None:
    last_index = len(journey.legs) - 1
    for index, leg in enumerate(journey.legs):
        problem = _ride_problem(leg, rides.get(_leg_key(leg)), tolerance, require_alight=index < last_index)
        if problem is not None:
            return problem
    return None


def _ride_problem(leg: TimedLeg, ride: SceneRide | None, tolerance: float, *, require_alight: bool) -> str | None:
    """관측 묶음의 탑승 구간을 현재 차량에 적용할 수 없는 이유를 반환한다."""
    if ride is None:
        return "scene_leg_missing"
    if not (_comparable(ride.board, leg.board_eta, tolerance) and _comparable(ride.alight, leg.alight_eta, tolerance)):
        return "scene_not_comparable"
    board_error, alight_error = ride.board.error_seconds, ride.alight.error_seconds
    if board_error is None or (require_alight and alight_error is None):
        return "scene_observation_missing"
    if ride.board.run_key is None or ((require_alight or alight_error is not None) and ride.alight.run_key is None):
        return "scene_run_unconfirmed"
    if ride.alight.run_key is not None and ride.alight.run_key != ride.board.run_key:
        return "scene_run_mismatch"
    if alight_error is not None:
        if ride.alight.observed.observed_at <= ride.board.observed.observed_at:
            return "scene_time_inconsistent"
        board_at = leg.board_eta.predicted_arrival_at + timedelta(seconds=board_error)
        alight_at = leg.alight_eta.predicted_arrival_at + timedelta(seconds=alight_error)
        if alight_at <= board_at:
            return "scene_time_inconsistent"
    return None


def _replay_connections(
    journey: PlannedJourney,
    rides: Mapping[SceneRideKey, SceneRide],
    first_ready_at: datetime,
) -> _Replay:
    """관측 오차를 현재 기준 ETA에 적용해 첫 승차부터 순서대로 연결을 판정한다."""
    ready = first_ready_at
    last_index = len(journey.legs) - 1
    for index, leg in enumerate(journey.legs):
        ride = rides[_leg_key(leg)]
        board_at = leg.board_eta.predicted_arrival_at + timedelta(seconds=ride.board.error_seconds)
        if ready > board_at:
            return _Replay(index, ready, None)
        alight_error = ride.alight.error_seconds
        if alight_error is None:
            return _Replay(None, None, None)
        alight_at = leg.alight_eta.predicted_arrival_at + timedelta(seconds=alight_error)
        if index == last_index:
            arrival_at = (
                alight_at + timedelta(seconds=journey.egress_walk_seconds)
                if journey.egress_walk_seconds is not None
                else None
            )
            return _Replay(None, None, arrival_at)
        following = journey.connections[index + 1]
        ready = alight_at + timedelta(seconds=following.walk_seconds + following.buffer_seconds)
    raise ValueError("탑승 구간이 없는 경로는 재생할 수 없습니다.")


def _scenario_total_seconds(
    journey: PlannedJourney,
    scene: SelectedScene,
    recovery_options: RecoveryOptions,
    policy: EvaluationPolicy,
) -> tuple[float | None, str | None]:
    """시나리오 하나의 전체 시간. 실패하면 그 시점에 알 수 있는 정보로 대응을 고르고 결과에만 관측을 적용한다."""
    current = journey
    replay = _replay_connections(current, scene.rides, current.connections[0].ready_at)
    missed: set[tuple[str, int, int]] = set()
    attempts = 0
    while replay.failed_ride_index is not None:
        if attempts == policy.max_recovery_attempts:
            return None, "scenario_no_arrival"
        attempts += 1
        failed_leg = current.legs[replay.failed_ride_index]
        missed.add((failed_leg.pattern_id, failed_leg.board_position, failed_leg.vehicle_rank))
        options = recovery_options(current, replay.failed_ride_index)
        recovery = _choose_recovery(options, replay.ready_at, missed)
        if recovery is None:
            return None, "recovery_option_unavailable"
        for leg in recovery.legs:
            if _ride_problem(
                leg,
                scene.rides.get(_leg_key(leg)),
                policy.max_horizon_difference_seconds,
                require_alight=True,
            ):
                return None, "recovery_outcome_unobserved"
        first_ready_at = _recovery_ready_at(recovery, replay.ready_at)
        current = recovery
        replay = _replay_connections(current, scene.rides, first_ready_at)
    if replay.arrival_at is None:
        return None, "scene_final_alight_unobserved"
    return (replay.arrival_at - journey.start_at).total_seconds(), None


def _recovery_ready_at(recovery: PlannedJourney, available_at: datetime) -> datetime:
    """실패 지점에서 이미 준비를 마쳤으므로 같은 정류소면 그대로, 다른 정류소로 걸어가면 도보·준비를 더한다."""
    first = recovery.connections[0]
    if first.from_ref == first.to_stop_id:
        return available_at
    return available_at + timedelta(seconds=first.walk_seconds + first.buffer_seconds)


def _choose_recovery(
    options: Sequence[PlannedJourney],
    available_at: datetime,
    missed: set[tuple[str, int, int]],
) -> PlannedJourney | None:
    """놓친 시각과 기준 ETA만으로 대응 경로를 고른다. 이후 관측은 선택에 사용하지 않는다."""
    eligible = []
    for option in options:
        first_leg = option.legs[0]
        if (first_leg.pattern_id, first_leg.board_position, first_leg.vehicle_rank) in missed:
            continue
        if first_leg.board_eta.predicted_arrival_at < _recovery_ready_at(option, available_at):
            continue
        walk_seconds = sum(connection.walk_seconds for connection in option.connections)
        eligible.append(
            (option.planned_arrival_at, option.candidate.transfer_count, walk_seconds, _journey_order(option), option)
        )
    if not eligible:
        return None
    return min(eligible, key=lambda item: item[:4])[-1]


def _journey_order(journey: PlannedJourney) -> tuple:
    return journey.candidate.static_candidate_id, tuple(leg.vehicle_rank for leg in journey.legs)
