"""활성 이동의 기본 안내 계산: 선택 경로의 남은 구간 → 공유 수집 최신값 → 기존 평가 엔진 → 안내(guidance).

- 남은 경로는 선택 스냅샷의 버스 구간으로 기존 StaticCandidate를 다시 구성하고 evaluate_candidates를 그대로 호출한다.
  핵심 함수의 계약은 바꾸지 않는다. 대안 재탐색·비교·제안은 Phase 5 범위라 여기서 하지 않는다(현재 선택만 평가).
- 탑승 전(walking·transferring): 다음 승차 정류소부터 평가한다. 출발 도보는 진행 위치를 모르므로 스냅샷의 전체 도보 시간을
  쓰고 그 가정을 사유로 남긴다. 탑승 중: 차량을 사용자가 확인했고 같은 차량의 하차 ETA가 있을 때만 하차 시각을 확정하고,
  이후 구간은 그 시각부터 평가한다. 탑승 중인 차량의 첫 승차 성공 여부를 다시 평가하지 않는다.
- ETA 기준 시각이 확인되지 않은 TAGO 도착정보는 기존과 같이 eta_basis_unconfirmed로 제외되고 미산출 값은 null과 사유다.
- 위치는 관측 시각 기준 최신성만 판단한다. 오래된 위치·미수신은 제한 사유이며 이동을 멈추거나 취소하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from app.core.config import Settings
from app.db.session import Database
from app.modules.evaluation import repository as evaluation_repository
from app.modules.evaluation import samples as sample_rules
from app.modules.evaluation.eta import index_etas
from app.modules.evaluation.schemas import DESTINATION_REF, ORIGIN_REF, EvaluationInputs, WalkResult
from app.modules.evaluation.service import evaluate_candidates
from app.modules.journeys import state as journey_state
from app.modules.routing.schemas import (
    CandidateSearchResult,
    Coordinate,
    SearchContext,
    SearchCoverage,
    StaticCandidate,
    StaticRide,
    TransferPair,
)
from app.modules.transit import repository as transit_repository
from app.modules.transit import service as transit_service
from app.modules.transit.schemas import RealtimeTarget, StaticNetwork
from app.workflows import search as search_flow

JOURNEY_SOURCE = "journey"
UPCOMING_LIMIT = 3


# ---------------------------------------------------------------- 남은 경로

def remaining_bus_legs(legs: list[dict[str, Any]], progress: dict[str, Any], movement: str) -> list[dict[str, Any]]:
    """아직 끝나지 않은 버스 구간(탑승 중이면 현재 구간부터)."""
    done = set(progress.get("completed_bus_legs") or [])
    current = (progress.get("boarded") or {}).get("leg_index") if movement == journey_state.ONBOARD else None
    return [leg for leg in journey_state.bus_legs(legs)
            if leg["leg_index"] not in done and (current is None or leg["leg_index"] >= current)]


def journey_targets(snapshot: dict[str, Any], progress: dict[str, Any], movement: str,
                    source: str) -> list[RealtimeTarget]:
    """남은 버스 구간의 승하차 정류소 도착·노선 위치. 끝난 구간은 수집 대상에서 뺀다."""
    srcs = frozenset({source})
    arrivals, positions = set(), set()
    for leg in remaining_bus_legs(snapshot.get("legs") or [], progress, movement):
        for side in ("board", "alight"):
            stop = (leg.get(side) or {}).get("stop_id")
            if stop:
                arrivals.add(stop)
        if leg.get("route_id"):
            positions.add(leg["route_id"])
    return transit_service.merge_targets([[RealtimeTarget("arrivals", s, srcs) for s in sorted(arrivals)],
                                          [RealtimeTarget("positions", r, srcs) for r in sorted(positions)]])


def _walk_result(leg: dict[str, Any] | None, from_ref: str, to_ref: str) -> WalkResult | None:
    if leg is None:
        return None
    status = leg.get("status")
    if status == "same_stop":
        return WalkResult(from_ref=from_ref, to_ref=to_ref, status="available", duration_seconds=0.0,
                          distance_meters=0.0)
    if status == "available" and leg.get("duration_seconds") is not None:
        return WalkResult(from_ref=from_ref, to_ref=to_ref, status="available",
                          duration_seconds=leg["duration_seconds"], distance_meters=leg.get("distance_m"))
    if status == "no_path":
        return WalkResult(from_ref=from_ref, to_ref=to_ref, status="no_route")
    if status == "error":
        return WalkResult(from_ref=from_ref, to_ref=to_ref, status="lookup_failed")
    return None  # 미조회(unknown)는 시간 미확인


def remaining_candidate(snapshot: dict[str, Any], rides: list[dict[str, Any]], *, first_walk_leg: dict | None,
                        static_version: str) -> tuple[StaticCandidate, list[WalkResult]]:
    """남은 버스 구간으로 정적 후보를 다시 만든다. 첫 도보(ORIGIN_REF→첫 승차)는 직전 도보 구간 값을 쓴다."""
    legs = snapshot["legs"]
    static_rides = tuple(StaticRide(
        route_id=leg["route_id"], pattern_id=leg["pattern_key"], direction=None,
        board_stop_id=leg["board"]["stop_id"], board_position=leg["board"]["seq"],
        alight_stop_id=leg["alight"]["stop_id"], alight_position=leg["alight"]["seq"]) for leg in rides)
    transfers = tuple(TransferPair(from_stop_id=a.alight_stop_id, to_stop_id=b.board_stop_id,
                                   same_stop=a.alight_stop_id == b.board_stop_id, straight_line_meters=None)
                      for a, b in zip(static_rides, static_rides[1:]))
    walks = []
    first = _walk_result(first_walk_leg, ORIGIN_REF, static_rides[0].board_stop_id)
    if first is not None:
        walks.append(first)
    for leg, nxt, pair in zip(rides, rides[1:], transfers):
        between = legs[leg["leg_index"] + 1] if leg["leg_index"] + 1 < nxt["leg_index"] else None
        if not pair.same_stop:
            result = _walk_result(between, pair.from_stop_id, pair.to_stop_id)
            if result is not None:
                walks.append(result)
    final = legs[-1] if legs and legs[-1].get("kind") == "walk" else None
    egress = _walk_result(final, static_rides[-1].alight_stop_id, DESTINATION_REF)
    if egress is not None:
        walks.append(egress)
    key = f"{snapshot.get('static_route_key')}|from-leg:{rides[0]['leg_index']}"
    candidate = StaticCandidate(
        static_candidate_id=hashlib.sha256(key.encode()).hexdigest()[:24],
        access_stop_id=static_rides[0].board_stop_id, egress_stop_id=static_rides[-1].alight_stop_id,
        rides=static_rides, transfers=transfers, boarding_count=len(static_rides),
        transfer_count=len(static_rides) - 1, access_straight_line_meters=None, egress_straight_line_meters=None)
    return candidate, walks


def _search_result(candidate: StaticCandidate, static_version: str) -> CandidateSearchResult:
    return CandidateSearchResult(
        static_snapshot_id=static_version, candidates=(candidate,), access_stops=(), egress_stops=(),
        reason_codes=("journey_remaining_route",),
        coverage=SearchCoverage(truncated=False, truncation_reasons=(), completed_start_stops=(),
                                partial_start_stop=None, unexplored_start_stops=(), expanded_states_by_level=(),
                                completed_candidates_by_level=(), pruned_cycle_count=0, pruned_split_ride_count=0),
        index_issues=())


# ---------------------------------------------------------------- 계산

@dataclass
class NetworkCache:
    """워커가 여러 이동을 계산할 때 같은 정적 공개본을 한 번만 읽는다(버전이 바뀌면 다시 읽음)."""

    network: StaticNetwork | None = None
    versions: dict[str, Any] = field(default_factory=dict)

    def get(self, db: Database) -> StaticNetwork | None:
        with db.transaction() as conn:
            versions = transit_repository.current_static_versions(conn)
        if len(versions) != 1:
            return None
        if self.network is None or self.network.data_version != versions[0]:
            self.network = transit_service.read_static_network(db)
        return self.network


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def location_status(journey: dict[str, Any], now: datetime, max_age_seconds: float) -> dict[str, Any]:
    observed = journey.get("location_observed_at")
    if observed is None:
        return {"status": "not_received", "observed_at": None, "received_at": None, "age_seconds": None,
                "accuracy_m": None, "max_age_seconds": max_age_seconds}
    age = (now - observed).total_seconds()
    return {"status": "stale" if age > max_age_seconds else "fresh", "observed_at": _iso(observed),
            "received_at": _iso(journey.get("location_received_at")), "age_seconds": age,
            "accuracy_m": journey.get("location_accuracy_m"), "max_age_seconds": max_age_seconds,
            "location": {"latitude": journey.get("location_lat"), "longitude": journey.get("location_lon")}}


def _eta_brief(eta, now: datetime) -> dict[str, Any]:
    return {"vehicle_id": eta.vehicle_id, "run_key": eta.run_key, "predicted_arrival_at": _iso(eta.predicted_arrival_at),
            "wait_seconds": max(0.0, (eta.predicted_arrival_at - now).total_seconds()), "eta_source": eta.eta_source,
            "basis_at": _iso(eta.basis_at), "basis_source": eta.basis_source, "collected_at": _iso(eta.collected_at)}


def _pick_option(evaluated: list, planned_run: str | None):
    """평가된 차량 조합 중 안내에 쓸 하나: 계획 차량(회차)이 있으면 그 조합, 없으면 기준 도착이 가장 이른 연결 가능 조합."""
    if not evaluated:
        return None
    if planned_run:
        for item in evaluated:
            if item.legs and item.legs[0].run_key == planned_run:
                return item
    feasible = [i for i in evaluated if i.nominally_feasible and i.planned_arrival_at is not None]
    if feasible:
        return min(feasible, key=lambda i: (i.planned_arrival_at, i.timed_candidate_id))
    return sorted(evaluated, key=lambda i: i.timed_candidate_id)[0]


def compute_guidance(
    db: Database,
    journey: dict[str, Any],
    settings: Settings,
    *,
    now: datetime,
    network_cache: NetworkCache | None = None,
    realtime_provider: search_flow.RealtimeProvider | None = None,
) -> dict[str, Any]:
    """journey 행(잠금 없이 읽은 값)으로 안내를 계산한다. 저장은 호출자가 상태 버전 조건으로 한다."""
    config = search_flow.load_search_config(settings)
    policy = config.evaluation_policy
    snapshot = journey["selected_snapshot"]
    legs = snapshot.get("legs") or []
    progress = journey.get("current_progress") or {}
    movement = journey["movement_state"]
    reasons: list[str] = []
    guidance: dict[str, Any] = {
        "base_state_version": journey["state_version"], "selection_version": journey["selection_version"],
        "evaluated_at": _iso(now), "config_version": config.config_version,
        "eta_basis_policy": config.eta_basis_policy_version,
        "selection_config_version": snapshot.get("config_version"),
        "ranking_mode": snapshot.get("ranking_mode") or "nominal",
        "min_improvement_seconds": policy.min_improvement_seconds,
        "movement_state": movement, "current_leg_index": progress.get("leg_index"),
        "next_boarding": None, "onboard": None, "arrival": None, "alternatives": None,
        "alternatives_reason": "phase5_reevaluation_not_implemented",
    }
    loc_max_age = settings.journey_location_max_age_seconds or policy.max_data_age_seconds
    guidance["location"] = location_status(journey, now, loc_max_age)
    if guidance["location"]["status"] == "not_received":
        reasons.append("location_not_received")
    elif guidance["location"]["status"] == "stale":
        reasons.append("location_stale")

    rides = remaining_bus_legs(legs, progress, movement)
    if not rides:
        guidance["arrival"] = {"final_walk_seconds": (legs[-1].get("duration_seconds") if legs else None),
                               "planned_arrival_at": None}
        reasons.append("no_remaining_bus_leg")
        guidance.update(status="available" if movement == journey_state.WALKING else "data_limited",
                        reason_codes=list(dict.fromkeys(reasons)), data_as_of=None, data_inputs=[])
        return guidance

    cache = network_cache or NetworkCache()
    network = cache.get(db)
    if network is None:
        reasons.append("static_data_unavailable")
        guidance.update(status="data_limited", reason_codes=reasons, data_as_of=None, data_inputs=[])
        return guidance
    static_version = str(network.data_version)
    if snapshot.get("static_version") and snapshot["static_version"] != static_version:
        reasons.append("static_version_changed_since_selection")

    targets = journey_targets(snapshot, progress, movement, f"{JOURNEY_SOURCE}:{journey['id']}")
    cohort = sample_rules.condition_key("tago", policy, config.eta_basis_policy)
    with db.transaction(isolation="REPEATABLE READ") as conn:
        if realtime_provider is not None:
            realtime = realtime_provider(conn, targets, network, now)
        else:
            realtime = search_flow.realtime_inputs(conn, targets, network, now=now,
                                                   interval_seconds=settings.realtime_interval_seconds,
                                                   max_data_age_seconds=policy.max_data_age_seconds,
                                                   eta_basis_policy=config.eta_basis_policy)
        segments = search_flow.segment_observations(conn, [leg["pattern_key"] for leg in rides], cohort, until=now)
        error_samples = sample_rules.transfer_error_samples(
            evaluation_repository.labeled_samples(conn, cohort, before=now))
        scenes = sample_rules.joint_scenes(evaluation_repository.complete_scenes(conn, cohort, before=now))
    states = {k: {key: v.get(key) for key in ("kind", "external_id", "state", "content", "collected_at",
                                               "age_seconds", "last_attempt_failed")}
              for k, v in realtime.target_states.items()}
    # 대상 키(원천 조회 조건)는 응답 변환에서 바뀌지 않도록 값으로 둔다.
    guidance["data_inputs"] = search_flow._jsonable([{"target_key": k, **v} for k, v in sorted(states.items())])
    collected = [s["collected_at"] for s in states.values() if s.get("collected_at")]
    guidance["data_as_of"] = _iso(min(collected)) if collected else None
    for s in states.values():
        if s.get("state") == "no_data":
            reasons.append("realtime_unavailable")
        elif s.get("state") == "stale":
            reasons.append("realtime_stale")
        if s.get("last_attempt_failed"):
            reasons.append("realtime_last_attempt_failed")
    etas = index_etas(realtime.eta_reports, evaluated_at=now, max_data_age_seconds=policy.max_data_age_seconds)
    data_snapshot_id = hashlib.sha256(json.dumps([static_version, sorted(realtime.batch_refs, key=str)],
                                                 default=str).encode()).hexdigest()[:32]
    guidance["data_snapshot_id"] = data_snapshot_id

    def evaluate(remaining: list[dict[str, Any]], first_walk: dict | None, start_at: datetime):
        candidate, walks = remaining_candidate(snapshot, remaining, first_walk_leg=first_walk,
                                               static_version=static_version)
        origin = (remaining[0].get("board") or {})
        context = SearchContext(
            origin=Coordinate(lat=origin.get("lat") or snapshot["origin"]["lat"],
                              lon=origin.get("lon") or snapshot["origin"]["lon"]),
            destination=Coordinate(lat=snapshot["destination"]["lat"], lon=snapshot["destination"]["lon"]),
            start_at=start_at, evaluated_at=now, static_snapshot_id=static_version)
        inputs = EvaluationInputs(
            data_snapshot_id=data_snapshot_id, sample_condition_key=cohort, walks=tuple(walks),
            eta_reports=tuple(realtime.eta_reports), queried_stop_ids=realtime.queried_stop_ids,
            segment_observations=tuple(segments), transfer_error_samples=tuple(error_samples),
            joint_scenes=tuple(scenes))
        result = evaluate_candidates(_search_result(candidate, static_version), context, inputs, policy)
        return [*result.candidates, *result.limited]

    def arrival_from(item) -> dict[str, Any]:
        if item is None:
            return {"planned_arrival_at": None, "remaining_seconds": None, "time_status": "unavailable",
                    "nominally_feasible": None, "journey_probability": None, "expected_total_seconds": None,
                    "reason_codes": ["remaining_route_not_evaluated"]}
        arrival = item.planned_arrival_at
        return {"planned_arrival_at": _iso(arrival),
                "remaining_seconds": (arrival - now).total_seconds() if arrival else None,
                "time_status": item.time_status, "nominally_feasible": item.nominally_feasible,
                "journey_probability": item.journey_probability,
                "transfer_probabilities": list(item.transfer_probabilities),
                "expected_total_seconds": item.expected_total_seconds, "reason_codes": list(item.reason_codes)}

    if movement == journey_state.ONBOARD:
        current = rides[0]
        boarded = progress.get("boarded") or {}
        alight = current.get("alight") or {}
        bucket = etas.by_position.get((current["pattern_key"], alight.get("seq"), alight.get("stop_id")), ())
        vehicle = boarded.get("vehicle_no")
        alight_eta = next((e for e in bucket if vehicle and e.vehicle_id == vehicle and e.predicted_arrival_at >= now),
                          None)
        onboard = {"leg_index": current["leg_index"], "route_id": current.get("route_internal_id"),
                   "alight_stop_id": alight.get("stop_internal_id"), "alight_seq": alight.get("seq"),
                   "vehicle_no": vehicle, "alight_eta": _eta_brief(alight_eta, now) if alight_eta else None}
        if alight_eta is None:
            reasons.append("onboard_vehicle_unconfirmed" if not vehicle else "onboard_alight_eta_unavailable")
        guidance["onboard"] = onboard
        later = rides[1:]
        if alight_eta is None:
            guidance["arrival"] = arrival_from(None)
        elif not later:
            final = legs[-1] if legs[-1].get("kind") == "walk" else None
            walk = _walk_result(final, current["alight"]["stop_id"], DESTINATION_REF)
            if walk is not None and walk.status == "available":
                arrival = alight_eta.predicted_arrival_at + timedelta(seconds=walk.duration_seconds)
                guidance["arrival"] = {"planned_arrival_at": _iso(arrival),
                                       "remaining_seconds": (arrival - now).total_seconds(),
                                       "time_status": "available", "nominally_feasible": True,
                                       "journey_probability": None, "expected_total_seconds": None,
                                       "reason_codes": []}
            else:
                guidance["arrival"] = arrival_from(None)
                reasons.append("egress_walk_unavailable")
        else:
            transfer_walk = legs[current["leg_index"] + 1] if current["leg_index"] + 1 < later[0]["leg_index"] else None
            item = _pick_option(evaluate(later, transfer_walk, alight_eta.predicted_arrival_at),
                                (later[0].get("vehicle") or {}).get("run_key"))
            guidance["arrival"] = arrival_from(item)
    else:
        nxt = rides[0]
        board = nxt.get("board") or {}
        previous = legs[nxt["leg_index"] - 1] if nxt["leg_index"] > 0 else None
        if movement == journey_state.WALKING and progress.get("leg_index", 0) < nxt["leg_index"]:
            reasons.append("access_walk_progress_unknown_full_duration_assumed")
        planned_run = (nxt.get("vehicle") or {}).get("run_key")
        item = _pick_option(evaluate(rides, previous, now), planned_run)
        bucket = etas.by_position.get((nxt["pattern_key"], board.get("seq"), board.get("stop_id")), ())
        upcoming = [_eta_brief(e, now) for e in bucket if e.predicted_arrival_at >= now][:UPCOMING_LIMIT]
        first_leg = item.legs[0] if item is not None and item.legs else None
        first_conn = item.connections[0] if item is not None and item.connections else None
        board_reasons = sorted(etas.rejected_by_stop.get((nxt["pattern_key"], board.get("stop_id")), ()))
        guidance["next_boarding"] = {
            "leg_index": nxt["leg_index"], "route_id": nxt.get("route_internal_id"), "route_no": nxt.get("route_no"),
            "pattern_key": nxt["pattern_key"], "stop_id": board.get("stop_internal_id"), "seq": board.get("seq"),
            "planned_vehicle": nxt.get("vehicle"),
            "vehicle": None if first_leg is None else {"vehicle_id": first_leg.vehicle_id,
                                                       "run_key": first_leg.run_key,
                                                       "matches_planned": bool(planned_run)
                                                       and first_leg.run_key == planned_run},
            "predicted_arrival_at": _iso(first_leg.board_eta.predicted_arrival_at) if first_leg else None,
            "wait_seconds": (max(0.0, (first_leg.board_eta.predicted_arrival_at - now).total_seconds())
                             if first_leg else None),
            "connection_status": first_conn.status if first_conn else None,
            "slack_seconds": first_conn.slack_seconds if first_conn else None,
            "upcoming": upcoming, "eta_rejections": board_reasons,
        }
        if first_leg is None:
            reasons.append("next_boarding_eta_unavailable")
            reasons.extend(board_reasons)
        guidance["arrival"] = arrival_from(item)
    if guidance["arrival"] and guidance["arrival"].get("reason_codes"):
        reasons.extend(guidance["arrival"]["reason_codes"])
    complete = guidance["arrival"] is not None and guidance["arrival"].get("planned_arrival_at") is not None
    guidance["status"] = "available" if complete else "data_limited"
    guidance["reason_codes"] = list(dict.fromkeys(reasons))
    return guidance


# ---------------------------------------------------------------- 예측 스냅샷 (결과 전에 고정)

def evaluation_snapshots(journey_id: UUID, candidate: dict[str, Any], *, selection_version: int,
                         config_version: str | None, fixed_at: datetime) -> list[tuple[str, dict[str, Any]]]:
    """선택 후보의 환승별 예측을 사용자 결과 전에 고정한다. (idempotency_key, payload) 목록.

    현재 guidance에서 사후 복사하지 않고, 선택 시점의 후보 평가(검색 저장값)만 쓴다. 키가 같으면 다시 만들지 않는다.
    """
    evaluation = candidate.get("evaluation") or {}
    legs = candidate.get("legs") or []
    provenance = evaluation.get("provenance") or {}
    by_index = {leg["leg_index"]: leg for leg in legs}
    result = []
    for transfer in evaluation.get("transfers") or []:
        index = transfer.get("transfer_index")
        front, nxt = by_index.get(transfer.get("front_leg_index"), {}), by_index.get(transfer.get("next_leg_index"), {})
        snapshot_key = f"{journey_id}:s{selection_version}:t{index}"
        result.append((f"evaluation_snapshot:s{selection_version}:t{index}", {
            "snapshot_key": snapshot_key, "selection_version": selection_version, "transfer_index": index,
            "candidate_id": str(candidate["id"]), "search_id": str(candidate["search_id"]),
            "static_route_key": candidate["static_route_key"], "option_key": candidate["option_key"],
            "sample_id": transfer.get("sample_id"), "sample_reason": transfer.get("sample_reason"),
            "predicted_probability": transfer.get("probability"), "slack_seconds": transfer.get("slack_seconds"),
            "connection_status": transfer.get("status"), "reason_codes": transfer.get("reason") or [],
            "front_leg_index": transfer.get("front_leg_index"), "next_leg_index": transfer.get("next_leg_index"),
            "front_alight_eta": front.get("alight_eta"), "next_board_eta": nxt.get("board_eta"),
            "front_vehicle": front.get("vehicle"), "next_vehicle": nxt.get("vehicle"),
            "evaluated_at": _iso(candidate["evaluated_at"]) if isinstance(candidate.get("evaluated_at"), datetime)
            else candidate.get("evaluated_at"),
            "data_snapshot_id": provenance.get("data_snapshot_id"),
            "timing_policy_version": provenance.get("timing_policy_version"),
            "sample_policy_version": provenance.get("sample_policy_version"),
            "config_version": config_version, "fixed_at": _iso(fixed_at),
            "journey_probability": None if candidate.get("journey_probability") is None
            else float(candidate["journey_probability"]),
        }))
    return result
