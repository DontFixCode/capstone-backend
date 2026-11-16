"""앱 API(stops·routes)의 응답 구성. 정적 공개본과 공유 수집이 공개한 최신 배치만 읽고 외부 API를 호출하지 않는다.

- 내부 정류소·노선 ID(BIGINT)는 10진 문자열로 내보낸다. 경유 위치는 routeId + patternKey + seq로 식별한다
  (route_stops.id는 정적 갱신 때 다시 만들어지므로 쓰지 않는다).
- 실시간 자료 상태는 정상 빈 배치(empty)·공개본 미확보(unavailable)·오래된 값(isStale)·최근 수집 실패(lastAttemptStatus)를
  구분한다. 미확인 차량·회차·절대 ETA는 추정해서 채우지 않고 null로 둔다.
- 도착정보 remainingSeconds는 수집한 원천 잔여초 그대로다. 절대 ETA(predictedArrivalAt)는 확인한 기준 시각 또는 설정한
  근사 정책(eta_basis)으로만 만들고 기준 출처(etaBasisSource)를 함께 준다. 현재 남은 시간은 앱이 predictedArrivalAt에서
  계산한다. 예측 시각이 지났다는 사실만으로 차량이 통과했다고 표시하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import psycopg

from app.integrations import route_shapes_api as route_shapes
from app.modules.transit import eta_basis, repository
from app.modules.transit.schemas import LatestSnapshot, RealtimeTarget
from app.modules.transit.service import latest_snapshot


def sid(value: int | None) -> str | None:
    return None if value is None else str(value)


def location(lat: float | None, lon: float | None) -> dict[str, float] | None:
    return None if lat is None or lon is None else {"latitude": lat, "longitude": lon}


def current_version(conn: psycopg.Connection) -> str | None:
    versions = repository.current_static_versions(conn)
    return str(versions[0]) if len(versions) == 1 else None


def stop_view(row: dict[str, Any], data_version: str | None) -> dict[str, Any]:
    return {"id": sid(row["id"]), "provider": row["provider"], "externalId": row["external_id"], "name": row["name"],
            "stopNo": row["stop_no"], "location": location(row["lat"], row["lon"]), "isActive": row["is_active"],
            "dataVersion": data_version}


def route_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": sid(row["id"]), "provider": row["provider"], "externalId": row["external_id"],
            "routeNo": row["route_no"], "routeType": row["route_type"], "startName": row["start_name"],
            "endName": row["end_name"], "isActive": row["is_active"]}


# ---------------------------------------------------------------- 실시간 자료 상태

def data_status(snapshot: LatestSnapshot) -> dict[str, Any]:
    if snapshot.batch_key is None:
        availability = "unavailable"
    else:
        availability = "available" if snapshot.content == "items" else "empty"
    last = snapshot.last_attempt
    if last is None:
        last_status = None
    elif last.published:
        last_status = "published"
    else:
        last_status = last.result_status if last.result_status in ("error", "timeout", "partial") else "unpublished"
    published = snapshot.batch_key is not None
    return {
        "availability": availability,
        "publishedAt": snapshot.published_at if published else None,
        "collectedAt": snapshot.collected_at if published else None,
        "ageSeconds": snapshot.age_seconds if published else None,
        "lastAttemptAt": last.requested_at if last else None,
        "lastAttemptStatus": last_status,
        "lastAttemptFailed": snapshot.last_attempt_failed,
        "isStale": (snapshot.state == "stale") if published else None,
        "notProvided": list(snapshot.not_provided),
    }


def _route_briefs(conn: psycopg.Connection, external_ids: set[str]) -> dict[str, dict[str, Any]]:
    result = {}
    for ext in sorted(i for i in external_ids if i):
        row = repository.route_by_external_id(conn, repository.PROVIDER, ext)
        if row is not None:
            result[ext] = {"id": sid(row["id"]), "externalId": ext, "routeNo": row["route_no"],
                           "routeType": row["route_type"]}
    return result


def stop_arrivals(conn: psycopg.Connection, stop: dict[str, Any], *, interval_seconds: float,
                  now: datetime, basis_policy: str = eta_basis.UNCONFIRMED) -> dict[str, Any]:
    snap = latest_snapshot(conn, RealtimeTarget("arrivals", stop["external_id"]), interval_seconds=interval_seconds,
                           now=now)
    briefs = _route_briefs(conn, {o["route_external_id"] for o in snap.observations})
    items = []
    for obs in snap.observations:
        # 대전시 도착정보는 차량번호를 직접 준다(운행 회차 키는 없음). 차량번호가 없으면 차량을 표시하지 않는다.
        linked = obs["vehicle_no"] is not None or obs["run_key"] is not None
        basis_at, basis_source = eta_basis.resolve_basis(obs, basis_policy)
        items.append({
            "route": briefs.get(obs["route_external_id"]),
            "stopId": sid(stop["id"]),
            "patternKey": obs["pattern_key"],
            "seq": obs["stop_seq"],
            "remainingSeconds": obs["remaining_seconds"],
            # 기준 시각이 없으면(TAGO 기본 도착정보 + unconfirmed 정책) 절대 ETA는 null이다.
            "predictedArrivalAt": eta_basis.predicted_arrival(obs, basis_at),
            "etaBasisAt": basis_at,
            "etaBasisSource": basis_source,
            "sourceGeneratedAt": obs["source_generated_at"],
            "collectedAt": obs["collected_at"],
            "stopsAway": (obs.get("details") or {}).get("stops_away"),
            "vehicle": {"vehicleNo": obs["vehicle_no"], "runKey": obs["run_key"]} if linked else None,
        })
    return {"stopId": sid(stop["id"]), "items": items, "dataStatus": data_status(snap)}


def route_positions(conn: psycopg.Connection, route: dict[str, Any], *, interval_seconds: float,
                    now: datetime) -> dict[str, Any]:
    snap = latest_snapshot(conn, RealtimeTarget("positions", route["external_id"]), interval_seconds=interval_seconds,
                           now=now)
    _, stop_map = repository.internal_ids(conn, (), {o["stop_external_id"] for o in snap.observations})
    items = [{
        "vehicleNo": obs["vehicle_no"],
        "location": location(obs["lat"], obs["lon"]),
        "patternKey": obs["pattern_key"],
        "seq": obs["stop_seq"],
        "stopId": sid(stop_map.get(obs["stop_external_id"])),
        "sourceGeneratedAt": obs["source_generated_at"],
        "collectedAt": obs["collected_at"],
    } for obs in snap.observations]
    return {"routeId": sid(route["id"]), "items": items, "dataStatus": data_status(snap)}


# ---------------------------------------------------------------- 노선·패턴·형상

def service_info_view(info: dict[str, Any]) -> dict[str, Any]:
    """첫차·막차·배차간격. 원천 단위·형식이 확인되지 않아(Phase 2) 초 단위 배차간격은 null로 두고 원값과 상태를 준다."""
    fields = (info or {}).get("fields") or {}
    unit_confirmed = (info or {}).get("unit_status") == "confirmed"

    def provided(name: str) -> dict[str, Any]:
        field = fields.get(name) or {}
        state = field.get("state")
        status = "PROVIDED" if state in ("present", "zero") else "NOT_PROVIDED"
        return {"status": status, "rawValue": field.get("value") if status == "PROVIDED" else None}

    def interval(name: str) -> dict[str, Any]:
        base = provided(name)
        if base["status"] == "PROVIDED" and not unit_confirmed:
            base["status"] = "UNIT_UNCONFIRMED"
        return {**base, "seconds": None}

    return {
        "unitStatus": (info or {}).get("unit_status", "unconfirmed"),
        "firstDeparture": provided("startvehicletime"),
        "lastDeparture": provided("endvehicletime"),
        "headway": interval("intervaltime"),
        "headwaySaturday": interval("intervalsattime"),
        "headwaySunday": interval("intervalsuntime"),
        "note": "배차간격은 참고값이며 실제 다음 차량 시각이나 구간 주행시간으로 쓰지 않는다.",
    }


def shape_status_view(route: dict[str, Any], pattern_key: str) -> dict[str, Any]:
    entry = route_shapes.pattern_entry(route["shape_info"], pattern_key)
    return {"patternKey": pattern_key, "status": entry["status"].upper(), "source": entry["source"],
            "version": entry["version"], "fetchedAt": entry["fetched_at"], "reason": entry["reason"]}


def shape_view(route: dict[str, Any], pattern_key: str, data_version: str | None) -> tuple[dict[str, Any], str]:
    entry = route_shapes.pattern_entry(route["shape_info"], pattern_key)
    body = {"routeId": sid(route["id"]), "patternKey": pattern_key, "status": entry["status"].upper(),
            "geometry": entry["geometry"], "source": entry["source"], "version": entry["version"],
            "fetchedAt": entry["fetched_at"], "reason": entry["reason"],
            # 형상이 없을 때 앱이 정류소를 이어 그리면 실제 도로 형상이 아닌 개략(schematic) 표시다.
            "fallbackDisplay": None if entry["status"] == "available" else "schematic_stop_sequence",
            "dataVersion": data_version}
    digest = hashlib.sha256(json.dumps([data_version, pattern_key, entry["status"], entry["version"],
                                        entry["fetched_at"], entry["reason"]], default=str).encode()).hexdigest()
    return body, f'"{digest[:32]}"'


def pattern_views(route: dict[str, Any], patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for pattern in patterns:
        stops = pattern["stops"]
        codes = list(dict.fromkeys(s["direction_code"] for s in stops if s["direction_code"] is not None))
        items.append({
            "patternKey": pattern["pattern_key"],
            # 방향 코드는 원천의 경유 행별 원값(updowncd)이다. 패턴 첫 행의 값과 패턴 안의 값 목록을 함께 준다.
            "directionCode": stops[0]["direction_code"],
            "directionCodes": codes,
            "firstStop": {"id": sid(stops[0]["stop_id"]), "name": stops[0]["name"], "seq": stops[0]["seq"]},
            "lastStop": {"id": sid(stops[-1]["stop_id"]), "name": stops[-1]["name"], "seq": stops[-1]["seq"]},
            "stopCount": len(stops),
            "shapeStatus": shape_status_view(route, pattern["pattern_key"])["status"],
        })
    return items
