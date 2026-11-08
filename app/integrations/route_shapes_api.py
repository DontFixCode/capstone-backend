"""버스 노선 지도 형상 제공처 어댑터 계약.

검증한 외부 제공처만 연결한다(Repository Structure·Public Data). 2026-11-08 현재 노선 ID·방향/패턴·경유 순서 연결을 확인한
제공처가 없으므로 설정된 제공처가 없고, 형상은 모두 '미확보(unavailable)'와 사유로 반환한다. 정류소 좌표를 이은 선을
실제 도로 형상으로 저장하지 않는다(앱이 그리면 schematic 개략 표시).

제공처를 연결할 때 저장 형식(app.routes.shape_info)은 다음을 따른다.

    {"patterns": {"<pattern_key>": {
        "status": "available" | "unavailable" | "error",
        "geometry": GeoJSON LineString | MultiLineString | null,   # 좌표 순서 [경도, 위도]
        "source": "<제공처>", "source_route_id": "...", "version": "...", "fetched_at": "<ISO8601>",
        "reason": null | "<미확보·오류 사유>",
        # 경유 순서(seq) → geometry 꼭짓점 위치. 원천에서 노선·경유 순서 연결을 확인한 경우에만 둔다.
        "stop_vertex_index": {"1": 0, "2": 17, ...}
    }}}

승하차 구간 형상은 stop_vertex_index로 자른다. 연결 근거(stop_vertex_index)가 없으면 좌표 근접으로 추정하지 않고 미확보로 둔다.
"""

from __future__ import annotations

from typing import Any, Protocol

NOT_CONFIGURED_REASON = "shape_provider_not_configured"
PATTERN_MISSING_REASON = "pattern_shape_not_collected"
STOP_MAPPING_MISSING_REASON = "shape_stop_mapping_unavailable"
SEGMENT_INVALID_REASON = "shape_segment_order_invalid"
STATUSES = ("available", "unavailable", "error")


class RouteShapeProvider(Protocol):
    """검증된 제공처 어댑터가 구현할 인터페이스. 패턴별 형상 항목(위 형식)을 반환한다."""

    name: str

    def pattern_shape(self, route_external_id: str, pattern_key: str) -> dict[str, Any]: ...


def configured_provider(name: str | None) -> RouteShapeProvider | None:
    """설정된 형상 제공처. 검증된 제공처가 아직 없으므로 항상 None이다."""
    return None


def pattern_entry(shape_info: dict[str, Any] | None, pattern_key: str) -> dict[str, Any]:
    """routes.shape_info에서 패턴 형상 항목을 꺼낸다. 없으면 제공처 미설정 사유의 미확보 항목."""
    patterns = (shape_info or {}).get("patterns") or {}
    entry = patterns.get(pattern_key)
    if not isinstance(entry, dict):
        return {"status": "unavailable", "geometry": None, "source": None, "source_route_id": None,
                "version": None, "fetched_at": None,
                "reason": NOT_CONFIGURED_REASON if not patterns else PATTERN_MISSING_REASON}
    status = entry.get("status") if entry.get("status") in STATUSES else "error"
    geometry, reason = entry.get("geometry"), entry.get("reason")
    if status == "available" and not _valid_geometry(geometry):
        status, geometry, reason = "error", None, "shape_geometry_invalid"
    elif status != "available":
        geometry = None
    return {"status": status, "geometry": geometry, "source": entry.get("source"),
            "source_route_id": entry.get("source_route_id"), "version": entry.get("version"),
            "fetched_at": entry.get("fetched_at"), "reason": reason,
            "stop_vertex_index": entry.get("stop_vertex_index") or {}}


def _valid_geometry(geometry: Any) -> bool:
    if not isinstance(geometry, dict):
        return False
    coords = geometry.get("coordinates")
    if geometry.get("type") == "LineString":
        return isinstance(coords, list) and len(coords) >= 2
    if geometry.get("type") == "MultiLineString":
        return isinstance(coords, list) and bool(coords) and all(isinstance(p, list) and len(p) >= 2 for p in coords)
    return False


def _flat(geometry: dict[str, Any]) -> list[list[float]]:
    if geometry["type"] == "LineString":
        return list(geometry["coordinates"])
    return [point for part in geometry["coordinates"] for point in part]


def segment_geometry(entry: dict[str, Any], board_seq: int, alight_seq: int) -> dict[str, Any]:
    """승차 seq~하차 seq 구간의 형상. 결과는 후보 bus leg의 geometry_* 필드 형식이다."""
    base = {"geometry": None, "geometry_status": entry["status"] if entry["status"] != "available" else "unavailable",
            "geometry_source": entry.get("source"), "geometry_version": entry.get("version"),
            "geometry_fetched_at": entry.get("fetched_at"), "geometry_reason": entry.get("reason")}
    if entry["status"] != "available" or entry.get("geometry") is None:
        return base
    index = entry.get("stop_vertex_index") or {}
    start, end = index.get(str(board_seq)), index.get(str(alight_seq))
    if not isinstance(start, int) or not isinstance(end, int):
        return {**base, "geometry_reason": STOP_MAPPING_MISSING_REASON}
    coords = _flat(entry["geometry"])
    if not (0 <= start < end < len(coords)):
        return {**base, "geometry_reason": SEGMENT_INVALID_REASON}
    return {**base, "geometry": {"type": "LineString", "coordinates": coords[start:end + 1]},
            "geometry_status": "available", "geometry_reason": None}
