"""이동 시작·조회·수동 입력·위치 반영의 업무 규칙.

- 시작: 검색 소유권·유효기간·후보의 검색 소속과 시작 방식(routing.views.start_mode)을 확인한다. 순위 후보는 ranked,
  자료 부족으로 순위가 없는 후보는 data_limited로 시작하고(시간·확률은 null과 사유), 기준 ETA상 연결 불가·실행 불가가
  확인된 후보는 CANDIDATE_NOT_STARTABLE(409)이다. 사용자당 활성 이동 1개는 DB 부분 UNIQUE와 같은 트랜잭션으로 보장한다. 선택 스냅샷(구간·승하차 위치·형상·차량 계획·
  목적지)을 저장하고 최초 재평가를 바로 예약하며, 환승별 예측을 evaluation_snapshot 이벤트로 결과 전에 고정한다.
- 입력: 세션 행을 잠그고 Idempotency-Key 중복을 먼저 검사한다(같은 입력은 기존 결과, 다른 입력은 409). 이어서 stateVersion·
  종료 여부·전이를 확인하고 이벤트 저장·상태 전이·버전 증가·재평가 예약(또는 종료 시 해제)을 한 트랜잭션에서 처리한다.
- 위치: 관측 시각이 더 새로울 때만 반영하고 수신 시각을 따로 기록한다. 위치가 없거나 오래됐던 상태에서 새 위치가 오면
  즉시 재평가를 예약한다.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from pydantic import ValidationError

from app.core.config import Settings
from app.db.session import Database
from app.modules.journeys import repository
from app.modules.journeys import state as journey_state
from app.modules.journeys.schemas import PAYLOADS, EventBody, LocationBody
from app.modules.routing import repository as search_repository
from app.modules.routing import views as route_views
from app.modules.transit import repository as transit_repository
from app.workflows.guidance import evaluation_snapshots

PAGE_SIZE = 20


class JourneyError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


# ---------------------------------------------------------------- 시작

def start_journey(db: Database, user_id: UUID, search_id: UUID, candidate_id: UUID, *, now: datetime) -> dict:
    with db.transaction() as conn:
        search = search_repository.read_user_search(conn, search_id, user_id)
        if search is None:
            raise JourneyError(404, "NOT_FOUND", "검색이 없거나 요청 사용자 소유가 아닙니다.")
        candidate = search_repository.read_candidate(conn, candidate_id)
        if candidate is None or candidate["search_id"] != search_id:
            raise JourneyError(404, "NOT_FOUND", "후보가 없거나 이 검색에 속하지 않습니다.")
        mode, reason = route_views.start_mode(search, candidate, now)
        if mode is None:
            messages = {"SEARCH_NOT_READY": "검색을 계산하는 중입니다.", "SEARCH_FAILED": "실패한 검색입니다.",
                        "SEARCH_EXPIRED": "검색 결과의 유효기간이 지났습니다. 다시 검색하세요.",
                        "CANDIDATE_NOT_STARTABLE": "기준 ETA상 연결할 수 없거나 실행 불가가 확인된 후보는 안내를 "
                                                   "시작할 수 없습니다."}
            details = None
            if reason == "CANDIDATE_NOT_STARTABLE":
                details = {"connectionStatus": (candidate["evaluation"] or {}).get("connection_status")}
            raise JourneyError(409, reason, messages[reason], details=details)
        legs = candidate["legs"] or []
        try:
            progress = journey_state.initial_progress(legs)
        except journey_state.TransitionError as exc:
            raise JourneyError(exc.status_code, exc.code, exc.message) from None
        info = search.get("place_info") or {}

        def place(kind: str, lat: float, lon: float) -> dict[str, Any]:
            p = info.get(kind) or {}
            return {"lat": lat, "lon": lon, "name": p.get("name"), "address": p.get("address"),
                    "provider": p.get("provider"), "external_id": p.get("external_id")}

        snapshot = {
            "candidate_id": str(candidate["id"]), "search_id": str(search_id),
            "static_route_key": candidate["static_route_key"], "option_key": candidate["option_key"],
            "selection_version": 1, "legs": legs, "evaluation": candidate["evaluation"],
            "boarding_count": candidate["boarding_count"], "walking_seconds": candidate["walking_seconds"],
            "nominal_total_seconds": candidate["nominal_total_seconds"],
            "expected_total_seconds": candidate["expected_total_seconds"],
            "journey_probability": None if candidate["journey_probability"] is None
            else float(candidate["journey_probability"]),
            "time_status": candidate["time_status"], "nominally_feasible": candidate["nominally_feasible"],
            "ranking_mode": candidate["ranking_mode"], "rank_no": candidate["rank_no"],
            "start_mode": mode, "start_limit_reasons": route_views.start_limit_reasons(candidate),
            "evaluated_at": _iso(candidate["evaluated_at"]),
            "origin": place("origin", search["origin_lat"], search["origin_lon"]),
            "destination": place("destination", search["destination_lat"], search["destination_lon"]),
            "search_start_at": _iso(search["start_at"]), "static_version": str(search["static_version"]),
            "config_version": search["config_version"], "selected_at": _iso(now),
        }
        conn.execute("SAVEPOINT start_journey")
        try:
            journey_id = repository.insert_journey(
                conn, user_id=user_id, search_id=search_id, candidate_id=candidate_id, snapshot=snapshot,
                movement_state=journey_state.initial_movement(legs), progress=progress, now=now)
        except psycopg.errors.UniqueViolation:
            conn.execute("ROLLBACK TO SAVEPOINT start_journey")
            active = repository.active_journey_id(conn, user_id)
            raise JourneyError(409, "ACTIVE_JOURNEY_EXISTS", "이미 진행 중인 이동이 있습니다.",
                               details={"activeJourneyId": str(active) if active else None}) from None
        # 사용자 결과를 알기 전에 비교 대상 예측(환승별)을 고정한다. 같은 키는 다시 만들거나 덮어쓰지 않는다.
        for key, payload in evaluation_snapshots(journey_id, candidate, selection_version=1,
                                                 config_version=search["config_version"], fixed_at=now):
            repository.insert_event(conn, journey_id=journey_id, idempotency_key=key,
                                    event_type="evaluation_snapshot", source="system", occurred_at=None,
                                    received_at=now, state_version_after=1, payload=payload, skip_existing=True)
        return repository.get_journey(conn, journey_id, user_id)


# ---------------------------------------------------------------- 조회

def _encode_cursor(row: dict[str, Any]) -> str:
    raw = json.dumps([row["started_at"].isoformat(), str(row["id"])])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        started, jid = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        return datetime.fromisoformat(started), UUID(jid)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise JourneyError(422, "INVALID_CURSOR", "cursor 형식이 잘못됐습니다.") from None


def list_user_journeys(db: Database, user_id: UUID, *, status: str | None,
                       cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
    before = _decode_cursor(cursor) if cursor else None
    with db.transaction() as conn:
        rows = repository.list_journeys(conn, user_id, status=status, before=before, limit=PAGE_SIZE + 1)
    more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    return rows, (_encode_cursor(rows[-1]) if more and rows else None)


def get_user_journey(db: Database, user_id: UUID, journey_id: UUID) -> dict[str, Any]:
    with db.transaction() as conn:
        row = repository.get_journey(conn, journey_id, user_id)
    if row is None:
        raise JourneyError(404, "NOT_FOUND", "이동이 없거나 요청 사용자 소유가 아닙니다.")
    return row


# ---------------------------------------------------------------- 입력

def request_fingerprint(body: EventBody) -> tuple[dict[str, Any], str]:
    canonical = {"eventType": body.eventType, "stateVersion": body.stateVersion,
                 "occurredAt": body.occurredAt.isoformat() if body.occurredAt else None, "payload": body.payload}
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return canonical, hashlib.sha256(text.encode()).hexdigest()


def record_event(db: Database, user_id: UUID, journey_id: UUID, idempotency_key: str, body: EventBody, *,
                 now: datetime) -> dict[str, Any]:
    canonical, fingerprint = request_fingerprint(body)
    with db.transaction() as conn:
        journey = repository.get_journey(conn, journey_id, user_id, for_update=True)
        if journey is None:
            raise JourneyError(404, "NOT_FOUND", "이동이 없거나 요청 사용자 소유가 아닙니다.")
        existing = repository.find_event(conn, journey_id, idempotency_key)
        if existing is not None:
            if existing["source"] == "manual" and existing["payload"].get("request_hash") == fingerprint:
                return {"event_id": existing["id"], "state_version_after": existing["state_version_after"],
                        "replayed": True}
            raise JourneyError(409, "IDEMPOTENCY_KEY_REUSED", "같은 Idempotency-Key가 다른 입력에 이미 사용됐습니다.")
        if body.eventType in journey_state.DEFERRED_EVENTS:
            raise JourneyError(422, "UNSUPPORTED_EVENT_TYPE", f"{body.eventType}은 "
                               f"{journey_state.DEFERRED_EVENTS[body.eventType]} 범위라 아직 받지 않습니다.")
        model = PAYLOADS[body.eventType]
        try:
            payload = model.model_validate(body.payload).model_dump(exclude_unset=True)
        except ValidationError as exc:
            raise JourneyError(422, "INVALID_PAYLOAD", "payload 형식이 잘못됐습니다.",
                               details=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]) from None
        if body.stateVersion != journey["state_version"]:
            raise JourneyError(409, "STATE_VERSION_CONFLICT", "앱이 확인한 상태 버전이 현재와 다릅니다.",
                               details={"currentStateVersion": journey["state_version"]})
        resolved = _resolve_references(conn, journey_id, body.eventType, payload)
        legs = (journey["selected_snapshot"] or {}).get("legs") or []
        try:
            transition = journey_state.apply(
                lifecycle_status=journey["lifecycle_status"], movement_state=journey["movement_state"],
                progress=journey["current_progress"] or {}, legs=legs, event_type=body.eventType, payload=payload,
                occurred_at=body.occurredAt, received_at=now, resolved=resolved)
        except journey_state.TransitionError as exc:
            raise JourneyError(exc.status_code, exc.code, exc.message) from None
        version = journey["state_version"] + 1
        event_id = repository.insert_event(
            conn, journey_id=journey_id, idempotency_key=idempotency_key, event_type=body.eventType, source="manual",
            occurred_at=body.occurredAt, received_at=now, state_version_after=version,
            payload={"request": canonical, "request_hash": fingerprint, "state_version_before":
                     journey["state_version"], "effect": transition.effect})
        progress = journey_state.stamp_event_id(body.eventType, transition.progress, event_id)
        active = transition.lifecycle_status == journey_state.ACTIVE
        repository.update_state(
            conn, journey_id, lifecycle_status=transition.lifecycle_status, movement_state=transition.movement_state,
            progress=progress, state_version=version, ended_at=now if transition.ended else journey["ended_at"],
            # 상태가 바뀌면 즉시 재평가를 예약하고, 완료·취소면 예약을 해제한다(수집 대상도 함께 빠진다).
            next_recalc_at=now if active else None)
        return {"event_id": event_id, "state_version_after": version, "replayed": False}


def _resolve_references(conn, journey_id: UUID, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    if payload.get("routeId") is not None:
        route = transit_repository.route_by_id(conn, int(payload["routeId"]))
        if route is None:
            raise JourneyError(422, "ROUTE_NOT_FOUND", "routeId에 해당하는 노선이 없습니다.")
        resolved.update(route_internal_id=route["id"], route_external_id=route["external_id"])
    if payload.get("stopId") is not None:
        stop = transit_repository.stop_by_id(conn, int(payload["stopId"]))
        if stop is None:
            raise JourneyError(422, "STOP_NOT_FOUND", "stopId에 해당하는 정류소가 없습니다.")
        resolved["stop_internal_id"] = stop["id"]
    if event_type == "correction":
        target = repository.get_event(conn, journey_id, int(payload["supersedesEventId"]))
        if target is None or target["event_type"] not in ("board", "alight"):
            raise JourneyError(422, "INVALID_CORRECTION_TARGET", "수정할 탑승·하차 이벤트가 이 이동에 없습니다.")
        resolved["target"] = {"id": target["id"], "event_type": target["event_type"]}
    return resolved


# ---------------------------------------------------------------- 위치

def update_location(db: Database, user_id: UUID, journey_id: UUID, body: LocationBody, *, now: datetime,
                    settings: Settings, max_age_seconds: float) -> bool:
    observed = body.observedAt
    if (observed - now).total_seconds() > settings.location_max_future_skew_seconds:
        raise JourneyError(422, "OBSERVED_AT_IN_FUTURE", "observedAt이 서버 수신 시각보다 미래입니다.")
    with db.transaction() as conn:
        journey = repository.get_journey(conn, journey_id, user_id, for_update=True)
        if journey is None:
            raise JourneyError(404, "NOT_FOUND", "이동이 없거나 요청 사용자 소유가 아닙니다.")
        if journey["lifecycle_status"] != journey_state.ACTIVE:
            raise JourneyError(409, "JOURNEY_ENDED", "이미 종료된 이동입니다.")
        previous = journey["location_observed_at"]
        was_limited = previous is None or (now - previous).total_seconds() > max_age_seconds
        return repository.update_location(conn, journey_id, lat=body.latitude, lon=body.longitude,
                                          observed_at=observed, received_at=now, accuracy_m=body.accuracyM,
                                          recalc_now=was_limited)


# ---------------------------------------------------------------- 응답 구성

def _camel(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            head, *rest = str(key).split("_")
            out[head + "".join(p[:1].upper() + p[1:] for p in rest)] = _camel(item)
        return out
    if isinstance(value, list):
        return [_camel(v) for v in value]
    return value


def _sid(value: Any) -> str | None:
    return None if value is None else str(value)


def selected_route_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    pseudo_row = {**snapshot, "evaluation": snapshot.get("evaluation") or {}}
    return {
        "candidateId": snapshot.get("candidate_id"), "searchId": snapshot.get("search_id"),
        "staticRouteKey": snapshot.get("static_route_key"), "optionKey": snapshot.get("option_key"),
        "selectionVersion": snapshot.get("selection_version"), "rankingMode": snapshot.get("ranking_mode"),
        "rank": snapshot.get("rank_no"), "selectedAt": snapshot.get("selected_at"),
        # 이 기능 이전에 시작한 이동은 순위 후보만 허용됐으므로 ranked로 본다.
        "startMode": snapshot.get("start_mode") or route_views.START_RANKED,
        "startLimitReasons": snapshot.get("start_limit_reasons") or [],
        "origin": _camel(snapshot.get("origin")), "destination": _camel(snapshot.get("destination")),
        "legs": [route_views.leg_view(leg) for leg in snapshot.get("legs") or []],
        "evaluation": route_views.evaluation_view(pseudo_row),
    }


def progress_view(progress: dict[str, Any]) -> dict[str, Any]:
    def boarding(b: dict[str, Any] | None) -> dict[str, Any] | None:
        if not b:
            return None
        return {"legIndex": b.get("leg_index"), "routeId": _sid(b.get("route_internal_id")),
                "routeExternalId": b.get("route_external_id"), "patternKey": b.get("pattern_key"),
                "directionCode": b.get("direction_code"), "vehicleId": b.get("vehicle_no"),
                "basis": b.get("basis"), "matchesPlannedRoute": b.get("matches_planned_route"),
                "eventId": _sid(b.get("event_id")), "occurredAt": b.get("occurred_at"),
                "receivedAt": b.get("received_at"), "correctedBy": [_sid(e) for e in b.get("corrected_by", [])]}

    def alight(a: dict[str, Any] | None) -> dict[str, Any] | None:
        if not a:
            return None
        return {"legIndex": a.get("leg_index"), "stopId": _sid(a.get("stop_internal_id")), "seq": a.get("seq"),
                "matchesPlannedStop": a.get("matches_planned_stop"), "eventId": _sid(a.get("event_id")),
                "occurredAt": a.get("occurred_at"), "receivedAt": a.get("received_at"),
                "correctedBy": [_sid(e) for e in a.get("corrected_by", [])]}

    return {"legIndex": progress.get("leg_index"), "nextBusLegIndex": progress.get("next_bus_leg_index"),
            "boarded": boarding(progress.get("boarded")), "completedBusLegs": progress.get("completed_bus_legs", []),
            "lastAlight": alight(progress.get("last_alight")),
            "cancelled": _camel(progress.get("cancelled")), "completedAt": _camel(progress.get("completed_at"))}


def guidance_view(journey: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """현재 상태 버전에서 계산한 안내만 반환한다. 아직 새 상태로 계산하지 않았으면 None과 pending."""
    guidance = journey.get("guidance") or {}
    if not guidance or guidance.get("base_state_version") != journey["state_version"]:
        return None, "pending" if journey["lifecycle_status"] == journey_state.ACTIVE else "ended"
    view = _camel(guidance)
    for key in ("nextBoarding", "onboard"):
        if isinstance(view.get(key), dict):
            for id_key in ("routeId", "stopId", "alightStopId"):
                if id_key in view[key]:
                    view[key][id_key] = _sid(view[key][id_key])
    return view, "current"


def location_view(journey: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    if journey.get("location_observed_at") is None:
        return None
    return {"latitude": journey["location_lat"], "longitude": journey["location_lon"],
            "observedAt": journey["location_observed_at"], "receivedAt": journey["location_received_at"],
            "accuracyM": journey["location_accuracy_m"],
            "ageSeconds": (now - journey["location_observed_at"]).total_seconds()}


def journey_view(journey: dict[str, Any], now: datetime) -> dict[str, Any]:
    guidance, guidance_status = guidance_view(journey)
    return {
        "journeyId": str(journey["id"]), "lifecycleStatus": journey["lifecycle_status"],
        "movementState": journey["movement_state"], "connectionState": journey["connection_state"],
        "stateVersion": journey["state_version"], "selectionVersion": journey["selection_version"],
        "resultVersion": journey["result_version"],
        "selectedRoute": selected_route_view(journey["selected_snapshot"] or {}),
        # 대안 제안은 Phase 5 범위다.
        "proposedRoute": None if journey.get("proposed_snapshot") is None else _camel(journey["proposed_snapshot"]),
        "guidance": guidance, "guidanceStatus": guidance_status,
        "currentProgress": progress_view(journey["current_progress"] or {}),
        "location": location_view(journey, now),
        "initialSearchId": _sid(journey["initial_search_id"]),
        "startedAt": journey["started_at"], "endedAt": journey["ended_at"],
    }


def journey_summary(journey: dict[str, Any]) -> dict[str, Any]:
    snapshot = journey["selected_snapshot"] or {}

    def place(p: dict[str, Any] | None) -> dict[str, Any] | None:
        if not p:
            return None
        return {"latitude": p.get("lat"), "longitude": p.get("lon"), "name": p.get("name"),
                "address": p.get("address")}

    return {"journeyId": str(journey["id"]), "lifecycleStatus": journey["lifecycle_status"],
            "movementState": journey["movement_state"], "origin": place(snapshot.get("origin")),
            "destination": place(snapshot.get("destination")), "startedAt": journey["started_at"],
            "endedAt": journey["ended_at"]}
