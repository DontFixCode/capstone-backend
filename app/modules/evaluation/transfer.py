"""첫 승차 가능 여부·준비시각·환승 여유·시간상 연결 판정을 담당한다.

시간상 연결 조건은 앞 시각 + 도보 + 준비 ≤ 다음 차량의 정류소 도착시각이다. ETA는 도착시각이므로 출발시각과
같다고 가정하지 않으며 정차 후 탑승 가능 시간을 더하지 않는다. 여유가 음수여도 다음 차량으로 조용히 바꾸지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from app.modules.evaluation.schemas import ConnectionAssessment, VehicleEta, WalkResult


def resolve_walk_seconds(
    walks: Mapping[tuple[str, str], WalkResult],
    from_ref: str,
    to_ref: str,
) -> tuple[float | None, str | None]:
    """방향별 도보시간과 미확인 이유를 반환한다. 같은 정류소 안의 환승만 도보 0초로 확정한다."""
    if from_ref == to_ref:
        return 0.0, None
    result = walks.get((from_ref, to_ref))
    if result is None:
        return None, "walk_result_missing"
    if result.status == "no_route":
        return None, "walk_no_route"
    if result.status == "lookup_failed":
        return None, "walk_lookup_failed"
    return result.duration_seconds, None


def ready_at(base_at: datetime, walk_seconds: float, buffer_seconds: float) -> datetime:
    """다음 탑승 준비를 마치는 시각. 같은 정류소 환승에도 준비시간은 반영한다."""
    return base_at + timedelta(seconds=walk_seconds + buffer_seconds)


def connection_slack_seconds(
    base_at: datetime,
    next_arrival_at: datetime,
    walk_seconds: float,
    buffer_seconds: float,
) -> float:
    """연결 여유 m = 다음 예측시각 − 앞 시각 − 도보시간 − 준비시간."""
    return (next_arrival_at - base_at).total_seconds() - walk_seconds - buffer_seconds


def assess_connection(
    *,
    connection_index: int,
    from_ref: str,
    to_stop_id: str,
    base_at: datetime,
    walk_seconds: float | None,
    walk_reason: str | None,
    buffer_seconds: float,
    board_eta: VehicleEta | None,
    missing_eta_reasons: Sequence[str] = (),
) -> ConnectionAssessment:
    """첫 승차(connection_index=0) 또는 환승의 기준 ETA 연결을 판정한다.

    시간 미확인(time_unknown)·음수 기준 여유(negative_slack)·확인된 실행 불가(confirmed_infeasible)를 구분한다.
    """
    kind = "first_boarding" if connection_index == 0 else "transfer"

    def assessment(status: str, reasons: Sequence[str], **times: object) -> ConnectionAssessment:
        fields = {"ready_at": None, "next_arrival_at": None, "slack_seconds": None, "wait_seconds": None, **times}
        return ConnectionAssessment(
            connection_index=connection_index,
            from_ref=from_ref,
            to_stop_id=to_stop_id,
            walk_seconds=walk_seconds,
            buffer_seconds=buffer_seconds,
            status=status,
            reason_codes=tuple(reasons),
            **fields,
        )

    if walk_seconds is None:
        status = "confirmed_infeasible" if walk_reason == "walk_no_route" else "time_unknown"
        return assessment(status, (walk_reason, *missing_eta_reasons))

    ready = ready_at(base_at, walk_seconds, buffer_seconds)
    if board_eta is None:
        return assessment("time_unknown", missing_eta_reasons, ready_at=ready)

    times = {"ready_at": ready, "next_arrival_at": board_eta.predicted_arrival_at}
    passed_at = board_eta.observed_passed_at
    if passed_at is not None and passed_at < ready:
        return assessment("confirmed_infeasible", ("vehicle_confirmed_passed",), **times)

    slack = connection_slack_seconds(base_at, board_eta.predicted_arrival_at, walk_seconds, buffer_seconds)
    times["slack_seconds"] = slack
    if slack < 0:
        return assessment("negative_slack", (f"{kind}_negative_slack",), **times)
    return assessment("feasible", (), wait_seconds=slack, **times)
