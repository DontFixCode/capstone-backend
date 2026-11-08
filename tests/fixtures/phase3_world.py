"""Phase 3 격리 시험용 합성 정적·실시간·도보 응답. 실제 수집 결과가 아닌 가상 입력이다.

정류소(위도 0.005° ≈ 556 m 간격): A·B·C·D·E는 남북 일직선, C2는 C에서 약 82 m(다른 정류소 도보 환승), F는 D 동쪽.
노선
- R10 A→B→C→D→E (직행)
- R20 A→B→C→B→A (순환, B 반복 경유)
- R30 C2→D→E (C→C2 도보 환승)
- R40 A→C, R50 C→F, R60 F→E (환승 2회)
출발지는 A 근처, 목적지는 E 근처다. 도보는 FakeTmap이 직선거리 × 1.25, 1.2 m/s로 답한다.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

STOPS = {
    "TST000000A": ("가상A", 36.3500, 127.3800),
    "TST000000B": ("가상B", 36.3550, 127.3800),
    "TST000000C": ("가상C", 36.3600, 127.3800),
    "TST00000C2": ("가상C건너편", 36.3601, 127.3809),
    "TST000000D": ("가상D", 36.3650, 127.3800),
    "TST000000E": ("가상E", 36.3700, 127.3800),
    "TST000000F": ("가상F", 36.3650, 127.3850),
}
A, B, C, C2, D, E, F = "TST000000A", "TST000000B", "TST000000C", "TST00000C2", "TST000000D", "TST000000E", "TST000000F"
ROUTES = {
    "TSTR000010": ("10", [A, B, C, D, E]),
    "TSTR000020": ("20", [A, B, C, B, A]),
    "TSTR000030": ("30", [C2, D, E]),
    "TSTR000040": ("40", [A, C]),
    "TSTR000050": ("50", [C, F]),
    "TSTR000060": ("60", [F, E]),
}
ORIGIN = (36.3499, 127.3799)
DESTINATION = (36.3701, 127.3801)


def world() -> dict[str, Any]:
    """tests/fixtures/tago_world.FakeTago가 쓰는 형식의 정적·실시간 응답."""
    stations = [{"nodeid": sid, "nodenm": name, "nodeno": 90000 + i, "gpslati": lat, "gpslong": lon}
                for i, (sid, (name, lat, lon)) in enumerate(STOPS.items())]
    routes = {}
    for route_id, (no, stops) in ROUTES.items():
        item = {"routeid": route_id, "routeno": no, "routetp": "가상버스", "startnodenm": STOPS[stops[0]][0],
                "endnodenm": STOPS[stops[-1]][0], "startvehicletime": "0500", "endvehicletime": 2300}
        routes[route_id] = {
            "list": item, "info": dict(item, intervaltime=10),
            "stops": [{"routeid": route_id, "nodeord": order, "nodeid": sid, "nodenm": STOPS[sid][0],
                       "gpslati": STOPS[sid][1], "gpslong": STOPS[sid][2], "updowncd": 0 if order <= 3 else 1}
                      for order, sid in enumerate(stops, start=1)],
        }
    arrivals: dict[str, list[dict[str, Any]]] = {}
    for route_id, (no, stops) in ROUTES.items():
        for sid in dict.fromkeys(stops):
            arrivals.setdefault(sid, []).append({"nodeid": sid, "nodenm": STOPS[sid][0], "routeid": route_id,
                                                 "routeno": no, "routetp": "가상버스", "arrprevstationcnt": 2,
                                                 "vehicletp": "일반차량", "arrtime": 300})
    positions = {route_id: [{"routenm": no, "routetp": "가상버스", "nodeid": stops[0], "nodenm": STOPS[stops[0]][0],
                             "nodeord": 1, "vehicleno": f"대전70자{9000 + i}", "gpslati": STOPS[stops[0]][1],
                             "gpslong": STOPS[stops[0]][2]}]
                 for i, (route_id, (no, stops)) in enumerate(ROUTES.items())}
    return {"stations": stations, "routes": routes, "positions": positions, "arrivals": arrivals}


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


@dataclass
class FakeTmap:
    """합성 TMAP 보행자 경로 서버. fail_pairs의 (출발 좌표, 도착 좌표)는 400 오류를 돌려준다."""

    app_key: str = "synthetic-tmap-key-0000000000000000000000"
    fail_pairs: set[tuple[tuple[float, float], tuple[float, float]]] = field(default_factory=set)
    requests: list[dict[str, Any]] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("appKey") != self.app_key:
            return httpx.Response(403, json={"error": {"id": "403", "category": "gw", "code": "INVALID_API_KEY",
                                                       "message": "Forbidden"}})
        body = json.loads(request.content)
        self.requests.append(body)
        start, end = (body["startY"], body["startX"]), (body["endY"], body["endX"])
        if (start, end) in self.fail_pairs:
            return httpx.Response(400, json={"error": {"id": "400", "category": "tmap", "code": "9999",
                                                       "message": "synthetic failure"}})
        distance = round(haversine(*start, *end) * 1.25)
        seconds = round(distance / 1.2)
        return httpx.Response(200, json={"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [body["startX"], body["startY"]]},
             "properties": {"totalDistance": distance, "totalTime": seconds, "index": 0, "pointType": "SP"}},
            {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [
                [body["startX"], body["startY"]], [body["endX"], body["endY"]]]},
             "properties": {"index": 1, "distance": distance, "time": seconds}},
        ]})


def synthetic_eta_rows(patterns: Iterable[Any], *, basis_at: datetime, vehicles: int = 2, headway: int = 600,
                       per_stop: int = 120, first: int = 120, with_alight: bool = True,
                       run_keys: bool = True) -> list[dict[str, Any]]:
    """패턴의 모든 경유 위치에 차량별 제공 ETA(같은 차량·회차)를 만든다. 실제 TAGO 응답이 아니다."""
    rows = []
    for pattern in patterns:
        for k in range(vehicles):
            for stop in pattern.stops:
                if not with_alight and stop.seq > 1:
                    # 승차 후보 정류소(첫 경유 위치) 외에는 ETA를 주지 않아 하차 ETA 미확인을 만든다.
                    continue
                arrival = basis_at + timedelta(seconds=first + k * headway + (stop.seq - 1) * per_stop)
                rows.append({
                    "pattern_id": pattern.pattern_key, "stop_id": stop.stop_external_id, "stop_order": stop.seq,
                    "vehicle_id": f"V-{pattern.route_external_id}-{k}" if run_keys else None,
                    "run_key": f"{pattern.route_external_id}-run{k}" if run_keys else None,
                    "remaining_seconds": (arrival - basis_at).total_seconds(), "basis_at": basis_at,
                    "collected_at": basis_at,
                })
    return rows
