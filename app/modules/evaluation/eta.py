"""차량별 절대 ETA 구성과 관측 기반 하차 ETA 보완을 담당한다.

하차 ETA는 같은 차량·회차의 제공 ETA → 적격 관측 구간 소요시간 → 앞차 ETA 기반 근사(Phase 3B) 순으로 구성하고,
근거가 없으면 None과 이유를 반환한다. 거리 비례 추정은 시간·거리 단위와 운행 범위를 검증하기 전이므로 구현하지 않고
비활성으로 둔다. 승차 정류소에 앞차만 나오면 같은 근사로 그 뒤 차량의 승차 ETA를 차량 옵션에 더한다.
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
        anchor = (anchors or {}).get((ride.pattern_id, ride.alight_position, board_eta.vehicle_id))
        if anchor is None or evaluated_at is None:
            return None, tuple(sorted(reasons))
        gap_eta, gap_reason = leading_vehicle_gap_eta(ride, board_eta, table, anchor, evaluated_at)
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
) -> RideLegs:
    """정적 탑승 구간의 차량 옵션을 도착 예상 순서로 구성한다. 응답에 없는 차량을 운행 없음으로 해석하지 않는다.

    승차 정류소 응답에 없는 뒤 차량은 앞차 ETA 기반 근사로 만든 승차 ETA를 옵션에 더한다(following_board_etas).
    """
    listed = table.by_position.get((ride.pattern_id, ride.board_position, ride.board_stop_id), ())
    following, following_reasons = (
        following_board_etas(ride, table, anchors, evaluated_at) if anchors and evaluated_at is not None else ((), ())
    )
    board_etas = tuple(sorted((*listed, *following), key=_eta_order))
    horizon_end_at = start_at + timedelta(seconds=policy.search_horizon_seconds)
    legs: list[TimedLeg] = []
    horizon_excluded = 0
    for rank, board_eta in enumerate(board_etas):
        if board_eta.predicted_arrival_at > horizon_end_at:
            horizon_excluded += 1
            continue
        alight_eta, leg_reasons = resolve_alight_eta(ride, board_eta, table, durations, policy, anchors, evaluated_at)
        if board_eta.eta_source == "leading_vehicle_gap":
            leg_reasons = tuple(sorted({*leg_reasons, BOARD_ETA_BY_LEADING_VEHICLE_GAP}))
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

    reasons: set[str] = set(following_reasons)
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
ALIGHT_ETA_BY_LEADING_VEHICLE_GAP = "alight_eta_by_leading_vehicle_gap"
BOARD_ETA_BY_LEADING_VEHICLE_GAP = "board_eta_by_leading_vehicle_gap"
# A의 X 통과시각 근거가 없어 경과분을 0으로 둔 근사(MVP 대체). 보정한 근사와 표본을 섞지 않도록 모델 버전에 남긴다.
LEADING_VEHICLE_GAP_MODEL_VERSION = "leading-vehicle-gap/1;passage-uncorrected"


def leading_vehicle_gap_eta(
    ride: StaticRide,
    board_eta: VehicleEta,
    table: EtaTable,
    anchor: GapAnchor,
    evaluated_at: datetime,
) -> tuple[VehicleEta | None, str | None]:
    """앞차 ETA 기반 후행 차량 하차 ETA(Phase 3B, 검증 전). R_B(D;t0) = R_A(D;t0) + R_B(X;t0) + Δ_A(X).

    A는 하차 정류소 D에 먼저 도착할 앞차, X는 A가 마지막으로 통과한 경유 위치, B는 탈 차량(board_eta)이다. X의 도착
    목록에서 B가 가장 먼저 도착할 차량이고 진행 위치가 승차 예측과 맞을 때만 쓴다. A의 X 통과시각 근거(동일 차량·경유
    위치의 검증된 이벤트)가 없으므로 Δ_A(X)=0(MVP 대체)이다. 여러 정류소의 누적 ETA는 더하지 않는다.
    """
    pair, reason = _leading_gap_pair(ride.pattern_id, anchor, table)
    if pair is None:
        return None, reason
    a, x = pair
    if x.run_key != board_eta.run_key:
        return None, "leading_gap_trailing_eta_absent"
    hops = anchor.anchor_stop_order - ride.board_position
    if x.stops_away is not None and board_eta.stops_away is not None and x.stops_away - board_eta.stops_away != hops:
        return None, "leading_gap_progress_mismatch"
    remaining = _leading_gap_remaining(a, x, evaluated_at)
    if remaining is None:
        return None, "leading_gap_eta_before_evaluation"
    if evaluated_at + timedelta(seconds=remaining) <= board_eta.predicted_arrival_at:
        return None, "leading_gap_order_inconsistent"
    return _leading_gap_vehicle_eta(ride.pattern_id, ride.alight_stop_id, ride.alight_position, board_eta.vehicle_id,
                                    board_eta.run_key, a, x, anchor, remaining, evaluated_at), None


def following_board_etas(
    ride: StaticRide,
    table: EtaTable,
    anchors: GapAnchors,
    evaluated_at: datetime,
) -> tuple[tuple[VehicleEta, ...], tuple[str, ...]]:
    """승차 정류소 D에 앞차 A만 나올 때 그 뒤 차량 B의 승차 ETA(Phase 3B, 검증 전). R_B(D;t0) = R_A(D;t0) + R_B(X;t0).

    차량 조합(A는 D의 선두 차량, B는 노선 위치상 A 바로 뒤 차량, X는 A가 마지막으로 통과한 위치)은 검색이 고른다
    (workflows.search.leading_gap_anchors). X의 도착 목록에서 B가 가장 먼저 도착할 차량일 때만 만들고, D 응답에 이미
    나온 차량은 만들지 않는다. 만들지 못한 이유를 함께 돌려준다.
    """
    target = (ride.pattern_id, ride.board_position, ride.board_stop_id)
    listed = {eta.vehicle_id for eta in table.by_position.get(target, ())}
    etas: list[VehicleEta] = []
    reasons: set[str] = set()
    for key in sorted(anchors):
        anchor = anchors[key]
        if (anchor.pattern_id, anchor.target_stop_order, anchor.target_stop_id) != target \
                or anchor.trailing_vehicle_id in listed:
            continue
        pair, reason = _leading_gap_pair(ride.pattern_id, anchor, table)
        remaining = None if pair is None else _leading_gap_remaining(*pair, evaluated_at)
        if pair is None or remaining is None:
            reasons.add(reason or "leading_gap_eta_before_evaluation")
            continue
        a, x = pair
        hops = ride.board_position - anchor.anchor_stop_order
        etas.append(_leading_gap_vehicle_eta(
            ride.pattern_id, ride.board_stop_id, ride.board_position, anchor.trailing_vehicle_id, x.run_key, a, x,
            anchor, remaining, evaluated_at, stops_away=None if x.stops_away is None else x.stops_away + hops))
    return tuple(etas), tuple(sorted(reasons))


def _leading_gap_pair(
    pattern_id: str,
    anchor: GapAnchor,
    table: EtaTable,
) -> tuple[tuple[VehicleEta, VehicleEta] | None, str | None]:
    """목표 정류소 D의 앞차 A 예측과, 기준점 X에 가장 먼저 도착할 차량이 B일 때 그 예측."""
    leading = [
        eta
        for eta in table.by_position.get((pattern_id, anchor.target_stop_order, anchor.target_stop_id), ())
        if eta.vehicle_id == anchor.leading_vehicle_id
    ]
    if not leading or anchor.leading_vehicle_id == anchor.trailing_vehicle_id:
        return None, "leading_gap_leading_eta_absent"
    at_anchor = table.by_position.get((pattern_id, anchor.anchor_stop_order, anchor.anchor_stop_id), ())
    if not at_anchor or at_anchor[0].vehicle_id != anchor.trailing_vehicle_id:
        return None, "leading_gap_trailing_eta_absent"
    return (leading[0], at_anchor[0]), None


def _leading_gap_remaining(a: VehicleEta, x: VehicleEta, evaluated_at: datetime) -> float | None:
    """R_A(D;t0) + R_B(X;t0). 평가 시각에 어느 예상 도착이 이미 지났으면 None."""
    leading_remaining = (a.predicted_arrival_at - evaluated_at).total_seconds()
    trailing_remaining = (x.predicted_arrival_at - evaluated_at).total_seconds()
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
