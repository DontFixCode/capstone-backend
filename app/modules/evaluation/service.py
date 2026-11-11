"""정적 후보의 차량·회차·시각 조합 확장과 후보 평가 취합을 담당한다.

필요한 데이터는 호출자가 인수로 준비한다. 같은 정적 경로의 복수 차량 조합을 유지하며, 확률 자료가 없어도 시간 계산이
가능하면 후보를 유지한다. 평가하지 못한 범위와 이유는 평가 제한 목록과 잘림 메타데이터로 반환한다.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.modules.evaluation.eta import EtaTable, build_ride_legs, index_etas, index_segment_durations
from app.modules.evaluation.probability import (
    RecoveryOptions,
    estimate_expected_total_seconds,
    estimate_journey_probability,
    estimate_transfer_probability,
    select_error_pairs,
    select_joint_scenes,
)
from app.modules.evaluation.schemas import (
    DESTINATION_REF,
    ORIGIN_REF,
    CandidateProvenance,
    ConnectionAssessment,
    EvaluatedCandidate,
    EvaluationCoverage,
    EvaluationInputs,
    EvaluationPolicy,
    EvaluationResult,
    GapAnchor,
    PlannedJourney,
    RideLegs,
    TimedLeg,
    WalkResult,
)
from app.modules.evaluation.transfer import assess_connection, resolve_walk_seconds
from app.modules.routing.schemas import CandidateSearchResult, SearchContext, SearchIndex, StaticCandidate, StaticRide
from app.modules.routing.service import build_static_candidate, generate_candidates


@dataclass
class _SharedData:
    """같은 응답을 여러 경로·차량 조합이 공유하도록 모아 둔 평가 자료."""

    context: SearchContext
    inputs: EvaluationInputs
    policy: EvaluationPolicy
    walks: dict[tuple[str, str], WalkResult]
    etas: EtaTable
    durations: dict[tuple[str, int], tuple[float, ...]]
    gap_anchors: dict[tuple[str, int, str], GapAnchor] = field(default_factory=dict)
    ride_legs: dict[tuple[str, int, int], RideLegs] = field(default_factory=dict)
    recovery_search_truncated: bool = False


def evaluate_candidates(
    search: CandidateSearchResult,
    context: SearchContext,
    inputs: EvaluationInputs,
    policy: EvaluationPolicy,
    *,
    search_index: SearchIndex | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> EvaluationResult:
    """정적 후보를 첫 승차부터 마지막 도보까지 차량 조합별로 평가해 취합한다.

    search_index를 주면 실패 대응에서 같은 후보 생성 함수로 대체 경로를 재탐색하고, 없으면 다음 차량 대기만 평가한다.
    한도에 닿으면 멈추고 평가하지 못한 범위를 반환한다. 정적 후보는 식별자 순서로 처리해 발견 순서에 의존하지 않는다.
    """
    if search.static_snapshot_id != context.static_snapshot_id:
        raise ValueError("정적 후보와 검색 입력의 정적 스냅샷이 다릅니다.")

    shared = _SharedData(
        context=context,
        inputs=inputs,
        policy=policy,
        walks={(walk.from_ref, walk.to_ref): walk for walk in inputs.walks},
        etas=index_etas(
            inputs.eta_reports,
            evaluated_at=context.evaluated_at,
            max_data_age_seconds=policy.max_data_age_seconds,
        ),
        durations=index_segment_durations(inputs.segment_observations, condition_key=inputs.sample_condition_key),
        gap_anchors={
            (anchor.pattern_id, anchor.target_stop_order, anchor.trailing_vehicle_id): anchor
            for anchor in inputs.gap_anchors
        },
    )
    recovery_options = _recovery_provider(shared, search_index, monotonic)
    deadline = monotonic() + policy.evaluation_budget_ms / 1000

    candidates = sorted(search.candidates, key=lambda item: item.static_candidate_id)
    complete: list[EvaluatedCandidate] = []
    limited: list[EvaluatedCandidate] = []
    evaluated_ids: list[str] = []
    partial_id: str | None = None
    unevaluated_ids: list[str] = []
    stop_reason: str | None = None
    for position, candidate in enumerate(candidates):
        for journey in _expand_journeys(candidate, ORIGIN_REF, shared):
            if len(complete) + len(limited) >= policy.max_timed_candidates:
                stop_reason = "max_timed_candidates"
            elif monotonic() >= deadline:
                stop_reason = "evaluation_budget"
            if stop_reason is not None:
                break
            evaluated = _evaluate_journey(journey, shared, recovery_options)
            (complete if evaluated.nominal_total_seconds is not None else limited).append(evaluated)
        if stop_reason is not None:
            partial_id = candidate.static_candidate_id
            unevaluated_ids = [item.static_candidate_id for item in candidates[position + 1:]]
            break
        evaluated_ids.append(candidate.static_candidate_id)

    return EvaluationResult(
        static_snapshot_id=search.static_snapshot_id,
        data_snapshot_id=inputs.data_snapshot_id,
        start_at=context.start_at,
        evaluated_at=context.evaluated_at,
        candidates=tuple(sorted(complete, key=lambda item: item.timed_candidate_id)),
        limited=tuple(sorted(limited, key=lambda item: item.timed_candidate_id)),
        search_coverage=search.coverage,
        coverage=EvaluationCoverage(
            truncated=stop_reason is not None,
            truncation_reasons=(stop_reason,) if stop_reason is not None else (),
            evaluated_static_candidate_ids=tuple(evaluated_ids),
            partial_static_candidate_id=partial_id,
            unevaluated_static_candidate_ids=tuple(unevaluated_ids),
            timed_combination_count=len(complete) + len(limited),
            horizon_end_at=context.start_at + timedelta(seconds=policy.search_horizon_seconds),
            horizon_excluded_option_count=sum(item.horizon_excluded_count for item in shared.ride_legs.values()),
            rejected_eta_counts=dict(shared.etas.rejected_counts),
            recovery_search_truncated=shared.recovery_search_truncated,
        ),
    )


def _ride_legs(ride: StaticRide, shared: _SharedData) -> RideLegs:
    key = (ride.pattern_id, ride.board_position, ride.alight_position)
    if key not in shared.ride_legs:
        shared.ride_legs[key] = build_ride_legs(
            ride,
            shared.etas,
            shared.durations,
            start_at=shared.context.start_at,
            queried_stop_ids=shared.inputs.queried_stop_ids,
            policy=shared.policy,
            anchors=shared.gap_anchors,
            evaluated_at=shared.context.evaluated_at,
        )
    return shared.ride_legs[key]


def _expand_journeys(candidate: StaticCandidate, origin_ref: str, shared: _SharedData) -> Iterator[PlannedJourney]:
    """첫 버스의 차량 후보마다 하차·환승 도보·다음 탑승 순서로 상태를 확장한다. 원래 상태를 변경하지 않는다."""
    rides = candidate.rides
    start_at = shared.context.start_at
    buffer_seconds = shared.policy.boarding_buffer_seconds

    def stopped(
        ride_index: int,
        legs: tuple[TimedLeg, ...],
        connections: tuple[ConnectionAssessment, ...],
        reasons: Sequence[str],
    ) -> PlannedJourney:
        return PlannedJourney(
            candidate=candidate,
            start_at=start_at,
            legs=legs,
            connections=connections,
            limited_at_ride_index=ride_index,
            reason_codes=tuple(reasons),
        )

    def finished(legs: tuple[TimedLeg, ...], connections: tuple[ConnectionAssessment, ...]) -> PlannedJourney:
        walk_seconds, walk_reason = resolve_walk_seconds(shared.walks, rides[-1].alight_stop_id, DESTINATION_REF)
        reasons = _collected_reasons(legs, connections)
        if walk_reason is not None:
            reasons.append(f"egress_{walk_reason}")
        return PlannedJourney(
            candidate=candidate,
            start_at=start_at,
            legs=legs,
            connections=connections,
            egress_walk_seconds=walk_seconds,
            limited_at_ride_index=len(rides) if walk_seconds is None else None,
            reason_codes=tuple(reasons),
        )

    def extend(
        ride_index: int,
        base_at: datetime,
        from_ref: str,
        legs: tuple[TimedLeg, ...],
        connections: tuple[ConnectionAssessment, ...],
    ) -> Iterator[PlannedJourney]:
        ride = rides[ride_index]
        walk_seconds, walk_reason = resolve_walk_seconds(shared.walks, from_ref, ride.board_stop_id)
        options = _ride_legs(ride, shared)
        common = {
            "connection_index": ride_index,
            "from_ref": from_ref,
            "to_stop_id": ride.board_stop_id,
            "base_at": base_at,
            "walk_seconds": walk_seconds,
            "walk_reason": walk_reason,
            "buffer_seconds": buffer_seconds,
        }
        if walk_seconds is None or not options.legs:
            link = assess_connection(board_eta=None, missing_eta_reasons=options.reason_codes, **common)
            next_connections = (*connections, link)
            yield stopped(ride_index, legs, next_connections, _collected_reasons(legs, next_connections))
            return

        for leg in options.legs:
            link = assess_connection(board_eta=leg.board_eta, **common)
            next_connections = (*connections, link)
            next_legs = (*legs, leg)
            if link.status == "confirmed_infeasible":
                yield stopped(ride_index, legs, next_connections, _collected_reasons(legs, next_connections))
            elif leg.alight_eta is None:
                yield stopped(ride_index, next_legs, next_connections, _collected_reasons(next_legs, next_connections))
            elif ride_index + 1 == len(rides):
                yield finished(next_legs, next_connections)
            else:
                yield from extend(
                    ride_index + 1,
                    leg.alight_eta.predicted_arrival_at,
                    ride.alight_stop_id,
                    next_legs,
                    next_connections,
                )

    yield from extend(0, start_at, origin_ref, (), ())


def _collected_reasons(legs: Sequence[TimedLeg], connections: Sequence[ConnectionAssessment]) -> list[str]:
    reasons = [reason for leg in legs for reason in leg.reason_codes]
    reasons.extend(reason for connection in connections for reason in connection.reason_codes)
    return reasons


def _recovery_provider(
    shared: _SharedData,
    search_index: SearchIndex | None,
    monotonic: Callable[[], float],
) -> RecoveryOptions:
    """실패 지점에서 이어갈 경로를 돌려주는 함수를 만든다. 다음 차량 대기와 대체 경로를 같은 기준으로 비교한다."""
    searched: dict[str, tuple[StaticCandidate, ...]] = {}
    expanded: dict[tuple[str, str], tuple[PlannedJourney, ...]] = {}

    def alternatives(stop_id: str) -> tuple[StaticCandidate, ...]:
        recovery_search = shared.policy.recovery_search
        if search_index is None or recovery_search is None:
            return ()
        if stop_id not in searched:
            result = generate_candidates(
                search_index, shared.context, recovery_search, from_stop_id=stop_id, monotonic=monotonic
            )
            shared.recovery_search_truncated = shared.recovery_search_truncated or result.coverage.truncated
            searched[stop_id] = result.candidates
        return searched[stop_id]

    def usable_journeys(candidate: StaticCandidate, stop_id: str) -> tuple[PlannedJourney, ...]:
        key = (candidate.static_candidate_id, stop_id)
        if key not in expanded:
            expanded[key] = tuple(
                journey
                for journey in _expand_journeys(candidate, stop_id, shared)
                if journey.planned_arrival_at is not None
                and all(connection.status == "feasible" for connection in journey.connections[1:])
            )
        return expanded[key]

    def options(journey: PlannedJourney, failed_ride_index: int) -> tuple[PlannedJourney, ...]:
        candidate = journey.candidate
        stop_id = candidate.rides[failed_ride_index].board_stop_id
        remainder = build_static_candidate(
            candidate.rides[failed_ride_index:],
            transfer_meters=[item.straight_line_meters for item in candidate.transfers[failed_ride_index:]],
            access_straight_line_meters=0.0,
            egress_straight_line_meters=candidate.egress_straight_line_meters,
        )
        statics = {remainder.static_candidate_id: remainder}
        for alternative in alternatives(stop_id):
            statics.setdefault(alternative.static_candidate_id, alternative)
        return tuple(
            planned
            for key in sorted(statics)
            for planned in usable_journeys(statics[key], stop_id)
        )

    return options


def _evaluate_journey(
    journey: PlannedJourney,
    shared: _SharedData,
    recovery_options: RecoveryOptions,
) -> EvaluatedCandidate:
    """시간·연결 값과 확률·기대시간을 취합한다. 각 값의 자료 요건은 서로 대신하지 않는다."""
    context, inputs, policy = shared.context, shared.inputs, shared.policy
    reasons = list(journey.reason_codes)

    transfer_probabilities: list[float | None] = []
    sample_counts: list[int] = []
    sample_rejections: list[dict[str, int]] = []
    for index in range(1, journey.candidate.transfer_count + 1):
        probability, count, rejected = _transfer_probability(journey, index, shared)
        transfer_probabilities.append(probability)
        sample_counts.append(count)
        sample_rejections.append(rejected)
    if any(value is None for value in transfer_probabilities):
        reasons.append("transfer_probability_unavailable")

    journey_probability = first_boarding_probability = expected_total_seconds = None
    comparison_key = None
    scene_ids: tuple[str, ...] = ()
    scene_rejections: dict[str, int] = {}
    if journey.connections_complete:
        selection = select_joint_scenes(
            journey, inputs.joint_scenes, condition_key=inputs.sample_condition_key, policy=policy
        )
        scene_ids = tuple(scene.scene_id for scene in selection.scenes)
        scene_rejections = dict(selection.rejected)
        journey_probability, first_boarding_probability, joint_reasons = estimate_journey_probability(
            journey, selection, policy.min_joint_scenes
        )
        reasons.extend(joint_reasons)
        if not joint_reasons:
            expected_total_seconds, expected_reasons = estimate_expected_total_seconds(
                journey, selection, recovery_options=recovery_options, policy=policy
            )
            reasons.extend(expected_reasons)
            if expected_total_seconds is not None:
                comparison_key = _comparison_key(shared)
    else:
        reasons.append("joint_evaluation_requires_complete_times")
    if expected_total_seconds is None:
        reasons.append("expected_time_unavailable")

    statuses = {connection.status for connection in journey.connections}
    infeasible_egress = any(reason == "egress_walk_no_route" for reason in journey.reason_codes)
    connection_status = _overall_connection_status(statuses, journey, infeasible_egress)
    nominally_feasible = _nominally_feasible(statuses, journey, infeasible_egress)
    time_status = _time_status(journey)
    nominal_total_seconds = (
        (journey.planned_arrival_at - journey.start_at).total_seconds()
        if time_status == "available" and nominally_feasible
        else None
    )
    egress_known = journey.egress_walk_seconds is not None
    walk_parts = [connection.walk_seconds for connection in journey.connections]

    return EvaluatedCandidate(
        static_candidate_id=journey.candidate.static_candidate_id,
        timed_candidate_id=_timed_candidate_id(journey),
        legs=journey.legs,
        connections=journey.connections,
        transfer_count=journey.candidate.transfer_count,
        egress_walk_seconds=journey.egress_walk_seconds,
        walk_seconds=_sum_if_complete([*walk_parts, journey.egress_walk_seconds], time_status),
        buffer_seconds=(
            sum(connection.buffer_seconds for connection in journey.connections) if time_status == "available" else None
        ),
        wait_seconds=_sum_if_complete([connection.wait_seconds for connection in journey.connections], time_status),
        ride_seconds=_sum_if_complete([leg.ride_seconds for leg in journey.legs], time_status),
        planned_arrival_at=(journey.planned_arrival_at if nominally_feasible and egress_known else None),
        nominal_total_seconds=nominal_total_seconds,
        expected_total_seconds=expected_total_seconds,
        first_boarding_probability=first_boarding_probability,
        transfer_probabilities=tuple(transfer_probabilities),
        journey_probability=journey_probability,
        time_status=time_status,
        nominally_feasible=nominally_feasible,
        connection_status=connection_status,
        limited_at_ride_index=journey.limited_at_ride_index,
        expected_comparison_key=comparison_key,
        reason_codes=tuple(sorted(set(reasons))),
        provenance=CandidateProvenance(
            data_snapshot_id=inputs.data_snapshot_id,
            start_at=context.start_at,
            evaluated_at=context.evaluated_at,
            timing_policy_version=policy.timing_policy_version,
            sample_policy_version=policy.sample_policy_version,
            recovery_policy_version=policy.recovery_policy_version,
            transfer_sample_counts=tuple(sample_counts),
            transfer_sample_rejections=tuple(sample_rejections),
            joint_scene_ids=scene_ids,
            joint_scene_rejections=scene_rejections,
            expected_scenario_count=len(scene_ids) if expected_total_seconds is not None else None,
        ),
    )


def _transfer_probability(
    journey: PlannedJourney,
    transfer_index: int,
    shared: _SharedData,
) -> tuple[float | None, int, dict[str, int]]:
    """환승 순번의 개별 확률. 기준 여유와 두 차량의 ETA가 확인되지 않으면 None이다."""
    if len(journey.connections) <= transfer_index or len(journey.legs) <= transfer_index:
        return None, 0, {}
    connection = journey.connections[transfer_index]
    front_eta = journey.legs[transfer_index - 1].alight_eta
    if connection.slack_seconds is None or front_eta is None:
        return None, 0, {}
    pairs, rejected = select_error_pairs(
        shared.inputs.transfer_error_samples,
        front_eta=front_eta,
        next_eta=journey.legs[transfer_index].board_eta,
        condition_key=shared.inputs.sample_condition_key,
        policy=shared.policy,
    )
    probability = estimate_transfer_probability(
        connection.slack_seconds, pairs, shared.policy.min_probability_samples
    )
    return probability, len(pairs), rejected


def _overall_connection_status(statuses: set[str], journey: PlannedJourney, infeasible_egress: bool) -> str:
    if "confirmed_infeasible" in statuses or infeasible_egress:
        return "confirmed_infeasible"
    if "negative_slack" in statuses:
        return "negative_slack"
    if "time_unknown" in statuses or not journey.connections_complete:
        return "time_unknown"
    return "feasible"


def _nominally_feasible(statuses: set[str], journey: PlannedJourney, infeasible_egress: bool) -> bool | None:
    """첫 승차와 모든 환승을 기준 ETA로 마칠 수 있는지. 음수 여유·확인된 실행 불가는 false, 미확인은 null이다."""
    if "confirmed_infeasible" in statuses or "negative_slack" in statuses or infeasible_egress:
        return False
    if not journey.connections_complete:
        return None
    return True


def _time_status(journey: PlannedJourney) -> str:
    if journey.planned_arrival_at is not None:
        return "available"
    if journey.legs or any(connection.ready_at is not None for connection in journey.connections):
        return "partial"
    return "unavailable"


def _sum_if_complete(values: Sequence[float | None], time_status: str) -> float | None:
    if time_status != "available" or any(value is None for value in values):
        return None
    return sum(values)


def _comparison_key(shared: _SharedData) -> str:
    policy = shared.policy
    return "|".join(
        (
            shared.inputs.data_snapshot_id,
            shared.inputs.sample_condition_key,
            shared.context.start_at.isoformat(),
            policy.timing_policy_version,
            policy.sample_policy_version,
            policy.recovery_policy_version,
        )
    )


def _timed_candidate_id(journey: PlannedJourney) -> str:
    """정적 경로 ID와 차량·시각 조합으로 안정적인 식별자를 만든다."""
    payload = json.dumps(
        [
            journey.candidate.static_candidate_id,
            journey.limited_at_ride_index,
            [
                [
                    leg.pattern_id,
                    leg.vehicle_rank,
                    leg.vehicle_id,
                    leg.run_key,
                    leg.board_eta.predicted_arrival_at.timestamp(),
                ]
                for leg in journey.legs
            ],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
