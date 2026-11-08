"""정류소·노선 앱 조회 API (/api/v1/stops, /api/v1/routes, /api/v1/static-data/version).

공개 여부가 정해지지 않아 Phase 4에서는 모두 인증을 요구한다(탈퇴 계정 차단 포함). 정적 공개본과 공유 수집 워커가 공개한
최신 배치만 읽으며, 앱 요청마다 TAGO를 호출하지 않는다. 활성 노선 위치·정류소 도착 조회는 화면 수집 수요(기본 30분)를
등록해 워커가 그 대상을 공유 수집하게 한다(Phase 4A 후속 보완).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

import psycopg
from fastapi import APIRouter, Depends, Path, Query, Request, Response

from app.core.config import Settings
from app.core.dependencies import active_user, get_db, get_settings
from app.core.exceptions import ApiError, not_found
from app.core.security import AuthUser
from app.db.session import Database
from app.modules.transit import repository, views
from app.modules.transit.schemas import RealtimeTarget
from app.modules.transit.service import find_nearby_stops, realtime_target_key

router = APIRouter(prefix="/api/v1")
logger = logging.getLogger(__name__)
BIGINT_MAX = 2**63 - 1
def _id():
    """경로 매개변수마다 새 Path를 만든다(FastAPI는 같은 Path 객체를 여러 매개변수에 공유하면 이름을 섞는다)."""
    return Path(ge=1, le=BIGINT_MAX, description="내부 ID. BIGINT를 10진 문자열로 전달한다.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def nearby_radius(settings: Settings, requested: float | None) -> float:
    """기본·최대 반경은 배포 설정, 없으면 승인된 검색 반경(접근 반경, 접근·목적지 중 큰 값)을 쓴다."""
    access, egress = settings.search_access_radius_meters, settings.search_egress_radius_meters
    default = settings.stops_nearby_default_radius_meters or access
    maximum = settings.stops_nearby_max_radius_meters or (max(r for r in (access, egress) if r) if access or egress
                                                          else None)
    if default is None or maximum is None:
        raise ApiError(503, "STOPS_RADIUS_NOT_CONFIGURED", "주변 정류소 검색 반경 설정이 없습니다.")
    radius = default if requested is None else requested
    if radius > maximum:
        raise ApiError(400, "RADIUS_TOO_LARGE", f"검색 반경은 최대 {maximum:g}m입니다.", details={"maxRadiusM": maximum})
    return float(radius)


def register_view(db: Database, settings: Settings, target: RealtimeTarget) -> None:
    """화면 조회 수요를 공유 저장소에 등록·연장한다(응답 전 커밋). 외부 호출은 하지 않고 수집은 워커가 한다.

    같은 행을 동시에 갱신해도 직렬화 오류가 나지 않도록 짧은 READ COMMITTED 트랜잭션에서 처리한다.
    """
    try:
        with db.transaction() as conn:
            repository.register_view_target(conn, realtime_target_key(target),
                                            settings.realtime_view_target_ttl_seconds)
    except psycopg.errors.UndefinedTable:
        # migrations/10_realtime_view_targets.sql 적용 전인 DB: 조회 응답은 유지하고 등록만 건너뛴다.
        logger.warning("app.realtime_view_targets가 없어 화면 수집 수요를 등록하지 못했습니다(마이그레이션 10 미적용).")


@router.get("/static-data/version", tags=["static-data"])
def static_data_version(_user: AuthUser = Depends(active_user), db: Database = Depends(get_db)) -> dict:
    with db.transaction() as conn:
        publication = repository.static_publication(conn)
    versions = [str(v) for v in publication["versions"]]
    return {"dataVersion": versions[0] if len(versions) == 1 else None, "publishedAt": publication["published_at"],
            "mixedVersions": len(versions) > 1}


@router.get("/stops/nearby", tags=["stops"])
def stops_nearby(
    latitude: float = Query(ge=-90, le=90), longitude: float = Query(ge=-180, le=180),
    radiusM: float | None = Query(default=None, gt=0),
    _user: AuthUser = Depends(active_user), db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    if not (math.isfinite(latitude) and math.isfinite(longitude)) or (radiusM is not None and not math.isfinite(radiusM)):
        raise ApiError(422, "VALIDATION_ERROR", "좌표·반경은 유한한 값이어야 합니다.")
    radius = nearby_radius(settings, radiusM)
    try:
        stops = find_nearby_stops(db, latitude, longitude, radius)
    except ValueError as exc:
        raise ApiError(422, "VALIDATION_ERROR", str(exc)) from None
    with db.transaction() as conn:
        version = views.current_version(conn)
    return {"items": [{"id": str(s.stop_id), "externalId": s.external_id, "name": s.name, "stopNo": s.stop_no,
                       "location": views.location(s.lat, s.lon), "straightDistanceM": s.distance_m} for s in stops],
            "radiusM": radius, "dataVersion": version}


@router.get("/stops", tags=["stops"])
def stop_by_external(provider: str = Query(min_length=1), externalId: str = Query(min_length=1),
                     _user: AuthUser = Depends(active_user), db: Database = Depends(get_db)) -> dict:
    with db.transaction() as conn:
        row = repository.stop_by_external_id(conn, provider.strip(), externalId.strip())
        version = views.current_version(conn)
    if row is None:
        raise not_found("일치하는 정류소가 없습니다.")
    return views.stop_view(row, version)


@router.get("/stops/{stopId}", tags=["stops"])
def stop_detail(stopId: int = _id(), _user: AuthUser = Depends(active_user), db: Database = Depends(get_db)) -> dict:
    with db.transaction() as conn:
        row = repository.stop_by_id(conn, stopId)
        version = views.current_version(conn)
    if row is None:
        raise not_found("정류소가 없습니다.")
    return views.stop_view(row, version)


@router.get("/stops/{stopId}/routes", tags=["stops"])
def stop_routes(stopId: int = _id(), _user: AuthUser = Depends(active_user), db: Database = Depends(get_db)) -> dict:
    with db.transaction(isolation="REPEATABLE READ") as conn:
        row = repository.stop_by_id(conn, stopId)
        if row is None:
            raise not_found("정류소가 없습니다.")
        visits = repository.stop_route_visits(conn, stopId)
        version = views.current_version(conn)
    return {"stopId": str(stopId), "items": [{
        "route": {"id": str(v["route_id"]), "externalId": v["route_external_id"], "routeNo": v["route_no"],
                  "routeType": v["route_type"]},
        "patternKey": v["pattern_key"], "directionCode": v["direction_code"], "seq": v["seq"],
        "towardStopName": v["toward_name"],
    } for v in visits], "dataVersion": version}


@router.get("/stops/{stopId}/arrivals", tags=["stops"])
def stop_arrivals(stopId: int = _id(), _user: AuthUser = Depends(active_user), db: Database = Depends(get_db),
                  settings: Settings = Depends(get_settings)) -> dict:
    with db.transaction() as conn:
        row = repository.stop_by_id(conn, stopId)
        if row is None:
            raise not_found("정류소가 없습니다.")
    if row["is_active"] and row["provider"] == repository.PROVIDER:
        register_view(db, settings, RealtimeTarget("arrivals", row["external_id"]))
    with db.transaction(isolation="REPEATABLE READ") as conn:
        return views.stop_arrivals(conn, row, interval_seconds=settings.realtime_interval_seconds, now=_now(),
                                   basis_policy=settings.tago_eta_basis_policy)


@router.get("/routes", tags=["routes"])
def routes_search(routeNo: str | None = Query(default=None, min_length=1), provider: str | None = Query(default=None),
                  externalId: str | None = Query(default=None), _user: AuthUser = Depends(active_user),
                  db: Database = Depends(get_db)) -> dict:
    by_number = routeNo is not None
    by_external = provider is not None or externalId is not None
    if by_number == by_external or (by_external and not (provider and externalId)):
        raise ApiError(422, "VALIDATION_ERROR", "routeNo 또는 provider·externalId 조합 중 한 가지를 전달하세요.")
    with db.transaction() as conn:
        if by_number:
            rows = repository.routes_by_number(conn, routeNo.strip())
        else:
            row = repository.route_by_external_id(conn, provider.strip(), externalId.strip())
            rows = [row] if row else []
        version = views.current_version(conn)
    return {"items": [views.route_summary(r) for r in rows], "dataVersion": version}


def _route_or_404(conn, route_id: int) -> dict:
    row = repository.route_by_id(conn, route_id)
    if row is None:
        raise not_found("노선이 없습니다.")
    return row


@router.get("/routes/{routeId}", tags=["routes"])
def route_detail(routeId: int = _id(), _user: AuthUser = Depends(active_user), db: Database = Depends(get_db)) -> dict:
    with db.transaction(isolation="REPEATABLE READ") as conn:
        route = _route_or_404(conn, routeId)
        patterns = repository.route_patterns(conn, routeId)
        version = views.current_version(conn)
    return {**views.route_summary(route), "serviceInfo": views.service_info_view(route["service_info"]),
            "shapes": [views.shape_status_view(route, p["pattern_key"]) for p in patterns], "dataVersion": version}


@router.get("/routes/{routeId}/patterns", tags=["routes"])
def route_patterns(routeId: int = _id(), _user: AuthUser = Depends(active_user), db: Database = Depends(get_db)) -> dict:
    with db.transaction(isolation="REPEATABLE READ") as conn:
        route = _route_or_404(conn, routeId)
        patterns = repository.route_patterns(conn, routeId)
        version = views.current_version(conn)
    return {"routeId": str(routeId), "items": views.pattern_views(route, patterns), "dataVersion": version}


@router.get("/routes/{routeId}/patterns/{patternKey}/stops", tags=["routes"])
def pattern_stops(routeId: int = _id(), patternKey: str = Path(min_length=1),
                  afterSeq: int | None = Query(default=None, ge=0), _user: AuthUser = Depends(active_user),
                  db: Database = Depends(get_db)) -> dict:
    with db.transaction(isolation="REPEATABLE READ") as conn:
        _route_or_404(conn, routeId)
        rows = repository.pattern_stops(conn, routeId, patternKey, afterSeq)
        version = views.current_version(conn)
    if rows is None:
        raise not_found("노선에 해당 패턴이 없습니다.")
    return {"routeId": str(routeId), "patternKey": patternKey, "items": [{
        "seq": r["seq"], "directionCode": r["direction_code"],
        "stop": {"id": str(r["stop_id"]), "externalId": r["external_id"], "name": r["name"],
                 "location": views.location(r["lat"], r["lon"])},
        # 원천 누적거리의 단위가 확인되지 않아 저장하지 않았다(Phase 2). 값이 없으면 null.
        "cumulativeDistanceM": r["cumulative_distance_m"],
    } for r in rows], "dataVersion": version}


@router.get("/routes/{routeId}/shapes/{patternKey}", tags=["routes"])
def route_shape(request: Request, response: Response, routeId: int = _id(), patternKey: str = Path(min_length=1),
                _user: AuthUser = Depends(active_user), db: Database = Depends(get_db)):
    with db.transaction(isolation="REPEATABLE READ") as conn:
        route = _route_or_404(conn, routeId)
        if repository.pattern_stops(conn, routeId, patternKey, None) is None:
            raise not_found("노선에 해당 패턴이 없습니다.")
        version = views.current_version(conn)
    body, etag = views.shape_view(route, patternKey, version)
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    match = request.headers.get("if-none-match")
    if match and etag in [m.strip() for m in match.split(",")]:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return body


@router.get("/routes/{routeId}/positions", tags=["routes"])
def route_positions(routeId: int = _id(), _user: AuthUser = Depends(active_user), db: Database = Depends(get_db),
                    settings: Settings = Depends(get_settings)) -> dict:
    with db.transaction() as conn:
        route = _route_or_404(conn, routeId)
    if route["is_active"] and route["provider"] == repository.PROVIDER:
        register_view(db, settings, RealtimeTarget("positions", route["external_id"]))
    with db.transaction(isolation="REPEATABLE READ") as conn:
        return views.route_positions(conn, route, interval_seconds=settings.realtime_interval_seconds, now=_now())
