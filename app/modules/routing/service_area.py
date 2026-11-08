"""앱 최초 검색의 지원 범위(out_of_scope) 판정.

지점(출발지·목적지)마다 다음 순서로 판정한다.
1. 대전광역시 행정경계 안 또는 경계선 위(PostGIS ST_Covers)면 지원 범위다. 주변 정류소가 없어도 포함한다.
2. 아니면 탐색과 같은 정적 공개본의 활성 노선이 경유하는 정류소가 기존 승하차 후보 반경(출발지=접근 반경,
   목적지=도착 반경) 안에 있으면 지원 범위다(시외 경유 정류소 포함, 기존 ST_DWithin 조회 재사용).
3. 경계 밖으로 확인됐고 정적 공개본에서 반경 안 정류소가 없음이 확인되면 범위 밖이다.
4. 경계 자료가 없거나(파일 없음·해석 실패·형상 무효) 정적 공개본을 확인할 수 없어 3을 확정하지 못하면 '판단 불가'다.

검색 결과: 범위 밖 지점이 하나라도 있으면 out_of_scope(origin_out_of_scope·destination_out_of_scope), 범위 밖은 없고 판단
불가 지점이 있으면 data_limited(service_area_unavailable), 모두 범위 안이면 기존 후보 생성·평가를 진행한다.
거리 판정은 지원 범위 기준일 뿐이며 실제 도보 가능 여부는 기존 경로 탐색이 처리한다.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from app.modules.transit import repository as transit_repository

DEFAULT_BOUNDARY_PATH = Path(__file__).resolve().parents[3] / "data" / "service_area" / "daejeon.geojson"
ORIGIN, DESTINATION = "origin", "destination"
REASON_OUT = {ORIGIN: "origin_out_of_scope", DESTINATION: "destination_out_of_scope"}
REASON_UNAVAILABLE = "service_area_unavailable"
IN_SCOPE, OUT_OF_SCOPE, UNKNOWN = "in_scope", "out_of_scope", "unknown"

_cache: dict[tuple[str, int, int], BoundaryState] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class Boundary:
    name: str | None
    geometry_json: str = field(repr=False)
    path: str
    sha256: str


@dataclass(frozen=True)
class BoundaryState:
    boundary: Boundary | None
    reason: str | None = None


def load_boundary(path: str | Path | None) -> BoundaryState:
    """경계 GeoJSON(FeatureCollection, 피처 1건, Polygon/MultiPolygon)을 읽는다. 파일이 바뀌면 다시 읽는다."""
    target = Path(path) if path else DEFAULT_BOUNDARY_PATH
    try:
        stat = target.stat()
    except OSError:
        return BoundaryState(None, "boundary_file_missing")
    key = (str(target.resolve()), stat.st_mtime_ns, stat.st_size)
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached
    try:
        raw = target.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        features = data.get("features") if isinstance(data, dict) else None
        if data.get("type") != "FeatureCollection" or not isinstance(features, list) or len(features) != 1:
            raise ValueError("feature_count")
        geometry = features[0].get("geometry") or {}
        if geometry.get("type") not in ("Polygon", "MultiPolygon") or not geometry.get("coordinates"):
            raise ValueError("geometry_type")
        name = (features[0].get("properties") or {}).get("NAME")
        state = BoundaryState(Boundary(name, json.dumps(geometry, separators=(",", ":")), str(target),
                                       hashlib.sha256(raw).hexdigest()))
    except (OSError, UnicodeDecodeError, ValueError, AttributeError, TypeError):
        state = BoundaryState(None, "boundary_file_invalid")
    with _lock:
        _cache[key] = state
    return state


@dataclass
class PointScope:
    role: str
    lat: float
    lon: float
    radius_m: float
    inside_boundary: bool | None
    served_stops: int | None
    nearest_stop: dict[str, Any] | None
    status: str = UNKNOWN
    basis: str | None = None


@dataclass
class ScopeDecision:
    status: str
    reason_codes: list[str]
    points: list[PointScope]
    boundary: dict[str, Any]
    static_version: str | None

    def summary(self) -> dict[str, Any]:
        return {"status": self.status, "reason_codes": self.reason_codes, "boundary": self.boundary,
                "static_version": self.static_version, "points": [asdict(p) for p in self.points]}


def classify(point: PointScope) -> PointScope:
    """한 지점의 판정. inside_boundary·served_stops가 None이면 그 자료로는 확인하지 못했다는 뜻이다."""
    if point.inside_boundary:
        point.status, point.basis = IN_SCOPE, "boundary"
    elif point.served_stops:
        point.status, point.basis = IN_SCOPE, "served_stop_within_radius"
    elif point.inside_boundary is False and point.served_stops == 0:
        point.status, point.basis = OUT_OF_SCOPE, "outside_boundary_without_served_stop"
    else:
        point.status, point.basis = UNKNOWN, "insufficient_service_area_data"
    return point


def decide(points: list[PointScope], boundary: dict[str, Any], static_version: str | None) -> ScopeDecision:
    points = [classify(p) for p in points]
    out = [REASON_OUT[p.role] for p in points if p.status == OUT_OF_SCOPE]
    if out:
        return ScopeDecision(OUT_OF_SCOPE, out, points, boundary, static_version)
    if any(p.status == UNKNOWN for p in points):
        return ScopeDecision(UNKNOWN, [REASON_UNAVAILABLE], points, boundary, static_version)
    return ScopeDecision(IN_SCOPE, [], points, boundary, static_version)


def assess(conn: psycopg.Connection, state: BoundaryState, static_version: UUID | None,
           points: list[tuple[str, float, float, float]]) -> ScopeDecision:
    """(역할, 위도, 경도, 반경 m) 지점들을 판정한다. 같은 연결(트랜잭션)에서 경계·정류소를 함께 조회한다."""
    boundary_info: dict[str, Any] = {"available": False, "reason": state.reason}
    covers: dict[str, bool | None] = {role: None for role, *_ in points}
    if state.boundary is not None:
        rows = conn.execute(
            """
            WITH b AS (SELECT extensions.ST_SetSRID(extensions.ST_GeomFromGeoJSON(%(g)s), 4326) AS g),
                 p AS (SELECT * FROM unnest(%(roles)s::text[], %(lons)s::float8[], %(lats)s::float8[])
                        AS t(role, lon, lat))
            SELECT p.role, extensions.ST_IsValid(b.g),
                   extensions.ST_Covers(b.g, extensions.ST_SetSRID(extensions.ST_MakePoint(p.lon, p.lat), 4326))
              FROM b, p
            """,
            {"g": state.boundary.geometry_json, "roles": [p[0] for p in points], "lons": [p[2] for p in points],
             "lats": [p[1] for p in points]},
        ).fetchall()
        valid = all(row[1] for row in rows)
        boundary_info = {"available": valid, "reason": None if valid else "boundary_geometry_invalid",
                         "name": state.boundary.name, "file": Path(state.boundary.path).name,
                         "sha256": state.boundary.sha256}
        if valid:
            covers = {role: bool(inside) for role, _, inside in rows}
    published = static_version is not None and transit_repository.static_version_published(conn, static_version)
    results = []
    for role, lat, lon, radius in points:
        served, nearest = None, None
        if published:
            stops = transit_repository.nearby_stops(conn, lat, lon, radius, 1, served_version=static_version)
            served = len(stops)
            if stops:
                sid, ext, name, _, distance, _, _ = stops[0]
                nearest = {"stop_id": sid, "external_id": ext, "name": name, "distance_m": float(distance)}
        results.append(PointScope(role, lat, lon, radius, covers[role], served, nearest))
    return decide(results, boundary_info, str(static_version) if static_version else None)
