"""inspect_transit 실행 결과의 연결 검사·필드 현황·호출량 요약을 만든다.

API를 호출하지 않고 저장된 JSON만 읽는다. 일부만 조회한 자료의 ID 미연결을 원천 오류로 단정하지 않고,
방향·차량 연결 등 응답에 없는 정보는 추정하지 않는다.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.integrations import bus_api
from app.integrations.bus_api import field_state, text_value, to_int

SAMPLE_LIMIT = 20
# 공식 명세의 좌표기반근접정류소 조회 반경
NEARBY_RADIUS_M = 500
_JSON_TYPES = {
    "str": "string", "int": "number(int)", "float": "number(float)", "bool": "boolean",
    "NoneType": "null", "dict": "object", "list": "array",
}
_BLANK = ("missing", "null", "empty")


def find_runs(output_dir: Path) -> list[Path]:
    """출력 위치에서 조회 실행 디렉터리(run.json 보유)를 찾는다. analyze 결과는 제외된다."""
    if not output_dir.is_dir():
        return []
    return sorted(path for path in output_dir.iterdir() if (path / "run.json").is_file())


def load_runs(paths: list[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    runs: list[dict[str, Any]] = []
    collections: list[dict[str, Any]] = []
    for path in paths:
        run_file = path / "run.json"
        if not run_file.is_file():
            raise ValueError(f"run.json이 없는 경로입니다: {path}")
        run = json.loads(run_file.read_text(encoding="utf-8"))
        run["_dir"] = str(path)
        runs.append(run)
        for file in sorted((path / "responses").glob("*.json")):
            record = json.loads(file.read_text(encoding="utf-8"))
            record["_file"] = str(file)
            record["_run_started_at"] = run.get("started_at") or ""
            collections.append(record)
    return runs, collections


def route_ids(items: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(value for value in (text_value(item, "routeid") for item in items) if value))


def route_ids_from(path: Path) -> tuple[list[str], list[str]]:
    """저장된 노선 목록 결과에서 노선 ID를 읽는다."""
    _, collections = load_runs([path])
    lists = [record for record in collections if record["operation"] == bus_api.ROUTE_LIST.operation]
    if not lists:
        raise ValueError(f"{path}에 저장된 노선 목록(getRouteNoList) 결과가 없습니다.")
    ids = route_ids([item for record in lists for item in record["items"]])
    notes = [f"저장된 노선 목록 사용: {path} (노선 {len(ids)}개)"]
    for record in lists:
        if record["status"] != "success" or record["request"]["params"].get("routeNo"):
            notes.append(
                f"저장된 노선 목록({record['target']})이 {record['status']} 상태이거나 노선번호 조건 결과라 "
                "전체 노선이 아닐 수 있습니다."
            )
    return ids, notes


def summarize(
    runs: list[dict[str, Any]],
    collections: list[dict[str, Any]],
    *,
    active_hours: float = 24.0,
    interval_minutes: float = 10.0,
    location_targets: int | None = None,
    arrival_targets: int | None = None,
    daily_limit: int | None = None,
    max_retries: int = 0,
) -> dict[str, Any]:
    chosen = _preferred(collections)
    grouped: dict[str, list[dict[str, Any]]] = {operation: [] for operation in bus_api.ENDPOINTS}
    for record in chosen:
        grouped.setdefault(record["operation"], []).append(record)
    station_records = grouped[bus_api.STATION_LIST.operation]
    route_records = grouped[bus_api.ROUTE_LIST.operation]
    stations = _index(station_records, "nodeid")
    routes = _index(route_records, "routeid")
    station_full = _full_list(station_records, ("nodeNm", "nodeNo"))
    route_full = _full_list(route_records, ("routeNo",))
    stops_by_route = _by_param(grouped[bus_api.ROUTE_STOPS.operation], "routeId")
    locations_by_route = _by_param(grouped[bus_api.BUS_LOCATIONS.operation], "routeId")
    arrivals_by_node = _by_param(grouped[bus_api.ARRIVALS.operation], "nodeId")

    return {
        "schema": "transit-inspection/summary@1",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "inputs": [
            {"dir": run["_dir"], "mode": run.get("mode"), "status": run.get("status"), "started_at": run.get("started_at")}
            for run in runs
        ],
        "collections_used": {
            "loaded": len(collections),
            "used": len(chosen),
            "superseded": len(collections) - len(chosen),
            "rule": "같은 상세기능·조회 조건은 가장 완전한 결과를, 완전성이 같으면 최신 결과를 사용",
        },
        "counts": {
            "stations": _list_count(station_records, stations, station_full),
            "routes": _list_count(route_records, routes, route_full),
            "route_info": {
                "routes": len(grouped[bus_api.ROUTE_INFO.operation]),
                "items": sum(len(record["items"]) for record in grouped[bus_api.ROUTE_INFO.operation]),
            },
            "route_stops": {
                "routes": len(stops_by_route),
                "entries": sum(len(record["items"]) for record in stops_by_route.values()),
                "unique_nodeid": len({
                    text_value(item, "nodeid") for record in stops_by_route.values() for item in record["items"]
                } - {None}),
            },
            "bus_locations": {
                "routes": len(locations_by_route),
                "vehicles": sum(len(record["items"]) for record in locations_by_route.values()),
            },
            "arrivals": {
                "stations": len(arrivals_by_node),
                "items": sum(len(record["items"]) for record in arrivals_by_node.values()),
            },
            "nearby": {
                "queries": len(grouped[bus_api.NEARBY_STATIONS.operation]),
                "items": sum(len(record["items"]) for record in grouped[bus_api.NEARBY_STATIONS.operation]),
            },
        },
        "collection_status": {
            operation: dict(Counter(record["status"] for record in records))
            for operation, records in grouped.items()
            if records
        },
        "fields": {
            endpoint.operation: _field_inventory(endpoint, grouped[endpoint.operation])
            for endpoint in bus_api.ENDPOINTS.values()
            if grouped[endpoint.operation]
        },
        "stop_station_linkage": _stop_station_linkage(stops_by_route, stations, station_full, bool(station_records)),
        "route_stop_order": _route_stop_order(stops_by_route),
        "bus_locations": _locations(locations_by_route, routes, stops_by_route, bool(route_records)),
        "arrivals": _arrivals(
            arrivals_by_node, routes, stops_by_route, bool(route_records), stations, bool(station_records)
        ),
        "coordinates": _coordinates(grouped),
        "nearby": _nearby(grouped[bus_api.NEARBY_STATIONS.operation]),
        "call_volume": _call_volume(
            runs, grouped, routes, stops_by_route,
            active_hours=active_hours, interval_minutes=interval_minutes, location_targets=location_targets,
            arrival_targets=arrival_targets, daily_limit=daily_limit, max_retries=max_retries,
        ),
    }


def render(summary: dict[str, Any]) -> list[str]:
    """콘솔용 요약. 상세 내용은 summary.json에 있다."""
    counts = summary["counts"]
    lines = [f"[요약] 입력 실행 {len(summary['inputs'])}건, 조회 결과 {summary['collections_used']['used']}건 사용"]
    if summary["collections_used"]["superseded"]:
        lines.append(
            f"- 같은 조회 조건의 결과 {summary['collections_used']['superseded']}건은 더 완전하거나 최신인 결과로 대체"
        )
    for key, label in (("stations", "정류소"), ("routes", "노선")):
        count = counts[key]
        if count["collections"]:
            reported = "미확인" if count["reported_total"] is None else count["reported_total"]
            lines.append(
                f"- {label} 목록: 고유 ID {count['unique_ids']}개, 전체 목록 조회 완료 {'예' if count['complete_list'] else '아니오'}"
                f", 검색 조건 없는 목록의 totalCount {reported}"
            )
    if counts["route_stops"]["routes"]:
        lines.append(
            f"- 경유 정류소: 노선 {counts['route_stops']['routes']}개, 항목 {counts['route_stops']['entries']}개, "
            f"고유 정류소 {counts['route_stops']['unique_nodeid']}개"
        )
    linkage = summary["stop_station_linkage"]
    if linkage.get("checked"):
        lines.append(
            f"- 경유↔정류소 목록 ID 연결: {linkage['matched']}/{linkage['unique_stop_nodeids']} 연결, "
            f"미연결 {linkage['unmatched']}, 이름 불일치 {linkage['name_mismatch']} — {linkage['interpretation']}"
        )
    elif linkage.get("reason"):
        lines.append(f"- 경유↔정류소 목록 ID 연결: 검사 안 함 ({linkage['reason']})")
    order = summary["route_stop_order"]
    if order["routes"]:
        lines.append(
            f"- 경유 순서·방향: updowncd 값 {order['updowncd_values']}, 순번 중복 노선 {len(order['routes_with_duplicate_nodeord'])}개, "
            f"1부터 연속 아님 {len(order['routes_not_contiguous_from_1'])}개, updowncd 없음 {len(order['routes_without_updowncd'])}개"
        )
        repeated = order["repeated_visits"]
        lines.append(
            f"- 반복 경유: 노선 {repeated['routes']}개에서 정류소 {repeated['nodes']}곳 (오류로 단정하지 않음, routeId+nodeord로 구분)"
        )
    locations = summary["bus_locations"]
    if locations["routes"]:
        lines.append(
            f"- 버스 위치: 노선 {locations['routes']}개, 차량 {locations['vehicles']}대, 응답 routeid 항목 "
            f"{'있음' if locations['response_has_routeid'] else '없음 → 요청 routeId(조회 맥락)로만 연결'}, "
            f"경유 정류소와 nodeid 연결 {locations['nodeid_on_route_stops']}"
        )
    arrivals = summary["arrivals"]
    if arrivals["stations"]:
        arrtime = arrivals["arrtime"]
        lines.append(
            f"- 도착정보: 정류소 {arrivals['stations']}개, 항목 {arrivals['items']}개, arrtime 정수 변환 "
            f"{arrtime['integer_convertible']}/{arrtime['values']} (변환 불가 {arrtime['not_convertible']}), "
            f"차량번호 항목 {'있음' if arrivals['vehicle_number_field_present'] else '없음 → 위치정보 차량과 연결하지 않음'}"
        )
    for operation, info in summary["coordinates"].items():
        anomalies = info["missing_or_empty"] + info["non_numeric"] + info["zero"] + info["out_of_wgs84_range"]
        lines.append(
            f"- 좌표({operation}): 유효 {info['valid']}/{info['items']}, 이상 {anomalies} "
            f"(누락 {info['missing_or_empty']}, 숫자 아님 {info['non_numeric']}, 0값 {info['zero']}, 범위 밖 {info['out_of_wgs84_range']})"
        )
    for operation, inventory in summary["fields"].items():
        if inventory["undocumented"] or inventory["documented_never_present"]:
            lines.append(
                f"- 필드({operation}): 명세 외 {inventory['undocumented'] or '-'}, "
                f"값이 한 번도 없는 명세 필드 {inventory['documented_never_present'] or '-'}"
            )
    volume = summary["call_volume"]
    for service, stats in volume["observed_by_service"].items():
        lines.append(
            f"- 실제 호출({service}): HTTP 시도 {stats['http_attempts']}, 재시도 {stats['retries']}, "
            f"성공 {stats['succeeded']}(빈 결과 {stats['empty']}), 실패 {stats['failed']}, 예산으로 미전송 {stats['not_sent']}"
        )
    for key, label in (
        ("static_daily_fixed_full", "정적 하루 1회 전체 갱신"),
        ("realtime_fixed_full", "실시간 고정 전체 수집"),
        ("realtime_user_needed", "실시간 필요 대상 수집"),
    ):
        totals = volume[key]["service_totals"]
        if all(total["requests"] is None for total in totals.values()):
            lines.append(f"- 일일 호출량 추정({label}): 미확인 — 계산식과 미확인 항목은 summary.json 참고")
        else:
            text = ", ".join(f"{service} {_format_total(total)}" for service, total in totals.items())
            lines.append(f"- 일일 호출량 추정({label}): {text}")
    lines.append(f"- 계정 한도: {volume['daily_limit']['note']}")
    return lines


def _preferred(collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """같은 상세기능·조회 조건은 가장 완전한 결과를, 완전성이 같으면 최신 결과를 쓴다."""
    rank = {"success": 2, "empty": 2, "partial": 1, "failed": 0}
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for record in sorted(collections, key=lambda item: (item["_run_started_at"], item["_file"])):
        key = (record["operation"], json.dumps(record["request"]["params"], sort_keys=True))
        current = chosen.get(key)
        if current is None or rank.get(record["status"], 0) >= rank.get(current["status"], 0):
            chosen[key] = record
    return list(chosen.values())


def _index(records: list[dict[str, Any]], id_field: str) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for record in records:
        for item in record["items"]:
            key = text_value(item, id_field)
            if key is not None:
                index.setdefault(key, item)
    return index


def _by_param(records: list[dict[str, Any]], name: str) -> dict[str, dict[str, Any]]:
    return {record["request"]["params"][name]: record for record in records if record["request"]["params"].get(name)}


def _full_list(records: list[dict[str, Any]], filters: tuple[str, ...]) -> dict[str, Any] | None:
    """검색 조건 없이 조회한 목록 중 완료된 결과를 우선 고른다."""
    candidates = [record for record in records if not any(record["request"]["params"].get(name) for name in filters)]
    complete = [record for record in candidates if record["status"] in ("success", "empty")]
    if complete:
        return complete[-1]
    return candidates[-1] if candidates else None


def _list_count(records: list[dict[str, Any]], index: dict[str, Any], full: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "collections": len(records),
        "items": sum(len(record["items"]) for record in records),
        "unique_ids": len(index),
        "complete_list": bool(full and full["status"] in ("success", "empty")),
        "reported_total": full["total_count"] if full else None,
    }


def _field_inventory(endpoint: bus_api.Endpoint, records: list[dict[str, Any]]) -> dict[str, Any]:
    items = [item for record in records for item in record["items"]]
    documented = list(endpoint.documented_fields)
    extra = sorted({name for item in items for name in item} - set(documented))
    fields = {}
    for name in documented + extra:
        states = Counter(field_state(item, name) for item in items)
        types = Counter(_JSON_TYPES.get(type(item[name]).__name__, type(item[name]).__name__) for item in items if name in item)
        fields[name] = {
            "documented": name in documented,
            **{state: states.get(state, 0) for state in ("present", "zero", "empty", "null", "missing")},
            "json_types": dict(types),
        }
    return {
        "items": len(items),
        "fields": fields,
        "undocumented": extra,
        "documented_never_present": [
            name for name in documented if items and fields[name]["present"] + fields[name]["zero"] == 0
        ],
    }


def _stop_station_linkage(
    stops_by_route: dict[str, dict[str, Any]],
    stations: dict[str, dict[str, Any]],
    station_full: dict[str, Any] | None,
    stations_loaded: bool,
) -> dict[str, Any]:
    if not stops_by_route:
        return {"checked": False, "reason": "경유 정류소 미조회"}
    if not stations_loaded:
        return {"checked": False, "reason": "정류소 목록 미조회"}
    first_seen: dict[str, tuple[str, dict[str, Any]]] = {}
    entries = 0
    for route_id, record in stops_by_route.items():
        for item in record["items"]:
            entries += 1
            node_id = text_value(item, "nodeid")
            if node_id:
                first_seen.setdefault(node_id, (route_id, item))
    matched = [node_id for node_id in first_seen if node_id in stations]
    unmatched = [node_id for node_id in first_seen if node_id not in stations]
    mismatched_names = [
        {
            "nodeid": node_id,
            "station_list_nodenm": text_value(stations[node_id], "nodenm"),
            "route_stop_nodenm": text_value(first_seen[node_id][1], "nodenm"),
        }
        for node_id in matched
        if text_value(stations[node_id], "nodenm") != text_value(first_seen[node_id][1], "nodenm")
    ]
    complete = bool(station_full and station_full["status"] in ("success", "empty"))
    if complete:
        interpretation = (
            "검색 조건 없는 정류소 목록 전체와 비교했다. 미연결은 시외 경유 정류소이거나 두 API의 제공 범위 차이일 수 있어 "
            "원천 오류로 단정하지 않는다."
        )
    else:
        interpretation = "정류소 목록을 일부만 조회했으므로 미연결은 조회 범위 차이일 수 있으며 원천 데이터 오류로 판단하지 않는다."
    return {
        "checked": True,
        "station_list_complete": complete,
        "stop_entries": entries,
        "unique_stop_nodeids": len(first_seen),
        "matched": len(matched),
        "unmatched": len(unmatched),
        "unmatched_sample": [
            {"nodeid": node_id, "nodenm": text_value(first_seen[node_id][1], "nodenm"), "request_routeId": first_seen[node_id][0]}
            for node_id in unmatched[:SAMPLE_LIMIT]
        ],
        "name_mismatch": len(mismatched_names),
        "name_mismatch_sample": mismatched_names[:SAMPLE_LIMIT],
        "interpretation": interpretation,
    }


def _route_stop_order(stops_by_route: dict[str, dict[str, Any]]) -> dict[str, Any]:
    per_route = []
    updown_total: Counter[str] = Counter()
    for route_id, record in sorted(stops_by_route.items()):
        items = record["items"]
        orders = [_int(item.get("nodeord")) for item in items]
        valid = [order for order in orders if order is not None]
        duplicates = sorted(order for order, count in Counter(valid).items() if count > 1)
        updown = Counter(_label(item, "updowncd") for item in items)
        updown_total.update(updown)
        ordered = sorted(
            ((order, item) for order, item in zip(orders, items) if order is not None), key=lambda pair: pair[0]
        )
        sequence = [value for value in (text_value(item, "updowncd") for _, item in ordered) if value is not None]
        visits: dict[str, list[tuple[int | None, str | None, str | None]]] = defaultdict(list)
        for order, item in zip(orders, items):
            node_id = text_value(item, "nodeid")
            if node_id:
                visits[node_id].append((order, text_value(item, "updowncd"), text_value(item, "nodenm")))
        repeated = [
            {
                "nodeid": node_id,
                "nodenm": positions[0][2],
                "nodeord": [order for order, _, _ in positions],
                "updowncd": [direction for _, direction, _ in positions],
            }
            for node_id, positions in visits.items()
            if len(positions) > 1
        ]
        per_route.append({
            "request_routeId": route_id,
            "status": record["status"],
            "entries": len(items),
            "nodeord_non_numeric": len(items) - len(valid),
            "nodeord_duplicates": duplicates[:SAMPLE_LIMIT],
            "nodeord_contiguous_from_1": bool(valid) and sorted(valid) == list(range(1, len(valid) + 1)),
            "updowncd": dict(updown),
            "updowncd_changes_along_nodeord": sum(1 for before, after in zip(sequence, sequence[1:]) if before != after),
            "repeated_nodes": repeated,
        })
    complete = [route for route in per_route if route["status"] == "success"]
    repeated_routes = [route for route in per_route if route["repeated_nodes"]]
    return {
        "routes": len(per_route),
        "complete_routes": len(complete),
        "incomplete_routes": [route["request_routeId"] for route in per_route if route["status"] != "success"],
        "routes_with_non_numeric_nodeord": [route["request_routeId"] for route in per_route if route["nodeord_non_numeric"]],
        "routes_with_duplicate_nodeord": [route["request_routeId"] for route in per_route if route["nodeord_duplicates"]],
        "routes_not_contiguous_from_1": [
            route["request_routeId"] for route in complete if not route["nodeord_contiguous_from_1"]
        ],
        "updowncd_values": dict(updown_total),
        "routes_without_updowncd": [
            route["request_routeId"] for route in per_route
            if route["entries"] and all(label.startswith("<") for label in route["updowncd"])
        ],
        "routes_with_updowncd_change": sum(1 for route in per_route if route["updowncd_changes_along_nodeord"]),
        "repeated_visits": {
            "routes": len(repeated_routes),
            "nodes": sum(len(route["repeated_nodes"]) for route in repeated_routes),
            "sample": [
                {"request_routeId": route["request_routeId"], **node}
                for route in repeated_routes for node in route["repeated_nodes"]
            ][:SAMPLE_LIMIT],
        },
        "per_route": per_route,
        "notes": [
            "반복 경유는 오류로 보지 않는다. 정류소 자체(nodeid)와 노선상의 경유 위치(routeId+nodeord)를 구분한다.",
            "updowncd(0:상행, 1:하행)가 없는 항목의 방향·분기는 추정하지 않는다.",
        ],
    }


def _locations(
    by_route: dict[str, dict[str, Any]],
    routes: dict[str, dict[str, Any]],
    stops_by_route: dict[str, dict[str, Any]],
    routes_loaded: bool,
) -> dict[str, Any]:
    per_route = []
    on_route = [0, 0, 0]
    for route_id, record in sorted(by_route.items()):
        items = record["items"]
        vehicles = [value for value in (text_value(item, "vehicleno") for item in items) if value]
        route_names = sorted({value for value in (text_value(item, "routenm") for item in items) if value})
        route_no = text_value(routes[route_id], "routeno") if route_id in routes else None
        entry: dict[str, Any] = {
            "request_routeId": route_id,
            "request_routeId_in_route_list": (route_id in routes) if routes_loaded else None,
            "status": record["status"],
            "vehicles": len(items),
            "vehicleno_present": len(vehicles),
            "vehicleno_duplicates": sorted(value for value, count in Counter(vehicles).items() if count > 1),
            "response_routenm": route_names,
            "route_list_routeno": route_no,
            "routenm_matches_route_list": (route_no in route_names) if route_no and route_names else None,
            "route_stops_loaded": route_id in stops_by_route,
        }
        if route_id in stops_by_route:
            stop_items = stops_by_route[route_id]["items"]
            stop_ids = {text_value(stop, "nodeid") for stop in stop_items}
            stop_pairs = {(text_value(stop, "nodeid"), _int(stop.get("nodeord"))) for stop in stop_items}
            entry["nodeid_on_route_stops"] = sum(text_value(item, "nodeid") in stop_ids for item in items)
            entry["nodeid_nodeord_on_route_stops"] = sum(
                (text_value(item, "nodeid"), _int(item.get("nodeord"))) in stop_pairs for item in items
            )
            on_route[0] += entry["nodeid_on_route_stops"]
            on_route[1] += len(items)
            on_route[2] += 1
        per_route.append(entry)
    items = [item for record in by_route.values() for item in record["items"]]
    return {
        "routes": len(per_route),
        "vehicles": len(items),
        "response_has_routeid": any("routeid" in item for item in items),
        "route_list_loaded": routes_loaded,
        "nodeid_on_route_stops": f"{on_route[0]}/{on_route[1]}" if on_route[2] else "경유 정류소 미조회",
        "per_route": per_route,
        "notes": [
            "응답에 routeid 항목이 없으면 요청 routeId(조회 맥락)로만 노선과 연결하며 응답 원본 필드와 구분한다.",
            "명세상 좌표는 맵매칭 좌표이며 원천 관측 시각 항목은 없다(미제공).",
            "존재하지 않는 routeId도 빈 결과로 응답하므로 빈 결과만으로 미운행을 판단하지 않는다. 노선 목록과 대조한다.",
        ],
    }


def _arrivals(
    by_node: dict[str, dict[str, Any]],
    routes: dict[str, dict[str, Any]],
    stops_by_route: dict[str, dict[str, Any]],
    routes_loaded: bool,
    stations: dict[str, dict[str, Any]],
    stations_loaded: bool,
) -> dict[str, Any]:
    per_node = []
    for node_id, record in sorted(by_node.items()):
        items = record["items"]
        response_ids = [text_value(item, "routeid") for item in items]
        stop_checks = [
            node_id in {text_value(stop, "nodeid") for stop in stops_by_route[route_id]["items"]}
            for route_id in response_ids
            if route_id in stops_by_route
        ]
        per_node.append({
            "request_nodeId": node_id,
            "request_nodeId_in_station_list": (node_id in stations) if stations_loaded else None,
            "status": record["status"],
            "items": len(items),
            "response_nodeid_differs": sum(text_value(item, "nodeid") != node_id for item in items),
            "routeid_in_route_list": sum(route_id in routes for route_id in response_ids if route_id) if routes_loaded else None,
            "routeid_with_stops_loaded": len(stop_checks),
            "request_nodeId_on_route_stops": sum(stop_checks),
        })
    items = [item for record in by_node.values() for item in record["items"]]
    return {
        "stations": len(per_node),
        "items": len(items),
        "arrtime": _numeric_report(items, "arrtime"),
        "arrprevstationcnt": _numeric_report(items, "arrprevstationcnt"),
        "vehicle_number_field_present": any("vehicleno" in item for item in items),
        "per_station": per_node,
        "notes": [
            "명세상 arrtime 단위는 초다. 기본 도착 응답에는 차량번호가 없어 위치정보의 특정 차량과 연결하지 않는다.",
            "도착정보 갱신 지연·자동 탑승 식별 가능성은 이 요약으로 검증하지 않는다.",
        ],
    }


def _numeric_report(items: list[dict[str, Any]], name: str) -> dict[str, Any]:
    states = Counter(field_state(item, name) for item in items)
    converted: list[int] = []
    failed: list[Any] = []
    for item in items:
        if field_state(item, name) in _BLANK:
            continue
        value = _int(item[name])
        if value is None:
            failed.append(item[name])
        else:
            converted.append(value)
    return {
        "values": len(converted) + len(failed),
        "integer_convertible": len(converted),
        "not_convertible": len(failed),
        "not_convertible_sample": [repr(value) for value in failed[:SAMPLE_LIMIT]],
        "missing_or_empty": sum(states[state] for state in _BLANK),
        "zero": sum(value == 0 for value in converted),
        "negative": sum(value < 0 for value in converted),
        "min": min(converted) if converted else None,
        "max": max(converted) if converted else None,
        "median": statistics.median(converted) if converted else None,
    }


def _coordinates(grouped: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    result = {}
    for endpoint in bus_api.ENDPOINTS.values():
        items = [item for record in grouped[endpoint.operation] for item in record["items"]]
        if "gpslati" not in endpoint.documented_fields or not items:
            continue
        valid: list[tuple[float, float]] = []
        counts = Counter()
        for item in items:
            if field_state(item, "gpslati") in _BLANK or field_state(item, "gpslong") in _BLANK:
                counts["missing_or_empty"] += 1
                continue
            latitude, longitude = _float(item["gpslati"]), _float(item["gpslong"])
            if latitude is None or longitude is None:
                counts["non_numeric"] += 1
            elif latitude == 0 or longitude == 0:
                counts["zero"] += 1
            elif not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                counts["out_of_wgs84_range"] += 1
            else:
                valid.append((latitude, longitude))
        result[endpoint.operation] = {
            "items": len(items),
            "valid": len(valid),
            **{key: counts[key] for key in ("missing_or_empty", "non_numeric", "zero", "out_of_wgs84_range")},
            "extent": {
                "lat_min": min(lat for lat, _ in valid), "lat_max": max(lat for lat, _ in valid),
                "lon_min": min(lon for _, lon in valid), "lon_max": max(lon for _, lon in valid),
            } if valid else None,
        }
    return result


def _nearby(records: list[dict[str, Any]]) -> dict[str, Any]:
    queries = []
    for record in records:
        params = record["request"]["params"]
        origin = (_float(params.get("gpsLati")), _float(params.get("gpsLong")))
        distances = []
        for item in record["items"]:
            point = (_float(item.get("gpslati")), _float(item.get("gpslong")))
            if None not in origin and None not in point:
                distances.append(_haversine_m(origin, point))
        queries.append({
            "gpsLati": params.get("gpsLati"),
            "gpsLong": params.get("gpsLong"),
            "status": record["status"],
            "items": len(record["items"]),
            "citycode": dict(Counter(text_value(item, "citycode") or "<없음>" for item in record["items"])),
            "max_straight_distance_m": round(max(distances)) if distances else None,
            "beyond_documented_radius": sum(distance > NEARBY_RADIUS_M for distance in distances),
        })
    return {
        "queries": queries,
        "notes": [
            "거리는 두 좌표의 직선거리(구면 근사)이며 실제 도보 경로·도보시간이 아니다.",
            f"명세상 조회 반경은 {NEARBY_RADIUS_M}m이고 cityCode 없이 좌표로만 조회하므로 다른 도시 정류소가 포함될 수 있다.",
        ],
    }


def _call_volume(
    runs: list[dict[str, Any]],
    grouped: dict[str, list[dict[str, Any]]],
    routes: dict[str, dict[str, Any]],
    stops_by_route: dict[str, dict[str, Any]],
    *,
    active_hours: float,
    interval_minutes: float,
    location_targets: int | None,
    arrival_targets: int | None,
    daily_limit: int | None,
    max_retries: int,
) -> dict[str, Any]:
    observed: dict[str, Counter[str]] = defaultdict(Counter)
    for run in runs:
        for path, counts in (run.get("call_stats") or {}).items():
            observed[path].update(counts)
    by_service: dict[str, Counter[str]] = defaultdict(Counter)
    for path, counts in observed.items():
        by_service[path.split("/")[0]].update(counts)
    attempts = sum(counts["http_attempts"] for counts in observed.values())
    retries = sum(counts["retries"] for counts in observed.values())

    calls_per_target = math.ceil(active_hours * 60 / interval_minutes) if active_hours > 0 else 0
    station_list = _list_pages(grouped[bus_api.STATION_LIST.operation], ("nodeNm", "nodeNo"))
    route_list = _list_pages(grouped[bus_api.ROUTE_LIST.operation], ("routeNo",))
    station_total = station_list["total"] if station_list else None
    route_total = route_list["total"] if route_list else None

    known_stop_pages = {
        route_id: pages for route_id, record in stops_by_route.items()
        if route_id in routes and (pages := _pages_needed(record)) is not None
    }
    if route_total is None:
        stops_row = _row(bus_api.ROUTE_STOPS, None, "노선 수 Σ ceil(노선별 경유 정류소 수 / numOfRows)", "노선 목록 totalCount 미확인")
    elif len(known_stop_pages) >= route_total:
        stops_row = _row(bus_api.ROUTE_STOPS, sum(known_stop_pages.values()), "Σ ceil(노선별 경유 정류소 수 / numOfRows)", None)
    else:
        lower = sum(known_stop_pages.values()) + (route_total - len(known_stop_pages))
        stops_row = _row(
            bus_api.ROUTE_STOPS, lower, "Σ ceil(노선별 경유 정류소 수 / numOfRows)",
            f"{route_total - len(known_stop_pages)}개 노선의 경유 정류소 수 미확인 — 노선당 1페이지로 계산한 하한", lower_bound=True,
        )
    static_rows = [
        _row(bus_api.STATION_LIST, station_list["pages"] if station_list else None,
             "ceil(정류소 totalCount / numOfRows)", None if station_list else "정류소 목록 totalCount 미확인"),
        _row(bus_api.ROUTE_LIST, route_list["pages"] if route_list else None,
             "ceil(노선 totalCount / numOfRows)", None if route_list else "노선 목록 totalCount 미확인"),
        _row(bus_api.ROUTE_INFO, route_total, "노선 수 × 1", None if route_total is not None else "노선 수 미확인"),
        stops_row,
    ]

    location_pages = _max_pages(grouped[bus_api.BUS_LOCATIONS.operation])
    arrival_pages = _max_pages(grouped[bus_api.ARRIVALS.operation])
    realtime_formula = "대상 수 × ceil(활성 분 / 주기 분) × 호출당 페이지 수"
    full_rows = [
        _realtime_row(bus_api.BUS_LOCATIONS, route_total, "노선 수", calls_per_target, location_pages, realtime_formula),
        _realtime_row(bus_api.ARRIVALS, station_total, "정류소 수", calls_per_target, arrival_pages, realtime_formula),
    ]
    needed_rows = [
        _realtime_row(bus_api.BUS_LOCATIONS, location_targets, "위치 대상 노선 수(--location-targets)", calls_per_target, location_pages, realtime_formula),
        _realtime_row(bus_api.ARRIVALS, arrival_targets, "도착 대상 정류소 수(--arrival-targets)", calls_per_target, arrival_pages, realtime_formula),
    ]
    scenarios = {
        "static_daily_fixed_full": _scenario(
            "정적 정보를 하루 1회 대전 전체로 갱신(초기 적재도 같은 규모)", static_rows, daily_limit
        ),
        "realtime_fixed_full": _scenario(
            "고정된 전체 수집 가정: 전체 노선 위치·전체 정류소 도착을 활성 시간 동안 주기마다 조회", full_rows, daily_limit
        ),
        "realtime_user_needed": _scenario(
            "사용자 필요 대상 수집 가정: 경로·환승 후보에 필요한 노선·정류소의 합집합만 공유 갱신", needed_rows, daily_limit
        ),
    }
    scenarios["realtime_user_needed"]["notes"] = [
        "대상별 최초 조회를 첫 주기 조회로 계산해 중복하지 않는다. 24시간 연속 활성 대상은 144회다.",
        "대상이 비활성화된 뒤 다시 필요해지면 그 활성 구간마다 ceil(구간 분 / 주기 분)회(새 최초 조회 포함)가 추가된다.",
    ]
    return {
        "observed_by_operation": {path: dict(counts) for path, counts in sorted(observed.items())},
        "observed_by_service": {service: dict(counts) for service, counts in sorted(by_service.items())},
        "observed_retry_ratio": round(retries / attempts, 4) if attempts else None,
        "assumptions": {
            "interval_minutes": interval_minutes,
            "active_hours": active_hours,
            "calls_per_target": calls_per_target,
            "calls_per_target_formula": "ceil(활성 시간 × 60 / 주기 분) — 최초 조회 포함",
            "retry_worst_case_multiplier": 1 + max_retries,
        },
        **scenarios,
        "daily_limit": {
            "value": daily_limit,
            "note": (
                f"입력값 {daily_limit}회/일(서비스별)과 비교" if daily_limit else
                "실제 계정 한도 미확인(--daily-limit로 입력). 포털 상세기능 페이지의 개발계정 신청 가능 트래픽 표기(10,000)는 참고값이다."
            ),
        },
    }


def _row(
    endpoint: bus_api.Endpoint, requests: int | None, formula: str, unknown: str | None, *, lower_bound: bool = False,
) -> dict[str, Any]:
    return {
        "service": endpoint.service,
        "operation": endpoint.operation,
        "requests": requests,
        "formula": formula,
        "lower_bound": lower_bound,
        "unknown": unknown,
    }


def _realtime_row(
    endpoint: bus_api.Endpoint, targets: int | None, target_label: str, calls_per_target: int,
    pages: int | None, formula: str,
) -> dict[str, Any]:
    if targets is None:
        return _row(endpoint, None, formula, f"{target_label} 미확인")
    if pages is None:
        return _row(
            endpoint, targets * calls_per_target, formula, "호출당 페이지 수 미관측 — 1페이지로 계산한 하한", lower_bound=True,
        )
    return _row(endpoint, targets * calls_per_target * pages, formula, None) | {"pages_per_call_observed_max": pages}


def _scenario(description: str, rows: list[dict[str, Any]], daily_limit: int | None) -> dict[str, Any]:
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        total = totals.setdefault(row["service"], {"requests": 0, "lower_bound": False, "unknown": False})
        if row["requests"] is None:
            total["unknown"] = True
        else:
            total["requests"] += row["requests"]
            total["lower_bound"] = total["lower_bound"] or row["lower_bound"]
    for total in totals.values():
        if total["unknown"]:
            total["requests"] = None
        if daily_limit and total["requests"] is not None:
            total["ratio_to_daily_limit"] = round(total["requests"] / daily_limit, 4)
    return {"description": description, "rows": rows, "service_totals": totals}


def _list_pages(records: list[dict[str, Any]], filters: tuple[str, ...]) -> dict[str, Any] | None:
    """검색 조건 없는 목록 결과의 totalCount로 전체 페이지 수를 계산한다(첫 페이지만 받아도 가능)."""
    for record in reversed(records):
        if any(record["request"]["params"].get(name) for name in filters):
            continue
        pages = _pages_needed(record)
        if pages is not None:
            return {"total": record["total_count"], "rows": record["request"]["numOfRows"], "pages": pages}
    return None


def _pages_needed(record: dict[str, Any]) -> int | None:
    total, rows = record.get("total_count"), record["request"].get("numOfRows")
    if total is None or not rows:
        return None
    return max(1, math.ceil(total / rows))


def _max_pages(records: list[dict[str, Any]]) -> int | None:
    """항목이 있는 응답만 호출당 페이지 수의 근거로 쓴다. 빈 응답(미운행 시간 등)은 근거가 되지 않는다."""
    pages = [_pages_needed(record) for record in records if record.get("total_count")]
    pages = [value for value in pages if value is not None]
    return max(pages) if pages else None


def _format_total(total: dict[str, Any]) -> str:
    if total["requests"] is None:
        return "미확인"
    text = f"{total['requests']:,}회" + (" 이상(하한)" if total["lower_bound"] else "")
    if "ratio_to_daily_limit" in total:
        text += f" (한도 대비 {total['ratio_to_daily_limit']:.1%})"
    return text


def _label(item: dict[str, Any], name: str) -> str:
    state = field_state(item, name)
    return f"<{state}>" if state in _BLANK else str(text_value(item, name))


def _int(value: Any) -> int | None:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return to_int(value)


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _haversine_m(origin: tuple[float, float], point: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*origin, *point))
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))
