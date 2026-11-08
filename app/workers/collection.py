"""공유 수집 대상의 취합. 실행 시점은 scheduler, 수집 규칙은 modules/transit/service가 담당한다.

대상 출처
- 개발 설정: .env.api의 BACKEND_REALTIME_POSITION_ROUTE_IDS·BACKEND_REALTIME_ARRIVAL_STOP_IDS(쉼표 구분). 워커가 주기마다
  설정을 다시 읽으므로 목록을 고치면 등록·해제된다.
- 최초 검색: 유효기간(expires_at) 안의 app.route_searches가 summary.realtime_targets에 등록한 승하차 정류소·노선.
  유효기간이 지나면 자동으로 해제된다(Notion DB-C01).
- 화면 조회: GET /routes/{id}/positions·/stops/{id}/arrivals가 등록한 app.realtime_view_targets 중 만료 전 대상
  (기본 30분, 재조회 시 연장). 만료되면 집계에서 빠지고 다른 수요가 있으면 그 수요로 수집이 이어진다.
- 활성 이동: 재평가가 예약된 active journeys의 선택 경로 중 남은 버스 구간 승하차 정류소·노선. 백그라운드·연결 단절에도
  유지하고, 사용자가 완료·취소하면(예약 해제) 빠진다.
같은 조회 조건은 출처가 여러 개여도 한 번만 수집한다.
"""

from __future__ import annotations

from datetime import datetime

import psycopg

from app.core.config import Settings
from app.modules.journeys import repository as journey_repository
from app.modules.routing import repository as search_repository
from app.modules.transit import repository as transit_repository
from app.modules.transit.schemas import RealtimeTarget
from app.modules.transit.service import merge_targets, target_from_key

CONFIG_SOURCE = "dev_config"
SEARCH_SOURCE = "route_search"
JOURNEY_SOURCE = "journey"
VIEW_SOURCE = "realtime_view"


def configured_targets(settings: Settings) -> list[RealtimeTarget]:
    source = frozenset({CONFIG_SOURCE})
    positions = [RealtimeTarget("positions", route_id, source) for route_id in settings.realtime_position_route_ids]
    arrivals = [RealtimeTarget("arrivals", stop_id, source) for stop_id in settings.realtime_arrival_stop_ids]
    return merge_targets([positions, arrivals])


def search_targets(conn: psycopg.Connection, now: datetime) -> list[RealtimeTarget]:
    targets: list[RealtimeTarget] = []
    for row in search_repository.active_search_targets(conn, now):
        source = frozenset({f"{SEARCH_SOURCE}:{row['search_id']}"})
        for kind in ("arrivals", "positions"):
            for external_id in row["targets"].get(kind, []) or []:
                if isinstance(external_id, str) and external_id:
                    targets.append(RealtimeTarget(kind, external_id, source))
    return targets


def journey_targets(conn: psycopg.Connection) -> list[RealtimeTarget]:
    """재평가가 예약된 활성 이동의 남은 버스 구간 대상. 앱 연결 여부와 무관하게 유지하고, 완료·취소되면 빠진다."""
    from app.workflows.guidance import journey_targets as targets_for

    targets: list[RealtimeTarget] = []
    for row in journey_repository.active_journey_snapshots(conn):
        targets.extend(targets_for(row["selected_snapshot"] or {}, row["current_progress"] or {},
                                   row["movement_state"], f"{JOURNEY_SOURCE}:{row['id']}"))
    return targets


def view_targets(conn: psycopg.Connection, now: datetime) -> list[RealtimeTarget]:
    """만료 전인 화면 조회 수요(노선 위치·정류소 도착). 키는 수집기가 쓰는 대상 키 그대로다."""
    targets = []
    for key in transit_repository.active_view_target_keys(conn, now):
        target = target_from_key(key, source=VIEW_SOURCE)
        if target is not None:
            targets.append(target)
    return targets


def collection_targets(settings: Settings, conn: psycopg.Connection | None = None,
                       now: datetime | None = None) -> list[RealtimeTarget]:
    """현재 수집해야 할 대상의 합집합. 사용자가 없는 전체 노선·정류소는 넣지 않는다."""
    groups = [configured_targets(settings)]
    if conn is not None and now is not None:
        groups.append(search_targets(conn, now))
        groups.append(journey_targets(conn))
        groups.append(view_targets(conn, now))
    return merge_targets(groups)
