"""이동 상태와 수동 입력에 따른 상태 전이 규칙(Database Structure 15. 이동 상태 전이).

| 현재 상태                          | 입력       | 다음 상태 |
| active · walking/waiting/transferring | board      | onboard (차량 미확인 허용) |
| active · onboard                    | alight     | 다음 버스 구간이 있으면 transferring, 없으면 walking |
| active                             | completed  | completed · arrived, 종료 시각 기록 |
| active                             | cancelled  | cancelled, 종료 시각·사유 기록 |
| completed / cancelled              | 이동 입력   | 거부(수정 correction만 허용) |

버스 놓침·목적지 변경·대안 수락/거절은 Phase 5, 환승 결과 입력은 Phase 6 범위라 이 단계에서는 받지 않는다.
이 모듈은 DB·HTTP에 의존하지 않는다. 시각·차량 미확인 값을 확인된 진행 상태로 만들지 않는다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

ACTIVE, COMPLETED, CANCELLED = "active", "completed", "cancelled"
WALKING, WAITING, ONBOARD, TRANSFERRING, ARRIVED = "walking", "waiting", "onboard", "transferring", "arrived"
STATE_EVENTS = ("board", "alight", "completed", "cancelled")
SUPPORTED_EVENTS = (*STATE_EVENTS, "correction")
DEFERRED_EVENTS = {
    "missed_bus": "Phase 5(이동 중 재평가·대안 추천)",
    "destination_changed": "Phase 5(이동 중 재평가·대안 추천)",
    "route_accepted": "Phase 5(이동 중 재평가·대안 추천)",
    "route_rejected": "Phase 5(이동 중 재평가·대안 추천)",
    "transfer_result": "Phase 6(기록 관리·이용 결과)",
}


class TransitionError(Exception):
    """현재 상태에서 할 수 없는 입력(409) 또는 값 오류(422)."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Transition:
    lifecycle_status: str
    movement_state: str
    progress: dict[str, Any]
    ended: bool
    effect: dict[str, Any]


