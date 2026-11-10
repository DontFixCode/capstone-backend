"""차량별 절대 ETA 구성과 관측 기반 하차 ETA 보완을 담당한다.

하차 ETA는 같은 차량·회차의 제공 ETA → 적격 관측 구간 소요시간 순으로 구성하고, 근거가 없으면 None과 이유를
반환한다. 거리 비례 추정은 시간·거리 단위와 운행 범위를 검증하기 전이므로 구현하지 않고 비활성으로 둔다.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from app.modules.evaluation.schemas import (
    EtaReport,
    EvaluationPolicy,
    RideLegs,
    SegmentObservation,
    TimedLeg,
    VehicleEta,
)
from app.modules.routing.schemas import StaticRide

SegmentDurations = Mapping[tuple[str, int], tuple[float, ...]]


@dataclass(frozen=True)
class EtaTable:
    """정류소 경유 위치별 차량 ETA와 사용하지 못한 응답의 이유. 같은 응답을 여러 경로가 공유한다."""

    # (pattern_id, stop_order, stop_id) → 도착 예상 순서의 ETA.
    by_position: Mapping[tuple[str, int, str], tuple[VehicleEta, ...]]
    rejected_by_stop: Mapping[tuple[str, str], frozenset[str]]
    rejected_counts: Mapping[str, int]


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
    """응답을 절대 ETA로 바꿔 경유 위치별로 묶는다. 같은 차량·회차의 중복 응답은 가장 새로운 기준을 남긴다."""
    latest: dict[tuple[str, int, str], dict[tuple, VehicleEta]] = {}
    rejected_by_stop: dict[tuple[str, str], set[str]] = defaultdict(set)
    rejected_counts: Counter[str] = Counter()
    for report in reports:
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
    )


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
        return None, tuple(sorted(reasons))

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
) -> RideLegs:
    """정적 탑승 구간의 차량 옵션을 도착 예상 순서로 구성한다. 응답에 없는 차량을 운행 없음으로 해석하지 않는다."""
    board_etas = table.by_position.get((ride.pattern_id, ride.board_position, ride.board_stop_id), ())
    horizon_end_at = start_at + timedelta(seconds=policy.search_horizon_seconds)
    legs: list[TimedLeg] = []
    horizon_excluded = 0
    for rank, board_eta in enumerate(board_etas):
        if board_eta.predicted_arrival_at > horizon_end_at:
            horizon_excluded += 1
            continue
        alight_eta, leg_reasons = resolve_alight_eta(ride, board_eta, table, durations, policy)
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

    reasons: set[str] = set()
    if not board_etas:
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


def _provider_alight_eta(
    ride: StaticRide,
    board_eta: VehicleEta,
    table: EtaTable,
) -> tuple[VehicleEta | None, str | None]:
    """같은 차량·회차의 하차 정류소 제공 ETA를 찾는다. 차량번호가 같아도 다른 회차는 잇지 않는다.

    운행 회차 키를 주지 않는 제공처(대전시 도착정보)는 같은 패턴·같은 차량번호의 하차 예측 가운데, 남은 정류장 수 차이가
    승하차 경유 순번 차이와 같은 것만 같은 운행으로 본다(남은 정류장 수가 없으면 시각 순서만 확인). 검증 전 연결이므로
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
        same_run = [eta for eta in same_vehicle if eta.run_key is None and (
            eta.stops_away is None or board_eta.stops_away is None or eta.stops_away - board_eta.stops_away == hops)]
        if not same_run:
            return None, "alight_eta_progress_mismatch"
    if same_run[0].predicted_arrival_at <= board_eta.predicted_arrival_at:
        return None, "alight_eta_order_inconsistent"
    return same_run[0], None
