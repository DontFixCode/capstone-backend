"""격리 시험용 합성 TAGO 서버. 실제 수집 결과가 아닌 가상 입력이다.

httpx.MockTransport로 6개 상세기능(정류소 목록·노선 목록·노선 기본정보·경유 목록·노선별 위치·정류소별 도착)을
페이지 단위로 흉내 내고, 지정한 요청에 HTTP 500·시간초과·제공기관 오류·총건수 이전 빈 페이지·인증키 반사를 주입한다.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

FAKE_KEY = "Synthetic+Key/For==Phase2Tests0123"


def _station(node_id: str, name: str, lat: Any, lon: Any, no: Any) -> dict[str, Any]:
    item = {"nodeid": node_id, "nodenm": name, "nodeno": no}
    if lat is not None:
        item["gpslati"] = lat
    if lon is not None:
        item["gpslong"] = lon
    return item


def _route_stop(route_id: str, order: int, node_id: str, name: str, lat: float, lon: float, updown: int,
                no: Any = None) -> dict[str, Any]:
    item = {"routeid": route_id, "nodeord": order, "nodeid": node_id, "nodenm": name, "gpslati": lat,
            "gpslong": lon, "updowncd": updown}
    if no is not None:
        item["nodeno"] = no
    return item


def default_world() -> dict[str, Any]:
    """S1~S6은 대전 정류소 목록, X9는 정류소 목록에 없는 시외 경유 정류소다. S5는 (0,0), S6은 좌표 누락."""
    stations = [
        _station("DJB0000001", "가상정류소1", 36.3500, 127.3800, 10001),
        _station("DJB0000002", "가상정류소2", 36.3510, 127.3810, "10002"),
        _station("DJB0000003", "가상정류소3", 36.3600, 127.3900, 10003),
        _station("DJB0000004", "가상정류소4", 36.3700, 127.4000, 10004),
        _station("DJB0000005", "좌표0정류소", 0, 0, 10005),
        _station("DJB0000006", "좌표누락정류소", None, None, 10006),
    ]
    r1 = "DJB9000001"
    r2 = "DJB9000002"
    r3 = "DJB9000003"
    routes = {
        r1: {
            "list": {"routeid": r1, "routeno": 101, "routetp": "간선버스", "startnodenm": "가상정류소1",
                     "endnodenm": "가상정류소1", "startvehicletime": "0530", "endvehicletime": 2230},
            "info": {"routeid": r1, "routeno": 101, "routetp": "간선버스", "startnodenm": "가상정류소1",
                     "endnodenm": "가상정류소1", "startvehicletime": "0530", "endvehicletime": 2230,
                     "intervaltime": 12, "intervalsattime": 15, "intervalsuntime": 20},
            # 순환: S1→S2→S3(회차)→S2→S1. S1·S2를 서로 다른 순번·방향으로 반복 경유한다.
            "stops": [
                _route_stop(r1, 1, "DJB0000001", "가상정류소1", 36.3500, 127.3800, 0, 10001),
                _route_stop(r1, 2, "DJB0000002", "가상정류소2", 36.3510, 127.3810, 0, 10002),
                _route_stop(r1, 3, "DJB0000003", "가상정류소3", 36.3600, 127.3900, 1, 10003),
                _route_stop(r1, 4, "DJB0000002", "가상정류소2", 36.3510, 127.3810, 1, 10002),
                _route_stop(r1, 5, "DJB0000001", "가상정류소1", 36.3500, 127.3800, 1, 10001),
            ],
        },
        r2: {
            "list": {"routeid": r2, "routeno": "급행2", "routetp": "급행버스", "startnodenm": "가상정류소3",
                     "endnodenm": "시외정류소", "startvehicletime": "0600", "endvehicletime": 2200},
            "info": {"routeid": r2, "routeno": "급행2", "routetp": "급행버스", "startnodenm": "가상정류소3",
                     "endnodenm": "시외정류소", "startvehicletime": "0600", "endvehicletime": 2200,
                     "intervaltime": 30},
            "stops": [
                _route_stop(r2, 1, "DJB0000003", "가상정류소3", 36.3600, 127.3900, 0, 10003),
                _route_stop(r2, 2, "DJB0000004", "가상정류소4", 36.3700, 127.4000, 0, 10004),
                _route_stop(r2, 3, "CCB0000009", "시외정류소", 36.5000, 127.2000, 1),
            ],
        },
        r3: {
            "list": {"routeid": r3, "routeno": 3, "routetp": "지선버스", "startnodenm": "가상정류소4",
                     "endnodenm": "좌표누락정류소", "startvehicletime": "0700", "endvehicletime": 2100},
            "info": {"routeid": r3, "routeno": 3, "routetp": "지선버스", "startnodenm": "가상정류소4",
                     "endnodenm": "좌표누락정류소", "startvehicletime": "0700", "endvehicletime": 2100},
            "stops": [
                _route_stop(r3, 1, "DJB0000004", "가상정류소4", 36.3700, 127.4000, 0, 10004),
                _route_stop(r3, 2, "DJB0000005", "좌표0정류소", 36.3800, 127.4100, 0, 10005),
                _route_stop(r3, 3, "DJB0000006", "좌표누락정류소", 36.3900, 127.4200, 0, 10006),
            ],
        },
    }
    positions = {
        r1: [
            {"routenm": 101, "routetp": "간선버스", "nodeid": "DJB0000002", "nodenm": "가상정류소2", "nodeord": 2,
             "vehicleno": "대전70자1001", "gpslati": 36.3511, "gpslong": 127.3811},
            # nodeord 4의 경유 정류소는 S2인데 응답은 S3 → 경유 위치 미확정
            {"routenm": 101, "routetp": "간선버스", "nodeid": "DJB0000003", "nodenm": "가상정류소3", "nodeord": 4,
             "vehicleno": "대전70자1002", "gpslati": 36.3601, "gpslong": 127.3901},
            {"routenm": 101, "routetp": "간선버스", "nodeid": "DJB0000001", "nodenm": "가상정류소1", "nodeord": 5,
             "vehicleno": "대전70자1003", "gpslati": 0, "gpslong": 0},
        ],
    }
    arrivals = {
        "DJB0000003": [
            {"nodeid": "DJB0000003", "nodenm": "가상정류소3", "routeid": r1, "routeno": 101, "routetp": "간선버스",
             "arrprevstationcnt": 1, "vehicletp": "일반차량", "arrtime": 95},
            {"nodeid": "DJB0000003", "nodenm": "가상정류소3", "routeid": r2, "routeno": "급행2", "routetp": "급행버스",
             "arrprevstationcnt": 0, "vehicletp": "저상버스", "arrtime": 0},
        ],
        "DJB0000002": [
            {"nodeid": "DJB0000002", "nodenm": "가상정류소2", "routeid": r1, "routeno": 101, "routetp": "간선버스",
             "arrprevstationcnt": 3, "vehicletp": "일반차량", "arrtime": 400},
        ],
    }
    return {"stations": stations, "routes": routes, "positions": positions, "arrivals": arrivals}


@dataclass
class Fault:
    operation: str
    kind: str  # http500 | timeout | code | empty_page | echo_key
    match: dict[str, str] = field(default_factory=dict)
    page: int | None = None
    times: int | None = None  # None이면 계속 적용
    code: str = "99"
    message: str = "SYNTHETIC ERROR"


class FakeTago:
    def __init__(self, world: dict[str, Any] | None = None) -> None:
        self.world = world or default_world()
        self.faults: list[Fault] = []
        self.requests: list[tuple[str, dict[str, str]]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def count(self, operation: str | None = None) -> int:
        return sum(1 for op, _ in self.requests if operation is None or op == operation)

    def _items(self, operation: str, params: dict[str, str]) -> list[dict[str, Any]] | None:
        w = self.world
        if operation == "getSttnNoList":
            return w["stations"]
        if operation == "getRouteNoList":
            return [r["list"] for r in w["routes"].values()]
        if operation == "getRouteInfoIem":
            route = w["routes"].get(params.get("routeId", ""))
            return [route["info"]] if route else []
        if operation == "getRouteAcctoThrghSttnList":
            route = w["routes"].get(params.get("routeId", ""))
            return route["stops"] if route else []
        if operation == "getRouteAcctoBusLcList":
            return w["positions"].get(params.get("routeId", ""), [])
        if operation == "getSttnAcctoArvlPrearngeInfoList":
            return w["arrivals"].get(params.get("nodeId", ""), [])
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        params = dict(request.url.params)
        if params.get("serviceKey") != FAKE_KEY:
            return httpx.Response(401, text="unauthorized")
        public = {k: v for k, v in params.items() if k != "serviceKey"}
        self.requests.append((operation, public))
        page = int(params.get("pageNo", "1"))
        rows = int(params.get("numOfRows", "10"))
        for fault in self.faults:
            if fault.operation != operation or (fault.page is not None and fault.page != page):
                continue
            if any(public.get(k) != v for k, v in fault.match.items()):
                continue
            if fault.times is not None:
                if fault.times <= 0:
                    continue
                fault.times -= 1
            if fault.kind == "http500":
                return httpx.Response(500, text="internal error")
            if fault.kind == "timeout":
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            if fault.kind == "code":
                return httpx.Response(200, json={"response": {"header": {"resultCode": fault.code,
                                                                          "resultMsg": fault.message}}})
            if fault.kind == "empty_page":
                total = len(self._items(operation, public) or [])
                return httpx.Response(200, json=_body([], total=total, page=page, rows=rows))
            if fault.kind == "echo_key":
                items = self._items(operation, public) or []
                body = _body(copy.deepcopy(items[(page - 1) * rows: page * rows]), total=len(items), page=page,
                             rows=rows)
                body["response"]["header"]["resultMsg"] = f"echo {request.url}"
                return httpx.Response(200, text=json.dumps(body, ensure_ascii=False))
        items = self._items(operation, public)
        if items is None:
            return httpx.Response(404, text="unknown operation")
        chunk = copy.deepcopy(items[(page - 1) * rows: page * rows]) if operation != "getRouteInfoIem" else items
        return httpx.Response(200, text=json.dumps(_body(chunk, total=len(items), page=page, rows=rows),
                                                   ensure_ascii=False))


def _body(items: list[dict[str, Any]], *, total: int, page: int, rows: int) -> dict[str, Any]:
    if not items:
        container: Any = ""
    elif len(items) == 1:
        container = {"item": items[0]}
    else:
        container = {"item": items}
    return {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                         "body": {"items": container, "numOfRows": rows, "pageNo": page, "totalCount": total}}}