def bus_legs(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [leg for leg in legs if leg.get("kind") == "bus"]


def _next_bus_index(legs: list[dict[str, Any]], after: int) -> int | None:
    return next((leg["leg_index"] for leg in bus_legs(legs) if leg["leg_index"] > after), None)


def initial_progress(legs: list[dict[str, Any]]) -> dict[str, Any]:
    """시작 시점: 첫 구간(출발 도보)에서 시작하고 다음 버스 구간을 기록한다. 탑승 정보는 아직 없다."""
    first_bus = _next_bus_index(legs, -1)
    if first_bus is None:
        raise TransitionError(422, "CANDIDATE_INVALID", "버스 구간이 없는 후보입니다.")
    return {"leg_index": 0, "next_bus_leg_index": first_bus, "boarded": None, "completed_bus_legs": [],
            "last_alight": None, "history": []}


def initial_movement(legs: list[dict[str, Any]]) -> str:
    return WALKING if legs and legs[0].get("kind") == "walk" else WAITING


def _leg(legs: list[dict[str, Any]], index: Any) -> dict[str, Any]:
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(legs):
        raise TransitionError(422, "INVALID_LEG_INDEX", "legIndex가 선택 경로의 구간 범위를 벗어났습니다.")
    return legs[index]


def apply(
    *,
    lifecycle_status: str,
    movement_state: str,
    progress: dict[str, Any],
    legs: list[dict[str, Any]],
    event_type: str,
    payload: dict[str, Any],
    occurred_at: datetime | None,
    received_at: datetime,
    resolved: dict[str, Any] | None = None,
) -> Transition:
    """입력 하나를 적용한 다음 상태. resolved는 서비스가 확인한 참조 값(노선 내부 ID 존재, 수정 대상 이벤트 등)이다."""
    resolved = resolved or {}
    progress = copy.deepcopy(progress)
    when = {"occurred_at": occurred_at.isoformat() if occurred_at else None, "received_at": received_at.isoformat()}

    if event_type == "correction":
        return _correction(lifecycle_status, movement_state, progress, payload, resolved, when)
    if lifecycle_status != ACTIVE:
        raise TransitionError(409, "JOURNEY_ENDED", "종료된 이동에는 수정(correction) 외 입력을 받을 수 없습니다.")

    if event_type == "board":
        if movement_state not in (WALKING, WAITING, TRANSFERRING):
            raise TransitionError(409, "INVALID_TRANSITION", f"{movement_state} 상태에서는 탑승을 확인할 수 없습니다.")
        leg = _leg(legs, payload.get("legIndex"))
        if leg.get("kind") != "bus":
            raise TransitionError(422, "INVALID_LEG_INDEX", "탑승은 버스 구간(legIndex)에만 확인할 수 있습니다.")
        if leg["leg_index"] != progress.get("next_bus_leg_index"):
            raise TransitionError(409, "LEG_NOT_NEXT", "다음 버스 구간이 아닙니다.")
        planned_route = leg.get("route_internal_id")
        route_id = resolved.get("route_internal_id", planned_route)
        boarded = {
            "leg_index": leg["leg_index"],
            "route_internal_id": route_id,
            "route_external_id": resolved.get("route_external_id", leg.get("route_id")),
            "pattern_key": payload.get("patternKey") or leg.get("pattern_key"),
            "direction_code": payload.get("directionCode", leg.get("board", {}).get("direction_code")),
            # 차량을 모르면 null. 계획 차량으로 채우지 않는다.
            "vehicle_no": payload.get("vehicleId"),
            "basis": payload.get("confirmation") or "user_confirmed",
            "matches_planned_route": route_id == planned_route,
            "event_id": resolved.get("event_id"),
            **when,
        }
        progress.update(leg_index=leg["leg_index"], boarded=boarded, next_bus_leg_index=None)
        return Transition(ACTIVE, ONBOARD, progress, False, {"boarded": boarded})

    if event_type == "alight":
        if movement_state != ONBOARD or not progress.get("boarded"):
            raise TransitionError(409, "INVALID_TRANSITION", "탑승 중이 아니라 하차를 확인할 수 없습니다.")
        boarded = progress["boarded"]
        leg = _leg(legs, payload.get("legIndex"))
        if leg["leg_index"] != boarded["leg_index"]:
            raise TransitionError(409, "LEG_NOT_CURRENT", "현재 탑승 구간이 아닙니다.")
        planned_stop = (leg.get("alight") or {}).get("stop_internal_id")
        alight = {
            "leg_index": leg["leg_index"],
            "stop_internal_id": resolved.get("stop_internal_id", planned_stop),
            "seq": payload.get("seq", (leg.get("alight") or {}).get("seq")),
            "matches_planned_stop": resolved.get("stop_internal_id", planned_stop) == planned_stop,
            "event_id": resolved.get("event_id"),
            **when,
        }
        next_bus = _next_bus_index(legs, leg["leg_index"])
        progress["completed_bus_legs"] = [*progress.get("completed_bus_legs", []), leg["leg_index"]]
        progress["history"] = [*progress.get("history", []), {"boarded": boarded, "alighted": alight}]
        progress.update(leg_index=min(leg["leg_index"] + 1, len(legs) - 1), boarded=None, last_alight=alight,
                        next_bus_leg_index=next_bus)
        return Transition(ACTIVE, TRANSFERRING if next_bus is not None else WALKING, progress, False,
                          {"alighted": alight, "next_bus_leg_index": next_bus})

    if event_type == "completed":
        progress.update(completed_at=when)
        return Transition(COMPLETED, ARRIVED, progress, True, {"completed": when})

    if event_type == "cancelled":
        reason = payload.get("reason")
        progress.update(cancelled={"reason": reason, **when})
        return Transition(CANCELLED, movement_state, progress, True, {"cancelled": {"reason": reason}})

    if event_type in DEFERRED_EVENTS:
        raise TransitionError(422, "UNSUPPORTED_EVENT_TYPE",
                              f"{event_type}은 {DEFERRED_EVENTS[event_type]} 범위라 아직 받지 않습니다.")
    raise TransitionError(422, "UNSUPPORTED_EVENT_TYPE", f"지원하지 않는 이벤트입니다: {event_type}")


def _correction(lifecycle_status: str, movement_state: str, progress: dict[str, Any], payload: dict[str, Any],
                resolved: dict[str, Any], when: dict[str, Any]) -> Transition:
    """탑승·하차 기록을 고치는 새 이벤트. 원래 이벤트는 그대로 두고, 현재 탑승 정보면 진행 상태에 반영한다."""
    target = resolved.get("target")
    if not target:
        raise TransitionError(422, "INVALID_CORRECTION_TARGET", "수정할 탑승·하차 이벤트가 없습니다.")
    changes = {k: payload[k] for k in ("routeId", "patternKey", "directionCode", "vehicleId", "stopId", "seq")
               if k in payload}
    if not changes:
        raise TransitionError(422, "INVALID_CORRECTION", "수정할 값이 없습니다.")
    boarded = progress.get("boarded")
    applied_to = None
    if target["event_type"] == "board":
        if set(changes) - {"routeId", "patternKey", "directionCode", "vehicleId"}:
            raise TransitionError(422, "INVALID_CORRECTION", "탑승 수정은 노선·패턴·방향·차량만 바꿀 수 있습니다.")
        if boarded and boarded.get("event_id") == target["id"]:
            if "routeId" in changes:
                boarded["route_internal_id"] = resolved.get("route_internal_id")
                boarded["route_external_id"] = resolved.get("route_external_id")
            if "patternKey" in changes:
                boarded["pattern_key"] = changes["patternKey"]
            if "directionCode" in changes:
                boarded["direction_code"] = changes["directionCode"]
            if "vehicleId" in changes:
                boarded["vehicle_no"] = changes["vehicleId"]
            boarded["corrected_by"] = [*boarded.get("corrected_by", []), resolved.get("event_id")]
            applied_to = "current_boarding"
    elif target["event_type"] == "alight":
        if set(changes) - {"stopId", "seq"}:
            raise TransitionError(422, "INVALID_CORRECTION", "하차 수정은 정류소·경유 순서만 바꿀 수 있습니다.")
        last = progress.get("last_alight")
        if last and last.get("event_id") == target["id"]:
            if "stopId" in changes:
                last["stop_internal_id"] = resolved.get("stop_internal_id")
            if "seq" in changes:
                last["seq"] = changes["seq"]
            last["corrected_by"] = [*last.get("corrected_by", []), resolved.get("event_id")]
            applied_to = "last_alight"
    else:
        raise TransitionError(422, "INVALID_CORRECTION_TARGET", "탑승·하차 이벤트만 수정할 수 있습니다.")
    return Transition(lifecycle_status, movement_state, progress, False,
                      {"supersedes_event_id": target["id"], "changes": changes, "applied_to": applied_to,
                       **when})


def stamp_event_id(event_type: str, progress: dict[str, Any], event_id: int) -> dict[str, Any]:
    """이벤트를 저장해 ID가 정해진 뒤 진행 상태의 참조(event_id·corrected_by)를 채운다."""
    progress = copy.deepcopy(progress)
    if event_type == "board" and progress.get("boarded"):
        progress["boarded"]["event_id"] = event_id
    elif event_type == "alight" and progress.get("last_alight"):
        progress["last_alight"]["event_id"] = event_id
        if progress.get("history"):
            progress["history"][-1]["alighted"]["event_id"] = event_id
    elif event_type == "correction":
        for holder in (progress.get("boarded"), progress.get("last_alight")):
            if holder and holder.get("corrected_by"):
                holder["corrected_by"] = [event_id if e is None else e for e in holder["corrected_by"]]
    return progress
