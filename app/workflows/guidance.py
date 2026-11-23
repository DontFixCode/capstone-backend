"""활성 이동 재평가: 선택 경로·초기 검색의 대안 후보를 공유 최신값으로 다시 평가한다.

- 남은 경로와 초기 검색의 대안 후보를 기존 evaluate_candidates로 같은 수집 스냅샷·모드에서 평가하고,
  최소 개선량을 넘는 경우에만 사용자 확인이 필요한 제안을 만든다. 상태 버전 조건부 저장은 호출자가 한다.
- 탑승 전(walking·transferring): 다음 승차 정류소부터 평가한다. 출발 도보는 진행 위치를 모르므로 스냅샷의 전체 도보 시간을
  쓰고 그 가정을 사유로 남긴다. 탑승 중: 차량을 사용자가 확인했고 같은 차량의 하차 ETA가 있을 때만 하차 시각을 확정하고,
  이후 구간은 그 시각부터 평가한다. 탑승 중인 차량의 첫 승차 성공 여부를 다시 평가하지 않는다.
- ETA 기준 시각이 확인되지 않은 TAGO 도착정보는 기존과 같이 eta_basis_unconfirmed로 제외되고 미산출 값은 null과 사유다.
- 위치는 관측 시각 기준 최신성만 판단한다. 오래된 위치·미수신은 제한 사유이며 이동을 멈추거나 취소하지 않는다.
- 목적지가 바뀌면 이전 목적지의 도착 예측을 내보내지 않고 fresh-route-search 제한 사유를 반환한다.
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
from app.modules.journeys import presence as journey_presence
from app.modules.journeys import repository as journey_repository
from app.modules.recommendation.change import RouteOption, decide_change
from app.modules.routing import repository as search_repository
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
    targets = [[RealtimeTarget("arrivals", s, srcs) for s in sorted(arrivals)],
               [RealtimeTarget("positions", r, srcs) for r in sorted(positions)]]
    saved = snapshot.get("alternative_targets") or {}
    targets.extend([[RealtimeTarget(kind, external_id, srcs) for external_id in sorted(set(saved.get(kind) or []))]
                    for kind in ("arrivals", "positions")])
    return transit_service.merge_targets(targets)


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


def _candidate_matches_completed(candidate_legs: list[dict[str, Any]], progress: dict[str, Any]) -> bool:
    """대안의 고정 구간이 사용자가 확인한 탑승·하차 이력과 일치할 때만 비교한다."""
    history = progress.get("history") or []
    completed = progress.get("completed_bus_legs") or []
    rides = journey_state.bus_legs(candidate_legs)
    if len(history) < len(completed) or len(rides) <= len(completed):
        return False
    for position, record in enumerate(history[:len(completed)]):
        ride = rides[position]
        boarded, alighted = record.get("boarded") or {}, record.get("alighted") or {}
        if (ride.get("route_internal_id") != boarded.get("route_internal_id")
                or ride.get("pattern_key") != boarded.get("pattern_key")
                or (ride.get("alight") or {}).get("stop_internal_id") != alighted.get("stop_internal_id")):
            return False
    return True


def _merge_fixed_prefix(selected_legs: list[dict[str, Any]], alternative_legs: list[dict[str, Any]],
                        completed_count: int, *, alternative_has_prefix: bool = True
                        ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """완료된 선택 구간을 보존하고 대안의 남은 구간만 이어 붙인다."""
    selected_rides = journey_state.bus_legs(selected_legs)
    alternative_rides = journey_state.bus_legs(alternative_legs)
    if completed_count and alternative_has_prefix:
        selected_end = selected_rides[completed_count - 1]["leg_index"]
        alternative_end = alternative_rides[completed_count - 1]["leg_index"]
        prefix = [dict(leg) for leg in selected_legs[:selected_end + 1]]
        tail = [dict(leg) for leg in alternative_legs[alternative_end + 1:]]
    else:
        prefix, tail = [], [dict(leg) for leg in alternative_legs]
        if completed_count:
            selected_rides = journey_state.bus_legs(selected_legs)
            selected_end = selected_rides[completed_count - 1]["leg_index"]
            prefix = [dict(leg) for leg in selected_legs[:selected_end + 1]]
    merged = prefix + tail
    bus_indices = []
    ride_index = 0
    for index, leg in enumerate(merged):
        leg["leg_index"] = index
        if leg.get("kind") == "bus":
            leg["ride_index"] = ride_index
            ride_index += 1
            bus_indices.append(index)
    progress = {"completed_bus_legs": bus_indices[:completed_count], "boarded": None,
                "next_bus_leg_index": bus_indices[completed_count] if completed_count < len(bus_indices) else None,
                "leg_index": (bus_indices[completed_count - 1] + 1 if completed_count else 0)}
    return merged, progress


def _route_signature(legs: list[dict[str, Any]]) -> tuple[tuple[Any, ...], ...]:
    return tuple((leg.get("route_id"), leg.get("pattern_key"),
                  (leg.get("board") or {}).get("seq"), (leg.get("alight") or {}).get("seq"),
                  (leg.get("board") or {}).get("stop_id"), (leg.get("alight") or {}).get("stop_id"))
                 for leg in journey_state.bus_legs(legs))


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
    original_snapshot, original_legs, original_progress = snapshot, legs, progress
    destination_reroute = bool(snapshot.get("destination_changed_at"))
    reroute_candidates: list[dict[str, Any]] = []
    reroute_search: dict[str, Any] | None = None
    reroute_leg_map: dict[int, int] = {}
    reasons: list[str] = []
    guidance: dict[str, Any] = {
        "base_state_version": journey["state_version"], "selection_version": journey["selection_version"],
        "evaluated_at": _iso(now), "config_version": config.config_version,
        "eta_basis_policy": config.eta_basis_policy_version,
        "selection_config_version": snapshot.get("config_version"),
        "ranking_mode": snapshot.get("ranking_mode") or "nominal",
        "min_improvement_seconds": policy.min_improvement_seconds,
        "movement_state": movement, "current_leg_index": progress.get("leg_index"),
        "next_boarding": None, "onboard": None, "arrival": None, "alternatives": [],
        "alternatives_reason": "not_evaluated",
        "boarding_estimate": {"status": "insufficient_evidence", "reason_codes": ["not_evaluated"],
                              "suggestion": None, "evaluated_at": _iso(now)},
    }
    loc_max_age = settings.journey_location_max_age_seconds or policy.max_data_age_seconds
    guidance["location"] = location_status(journey, now, loc_max_age)
    if guidance["location"]["status"] == "not_received":
        reasons.append("location_not_received")
    elif guidance["location"]["status"] == "stale":
        reasons.append("location_stale")

    if destination_reroute:
        reroute_id = snapshot.get("reroute_search_id")
        if not reroute_id:
            reason = snapshot.get("reroute_reason") or "destination_reroute_not_queued"
            guidance.update(status="data_limited", arrival=None, alternatives=[], alternatives_reason=reason,
                            reason_codes=list(dict.fromkeys([*reasons, reason])), data_as_of=None, data_inputs=[])
            return guidance
        with db.transaction(isolation="REPEATABLE READ") as conn:
            reroute_search = search_repository.read_search(conn, UUID(str(reroute_id)))
            reroute_candidates = search_repository.read_candidates(conn, UUID(str(reroute_id)))
        if reroute_search is None or reroute_search["status"] == "processing":
            guidance.update(status="data_limited", arrival=None, alternatives=[],
                            alternatives_reason="destination_reroute_pending",
                            reason_codes=list(dict.fromkeys([*reasons, "destination_reroute_pending"])),
                            reroute_search_id=str(reroute_id), data_as_of=None, data_inputs=[])
            return guidance
        if reroute_search["status"] != "ready" or not reroute_candidates:
            reason = (reroute_search.get("summary") or {}).get("status_reason") or "destination_reroute_no_candidates"
            guidance.update(status="data_limited", arrival=None, alternatives=[], alternatives_reason=reason,
                            reason_codes=list(dict.fromkeys([*reasons, reason])),
                            reroute_search_id=str(reroute_id), data_as_of=None, data_inputs=[])
            return guidance
        old_remaining = remaining_bus_legs(original_legs, original_progress, movement)
        old_signature = tuple((leg.get("route_id"), leg.get("pattern_key"),
                               (leg.get("board") or {}).get("seq"), (leg.get("alight") or {}).get("seq"),
                               (leg.get("board") or {}).get("stop_id"), (leg.get("alight") or {}).get("stop_id"))
                              for leg in old_remaining)
        baseline = next((row for row in reroute_candidates if _route_signature(row.get("legs") or [])
                         == old_signature), None)
        if baseline is None:
            guidance.update(status="data_limited", arrival=None, alternatives=[],
                            alternatives_reason="current_route_not_comparable_for_destination",
                            reason_codes=list(dict.fromkeys([*reasons,
                                                             "current_route_not_comparable_for_destination"])),
                            reroute_search_id=str(reroute_id), data_as_of=None, data_inputs=[])
            return guidance
        search_summary = reroute_search.get("summary") or {}
        old_targets = snapshot.get("alternative_targets") or {}
        new_targets = search_summary.get("realtime_targets") or {}
        snapshot = {**snapshot, "legs": baseline["legs"], "candidate_id": str(baseline["id"]),
                    "search_id": str(reroute_id), "static_route_key": baseline["static_route_key"],
                    "ranking_mode": baseline.get("ranking_mode") or reroute_search.get("ranking_mode") or "nominal",
                    "origin": {"lat": reroute_search["origin_lat"], "lon": reroute_search["origin_lon"]},
                    "alternative_targets": {kind: sorted(set((old_targets.get(kind) or [])
                                                               + (new_targets.get(kind) or [])))
                                             for kind in ("arrivals", "positions")}}
        legs = snapshot["legs"]
        new_bus = journey_state.bus_legs(legs)
        for old_leg, new_leg in zip(old_remaining, new_bus):
            reroute_leg_map[new_leg["leg_index"]] = old_leg["leg_index"]
        # 재탐색 입력은 현재 위치에서 시작하므로 완료 구간은 안내 평가 입력에서 제외한다.
        progress = journey_state.initial_progress(legs)
        movement = journey_state.WALKING if legs and legs[0].get("kind") == "walk" else journey_state.WAITING

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
        patterns = {leg["pattern_key"] for leg in rides}
        if destination_reroute:
            patterns.update(leg["pattern_key"] for row in reroute_candidates
                            for leg in journey_state.bus_legs(row.get("legs") or []))
        segments = search_flow.segment_observations(conn, sorted(patterns), cohort, until=now)
        error_samples = sample_rules.transfer_error_samples(
            evaluation_repository.labeled_samples(conn, cohort, before=now))
        scenes = sample_rules.joint_scenes(evaluation_repository.complete_scenes(conn, cohort, before=now))
        if movement != journey_state.ONBOARD:
            route_ids = sorted({str(leg["route_id"]) for leg in rides if leg.get("route_id") is not None}
                               | set((snapshot.get("alternative_targets") or {}).get("positions") or []))
            gps_history, vehicle_history = journey_repository.recent_presence_samples(
                conn, journey["id"], route_ids, since=now - timedelta(minutes=5))
        else:
            gps_history, vehicle_history = [], []
    if movement != journey_state.ONBOARD:
        estimate = journey_presence.estimate_boarding(
            gps_history, vehicle_history, now=now, max_gps_age_seconds=loc_max_age)
        if estimate.get("suggestion") is not None and rides:
            estimate["suggestion"]["leg_index"] = rides[0]["leg_index"]
        estimate["evaluated_at"] = _iso(now)
        guidance["boarding_estimate"] = estimate
    else:
        guidance["boarding_estimate"] = {"status": "not_applicable", "reason_codes": ["already_confirmed_onboard"],
                                         "suggestion": None, "evaluated_at": _iso(now)}
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
    # 놓친 차량/회차를 다음 탑승 후보에서 제거한다. 사용자가 차량을 특정하지 않은 경우
    # 선택 경로에 기록된 계획 회차(run_key)만 제외하고, 다른 차량의 실시간 ETA는 유지한다.
    missed = progress.get("missed_vehicles") or []
    excluded_runs = {(item.get("pattern_key"), item.get("run_key")) for item in missed if item.get("run_key")}
    excluded_vehicles = {(item.get("pattern_key"), item.get("vehicle_id")) for item in missed
                         if item.get("vehicle_id")}
    eta_reports = tuple(report for report in realtime.eta_reports
                        if (report.pattern_id, report.run_key) not in excluded_runs
                        and (report.pattern_id, report.vehicle_id) not in excluded_vehicles)
    etas = index_etas(eta_reports, evaluated_at=now, max_data_age_seconds=policy.max_data_age_seconds)
    data_snapshot_id = hashlib.sha256(json.dumps([static_version, sorted(realtime.batch_refs, key=str)],
                                                 default=str).encode()).hexdigest()[:32]
    guidance["data_snapshot_id"] = data_snapshot_id

    def evaluate(remaining: list[dict[str, Any]], first_walk: dict | None, start_at: datetime,
                 route_snapshot: dict[str, Any] = snapshot):
        candidate, walks = remaining_candidate(route_snapshot, remaining, first_walk_leg=first_walk,
                                               static_version=static_version)
        origin = (remaining[0].get("board") or {})
        context = SearchContext(
            origin=Coordinate(lat=origin.get("lat") or snapshot["origin"]["lat"],
                              lon=origin.get("lon") or snapshot["origin"]["lon"]),
            destination=Coordinate(lat=route_snapshot["destination"]["lat"], lon=route_snapshot["destination"]["lon"]),
            start_at=start_at, evaluated_at=now, static_snapshot_id=static_version)
        inputs = EvaluationInputs(
            data_snapshot_id=data_snapshot_id, sample_condition_key=cohort, walks=tuple(walks),
            eta_reports=eta_reports, queried_stop_ids=realtime.queried_stop_ids,
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
        guidance["alternatives_reason"] = "confirmed_onboard_segment_fixed_until_alight"
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
        if destination_reroute:
            guidance["next_boarding"]["leg_index"] = reroute_leg_map.get(nxt["leg_index"], nxt["leg_index"])
        if first_leg is None:
            reasons.append("next_boarding_eta_unavailable")
            reasons.extend(board_reasons)
        guidance["arrival"] = arrival_from(item)
        # 같은 최초 검색에 저장된 후보들을 현재 활성 수집 자료와 현재 시각으로 다시 평가한다.
        if not progress.get("boarded"):
            original = snapshot.get("search_id")
            if original and item is not None:
                if destination_reroute:
                    alternatives, original_search = reroute_candidates, reroute_search
                else:
                    with db.transaction(isolation="REPEATABLE READ") as conn:
                        alternatives = search_repository.read_candidates(conn, UUID(str(original)))
                        original_search = search_repository.read_search(conn, UUID(str(original)))
                coverage = ((original_search or {}).get("summary") or {}).get("search_coverage", {})
                coverage_complete = not coverage.get("truncated", True)
                mode = snapshot.get("ranking_mode") or "nominal"
                comparison_key = f"{static_version}:{data_snapshot_id}:{now.isoformat()}:{mode}"
                current_cost = (item.expected_total_seconds if mode == "expected" else item.nominal_total_seconds)
                current_option = RouteOption(str(snapshot.get("candidate_id")), mode, current_cost,
                                             comparison_key, bool(item.nominally_feasible), coverage_complete)
                option_rows: dict[str, dict[str, Any]] = {}
                option_evaluations: dict[str, Any] = {}
                evaluated_options: list[RouteOption] = []
                for row in alternatives:
                    candidate_id = str(row["id"])
                    if candidate_id == str(snapshot.get("candidate_id")):
                        continue
                    if (original_progress.get("rejected_candidate_state_version") == journey["state_version"]
                            and candidate_id in (original_progress.get("rejected_candidate_ids") or [])):
                        continue
                    alt_snapshot = {**snapshot, "legs": row["legs"], "static_route_key": row["static_route_key"]}
                    alt_all_rides = journey_state.bus_legs(row["legs"] or [])
                    completed_count = len(progress.get("completed_bus_legs") or [])
                    if completed_count and not destination_reroute \
                            and not _candidate_matches_completed(row["legs"] or [], original_progress):
                        continue
                    alt_rides = alt_all_rides[completed_count:]
                    if not alt_rides:
                        continue
                    first = alt_rides[0]
                    first_walk = row["legs"][first["leg_index"] - 1] if first["leg_index"] > 0 else None
                    alt_item = _pick_option(evaluate(alt_rides, first_walk, now, alt_snapshot),
                                            (first.get("vehicle") or {}).get("run_key"))
                    if alt_item is None:
                        continue
                    cost = alt_item.expected_total_seconds if mode == "expected" else alt_item.nominal_total_seconds
                    evaluated_options.append(RouteOption(candidate_id, mode, cost, comparison_key,
                                                         bool(alt_item.nominally_feasible), coverage_complete,
                                                         tuple(alt_item.reason_codes)))
                    option_rows[candidate_id] = row
                    option_evaluations[candidate_id] = alt_item
                decision = decide_change(current_option, evaluated_options,
                                         min_improvement_seconds=policy.min_improvement_seconds)
                guidance["alternatives"] = [{"candidate_id": option.candidate_id,
                                              "cost_seconds": option.cost_seconds,
                                              "improvement_seconds": (current_cost - option.cost_seconds
                                                                      if current_cost is not None and
                                                                      option.cost_seconds is not None else None),
                                              "reason_codes": list(option.reason_codes)}
                                             for option in evaluated_options]
                guidance["alternatives_reason"] = decision.reason_codes[0]
                if decision.status == "propose" and decision.proposed_candidate_id:
                    chosen = option_rows[decision.proposed_candidate_id]
                    chosen_eval = option_evaluations[decision.proposed_candidate_id]
                    merged_completed_count = len(original_progress.get("completed_bus_legs") or [])
                    fresh_legs, accepted_progress = _merge_fixed_prefix(
                        original_legs, chosen["legs"], merged_completed_count,
                        alternative_has_prefix=not destination_reroute)
                    completed_count = merged_completed_count
                    ride_index = 0
                    for leg in fresh_legs:
                        if leg.get("kind") != "bus":
                            continue
                        if ride_index < completed_count:
                            ride_index += 1
                            continue
                        timed_index = ride_index - completed_count
                        if timed_index >= len(chosen_eval.legs):
                            break
                        timed_leg = chosen_eval.legs[timed_index]
                        leg["vehicle"] = {"vehicle_id": timed_leg.vehicle_id, "run_key": timed_leg.run_key,
                                          "vehicle_rank": timed_leg.vehicle_rank,
                                          "match_status": "confirmed" if timed_leg.run_key else "unconfirmed"}
                        leg["board_eta"] = search_flow._jsonable(timed_leg.board_eta)
                        leg["alight_eta"] = search_flow._jsonable(timed_leg.alight_eta) if timed_leg.alight_eta else None
                        leg["ride_seconds"] = timed_leg.ride_seconds
                        ride_index += 1
                    fresh_evaluation = dict(chosen["evaluation"] or {})
                    merged_bus_indices = [leg["leg_index"] for leg in fresh_legs if leg.get("kind") == "bus"]
                    fresh_transfers = []
                    for transfer_index in range(1, len(chosen_eval.legs)):
                        connection = (chosen_eval.connections[transfer_index]
                                      if transfer_index < len(chosen_eval.connections) else None)
                        front, following = chosen_eval.legs[transfer_index - 1:transfer_index + 1]
                        fresh_transfers.append({"transfer_index": transfer_index,
                                                "front_leg_index": merged_bus_indices[completed_count + transfer_index - 1],
                                                "next_leg_index": merged_bus_indices[completed_count + transfer_index],
                                                "slack_seconds": connection.slack_seconds if connection else None,
                                                "status": connection.status if connection else None,
                                                "probability": chosen_eval.transfer_probabilities[transfer_index - 1]
                                                if transfer_index - 1 < len(chosen_eval.transfer_probabilities) else None,
                                                "reason": list(connection.reason_codes) if connection else [],
                                                "sample_id": None, "sample_reason": "reevaluated_during_journey",
                                                "front_alight_eta": search_flow._jsonable(front.alight_eta)
                                                if front.alight_eta else None,
                                                "next_board_eta": search_flow._jsonable(following.board_eta),
                                                "front_vehicle": {"vehicle_id": front.vehicle_id, "run_key": front.run_key},
                                                "next_vehicle": {"vehicle_id": following.vehicle_id,
                                                                  "run_key": following.run_key}})
                    fresh_evaluation["transfers"] = fresh_transfers
                    fresh_evaluation["connections"] = search_flow._jsonable(chosen_eval.connections)
                    fresh_evaluation["first_boarding"] = (search_flow._jsonable(chosen_eval.connections[0])
                                                           if chosen_eval.connections else None)
                    fresh_evaluation["first_boarding_probability"] = chosen_eval.first_boarding_probability
                    fresh_evaluation["transfer_probabilities"] = list(chosen_eval.transfer_probabilities)
                    fresh_evaluation["journey_probability"] = chosen_eval.journey_probability
                    fresh_evaluation["expected_total_seconds"] = chosen_eval.expected_total_seconds
                    fresh_evaluation["connection_status"] = chosen_eval.connection_status
                    fresh_evaluation["limited_at_ride_index"] = chosen_eval.limited_at_ride_index
                    fresh_evaluation["reason_codes"] = list(chosen_eval.reason_codes)
                    fresh_evaluation["provenance"] = search_flow._jsonable(chosen_eval.provenance)
                    fresh_evaluation["times"] = {"walk_seconds": chosen_eval.walk_seconds,
                                                  "buffer_seconds": chosen_eval.buffer_seconds,
                                                  "wait_seconds": chosen_eval.wait_seconds,
                                                  "ride_seconds": chosen_eval.ride_seconds,
                                                  "egress_walk_seconds": chosen_eval.egress_walk_seconds,
                                                  "planned_arrival_at": search_flow._jsonable(
                                                      chosen_eval.planned_arrival_at)}
                    proposal_base = {**original_snapshot,
                                     **({"search_id": snapshot.get("search_id"),
                                         "static_version": static_version,
                                         "config_version": reroute_search.get("config_version") if reroute_search else None,
                                         "alternative_targets": snapshot.get("alternative_targets")}
                                        if destination_reroute else {})}
                    proposal = {**proposal_base, "candidate_id": decision.proposed_candidate_id,
                                "static_route_key": chosen["static_route_key"], "option_key": chosen["option_key"],
                                "legs": fresh_legs, "evaluation": fresh_evaluation,
                                "boarding_count": len(journey_state.bus_legs(fresh_legs)),
                                "walking_seconds": chosen_eval.walk_seconds,
                                "nominal_total_seconds": chosen_eval.nominal_total_seconds,
                                "expected_total_seconds": chosen_eval.expected_total_seconds,
                                "journey_probability": chosen_eval.journey_probability,
                                "time_status": chosen_eval.time_status,
                                "nominally_feasible": chosen_eval.nominally_feasible,
                                "ranking_mode": mode, "rank_no": chosen["rank_no"],
                                "evaluated_at": search_flow._jsonable(chosen_eval.provenance.evaluated_at),
                                "base_state_version": journey["state_version"],
                                "proposal_version": journey["selection_version"],
                                "accepted_progress": {**original_progress, **accepted_progress,
                                              "history": original_progress.get("history", []),
                                              "missed_vehicles": [],
                                              "last_alight": original_progress.get("last_alight")},
                                "change_decision": {"improvement_seconds": decision.improvement_seconds,
                                                    "reason_codes": list(decision.reason_codes)}}
                    guidance["proposal_snapshot"] = search_flow._jsonable(proposal)
            else:
                guidance["alternatives_reason"] = "current_route_not_evaluated"
    if guidance["arrival"] and guidance["arrival"].get("reason_codes"):
        reasons.extend(guidance["arrival"]["reason_codes"])
    complete = guidance["arrival"] is not None and guidance["arrival"].get("planned_arrival_at") is not None
    guidance["status"] = "available" if complete else "data_limited"
    guidance["reason_codes"] = list(dict.fromkeys(reasons))
    return guidance


# ---------------------------------------------------------------- 예측 스냅샷 (결과 전에 고정)

def _planned_route(leg: dict[str, Any]) -> dict[str, Any] | None:
    if not leg:
        return None
    return {"route_internal_id": leg.get("route_internal_id"), "route_external_id": leg.get("route_id"),
            "route_no": leg.get("route_no"), "pattern_key": leg.get("pattern_key")}


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
            # 실제 이용 연결과 비교할 계획 노선(검색·후보가 정리된 뒤에도 남도록 고정 시점에 복사).
            "front_route": _planned_route(front), "next_route": _planned_route(nxt),
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
