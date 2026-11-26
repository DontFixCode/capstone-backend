"""ETA 오차 표본 수집 흐름(워커): 설정 해석 → 수집 대상·주기 → 도착 이벤트 도출 → 오차 표본 생성.

버스 한 대씩의 '실제 도착 − 예측 도착' 오차를 모은다. 짝지은 두 차량의 표본(transfer_samples)이 부족할 때 환승 확률의
두 번째 단계(단일 차량 오차 결합, evaluation.probability)가 이 표본을 쓴다.

- 기본은 꺼짐이다(BACKEND_ETA_ERROR_SAMPLING_ENABLED). 켰는데 필수 설정이 빠지면 SamplingConfigError로 알리고, 워커는
  표본 수집만 건너뛴 채 기존 수집을 계속한다.
- 대상 정류소의 도착정보(기존 수집 경로)와 대상 노선의 대전시 위치를 설정한 주기·시간대(KST)에 받는다. 이 대상만 짧은
  주기로 돌고 나머지 대상은 기존 주기 그대로다(workers.scheduler의 대상별 간격). 시간대 밖에서는 대상에 넣지 않는다.
- 서비스(도착정보·노선 위치)별 그날(KST) 대전시 호출 수가 설정한 상한에 닿으면 그 서비스의 표본 수집 대상을 뺀다.
  검색·이동 안내가 등록한 대상은 그대로 수집한다.
- 도착 이벤트는 transit.arrival_events의 규칙으로, 오차 표본은 evaluation.eta_errors의 규칙으로 만든다. 이벤트는
  event_key로, 표본은 테이블 고유 키로 중복을 막아 다시 실행해도 결과가 같다.
- 외부 호출은 하지 않는다(수집은 워커의 공유 수집 루프가 한다).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from app.core.config import Settings
from app.modules.evaluation import eta_errors
from app.modules.evaluation import repository as evaluation_repository
from app.modules.transit import arrival_events
from app.modules.transit import repository as transit_repository
from app.modules.transit.schemas import RealtimeTarget
from app.modules.transit.service import (
    DAEJEON_MSG_TP_WAITING_DEPARTURE,
    DAEJEON_POSITIONS_KIND,
    DAEJEON_PROVIDER,
    KST,
    realtime_target_key,
)

SOURCE = "eta_error_sampling"
ACTIVE = "active"
OUTSIDE_HOURS = "outside_hours"
DAILY_CALL_LIMIT_REACHED = "daily_call_limit_reached"


class SamplingConfigError(RuntimeError):
    """표본 수집을 켰는데 필수 설정이 없거나 서로 맞지 않는다. 메시지에는 설정 이름만 담는다."""


@dataclass(frozen=True)
class SamplingConfig:
    stop_ids: tuple[str, ...]
    route_ids: tuple[str, ...]
    interval_seconds: float
    # KST 시간대 목록(시작 시 포함, 끝 시 제외).
    hours: tuple[tuple[int, int], ...]
    match_window_seconds: float
    horizon_edges_seconds: tuple[float, ...]
    run_gap_seconds: float
    arr_time_meaning: str
    daily_call_limit: int | None = None

    def in_hours(self, now: datetime) -> bool:
        hour = now.astimezone(KST).hour
        return any(start <= hour < end for start, end in self.hours)


_REQUIRED = {
    "eta_error_sampling_stops": "BACKEND_ETA_ERROR_SAMPLING_STOPS",
    "eta_error_sampling_routes": "BACKEND_ETA_ERROR_SAMPLING_ROUTES",
    "eta_error_sampling_interval_seconds": "BACKEND_ETA_ERROR_SAMPLING_INTERVAL_SECONDS",
    "eta_error_sampling_hours": "BACKEND_ETA_ERROR_SAMPLING_HOURS",
    "eta_error_match_window_seconds": "BACKEND_ETA_ERROR_MATCH_WINDOW_SECONDS",
    "eta_error_horizon_edges_seconds": "BACKEND_ETA_ERROR_HORIZON_EDGES_SECONDS",
    "eta_error_run_gap_seconds": "BACKEND_ETA_ERROR_RUN_GAP_SECONDS",
}


def load_config(settings: Settings) -> SamplingConfig | None:
    """표본 수집 설정. 꺼져 있으면 None, 켰는데 필수 값이 비었으면 누락된 키 이름을 모두 알려준다."""
    if not settings.eta_error_sampling_enabled:
        return None
    missing = [env for name, env in _REQUIRED.items() if getattr(settings, name) in (None, ())]
    if missing:
        raise SamplingConfigError("ETA 오차 표본 수집 설정이 없습니다(.env.api): " + ", ".join(missing))
    if settings.eta_error_run_gap_seconds <= settings.eta_error_sampling_interval_seconds:
        # 간격이 수집 주기보다 짧으면 연속한 기록이 모두 다른 운행으로 갈라져 도착 이벤트가 만들어지지 않는다.
        raise SamplingConfigError(
            "BACKEND_ETA_ERROR_RUN_GAP_SECONDS는 BACKEND_ETA_ERROR_SAMPLING_INTERVAL_SECONDS보다 커야 합니다.")
    return SamplingConfig(
        stop_ids=settings.eta_error_sampling_stops, route_ids=settings.eta_error_sampling_routes,
        interval_seconds=settings.eta_error_sampling_interval_seconds, hours=settings.eta_error_sampling_hours,
        match_window_seconds=settings.eta_error_match_window_seconds,
        horizon_edges_seconds=settings.eta_error_horizon_edges_seconds,
        run_gap_seconds=settings.eta_error_run_gap_seconds,
        arr_time_meaning=settings.daejeon_position_arr_time_meaning,
        daily_call_limit=settings.eta_error_sampling_daily_call_limit)


# ---------------------------------------------------------------- 수집 대상·주기

@dataclass(frozen=True)
class CollectionPlan:
    """이번 회차의 표본 수집 대상과 대상별 주기. 대상이 없으면 status에 이유가 있다."""

    status: str
    targets: tuple[RealtimeTarget, ...] = ()
    # 대상 키 → 수집 주기(초). 스케줄러가 이 대상에만 짧은 주기를 적용한다.
    intervals: dict[str, float] = field(default_factory=dict)
    # 상한에 닿아 뺀 서비스(api_kind)와 그날 호출 수.
    limited: dict[str, int] = field(default_factory=dict)

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self.intervals)


def day_start(now: datetime) -> datetime:
    """그날(KST) 0시. 일일 호출 수를 세는 기준이다(제공처의 집계 기준 시각은 확인 전)."""
    local = now.astimezone(KST)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def collection_plan(conn: psycopg.Connection | None, config: SamplingConfig, now: datetime) -> CollectionPlan:
    """수집 시간대 안이면 대상 정류소(도착정보)·노선(대전시 위치)을 주기와 함께 돌려준다. 밖이면 대상이 없다."""
    if not config.in_hours(now):
        return CollectionPlan(OUTSIDE_HOURS)
    source = frozenset({SOURCE})
    groups = {
        "arrivals": [RealtimeTarget("arrivals", stop_id, source) for stop_id in config.stop_ids],
        "positions": [RealtimeTarget(DAEJEON_POSITIONS_KIND, route_id, source) for route_id in config.route_ids],
    }
    limited: dict[str, int] = {}
    if config.daily_call_limit is not None and conn is not None:
        since = day_start(now)
        for api_kind in list(groups):
            calls = transit_repository.provider_call_count(conn, DAEJEON_PROVIDER, since, api_kind)
            if calls >= config.daily_call_limit:
                limited[api_kind] = calls
                groups[api_kind] = []
    targets = tuple(target for group in groups.values() for target in group)
    if not targets:
        return CollectionPlan(DAILY_CALL_LIMIT_REACHED, limited=limited)
    return CollectionPlan(ACTIVE, targets, {realtime_target_key(target): config.interval_seconds for target in targets},
                          limited)


# ---------------------------------------------------------------- 도착 이벤트·오차 표본

def _parse_time(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def derive_due(conn: psycopg.Connection, config: SamplingConfig, now: datetime) -> dict[str, Any]:
    """최근 위치 기록에서 대상 정류소의 도착 이벤트를 도출하고 오차 표본을 만든다. 호출자가 트랜잭션을 커밋한다.

    읽는 범위는 지금부터 (연결 창 + 운행 분리 간격 × 2) 이전까지다. 이벤트의 앞뒤 근거는 서로 운행 분리 간격 안에 있으므로
    워커가 그보다 오래 멈추지 않는 한 놓치지 않는다. 이미 만든 이벤트·표본은 다시 만들지 않는다.
    """
    result: dict[str, Any] = {"status": "done", "events_created": 0, "samples_created": 0, "excluded_stops": {}}
    if not evaluation_repository.eta_error_samples_ready(conn):
        return {"status": "skipped", "reason": "eta_error_samples_table_missing"}
    route_map = transit_repository.route_ids(conn, config.route_ids)
    visits = transit_repository.route_visits(conn, route_map.values())
    since = now - timedelta(seconds=config.match_window_seconds + 2 * config.run_gap_seconds)
    rows_by_route: dict[int, list[dict[str, Any]]] = {}
    for row in transit_repository.daejeon_positions_since(conn, sorted(route_map.values()), since):
        rows_by_route.setdefault(row["route_id"], []).append(row)
    window = timedelta(seconds=config.match_window_seconds)
    for route_external_id, route_id in sorted(route_map.items()):
        targets, excluded = arrival_events.target_positions(visits.get(route_id, []), config.stop_ids)
        repeated = sorted(stop_id for stop_id, reason in excluded.items() if reason == "repeated_visit")
        if repeated:
            result["excluded_stops"][route_external_id] = repeated
        by_seq = {visit["seq"]: visit for visit in targets.values()}
        patterns = {visit["pattern_key"] for visit in targets.values()}
        rows = [row for row in rows_by_route.get(route_id, []) if row["run_group"] and row["pattern_key"] in patterns]
        if not by_seq or not rows:
            continue
        vehicles = {row["run_group"]: row["vehicle_no"] for row in rows}
        positions = [arrival_events.RunPosition(row["id"], row["run_group"], row["stop_seq"],
                                                _parse_time(row["arr_time_at"]), row["collected_at"]) for row in rows]
        derived = arrival_events.derive_arrivals(positions, by_seq.keys(), config.arr_time_meaning)
        known = transit_repository.existing_event_keys(conn, [arrival.event_key for arrival in derived])
        # 한 운행이 대상 정류소 여러 곳을 지나면 먼저 도착한 정류소의 표본이 그 운행의 구간별 표본이 된다.
        for arrival in sorted(derived, key=lambda item: (item.upper_at, item.event_key)):
            if arrival.event_key in known:
                continue
            visit = by_seq[arrival.stop_seq]
            vehicle_no = vehicles[arrival.run_group]
            event_id = transit_repository.insert_arrival_event(
                conn, event_key=arrival.event_key, quality=arrival.quality, event_at=arrival.event_at,
                collected_at=arrival.collected_at, route_id=route_id, stop_id=visit["stop_id"],
                pattern_key=visit["pattern_key"], stop_seq=arrival.stop_seq, vehicle_no=vehicle_no,
                details={
                    "provider": DAEJEON_PROVIDER, "rule": arrival_events.RULE_VERSION,
                    "arr_time_meaning": config.arr_time_meaning, "basis": arrival.basis,
                    "run_group": arrival.run_group,
                    "bounded_lower_at": arrival.lower_at.isoformat(),
                    "bounded_upper_at": arrival.upper_at.isoformat(),
                    "lower_stop_seq": arrival.lower_stop_seq, "upper_stop_seq": arrival.upper_stop_seq,
                    "source_observation_ids": list(arrival.source_observation_ids),
                    "derived_at": now.isoformat(),
                    "collected_at_meaning": "근거 기록의 마지막 수집 시각(도착 시각 아님)",
                })
            if event_id is None:
                continue
            result["events_created"] += 1
            predictions = [
                eta_errors.ArrivalPrediction(
                    observation_id=row["id"], basis_at=row["basis_at"],
                    predicted_arrival_at=row["predicted_arrival_at"], basis_source=row["basis_source"],
                    waiting_departure=(row["msg_tp"] or "").strip() == DAEJEON_MSG_TP_WAITING_DEPARTURE)
                for row in transit_repository.arrival_predictions(
                    conn, route_id=route_id, stop_id=visit["stop_id"], vehicle_no=vehicle_no,
                    basis_from=arrival.upper_at - window, basis_until=arrival.upper_at)]
            records = eta_errors.build_error_samples(
                eta_errors.ObservedArrival(event_id, arrival.run_group, arrival.quality, arrival.lower_at,
                                           arrival.upper_at),
                predictions, horizon_edges_seconds=config.horizon_edges_seconds,
                match_window_seconds=config.match_window_seconds)
            result["samples_created"] += evaluation_repository.insert_eta_error_samples(conn, [{
                **record.__dict__, "route_id": route_id, "stop_id": visit["stop_id"],
                "pattern_key": visit["pattern_key"], "stop_seq": arrival.stop_seq, "vehicle_no": vehicle_no,
                "details": {"rule": eta_errors.RULE_VERSION, "event_rule": arrival_events.RULE_VERSION,
                            "arr_time_meaning": config.arr_time_meaning, "event_basis": arrival.basis,
                            "horizon_edges_seconds": list(config.horizon_edges_seconds),
                            "match_window_seconds": config.match_window_seconds},
            } for record in records])
    return result
