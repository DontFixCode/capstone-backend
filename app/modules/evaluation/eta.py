"""차량별 절대 ETA 구성과 관측 기반 하차 ETA 보완을 담당한다.

하차 ETA는 같은 차량·회차의 제공 ETA → 적격 관측 구간 소요시간 → 앞차 ETA 기반 근사(Phase 3B) 순으로 구성하고,
근거가 없으면 None과 이유를 반환한다. 거리 비례 추정은 시간·거리 단위와 운행 범위를 검증하기 전이므로 구현하지 않고
비활성으로 둔다. 승차 정류소에 앞차만 나오면 같은 근사로 그 뒤 차량의 승차 ETA를 차량 옵션에 더한다.
다음 차량이 아직 출발하지 않았으면(대전시 MSG_TP=07) 직전 차량 시각 + 배차간격으로 예상 차량을 옵션에 더한다
(headway_estimate, 검증 전, 순위는 실시간 후보 뒤).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from app.modules.evaluation.schemas import (
    EtaReport,
    EvaluationPolicy,
    GapAnchor,
    RideLegs,
    SegmentObservation,
    TimedLeg,
    VehicleEta,
)
from app.modules.routing.schemas import StaticRide

SegmentDurations = Mapping[tuple[str, int], tuple[float, ...]]
# (pattern_id, 목표 stop_order, 후행 차량 B) → 앞차 ETA 기반 근사의 차량 조합.
GapAnchors = Mapping[tuple[str, int, str], GapAnchor]


@dataclass(frozen=True)
class NotDeparted:
    """정류소 응답이 '다음 차량 출발 전'일 때의 직전 차량 통과 기록(대전시 MSG_TP=07, 검증 전)."""

    vehicle_id: str | None  # 이 정류소를 마지막으로 지난 차량
    passed_at: datetime     # 그 차량이 지난 시각(응답의 정보 제공 시각)
    collected_at: datetime
    source_ref: str | None = None


@dataclass(frozen=True)
class EtaTable:
    """정류소 경유 위치별 차량 ETA와 사용하지 못한 응답의 이유. 같은 응답을 여러 경로가 공유한다."""

    # (pattern_id, stop_order, stop_id) → 도착 예상 순서의 ETA.
    by_position: Mapping[tuple[str, int, str], tuple[VehicleEta, ...]]
    rejected_by_stop: Mapping[tuple[str, str], frozenset[str]]
    rejected_counts: Mapping[str, int]
    # (pattern_id, stop_order, stop_id) → 다음 차량이 아직 출발하지 않은 정류소의 직전 차량 통과 기록.
    not_departed: Mapping[tuple[str, int, str], NotDeparted] = field(default_factory=dict)


def build_absolute_eta(
    report: EtaReport,
    *,
    evaluated_at: datetime,
    max_data_age_seconds: float,
) -> tuple[VehicleEta | None, str | None]:
    """확인된 기준시각에 잔여시간을 더해 절대 ETA를 구성한다.

    재평가 시점의 현재 시각에 잔여시간을 다시 더하지 않으므로 같은 응답은 언제 평가해도 같은 ETA가 된다.
    기준시각이 확인되지 않았으면 수집 시각으로 대신하지 않고 이유를 반환한다.
    """
    if report.stop_order is None:
        return None, "eta_position_unconfirmed"
    if report.basis_at is None:
        return None, "eta_basis_unconfirmed"
    if report.remaining_seconds is None:
        return None, "eta_remaining_missing"
    age_seconds = (evaluated_at - report.basis_at).total_seconds()
    if age_seconds < 0:
        return None, "eta_basis_after_evaluation"
    if age_seconds > max_data_age_seconds:
        return None, "eta_stale"
    return (
        VehicleEta(
            pattern_id=report.pattern_id,
            stop_id=report.stop_id,
            stop_order=report.stop_order,
            vehicle_id=report.vehicle_id,
            run_key=report.run_key,
            predicted_arrival_at=report.basis_at + timedelta(seconds=report.remaining_seconds),
            eta_source="provider",
            model_version=None,
            horizon_seconds=report.remaining_seconds,
            basis_at=report.basis_at,
            source_generated_at=report.source_generated_at,
            collected_at=report.collected_at,
            observed_passed_at=report.observed_passed_at,
            source_ref=report.source_ref,
            basis_source=report.basis_source,
            stops_away=report.stops_away,
        ),
        None,
    )


def index_etas(reports: Iterable[EtaReport], *, evaluated_at: datetime, max_data_age_seconds: float) -> EtaTable:
    """응답을 절대 ETA로 바꿔 경유 위치별로 묶는다. 같은 차량·회차의 중복 응답은 가장 새로운 기준을 남긴다.

    '다음 차량 출발 전' 응답(not_departed)은 ETA로 만들지 않고 직전 차량 통과 기록으로 남긴다. 이 응답의 기준시각은
    직전 차량이 지난 시각이라 오래될 수 있으므로 최신성은 수집 시각으로 본다.
    """
    latest: dict[tuple[str, int, str], dict[tuple, VehicleEta]] = {}
    rejected_by_stop: dict[tuple[str, str], set[str]] = defaultdict(set)
    rejected_counts: Counter[str] = Counter()
    not_departed: dict[tuple[str, int, str], NotDeparted] = {}
    for report in reports:
        if report.not_departed:
            reason = _not_departed_rejection(report, evaluated_at, max_data_age_seconds)
            if reason is not None:
                rejected_by_stop[(report.pattern_id, report.stop_id)].add(reason)
                rejected_counts[reason] += 1
                continue
            key = (report.pattern_id, report.stop_order, report.stop_id)
            if key not in not_departed or report.collected_at > not_departed[key].collected_at:
                not_departed[key] = NotDeparted(report.last_passed_vehicle_id, report.basis_at, report.collected_at,
                                                report.source_ref)
            continue
        eta, reason = build_absolute_eta(report, evaluated_at=evaluated_at, max_data_age_seconds=max_data_age_seconds)
        if eta is None:
            rejected_by_stop[(report.pattern_id, report.stop_id)].add(reason)
            rejected_counts[reason] += 1
            continue
        # 차량번호가 없는 응답에는 임의 번호를 붙이지 않고 완전히 같은 ETA만 하나로 합친다.
        identity = (
            (eta.vehicle_id, eta.run_key)
            if eta.vehicle_id is not None or eta.run_key is not None
            else (None, None, eta.predicted_arrival_at)
        )
        bucket = latest.setdefault((eta.pattern_id, eta.stop_order, eta.stop_id), {})
        current = bucket.get(identity)
        if current is None or (eta.basis_at, eta.predicted_arrival_at) > (
            current.basis_at,
            current.predicted_arrival_at,
        ):
            bucket[identity] = eta
    return EtaTable(
        by_position={
            key: tuple(sorted(bucket.values(), key=_eta_order))
            for key, bucket in latest.items()
        },
        rejected_by_stop={key: frozenset(reasons) for key, reasons in rejected_by_stop.items()},
        rejected_counts=dict(rejected_counts),
        not_departed=not_departed,
    )


def _not_departed_rejection(report: EtaReport, evaluated_at: datetime, max_data_age_seconds: float) -> str | None:
    if report.stop_order is None:
        return "eta_position_unconfirmed"
    if report.basis_at is None:
        return "eta_basis_unconfirmed"
    if report.basis_at > evaluated_at:
        return "eta_basis_after_evaluation"
    if (evaluated_at - report.collected_at).total_seconds() > max_data_age_seconds:
        return "eta_stale"
    return None


def index_segment_durations(
    observations: Iterable[SegmentObservation],
    *,
    condition_key: str,
) -> dict[tuple[str, int], tuple[float, ...]]:
    """적격 관측의 인접 구간 소요시간을 (pattern_id, 앞 stop_order)별로 모은다.

    같은 운행의 인접 위치에서 도착으로 확인된 두 이벤트만 사용한다. 도착·통과·출발을 섞거나 다른 회차를 잇지
    않으며, 한 운행의 같은 구간은 한 번만 센다.
    """
    by_run: dict[tuple[str, int], dict[str, float]] = {}
    for item in observations:
        if item.condition_key != condition_key or item.run_key is None:
            continue
        if item.to_stop_order != item.from_stop_order + 1:
            continue
        started_at = item.from_event.confirmed_arrival_at
        ended_at = item.to_event.confirmed_arrival_at
        if started_at is None or ended_at is None or ended_at <= started_at:
            continue
        by_run.setdefault((item.pattern_id, item.from_stop_order), {}).setdefault(
            item.run_key, (ended_at - started_at).total_seconds()
        )
    return {key: tuple(sorted(runs.values())) for key, runs in by_run.items()}


def observed_segment_seconds(
    pattern_id: str,
    board_position: int,
    alight_position: int,
    durations: SegmentDurations,
    min_segment_samples: int,
) -> tuple[float | None, tuple[int, ...], str | None]:
    """승차 위치부터 하차 위치까지 인접 구간 소요시간 중앙값을 더한다. 한 구간이라도 부족하면 합을 완성하지 않는다."""
    total = 0.0
    counts: list[int] = []
    for stop_order in range(board_position, alight_position):
        samples = durations.get((pattern_id, stop_order), ())
        if len(samples) < min_segment_samples:
            return None, tuple(counts), "segment_samples_insufficient"
        total += median(samples)
        counts.append(len(samples))
    return total, tuple(counts), None


def resolve_alight_eta(
    ride: StaticRide,
    board_eta: VehicleEta,
    table: EtaTable,
    durations: SegmentDurations,
    policy: EvaluationPolicy,
    anchors: GapAnchors | None = None,
    evaluated_at: datetime | None = None,
) -> tuple[VehicleEta | None, tuple[str, ...]]:
    """하차 정류소 ETA를 구성한다. 사용하지 못한 근거의 이유는 보완에 성공해도 함께 반환한다."""
    provider_eta, provider_reason = _provider_alight_eta(ride, board_eta, table)
    if provider_eta is not None:
        # 회차 키 없이 같은 차량번호·진행 일관성으로 이은 경우 그 사실을 남긴다(검증 전 연결).
        return provider_eta, ((ALIGHT_LINKED_WITHOUT_RUN_KEY,) if board_eta.run_key is None else ())

    reasons = {provider_reason, *table.rejected_by_stop.get((ride.pattern_id, ride.alight_stop_id), ())}
    seconds, counts, segment_reason = observed_segment_seconds(
        ride.pattern_id, ride.board_position, ride.alight_position, durations, policy.min_segment_samples
    )
    if seconds is None:
        reasons.add(segment_reason)
        if (ride.pattern_id, ride.alight_position, board_eta.vehicle_id) not in (anchors or {}) or evaluated_at is None:
            return None, tuple(sorted(reasons))
        gap_eta, gap_reason = leading_vehicle_gap_eta(ride, board_eta, table, anchors, evaluated_at)
        reasons.add(ALIGHT_ETA_BY_LEADING_VEHICLE_GAP if gap_eta is not None else gap_reason)
        return gap_eta, tuple(sorted(reasons))

    return (
        VehicleEta(
            pattern_id=ride.pattern_id,
            stop_id=ride.alight_stop_id,
            stop_order=ride.alight_position,
            vehicle_id=board_eta.vehicle_id,
            run_key=board_eta.run_key,
            predicted_arrival_at=board_eta.predicted_arrival_at + timedelta(seconds=seconds),
            eta_source="historical_segments",
            model_version=policy.segment_model_version,
            horizon_seconds=board_eta.horizon_seconds + seconds,
            basis_at=board_eta.basis_at,
            source_generated_at=board_eta.source_generated_at,
            collected_at=board_eta.collected_at,
            source_ref=board_eta.source_ref,
            segment_sample_counts=counts,
            basis_source=board_eta.basis_source,
        ),
        tuple(sorted(reasons)),
    )


def build_ride_legs(
    ride: StaticRide,
    table: EtaTable,
    durations: SegmentDurations,
    *,
    start_at: datetime,
    queried_stop_ids: frozenset[str],
    policy: EvaluationPolicy,
    anchors: GapAnchors | None = None,
    evaluated_at: datetime | None = None,
    headway_seconds: float | None = None,
    rear_vehicle_ids: frozenset[str] = frozenset(),
) -> RideLegs:
    """정적 탑승 구간의 차량 옵션을 도착 예상 순서로 구성한다. 응답에 없는 차량을 운행 없음으로 해석하지 않는다.

    승차 정류소 응답에 없는 뒤 차량은 앞차 ETA 기반 근사로 만든 승차 ETA를 옵션에 더하고(following_board_etas), 그
    차량의 하차 ETA를 다른 근거로 만들지 못하면 바로 앞 차량의 하차 ETA에 같은 간격을 더한다(_alight_behind_ahead).
    다음 차량이 아직 출발하지 않았고 배차간격(headway_seconds)이 있으면 예상 차량을 뒤에 더한다(headway_legs).
    """
    target = (ride.pattern_id, ride.board_position, ride.board_stop_id)
    listed = table.by_position.get(target, ())
    following, following_reasons = (
        following_board_etas(ride, table, anchors, evaluated_at) if anchors and evaluated_at is not None else ((), ())
    )
    ahead_of = {anchor.trailing_vehicle_id: anchor.leading_vehicle_id for anchor in (anchors or {}).values()
                if (anchor.pattern_id, anchor.target_stop_order, anchor.target_stop_id) == target}
    board_etas = tuple(sorted((*listed, *following), key=_eta_order))
    horizon_end_at = start_at + timedelta(seconds=policy.search_horizon_seconds)
    legs: list[TimedLeg] = []
    by_vehicle: dict[str, TimedLeg] = {}
    horizon_excluded = 0
    for rank, board_eta in enumerate(board_etas):
        if board_eta.predicted_arrival_at > horizon_end_at:
            horizon_excluded += 1
            continue
        alight_eta, leg_reasons = resolve_alight_eta(ride, board_eta, table, durations, policy, anchors, evaluated_at)
        if board_eta.eta_source == "leading_vehicle_gap":
            added = {BOARD_ETA_BY_LEADING_VEHICLE_GAP}
            ahead = by_vehicle.get(ahead_of.get(board_eta.vehicle_id, ""))
            if alight_eta is None and ahead is not None and evaluated_at is not None:
                alight_eta = _alight_behind_ahead(ride, board_eta, ahead, evaluated_at)
                if alight_eta is not None:
                    added.add(ALIGHT_ETA_BY_LEADING_VEHICLE_GAP)
            leg_reasons = tuple(sorted({*leg_reasons, *added}))
        legs.append(
            TimedLeg(
                route_id=ride.route_id,
                pattern_id=ride.pattern_id,
                direction=ride.direction,
                vehicle_id=board_eta.vehicle_id,
                run_key=board_eta.run_key,
                vehicle_rank=rank,
                board_stop_id=ride.board_stop_id,
                board_position=ride.board_position,
                alight_stop_id=ride.alight_stop_id,
                alight_position=ride.alight_position,
                board_eta=board_eta,
                alight_eta=alight_eta,
                ride_seconds=(
                    (alight_eta.predicted_arrival_at - board_eta.predicted_arrival_at).total_seconds()
                    if alight_eta is not None
                    else None
                ),
                reason_codes=leg_reasons,
            )
        )
        if board_eta.vehicle_id is not None:
            by_vehicle[board_eta.vehicle_id] = legs[-1]

    reasons: set[str] = set(following_reasons)
    if target in table.not_departed:
        reasons.add(NEXT_VEHICLE_NOT_DEPARTED)
    if headway_seconds and evaluated_at is not None:
        legs.extend(headway_legs(ride, table, legs, anchors or {}, evaluated_at, headway_seconds,
                                 first_rank=len(board_etas), horizon_end_at=horizon_end_at,
                                 rear_vehicle_ids=rear_vehicle_ids))
    if not legs and not horizon_excluded:
        reasons.add(
            "boarding_eta_absent" if ride.board_stop_id in queried_stop_ids else "boarding_stop_not_queried"
        )
        reasons.update(table.rejected_by_stop.get((ride.pattern_id, ride.board_stop_id), ()))
    if horizon_excluded:
        reasons.add("vehicles_beyond_search_horizon")
    return RideLegs(legs=tuple(legs), reason_codes=tuple(sorted(reasons)), horizon_excluded_count=horizon_excluded)


def _eta_order(eta: VehicleEta) -> tuple:
    return eta.predicted_arrival_at, eta.vehicle_id or "", eta.run_key or ""


ALIGHT_LINKED_WITHOUT_RUN_KEY = "alight_linked_by_vehicle_without_run_key"
ALIGHT_ETA_BY_LEADING_VEHICLE_GAP = "alight_eta_by_leading_vehicle_gap"
BOARD_ETA_BY_LEADING_VEHICLE_GAP = "board_eta_by_leading_vehicle_gap"
# A의 X 통과시각 근거가 없어 경과분을 0으로 둔 근사(MVP 대체). 보정한 근사와 표본을 섞지 않도록 모델 버전에 남긴다.
LEADING_VEHICLE_GAP_MODEL_VERSION = "leading-vehicle-gap/1;passage-uncorrected"

NEXT_VEHICLE_NOT_DEPARTED = "next_vehicle_not_departed"
BOARD_ETA_BY_HEADWAY_ESTIMATE = "board_eta_by_headway_estimate"
ALIGHT_ETA_BY_HEADWAY_ESTIMATE = "alight_eta_by_headway_estimate"
HEADWAY_REFERENCE_ALIGHT_ABSENT = "headway_reference_alight_absent"
HEADWAY_ESTIMATE_MODEL_VERSION = "headway-estimate/1"
# 직전 차량 뒤로 이어 붙이는 예상 차량 수. 뒤로 갈수록 배차 오차가 쌓이므로 제한한다.
HEADWAY_MAX_VEHICLES = 3


def headway_legs(
    ride: StaticRide,
    table: EtaTable,
    legs: list[TimedLeg],
    anchors: GapAnchors,
    evaluated_at: datetime,
    headway_seconds: float,
    *,
    first_rank: int,
    horizon_end_at: datetime,
    rear_vehicle_ids: frozenset[str] = frozenset(),
) -> list[TimedLeg]:
    """아직 출발하지 않은 다음 차량들의 예상 옵션(검증 전). k번째 예상 차량 = 직전 차량 시각 + k × 배차간격.

    직전 차량(기준 차량)은 실제로 운행 중인 마지막 차량이다.
    - 승차 정류소 응답이 '다음 차량 출발 전'이면 그 정류소를 마지막으로 지난 차량이고, 승차 기준 시각은 그 통과 시각이다.
    - 승차 정류소에 오는 차량이 있으면 그중 마지막 차량(legs의 가장 늦은 차량)이고, 그 차량이 지나온 상류 정류소 응답이
      '다음 차량 출발 전·직전 차량 = 그 차량'일 때만 쓴다(그 뒤에 운행 중인 차량이 없다는 근거). 기점 정류소 응답처럼
      직전 차량번호가 없으면, 노선 위치로 그 차량이 맨 뒤 차량임을 확인했을 때(rear_vehicle_ids)만 근거로 본다.
    하차 시각도 기준 차량의 하차 정류소 시각에 같은 간격을 더한다(탑승 시간은 기준 차량과 같다). 평가 시각보다 이른 예상은
    만들지 않는다. 배차간격은 실제 출발 간격과 수 분 차이 날 수 있어 실시간 예측과 구분한다(eta_source=headway_estimate).
    """
    reference = _headway_reference(ride, table, legs, anchors, evaluated_at, rear_vehicle_ids)
    if reference is None:
        return []
    board_ref, alight_ref, collected_at, source_ref = reference
    if alight_ref is not None and alight_ref <= board_ref:
        alight_ref = None

    def eta(stop_id: str, stop_order: int, at: datetime, count: int) -> VehicleEta:
        return VehicleEta(
            pattern_id=ride.pattern_id, stop_id=stop_id, stop_order=stop_order, vehicle_id=None, run_key=None,
            predicted_arrival_at=at, eta_source="headway_estimate", model_version=HEADWAY_ESTIMATE_MODEL_VERSION,
            horizon_seconds=(at - evaluated_at).total_seconds(), basis_at=evaluated_at, source_generated_at=None,
            collected_at=collected_at,
            source_ref=f"headway_estimate|ref:{source_ref}|headway_seconds:{headway_seconds:g}|vehicles_after:{count}",
        )

    result: list[TimedLeg] = []
    for count in range(1, HEADWAY_MAX_VEHICLES + 1):
        shift = timedelta(seconds=headway_seconds * count)
        board_at = board_ref + shift
        if board_at < evaluated_at:
            continue
        if board_at > horizon_end_at:
            break
        board_eta = eta(ride.board_stop_id, ride.board_position, board_at, count)
        alight_eta = (None if alight_ref is None
                      else eta(ride.alight_stop_id, ride.alight_position, alight_ref + shift, count))
        result.append(TimedLeg(
            route_id=ride.route_id, pattern_id=ride.pattern_id, direction=ride.direction, vehicle_id=None,
            run_key=None, vehicle_rank=first_rank + len(result), board_stop_id=ride.board_stop_id,
            board_position=ride.board_position, alight_stop_id=ride.alight_stop_id,
            alight_position=ride.alight_position, board_eta=board_eta, alight_eta=alight_eta,
            ride_seconds=None if alight_ref is None else (alight_ref - board_ref).total_seconds(),
            reason_codes=((BOARD_ETA_BY_HEADWAY_ESTIMATE, HEADWAY_REFERENCE_ALIGHT_ABSENT) if alight_eta is None
                          else (ALIGHT_ETA_BY_HEADWAY_ESTIMATE, BOARD_ETA_BY_HEADWAY_ESTIMATE)),
        ))
    return result


def _headway_reference(
    ride: StaticRide,
    table: EtaTable,
    legs: list[TimedLeg],
    anchors: GapAnchors,
    evaluated_at: datetime,
    rear_vehicle_ids: frozenset[str] = frozenset(),
) -> tuple[datetime, datetime | None, datetime, str | None] | None:
    """배차간격 예상의 기준 차량: (승차 정류소 시각, 하차 정류소 시각, 근거 수집 시각, 근거 참조). 근거가 없으면 None."""
    mark = table.not_departed.get((ride.pattern_id, ride.board_position, ride.board_stop_id))
    if mark is not None:
        return mark.passed_at, _reference_alight_at(ride, mark.vehicle_id, table, anchors, evaluated_at), \
            mark.collected_at, mark.source_ref
    if not legs:
        return None
    last = max(legs, key=lambda leg: leg.board_eta.predicted_arrival_at)
    if last.vehicle_id is None:
        return None
    rear_confirmed = last.vehicle_id in rear_vehicle_ids
    evidence = [found for (pattern_id, stop_order, _), found in sorted(table.not_departed.items())
                if pattern_id == ride.pattern_id and stop_order < ride.board_position
                and (found.vehicle_id == last.vehicle_id or (found.vehicle_id is None and rear_confirmed))]
    if not evidence:
        return None
    return (last.board_eta.predicted_arrival_at,
            None if last.alight_eta is None else last.alight_eta.predicted_arrival_at,
            max(last.board_eta.collected_at, evidence[-1].collected_at),
            f"{last.board_eta.source_ref}|not_departed:{evidence[-1].source_ref}")


def _reference_alight_at(
    ride: StaticRide,
    vehicle_id: str | None,
    table: EtaTable,
    anchors: GapAnchors,
    evaluated_at: datetime,
) -> datetime | None:
    """승차 정류소를 이미 지난 기준 차량의 하차 정류소 시각. 이미 지났으면 통과 시각, 아니면 제공 ETA나 앞차 근사."""
    target = (ride.pattern_id, ride.alight_position, ride.alight_stop_id)
    mark = table.not_departed.get(target)
    if mark is not None:
        same = vehicle_id is None or mark.vehicle_id is None or mark.vehicle_id == vehicle_id
        return mark.passed_at if same else None
    if vehicle_id is None:
        return None
    for eta in table.by_position.get(target, ()):
        if eta.vehicle_id == vehicle_id:
            return eta.predicted_arrival_at
    chained, _ = gap_chain_etas(ride.pattern_id, ride.alight_stop_id, ride.alight_position, table, anchors,
                                evaluated_at)
    return chained[vehicle_id].predicted_arrival_at if vehicle_id in chained else None


def leading_vehicle_gap_eta(
    ride: StaticRide,
    board_eta: VehicleEta,
    table: EtaTable,
    anchors: GapAnchors,
    evaluated_at: datetime,
) -> tuple[VehicleEta | None, str | None]:
    """앞차 ETA 기반 후행 차량 하차 ETA(Phase 3B, 검증 전). R_B(D;t0) = R_A(D;t0) + R_B(X;t0) + Δ_A(X).

    A는 하차 정류소 D에서 B 바로 앞 차량, X는 A가 마지막으로 통과한 경유 위치, B는 탈 차량(board_eta)이다. A는 D의 선두
    차량이거나, 선두 차량부터 같은 방식으로 이은 차량이다(gap_chain_etas). X의 도착 목록에서 B가 가장 먼저 도착할 차량이고
    진행 위치가 승차 예측과 맞을 때만 쓴다. A의 X 통과시각 근거(동일 차량·경유 위치의 검증된 이벤트)가 없으므로
    Δ_A(X)=0(MVP 대체)이다.
    """
    anchor = anchors[(ride.pattern_id, ride.alight_position, board_eta.vehicle_id)]
    x = _trailing_at_anchor(ride.pattern_id, anchor, table)
    if x is not None and (x.run_key != board_eta.run_key or not _same_progress(
            board_eta.stops_away, x.stops_away, anchor.anchor_stop_order - ride.board_position,
            relaxed=anchor.order_confirmed)):
        return None, "leading_gap_progress_mismatch" if x.run_key == board_eta.run_key else "leading_gap_trailing_eta_absent"
    chained, failures = gap_chain_etas(ride.pattern_id, ride.alight_stop_id, ride.alight_position, table, anchors,
                                       evaluated_at)
    eta = chained.get(board_eta.vehicle_id)
    if eta is None:
        return None, failures.get(board_eta.vehicle_id, "leading_gap_trailing_eta_absent")
    if eta.predicted_arrival_at <= board_eta.predicted_arrival_at:
        return None, "leading_gap_order_inconsistent"
    return eta.model_copy(update={"run_key": board_eta.run_key, "stops_away": None}), None


def following_board_etas(
    ride: StaticRide,
    table: EtaTable,
    anchors: GapAnchors,
    evaluated_at: datetime,
) -> tuple[tuple[VehicleEta, ...], tuple[str, ...]]:
    """승차 정류소 D에 앞차만 나올 때 그 뒤 차량들의 승차 ETA(Phase 3B, 검증 전, gap_chain_etas를 승차 정류소에 적용).

    차량 조합은 검색이 노선 위치로 고른다(workflows.search.leading_gap_anchors). 다음 차를 놓치면 그 뒤 차량을 같은
    방식으로 잇는다(B1은 선두 차량 기준, B2는 B1 기준). 만들지 못한 이유를 함께 돌려준다.
    """
    chained, failures = gap_chain_etas(ride.pattern_id, ride.board_stop_id, ride.board_position, table, anchors,
                                       evaluated_at)
    return tuple(chained.values()), tuple(sorted(set(failures.values())))


def gap_chain_etas(
    pattern_id: str,
    stop_id: str,
    stop_order: int,
    table: EtaTable,
    anchors: GapAnchors,
    evaluated_at: datetime,
) -> tuple[dict[str, VehicleEta], dict[str, str]]:
    """목표 정류소 D에서 앞차 ETA 기반 근사로 이은 차량별 ETA. T_B(D) = t0 + R_A(D;t0) + R_B(X;t0).

    D 응답에 나온 차량에서 시작해, 앞 차량 A의 D ETA가 있는 조합마다 바로 뒤 차량 B를 잇는다(A는 응답에 나온 차량이거나
    이미 이은 차량). X의 도착 목록에서 B가 가장 먼저 도착할 차량일 때만 만들고, D 응답에 이미 나온 차량은 만들지 않는다.
    만든 ETA(응답에 나온 차량 제외)와 만들지 못한 차량별 이유를 돌려준다.
    """
    target = (pattern_id, stop_order, stop_id)
    known = {eta.vehicle_id: eta for eta in table.by_position.get(target, ()) if eta.vehicle_id is not None}
    listed = set(known)
    pending = [anchor for _, anchor in sorted(anchors.items())
               if (anchor.pattern_id, anchor.target_stop_order, anchor.target_stop_id) == target
               and anchor.trailing_vehicle_id not in known]
    failures: dict[str, str] = {}
    while ready := [anchor for anchor in pending if anchor.leading_vehicle_id in known]:
        for anchor in ready:
            pending.remove(anchor)
            lead, x = known[anchor.leading_vehicle_id], _trailing_at_anchor(pattern_id, anchor, table)
            remaining = None if x is None else _leading_gap_remaining(lead, x, evaluated_at,
                                                                      order_confirmed=anchor.order_confirmed)
            if x is None or remaining is None:
                failures[anchor.trailing_vehicle_id] = ("leading_gap_trailing_eta_absent" if x is None
                                                        else "leading_gap_eta_before_evaluation")
                continue
            hops = stop_order - anchor.anchor_stop_order
            known[anchor.trailing_vehicle_id] = _leading_gap_vehicle_eta(
                pattern_id, stop_id, stop_order, anchor.trailing_vehicle_id, x.run_key, lead, x, anchor, remaining,
                evaluated_at, stops_away=None if x.stops_away is None else x.stops_away + hops)
    failures.update((anchor.trailing_vehicle_id, "leading_gap_leading_eta_absent") for anchor in pending)
    return {vehicle: eta for vehicle, eta in known.items() if vehicle not in listed}, failures


def _same_progress(before: int | None, after: int | None, hops: int, *, relaxed: bool) -> bool:
    """같은 차량의 두 정류소 남은 정류장 수(대전시 STATUS_POS)가 같은 운행으로 볼 만큼 맞는지.

    엄격하게는 차이가 경유 순번 차이(hops)와 같아야 한다. 대전시 정류장 수와 노선 경유 순번은 정류소 구성이 달라 어긋날 수
    있으므로, relaxed(노선 위치로 순서 확인 등)이면 진행 방향(차이의 부호)만 맞으면 된다. 값이 없으면 확인하지 않는다.
    """
    if before is None or after is None:
        return True
    difference = after - before
    if not relaxed:
        return difference == hops
    return (difference > 0) == (hops > 0) and (difference < 0) == (hops < 0)


def _alight_behind_ahead(
    ride: StaticRide,
    board_eta: VehicleEta,
    ahead: TimedLeg,
    evaluated_at: datetime,
) -> VehicleEta | None:
    """뒤 차량 B의 하차 ETA = 바로 앞 차량 A의 하차 ETA + R_B(X)(Phase 3B를 같은 X로 하차 정류소에 적용, 검증 전).

    B의 승차 근사가 T_B(D) = T_A(D) + R_B(X)이므로 하차 정류소 D'에서도 T_B(D') = T_A(D') + R_B(X)다. A의 하차 ETA가
    없거나 평가 시각보다 이르면 만들지 않는다.
    """
    if ahead.alight_eta is None:
        return None
    predicted = ahead.alight_eta.predicted_arrival_at + (board_eta.predicted_arrival_at
                                                         - ahead.board_eta.predicted_arrival_at)
    remaining = (predicted - evaluated_at).total_seconds()
    if remaining < 0 or predicted <= board_eta.predicted_arrival_at:
        return None
    return VehicleEta(
        pattern_id=ride.pattern_id,
        stop_id=ride.alight_stop_id,
        stop_order=ride.alight_position,
        vehicle_id=board_eta.vehicle_id,
        run_key=board_eta.run_key,
        predicted_arrival_at=predicted,
        eta_source="leading_vehicle_gap",
        model_version=LEADING_VEHICLE_GAP_MODEL_VERSION,
        horizon_seconds=remaining,
        basis_at=evaluated_at,
        source_generated_at=None,
        collected_at=max(board_eta.collected_at, ahead.alight_eta.collected_at),
        source_ref=f"leading_vehicle_gap|ahead_alight:{ahead.alight_eta.source_ref}|board:{board_eta.source_ref}",
        basis_source=board_eta.basis_source if board_eta.basis_source == ahead.alight_eta.basis_source else None,
    )


def _trailing_at_anchor(pattern_id: str, anchor: GapAnchor, table: EtaTable) -> VehicleEta | None:
    """기준점 X에 가장 먼저 도착할 차량이 B이면 그 예측."""
    at_anchor = table.by_position.get((pattern_id, anchor.anchor_stop_order, anchor.anchor_stop_id), ())
    return at_anchor[0] if at_anchor and at_anchor[0].vehicle_id == anchor.trailing_vehicle_id else None


def _leading_gap_remaining(a: VehicleEta, x: VehicleEta, evaluated_at: datetime, *,
                           order_confirmed: bool = False) -> float | None:
    """R_A(D;t0) + R_B(X;t0). 평가 시각에 어느 예상 도착이 이미 지났으면 None.

    order_confirmed(노선 위치로 A가 D를, B가 X를 아직 지나지 않음을 확인)이면 지난 예측을 지금 도착 중으로 보고 0으로 둔다.
    """
    leading_remaining = (a.predicted_arrival_at - evaluated_at).total_seconds()
    trailing_remaining = (x.predicted_arrival_at - evaluated_at).total_seconds()
    if order_confirmed:
        leading_remaining, trailing_remaining = max(leading_remaining, 0.0), max(trailing_remaining, 0.0)
    if leading_remaining < 0 or trailing_remaining < 0:
        return None
    return leading_remaining + trailing_remaining


def _leading_gap_vehicle_eta(
    pattern_id: str,
    stop_id: str,
    stop_order: int,
    vehicle_id: str | None,
    run_key: str | None,
    a: VehicleEta,
    x: VehicleEta,
    anchor: GapAnchor,
    remaining: float,
    evaluated_at: datetime,
    stops_away: int | None = None,
) -> VehicleEta:
    return VehicleEta(
        pattern_id=pattern_id,
        stop_id=stop_id,
        stop_order=stop_order,
        vehicle_id=vehicle_id,
        run_key=run_key,
        predicted_arrival_at=evaluated_at + timedelta(seconds=remaining),
        eta_source="leading_vehicle_gap",
        model_version=LEADING_VEHICLE_GAP_MODEL_VERSION,
        horizon_seconds=remaining,
        basis_at=evaluated_at,
        source_generated_at=None,
        collected_at=max(a.collected_at, x.collected_at),
        source_ref=f"leading_vehicle_gap|A:{a.source_ref}|X:{x.source_ref}|order:{anchor.source_ref}",
        basis_source=a.basis_source if a.basis_source == x.basis_source else None,
        stops_away=stops_away,
    )


def _provider_alight_eta(
    ride: StaticRide,
    board_eta: VehicleEta,
    table: EtaTable,
) -> tuple[VehicleEta | None, str | None]:
    """같은 차량·회차의 하차 정류소 제공 ETA를 찾는다. 차량번호가 같아도 다른 회차는 잇지 않는다.

    운행 회차 키를 주지 않는 제공처(대전시 도착정보)는 같은 패턴·같은 차량번호의 하차 예측 가운데, 하차 정류소의 남은
    정류장 수가 승차 정류소보다 많은 것만 같은 운행으로 본다(남은 정류장 수가 없으면 시각 순서만 확인). 대전시 정류장 수와
    노선 경유 순번은 정류소 구성이 달라 차이가 같지 않을 수 있어 진행 방향만 맞춘다(_same_progress). 검증 전 연결이므로
    호출자가 사유 코드로 표시한다.
    """
    alight_etas = table.by_position.get((ride.pattern_id, ride.alight_position, ride.alight_stop_id), ())
    if not alight_etas:
        return None, "alight_eta_absent"
    if board_eta.vehicle_id is None:
        return None, "vehicle_run_unconfirmed"
    same_vehicle = [eta for eta in alight_etas if eta.vehicle_id == board_eta.vehicle_id]
    if not same_vehicle:
        return None, "alight_eta_vehicle_absent"
    if board_eta.run_key is not None:
        same_run = [eta for eta in same_vehicle if eta.run_key == board_eta.run_key]
        if not same_run:
            return None, "alight_eta_run_mismatch"
    else:
        hops = ride.alight_position - ride.board_position
        same_run = [eta for eta in same_vehicle if eta.run_key is None
                    and _same_progress(board_eta.stops_away, eta.stops_away, hops, relaxed=True)]
        if not same_run:
            return None, "alight_eta_progress_mismatch"
    if same_run[0].predicted_arrival_at <= board_eta.predicted_arrival_at:
        return None, "alight_eta_order_inconsistent"
    return same_run[0], None
