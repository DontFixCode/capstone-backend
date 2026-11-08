"""검색 결과·후보 구간의 앱 응답 구성. 저장 형식(route_candidates.legs·evaluation, snake_case)을 API 형식(camelCase)으로 옮긴다.

- 정류소·노선은 내부 ID 문자열과 원천 ID를 함께 준다. 미산출 값은 null과 사유로 둔다.
- 버스 구간 형상은 geometryStatus(available/unavailable/error)와 출처·버전·사유를 함께 준다. 미확보 시 앱은 정류소 연결
  개략선(schematic)을 그릴 수 있으며 실제 도로 형상과 구분한다.
- 이동 시작(startMode): 순위가 있는 후보는 ranked, 순위가 없지만 시간·차량 자료가 부족할 뿐인 후보는 data_limited로
  시작한다(차량 미확인 탑승·부분 계산 허용, Notion DB-J01·요구사항명세서 4절). 기준 ETA상 연결이 성립하지 않거나
  (negative_slack) 실행 불가가 확인된(confirmed_infeasible, 도보 경로 없음 포함) 후보는 CANDIDATE_NOT_STARTABLE이다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

PROCESSING = "processing"
TERMINAL_STATUSES = ("ready", "data_limited", "no_path", "out_of_scope", "error")
START_RANKED = "ranked"
START_DATA_LIMITED = "data_limited"
# 자료 부족(time_unknown)이 아니라 기준 ETA·도보 결과로 연결이 성립하지 않는다고 판정된 상태.
BLOCKING_CONNECTION_STATUSES = ("confirmed_infeasible", "negative_slack")


def _sid(value: Any) -> str | None:
    return None if value is None else str(value)


def point_view(point: dict[str, Any] | None) -> dict[str, Any] | None:
    if not point:
        return None
    lat, lon = point.get("lat"), point.get("lon")
    return {
        "kind": point.get("kind"),
        "stopId": _sid(point.get("stop_internal_id")),
        "externalId": point.get("stop_id"),
        "name": point.get("name"),
        "location": None if lat is None or lon is None else {"latitude": lat, "longitude": lon},
    }


def eta_view(eta: dict[str, Any] | None) -> dict[str, Any] | None:
    if not eta:
        return None
    return {
        "predictedArrivalAt": eta.get("predicted_arrival_at"), "etaSource": eta.get("eta_source"),
        "basisAt": eta.get("basis_at"), "basisSource": eta.get("basis_source"),
        "remainingSeconds": eta.get("horizon_seconds"),
        "sourceGeneratedAt": eta.get("source_generated_at"), "collectedAt": eta.get("collected_at"),
        "vehicleId": eta.get("vehicle_id"), "runKey": eta.get("run_key"), "modelVersion": eta.get("model_version"),
        "stopOrder": eta.get("stop_order"),
    }


def leg_view(leg: dict[str, Any]) -> dict[str, Any]:
    if leg.get("kind") == "walk":
        return {
            "legIndex": leg["leg_index"], "kind": "walk", "from": point_view(leg.get("from")),
            "to": point_view(leg.get("to")), "status": leg.get("status"), "distanceM": leg.get("distance_m"),
            "durationSeconds": leg.get("duration_seconds"), "geometry": leg.get("geometry"),
            "provider": leg.get("provider"), "fetchedAt": leg.get("fetched_at"),
            "queryOptions": leg.get("query_options") or {}, "reason": leg.get("reason"),
        }

    def stop_side(side: dict[str, Any] | None) -> dict[str, Any] | None:
        if side is None:
            return None
        return {**(point_view(side) or {}), "seq": side.get("seq"), "directionCode": side.get("direction_code")}

    vehicle = leg.get("vehicle")
    return {
        "legIndex": leg["leg_index"], "kind": "bus", "rideIndex": leg.get("ride_index"),
        "route": {"id": _sid(leg.get("route_internal_id")), "externalId": leg.get("route_id"),
                  "routeNo": leg.get("route_no"), "routeType": leg.get("route_type")},
        "patternKey": leg.get("pattern_key"),
        "board": stop_side(leg.get("board")), "alight": stop_side(leg.get("alight")),
        "vehicle": None if not vehicle else {"vehicleId": vehicle.get("vehicle_id"), "runKey": vehicle.get("run_key"),
                                             "matchStatus": vehicle.get("match_status")},
        "boardEta": eta_view(leg.get("board_eta")), "alightEta": eta_view(leg.get("alight_eta")),
        "rideSeconds": leg.get("ride_seconds"), "reasonCodes": leg.get("reason_codes") or [],
        "geometry": leg.get("geometry"), "geometryStatus": leg.get("geometry_status"),
        "geometrySource": leg.get("geometry_source"), "geometryVersion": leg.get("geometry_version"),
        "geometryFetchedAt": leg.get("geometry_fetched_at"), "geometryReason": leg.get("geometry_reason"),
    }


def _connection(conn: dict[str, Any] | None) -> dict[str, Any] | None:
    if not conn:
        return None
    return {"status": conn.get("status"), "slackSeconds": conn.get("slack_seconds"),
            "waitSeconds": conn.get("wait_seconds"), "walkSeconds": conn.get("walk_seconds"),
            "bufferSeconds": conn.get("buffer_seconds"), "readyAt": conn.get("ready_at"),
            "nextArrivalAt": conn.get("next_arrival_at"), "reasonCodes": conn.get("reason_codes") or []}


def evaluation_view(row: dict[str, Any]) -> dict[str, Any]:
    ev = row.get("evaluation") or {}
    times = ev.get("times") or {}
    provenance = ev.get("provenance") or {}
    return {
        "timeStatus": row.get("time_status"), "nominallyFeasible": row.get("nominally_feasible"),
        "connectionStatus": ev.get("connection_status"),
        "nominalTotalSeconds": row.get("nominal_total_seconds"),
        "expectedTotalSeconds": row.get("expected_total_seconds"),
        "journeyProbability": None if row.get("journey_probability") is None else float(row["journey_probability"]),
        "firstBoardingProbability": ev.get("first_boarding_probability"),
        "walkingSeconds": row.get("walking_seconds"), "waitSeconds": times.get("wait_seconds"),
        "rideSeconds": times.get("ride_seconds"), "bufferSeconds": times.get("buffer_seconds"),
        "egressWalkSeconds": times.get("egress_walk_seconds"), "plannedArrivalAt": times.get("planned_arrival_at"),
        "firstBoarding": _connection(ev.get("first_boarding")),
        "transfers": [{
            "transferIndex": t.get("transfer_index"), "frontLegIndex": t.get("front_leg_index"),
            "nextLegIndex": t.get("next_leg_index"), "slackSeconds": t.get("slack_seconds"),
            "status": t.get("status"), "probability": t.get("probability"),
            "reasonCodes": t.get("reason") or [],
        } for t in ev.get("transfers") or []],
        "evaluatedAt": row.get("evaluated_at"), "dataSnapshotId": provenance.get("data_snapshot_id"),
        "limitedAtRideIndex": ev.get("limited_at_ride_index"), "reasonCodes": ev.get("reason_codes") or [],
    }


def start_mode(search: dict[str, Any], candidate: dict[str, Any], now: datetime) -> tuple[str | None, str | None]:
    """(시작 방식, 차단 사유). 시작 방식은 ranked / data_limited, 시작할 수 없으면 None과 사유 코드."""
    if search["status"] == PROCESSING:
        return None, "SEARCH_NOT_READY"
    if search["status"] == "error":
        return None, "SEARCH_FAILED"
    if search["expires_at"] <= now:
        return None, "SEARCH_EXPIRED"
    if candidate.get("rank_no") is not None:
        return START_RANKED, None
    if (candidate.get("evaluation") or {}).get("connection_status") in BLOCKING_CONNECTION_STATUSES:
        return None, "CANDIDATE_NOT_STARTABLE"
    return START_DATA_LIMITED, None


def startability(search: dict[str, Any], candidate: dict[str, Any], now: datetime) -> tuple[bool, str | None]:
    mode, reason = start_mode(search, candidate, now)
    return mode is not None, reason


def start_limit_reasons(candidate: dict[str, Any]) -> list[str]:
    """순위 없이 시작한 후보의 제한 사유(평가·순위 제외 사유). 순위 후보는 빈 목록."""
    if candidate.get("rank_no") is not None:
        return []
    evaluation = candidate.get("evaluation") or {}
    ranking = evaluation.get("ranking") or {}
    return list(dict.fromkeys([*(evaluation.get("reason_codes") or []), *(ranking.get("reason_codes") or [])]))


def candidate_view(search: dict[str, Any], row: dict[str, Any], now: datetime) -> dict[str, Any]:
    mode, reason = start_mode(search, row, now)
    ranking = (row.get("evaluation") or {}).get("ranking") or {}
    return {
        "candidateId": str(row["id"]), "rank": row.get("rank_no"), "rankingMode": row.get("ranking_mode"),
        "startable": mode is not None, "startMode": mode, "startBlockedReason": reason,
        "staticRouteKey": row["static_route_key"], "optionKey": row["option_key"],
        "boardingCount": row["boarding_count"], "transferCount": max(0, row["boarding_count"] - 1),
        "legs": [leg_view(leg) for leg in row.get("legs") or []],
        "evaluation": evaluation_view(row),
        "rankingReasonCodes": ranking.get("reason_codes") or [],
    }


def service_area_view(scope: dict[str, Any] | None) -> dict[str, Any] | None:
    """지원 범위 판정 결과. 범위 밖인 입력 지점(outOfScopePoints)과 지점별 판정 근거를 준다."""
    if not scope:
        return None
    points = scope.get("points") or []
    boundary = scope.get("boundary") or {}
    return {
        "status": scope.get("status"),
        "outOfScopePoints": [p["role"] for p in points if p.get("status") == "out_of_scope"],
        "points": [{
            "role": p.get("role"), "status": p.get("status"), "basis": p.get("basis"),
            "insideBoundary": p.get("inside_boundary"), "radiusM": p.get("radius_m"),
            "servedStopWithinRadius": None if p.get("served_stops") is None else p["served_stops"] > 0,
            "nearestServedStop": None if not p.get("nearest_stop") else {
                "stopId": _sid(p["nearest_stop"].get("stop_id")), "name": p["nearest_stop"].get("name"),
                "straightDistanceM": p["nearest_stop"].get("distance_m")},
        } for p in points],
        "boundary": {"available": boundary.get("available"), "name": boundary.get("name"),
                     "reason": boundary.get("reason")},
    }


def _data_as_of(summary: dict[str, Any]) -> Any:
    times = [s.get("collected_at") for s in (summary.get("data_inputs") or {}).values() if s.get("collected_at")]
    return min(times) if times else None


def search_view(search: dict[str, Any], candidates: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    summary = search.get("summary") or {}
    processing = search["status"] == PROCESSING
    recommendation = summary.get("recommendation") or {}
    recommended = recommendation.get("recommended_timed_candidate_id")
    recommended_id = next((str(c["id"]) for c in candidates if recommended and c["option_key"] == recommended
                           and c.get("rank_no") is not None), None)
    if processing:
        reasons = ["search_processing"]
    elif search["status"] == "error":
        reasons = [(summary.get("job") or {}).get("reason") or "search_failed"]
    else:
        reasons = list(dict.fromkeys([r for r in [*(summary.get("scope_reason_codes") or []),
                                                  summary.get("status_reason"),
                                                  *(recommendation.get("mode_reason_codes") or []),
                                                  *(recommendation.get("reason_codes") or [])] if r]))
    info = search.get("place_info") or {}

    def place(kind: str, lat: float, lon: float) -> dict[str, Any]:
        p = info.get(kind) or {}
        return {"latitude": lat, "longitude": lon, "name": p.get("name"), "address": p.get("address"),
                "provider": p.get("provider"), "externalId": p.get("external_id")}

    return {
        "searchId": str(search["id"]), "status": search["status"], "expiresAt": search["expires_at"],
        "expired": search["expires_at"] <= now, "createdAt": search["created_at"], "startAt": search["start_at"],
        "origin": place("origin", search["origin_lat"], search["origin_lon"]),
        "destination": place("destination", search["destination_lat"], search["destination_lon"]),
        # 비교(평가·추천)를 하지 않은 검색(처리 중·오류·범위 밖·범위 판단 불가)은 비교 모드가 없다.
        "rankingMode": search["ranking_mode"] if recommendation and search["status"] != "error" else None,
        "recommendedCandidateId": recommended_id,
        "reasonCodes": reasons,
        "serviceArea": service_area_view(summary.get("service_area")),
        "staticVersion": str(search["static_version"]),
        "dataAsOf": None if processing else _data_as_of(summary),
        "coverage": None if processing else {
            "searchTruncated": (summary.get("search_coverage") or {}).get("truncated"),
            "evaluationTruncated": (summary.get("evaluation_coverage") or {}).get("truncated"),
            "counts": summary.get("counts"),
        },
        "candidates": [] if processing else [candidate_view(search, c, now) for c in candidates],
    }
