"""사용자 환승 결과(transfer_result·correction)와 결과 전에 고정한 예측(evaluation_snapshot)의 대조.

- 한 환승(snapshot_key)에는 원래 결과 1건과 그 결과를 차례로 고친 correction 사슬이 있다. 사슬은 서비스가 한 줄로만
  이어지게 저장하며(가장 최근 응답만 수정 대상), 마지막 유효 응답 하나만 쓴다. 재전송(같은 Idempotency-Key)은 새 행을 만들지 않는다.
- 각 이벤트의 effect에는 그 이벤트까지 반영한 결과 전체(result·실제 연결·연결 일치·제외 사유)를 저장하므로, 사슬의 마지막
  effect가 곧 유효 결과다.
- unknown·미응답은 성공/실패로 집계하지 않는다. 실제 이용 연결이 계획과 다르면 결과 값과 관계없이 당시 예측과 대조하지 않는다
  (다른 연결 이용을 실패로 채우지 않는다).
- 공공 관측·transfer_samples.time_feasible을 바꾸지 않는다. 이 모듈은 DB·HTTP에 의존하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

RESULTS = ("success", "failure", "unknown")
COUNTED_RESULTS = ("success", "failure")
EFFECT_KIND = "transfer_result"

# 연결 일치: planned(계획과 같음), different(계획과 다름), unverified(비교할 근거 없음).
MATCH_PLANNED, MATCH_DIFFERENT, MATCH_UNVERIFIED = "planned", "different", "unverified"


@dataclass(frozen=True)
class EffectiveResult:
    snapshot_key: str
    original_event_id: int
    effective_event_id: int
    chain: tuple[int, ...]
    result: str
    actual_connection: dict[str, Any]
    connection_match: str
    connection_evidence: str | None
    excluded_reasons: tuple[str, ...]

    @property
    def counted(self) -> bool:
        return self.result in COUNTED_RESULTS and not self.excluded_reasons


def _planned_vehicle(vehicle: Mapping[str, Any] | None) -> str | None:
    return (vehicle or {}).get("vehicle_id") if isinstance(vehicle, Mapping) else None


def match_connection(snapshot: Mapping[str, Any], actual: Mapping[str, Any],
                     recorded: Mapping[str, Any] | None = None) -> tuple[str, str | None, list[str]]:
    """실제 이용 연결이 고정 예측의 계획 연결과 같은지 판단한다. (일치, 근거 출처, 차이 사유).

    actual은 사용자가 결과와 함께 알려준 값(usedPlanned·frontRouteId·nextRouteId·frontVehicleId·nextVehicleId, 노선은 내부 ID),
    recorded는 이동 중 탑승 확인(board)으로 기록된 앞·다음 구간의 노선·차량이다. 사용자 입력이 있으면 그것을 우선한다.
    계획 차량을 모르면(null) 차량은 비교하지 않는다.
    """
    planned = {
        "front_route": (snapshot.get("front_route") or {}).get("route_internal_id"),
        "next_route": (snapshot.get("next_route") or {}).get("route_internal_id"),
        "front_vehicle": _planned_vehicle(snapshot.get("front_vehicle")),
        "next_vehicle": _planned_vehicle(snapshot.get("next_vehicle")),
    }
    reported = {
        "front_route": actual.get("frontRouteId"), "next_route": actual.get("nextRouteId"),
        "front_vehicle": actual.get("frontVehicleId"), "next_vehicle": actual.get("nextVehicleId"),
    }
    used_planned = actual.get("usedPlanned")
    has_report = used_planned is not None or any(v is not None for v in reported.values())
    if has_report:
        evidence, values = "user_reported", reported
    elif recorded and any(v is not None for v in recorded.values()):
        evidence, values = "recorded_boarding", dict(recorded)
    else:
        return MATCH_UNVERIFIED, None, []
    differences = []
    if used_planned is False and evidence == "user_reported":
        differences.append("user_reported_other_connection")
    compared = False
    for key in ("front_route", "next_route", "front_vehicle", "next_vehicle"):
        value, plan = values.get(key), planned[key]
        if value is None or plan is None:
            continue
        compared = True
        if str(value) != str(plan):
            differences.append(f"{key}_differs")
    if differences:
        return MATCH_DIFFERENT, evidence, differences
    if compared or used_planned is True:
        return MATCH_PLANNED, evidence, []
    return MATCH_UNVERIFIED, evidence, []


def exclusion_reasons(result: str, match: str, differences: Iterable[str]) -> list[str]:
    """당시 예측과 대조하지 않는 이유. unknown은 제외 사유가 아니라 집계 대상이 아닌 응답이다."""
    reasons = []
    if match == MATCH_DIFFERENT:
        reasons.append("different_connection")
        reasons.extend(differences)
    return reasons


def _is_result_event(event: Mapping[str, Any]) -> bool:
    effect = (event.get("payload") or {}).get("effect") or {}
    return effect.get("kind") == EFFECT_KIND


def resolve(events: Iterable[Mapping[str, Any]]) -> dict[str, EffectiveResult]:
    """이동 이벤트 목록에서 환승별 유효 결과를 구한다. 수정 사슬을 따라 마지막 응답을 쓴다."""
    result_events = sorted((e for e in events if _is_result_event(e)), key=lambda e: e["id"])
    by_id = {e["id"]: e for e in result_events}
    successor: dict[int, int] = {}
    for event in result_events:
        if event["event_type"] == "correction":
            target = event["payload"]["effect"].get("supersedes_event_id")
            if target in by_id:
                # 서비스가 한 줄 사슬만 저장하지만, 갈라진 기록이 있어도 결과가 하나로 정해지도록 나중 이벤트를 쓴다.
                successor[target] = max(successor.get(target, event["id"]), event["id"])
    out: dict[str, EffectiveResult] = {}
    for event in result_events:
        if event["event_type"] != "transfer_result":
            continue
        chain = [event["id"]]
        while chain[-1] in successor and len(chain) <= len(result_events):
            chain.append(successor[chain[-1]])
        head = by_id[chain[-1]]["payload"]["effect"]
        key = event["payload"]["effect"]["snapshot_key"]
        out[key] = EffectiveResult(
            snapshot_key=key, original_event_id=event["id"], effective_event_id=chain[-1], chain=tuple(chain),
            result=head["result"], actual_connection=dict(head.get("actual_connection") or {}),
            connection_match=head.get("connection_match") or MATCH_UNVERIFIED,
            connection_evidence=head.get("connection_evidence"),
            excluded_reasons=tuple(head.get("excluded_reasons") or ()))
    return out


def chain_of(events: Iterable[Mapping[str, Any]], event_id: int) -> EffectiveResult | None:
    """event_id가 속한 환승 결과 사슬. 결과·결과 수정 이벤트가 아니면 None."""
    for effective in resolve(events).values():
        if event_id in effective.chain:
            return effective
    return None


def comparison_rows(snapshots: Iterable[Mapping[str, Any]],
                    effective: Mapping[str, EffectiveResult]) -> list[dict[str, Any]]:
    """고정 예측 하나마다 사용자 결과와의 대조 상태. status: counted / unknown / excluded / no_response."""
    rows = []
    for snap in sorted(snapshots, key=lambda s: (s.get("selection_version") or 0, s.get("transfer_index") or 0)):
        found = effective.get(snap.get("snapshot_key"))
        if found is None:
            status = "no_response"
        elif found.excluded_reasons:
            status = "excluded"
        elif found.result == "unknown":
            status = "unknown"
        else:
            status = "counted"
        rows.append({
            "snapshot_key": snap.get("snapshot_key"), "selection_version": snap.get("selection_version"),
            "transfer_index": snap.get("transfer_index"),
            "predicted_probability": snap.get("predicted_probability"), "slack_seconds": snap.get("slack_seconds"),
            "connection_status": snap.get("connection_status"),
            "result": found.result if found else None, "status": status, "counted": status == "counted",
            "result_event_id": found.effective_event_id if found else None,
            "original_event_id": found.original_event_id if found else None,
            "excluded_reasons": list(found.excluded_reasons) if found else [],
            "connection_match": found.connection_match if found else None,
        })
    return rows


def summarize(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """대조 집계. 같은 환승은 한 번만 센다(행이 환승 하나)."""
    summary = {"snapshots": 0, "responded": 0, "counted": 0, "success": 0, "failure": 0, "unknown": 0,
               "excluded": 0, "no_response": 0}
    for row in rows:
        summary["snapshots"] += 1
        status = row["status"]
        if status == "no_response":
            summary["no_response"] += 1
            continue
        summary["responded"] += 1
        if status == "counted":
            summary["counted"] += 1
            summary[row["result"]] += 1
        elif status == "unknown":
            summary["unknown"] += 1
        else:
            summary["excluded"] += 1
    return summary
