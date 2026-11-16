"""버스 노선 지도 형상의 저장 형식·정류소 대응·승하차 구간 자르기.

제공처는 실제 응답으로 노선 ID 연결을 확인한 곳만 쓴다(Repository Structure·Public Data). 현재 지원: ODsay 노선 그래픽
데이터(app/integrations/odsay_api.py, 2026-11-12 실응답 확인). 설정된 제공처가 없거나 아직 수집하지 않은 노선은
'미확보(unavailable)'와 사유로 반환한다. 정류소 좌표를 이은 선을 실제 도로 형상으로 저장하지 않는다(앱이 그리면 schematic 개략 표시).

저장 형식(app.routes.shape_info):

    {"provider": {"name": "odsay", "status": "matched" | "not_found", "route_id": "<제공처 노선 ID>" | null,
                  "checked_at": "<ISO8601>"},
     "patterns": {"<pattern_key>": {
        "status": "available" | "unavailable" | "error",
        "geometry": GeoJSON LineString | MultiLineString | null,   # 좌표 순서 [경도, 위도]
        "source": "<제공처>", "source_route_id": "...", "version": "...", "fetched_at": "<ISO8601>",
        "reason": null | "<미확보·오류 사유>",
        # 경유 순서(seq) → geometry 꼭짓점 위치.
        "stop_vertex_index": {"1": 0, "2": 17, ...},
        "stop_match": {"method": "...", "stops": 74, "matched": 74, "median_meters": 2.6, "max_meters": 9.1,
                       "unmatched_seqs": []}
    }}}

정류소 대응(build_pattern_shape): 제공처 형상은 노선 전체(기점→회차→종점)의 선 하나이고 정류소 위치 표시가 없다. 패턴의
정류소를 경유 순서대로 선 위에 투영한다(순서를 거스르지 않는 조합 중 정류소-선 거리 합이 최소인 조합). 투영점을 꼭짓점으로
끼워 넣고 그 위치를 stop_vertex_index에 둔다. 선에서 STOP_MATCH_MAX_METERS보다 먼 정류소는 대응에서 빼고(그 정류소에서
타고 내리는 구간은 shape_stop_mapping_unavailable), 대응된 정류소 비율이 PATTERN_MATCH_MIN_SHARE 미만이면 이 패턴의
형상이 아니라고 보고 저장하지 않는다(error, shape_stop_alignment_failed). 두 기준값은 2026-11-12 실데이터 분포를 보고 정한
개발 설정값이다(Notion Backend > 검증 대기 항목 6).

승하차 구간 형상은 stop_vertex_index로 자른다. 대응이 없는 정류소는 가까운 꼭짓점으로 추정하지 않고 미확보로 둔다.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

NOT_CONFIGURED_REASON = "shape_provider_not_configured"
PATTERN_MISSING_REASON = "pattern_shape_not_collected"
ROUTE_NOT_IN_PROVIDER_REASON = "shape_route_not_in_provider"
STOP_MAPPING_MISSING_REASON = "shape_stop_mapping_unavailable"
STOP_ALIGNMENT_FAILED_REASON = "shape_stop_alignment_failed"
SEGMENT_INVALID_REASON = "shape_segment_order_invalid"
STATUSES = ("available", "unavailable", "error")

SUPPORTED_PROVIDERS = ("odsay",)
STOP_MATCH_METHOD = "ordered_projection/1"
# 정류소가 형상 선에서 이 거리보다 멀면 그 정류소는 대응에서 뺀다.
STOP_MATCH_MAX_METERS = 100.0
# 대응된 정류소 비율이 이 값보다 낮으면 패턴과 형상이 맞지 않는 것으로 본다.
PATTERN_MATCH_MIN_SHARE = 0.9


@dataclass(frozen=True)
class StopPoint:
    """패턴의 경유 정류소 1개(경유 순서와 좌표)."""

    seq: int
    lon: float
    lat: float


@dataclass(frozen=True)
class StopMatch:
    """정류소를 선 위에 투영한 결과. segment는 line[segment]→line[segment+1] 구간, t는 그 구간 안의 위치(0~1)."""

    seq: int
    segment: int
    t: float
    lon: float
    lat: float
    distance_m: float


def configured_provider(name: str | None) -> str | None:
    """설정된 형상 제공처 이름. 비었거나 지원하지 않는 이름이면 None(형상 수집 안 함)."""
    value = (name or "").strip().lower()
    return value if value in SUPPORTED_PROVIDERS else None


def pattern_entry(shape_info: dict[str, Any] | None, pattern_key: str) -> dict[str, Any]:
    """routes.shape_info에서 패턴 형상 항목을 꺼낸다. 없으면 사유가 있는 미확보 항목."""
    info = shape_info or {}
    patterns = info.get("patterns") or {}
    entry = patterns.get(pattern_key)
    if not isinstance(entry, dict):
        provider = info.get("provider") if isinstance(info.get("provider"), dict) else {}
        if provider.get("status") == "not_found":
            reason = ROUTE_NOT_IN_PROVIDER_REASON
        else:
            reason = PATTERN_MISSING_REASON if patterns or provider else NOT_CONFIGURED_REASON
        return {"status": "unavailable", "geometry": None, "source": provider.get("name"),
                "source_route_id": provider.get("route_id"), "version": None, "fetched_at": None, "reason": reason}
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


# ---------------------------------------------------------------- 정류소 대응·형상 항목 생성

def match_stops(line: Sequence[Sequence[float]], stops: Sequence[StopPoint]) -> list[StopMatch]:
    """정류소를 경유 순서대로 선 위에 투영한다. 결과는 stops와 같은 순서다.

    뒤 정류소가 앞 정류소보다 선의 앞쪽에 놓이지 않는 조합 중 정류소-선 거리 합이 최소인 조합을 고른다. 같은 도로를 갈 때와
    올 때 두 번 지나는 노선도 경유 순서로 구분된다. 거리는 노선 범위에서의 평면 근사(미터)다.
    """
    segment_count = len(line) - 1
    if segment_count < 1 or not stops:
        return []
    lon0 = sum(p[0] for p in line) / len(line)
    lat0 = sum(p[1] for p in line) / len(line)
    kx, ky = 111_320.0 * math.cos(math.radians(lat0)), 110_540.0
    xs = [(p[0] - lon0) * kx for p in line]
    ys = [(p[1] - lat0) * ky for p in line]
    segments = [(xs[j], ys[j], xs[j + 1] - xs[j], ys[j + 1] - ys[j]) for j in range(segment_count)]
    lengths = [dx * dx + dy * dy for _, _, dx, dy in segments]

    # costs[j]: 이 정류소를 구간 j에 놓을 때 여기까지의 최소 거리 합. offsets[j]: 그때 구간 안 위치(0~1).
    # sources[i][j]: 그 조합에서 앞 정류소가 놓인 구간. 앞 정류소와 같은 구간이면 그 위치보다 앞에 놓지 않는다.
    costs: list[float] = []
    offsets: list[float] = []
    sources: list[list[int]] = []
    offsets_by_stop: list[list[float]] = []
    for number, stop in enumerate(stops):
        px, py = (stop.lon - lon0) * kx, (stop.lat - lat0) * ky
        next_costs = [0.0] * segment_count
        next_offsets = [0.0] * segment_count
        source = [-1] * segment_count
        earlier_cost, earlier_segment = math.inf, -1      # 앞 정류소를 j보다 앞 구간에 놓는 조합의 최소 비용
        for j, (ax, ay, dx, dy) in enumerate(segments):
            length = lengths[j]
            t = 0.0 if length == 0.0 else ((px - ax) * dx + (py - ay) * dy) / length
            t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
            distance = math.hypot(ax + t * dx - px, ay + t * dy - py)
            if number == 0:
                next_costs[j], next_offsets[j] = distance, t
                continue
            same_t = t if t >= offsets[j] else offsets[j]
            same_cost = costs[j] + (distance if same_t == t else
                                    math.hypot(ax + same_t * dx - px, ay + same_t * dy - py))
            if earlier_cost + distance <= same_cost:
                next_costs[j], next_offsets[j], source[j] = earlier_cost + distance, t, earlier_segment
            else:
                next_costs[j], next_offsets[j], source[j] = same_cost, same_t, j
            if costs[j] < earlier_cost:
                earlier_cost, earlier_segment = costs[j], j
        costs, offsets = next_costs, next_offsets
        sources.append(source)
        offsets_by_stop.append(next_offsets)

    segment = min(range(segment_count), key=costs.__getitem__)
    chosen = [0] * len(stops)
    for i in range(len(stops) - 1, -1, -1):
        chosen[i] = segment
        segment = sources[i][segment]

    matches: list[StopMatch] = []
    for stop, segment, stop_offsets in zip(stops, chosen, offsets_by_stop):
        t = stop_offsets[segment]
        a, b = line[segment], line[segment + 1]
        lon, lat = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
        distance = math.hypot((lon - stop.lon) * kx, (lat - stop.lat) * ky)
        matches.append(StopMatch(seq=stop.seq, segment=segment, t=t, lon=lon, lat=lat, distance_m=distance))
    return matches


def line_version(line: Sequence[Sequence[float]]) -> str:
    """제공처가 형상 버전을 주지 않으므로 좌표 내용의 해시를 버전으로 쓴다(형상이 바뀌면 값이 바뀐다)."""
    body = json.dumps([[p[0], p[1]] for p in line], separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()[:16]


def build_pattern_shape(line: Sequence[Sequence[float]], stops: Sequence[StopPoint], *, source: str,
                        source_route_id: str, fetched_at: str) -> dict[str, Any]:
    """제공처 형상 선과 패턴의 정류소로 routes.shape_info의 패턴 항목을 만든다."""
    base = {"source": source, "source_route_id": source_route_id, "version": line_version(line) if line else None,
            "fetched_at": fetched_at}
    if len(line) < 2:
        return {**base, "status": "error", "geometry": None, "reason": "shape_geometry_invalid",
                "stop_vertex_index": {}}
    matches = match_stops(line, stops)
    by_segment: dict[int, list[StopMatch]] = {}
    for match in matches:
        by_segment.setdefault(match.segment, []).append(match)
    coordinates: list[list[float]] = []
    index: dict[str, int] = {}
    for j, vertex in enumerate(line):
        coordinates.append([vertex[0], vertex[1]])
        for match in by_segment.get(j, ()):
            # 투영점을 꼭짓점으로 끼워 넣는다. 선분 위의 점이므로 형상은 바뀌지 않고, 구간을 정류소 위치에서 자를 수 있다.
            coordinates.append([round(match.lon, 7), round(match.lat, 7)])
            if match.distance_m <= STOP_MATCH_MAX_METERS:
                index[str(match.seq)] = len(coordinates) - 1
    distances = sorted(m.distance_m for m in matches)
    stats = {"method": STOP_MATCH_METHOD, "stops": len(stops), "matched": len(index),
             "median_meters": round(distances[len(distances) // 2], 1) if distances else None,
             "max_meters": round(distances[-1], 1) if distances else None,
             "unmatched_seqs": [m.seq for m in matches if str(m.seq) not in index]}
    if not stops or len(index) < PATTERN_MATCH_MIN_SHARE * len(stops):
        return {**base, "status": "error", "geometry": None, "reason": STOP_ALIGNMENT_FAILED_REASON,
                "stop_vertex_index": {}, "stop_match": stats}
    return {**base, "status": "available", "geometry": {"type": "LineString", "coordinates": coordinates},
            "reason": None, "stop_vertex_index": index, "stop_match": stats}
