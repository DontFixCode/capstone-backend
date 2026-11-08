"""최초 검색 흐름: 정적 후보 생성 → 도보(TMAP) 조회 → 실시간 공유 수집 → 평가 → 추천 → 저장.

기존 핵심 함수(routing.build_search_index·generate_candidates, evaluation.evaluate_candidates,
recommendation.recommend_candidates)를 그대로 호출하고, 이 모듈은 입력 준비·결과 저장을 잇는 최소 어댑터만 둔다.

- 한 검색은 REPEATABLE READ 한 번으로 읽은 하나의 정적 공개본만 사용한다. DFS 중에는 외부 API를 호출하지 않는다.
- 도보는 정적 후보 생성 뒤, 같은 방향·좌표·옵션의 구간을 한 번만 TMAP으로 조회한다. 같은 정류소 환승은 조회하지 않는다.
  경로 없음·조회 실패·시간 미확인(미조회)을 구분하고 미확인 도보시간을 0으로 채우지 않는다.
- 실시간 대상(승하차 정류소 도착·노선 위치)은 검색 행(summary.realtime_targets, expires_at)에 등록해 워커가 공유 수집하고,
  검색 시점에는 같은 Phase 2 수집기(RealtimeCollector)로 간격 이내 공개값은 재사용·부족한 대상만 조회한다.
- TAGO 도착정보는 기준시각이 확인되지 않아(eta_basis_at=NULL) 기본 정책(unconfirmed)에서는 그대로 전달한다
  (2026-11-07 결정). 수신 시각 근사(BACKEND_TAGO_ETA_BASIS_POLICY=server_received_at)는 Notion DB-C04에 따라 검증한
  뒤에만 켜며, 켜면 기준 출처·정책 버전이 결과와 표본 조건 키에 함께 남는다(app.modules.transit.eta_basis).
- 결과(검색 상태·비교 모드·후보 전체·표본/묶음)는 한 트랜잭션에서 확정한다. processing인 검색만 확정한다.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.db.session import Database
from app.integrations import route_shapes_api as route_shapes
from app.integrations.bus_api import TagoClient
from app.integrations.walking_api import PROVIDER as WALK_PROVIDER, TmapWalkingClient, WalkQuery, WalkResponse
from app.modules.evaluation import repository as evaluation_repository
from app.modules.evaluation import samples as sample_rules
from app.modules.evaluation.schemas import (
    DESTINATION_REF,
    ORIGIN_REF,
    EtaReport,
    EvaluatedCandidate,
    EvaluationInputs,
    EvaluationPolicy,
    EvaluationResult,
    ObservedEvent,
    SegmentObservation,
    WalkResult,
)
from app.modules.evaluation.service import evaluate_candidates
from app.modules.recommendation.schemas import RecommendationResult
from app.modules.recommendation.service import recommend_candidates
from app.modules.routing import repository as search_repository
from app.modules.routing import service_area
from app.modules.routing.schemas import (
    CandidateSearchResult,
    Coordinate,
    PatternStop,
    RoutePattern,
    SearchContext,
    SearchPolicy,
    StaticCandidate,
    StaticSnapshot,
    StopRecord,
)
from app.modules.routing.service import build_search_index, generate_candidates
from app.modules.transit import eta_basis
from app.modules.transit import repository as transit_repository
from app.modules.transit import service as transit_service
from app.modules.transit.schemas import LatestSnapshot, RealtimeTarget, StaticNetwork

logger = logging.getLogger(__name__)

SOURCE_CLI = "cli"
SOURCE_APP = "app"
# 기본 정책의 버전(기존 결과와 같은 값). 실제 적용 정책은 SearchConfig.eta_basis_policy·eta_basis.policy_version을 따른다.
ETA_BASIS_POLICY = eta_basis.policy_version(eta_basis.UNCONFIRMED)
GEOMETRY_STATUS = "unavailable"
GEOMETRY_REASON = route_shapes.NOT_CONFIGURED_REASON
WALK_STATUSES = ("available", "no_path", "error", "unknown", "same_stop")


class SearchConfigError(RuntimeError):
    """검색 실행 설정이 없거나 범위를 벗어났다. 메시지에는 설정 이름만 담는다."""


class SearchError(RuntimeError):
    """검색을 시작할 수 없다(정적 공개본 없음 등)."""


# ---------------------------------------------------------------- 설정

_REQUIRED = {
    "search_max_boardings": "BACKEND_SEARCH_MAX_BOARDINGS",
    "search_access_radius_meters": "BACKEND_SEARCH_ACCESS_RADIUS_METERS",
    "search_transfer_radius_meters": "BACKEND_SEARCH_TRANSFER_RADIUS_METERS",
    "search_egress_radius_meters": "BACKEND_SEARCH_EGRESS_RADIUS_METERS",
    "search_max_candidates": "BACKEND_SEARCH_MAX_CANDIDATES",
    "search_max_states": "BACKEND_SEARCH_MAX_STATES",
    "search_budget_ms": "BACKEND_SEARCH_BUDGET_MS",
    "eval_search_horizon_seconds": "BACKEND_EVAL_SEARCH_HORIZON_SECONDS",
    "eval_budget_ms": "BACKEND_EVAL_BUDGET_MS",
    "eval_max_timed_candidates": "BACKEND_EVAL_MAX_TIMED_CANDIDATES",
    "eval_boarding_buffer_seconds": "BACKEND_EVAL_BOARDING_BUFFER_SECONDS",
    "eval_max_data_age_seconds": "BACKEND_EVAL_MAX_DATA_AGE_SECONDS",
    "eval_min_segment_samples": "BACKEND_EVAL_MIN_SEGMENT_SAMPLES",
    "eval_min_probability_samples": "BACKEND_EVAL_MIN_PROBABILITY_SAMPLES",
    "eval_min_joint_scenes": "BACKEND_EVAL_MIN_JOINT_SCENES",
    "eval_min_improvement_seconds": "BACKEND_EVAL_MIN_IMPROVEMENT_SECONDS",
    "eval_max_horizon_difference_seconds": "BACKEND_EVAL_MAX_HORIZON_DIFFERENCE_SECONDS",
    "eval_max_recovery_attempts": "BACKEND_EVAL_MAX_RECOVERY_ATTEMPTS",
    "eval_recovery_search_enabled": "BACKEND_EVAL_RECOVERY_SEARCH_ENABLED",
    "eval_timing_policy_version": "BACKEND_EVAL_TIMING_POLICY_VERSION",
    "eval_sample_policy_version": "BACKEND_EVAL_SAMPLE_POLICY_VERSION",
    "eval_recovery_policy_version": "BACKEND_EVAL_RECOVERY_POLICY_VERSION",
    "eval_segment_model_version": "BACKEND_EVAL_SEGMENT_MODEL_VERSION",
    "search_expires_seconds": "BACKEND_SEARCH_EXPIRES_SECONDS",
    "tmap_search_option": "BACKEND_TMAP_SEARCH_OPTION",
    "tmap_max_lookups_per_search": "BACKEND_TMAP_MAX_LOOKUPS_PER_SEARCH",
    "sample_label_deadline_seconds": "BACKEND_SAMPLE_LABEL_DEADLINE_SECONDS",
}


@dataclass(frozen=True)
class SearchConfig:
    search_policy: SearchPolicy
    evaluation_policy: EvaluationPolicy
    expires_seconds: float
    tmap_search_option: str
    tmap_max_lookups: int
    label_deadline_seconds: float
    realtime_interval_seconds: float
    params: dict[str, Any]
    config_version: str
    # 앱 검색 지원 범위 판정에 쓰는 행정경계 GeoJSON 경로(None이면 저장소 기본 파일). CLI 검색은 쓰지 않는다.
    service_area_path: str | None = None
    # TAGO 도착정보 ETA 기준 시각 정책(app.modules.transit.eta_basis). 기본은 기준 미확인(절대 ETA 미산출).
    eta_basis_policy: str = eta_basis.UNCONFIRMED

    @property
    def eta_basis_policy_version(self) -> str:
        return eta_basis.policy_version(self.eta_basis_policy)


def load_search_config(settings: Settings) -> SearchConfig:
    """검색 설정을 읽는다. 하나라도 비어 있으면 누락된 키 이름을 모두 알려준다(근거 없는 기본값을 쓰지 않는다)."""
    missing = [env for name, env in _REQUIRED.items() if getattr(settings, name) is None]
    if missing:
        raise SearchConfigError("검색 설정이 없습니다(.env.api): " + ", ".join(missing))
    s = settings
    try:
        search_policy = SearchPolicy(
            max_boardings=s.search_max_boardings, access_radius_meters=s.search_access_radius_meters,
            transfer_radius_meters=s.search_transfer_radius_meters, egress_radius_meters=s.search_egress_radius_meters,
            max_candidates=s.search_max_candidates, max_search_states=s.search_max_states,
            search_budget_ms=s.search_budget_ms,
        )
        policy = EvaluationPolicy(
            **search_policy.model_dump(),
            search_horizon_seconds=s.eval_search_horizon_seconds, evaluation_budget_ms=s.eval_budget_ms,
            max_timed_candidates=s.eval_max_timed_candidates, boarding_buffer_seconds=s.eval_boarding_buffer_seconds,
            max_data_age_seconds=s.eval_max_data_age_seconds, min_segment_samples=s.eval_min_segment_samples,
            min_probability_samples=s.eval_min_probability_samples, min_joint_scenes=s.eval_min_joint_scenes,
            min_improvement_seconds=s.eval_min_improvement_seconds,
            max_horizon_difference_seconds=s.eval_max_horizon_difference_seconds,
            max_recovery_attempts=s.eval_max_recovery_attempts,
            recovery_search=search_policy if s.eval_recovery_search_enabled else None,
            timing_policy_version=s.eval_timing_policy_version, sample_policy_version=s.eval_sample_policy_version,
            recovery_policy_version=s.eval_recovery_policy_version,
            segment_model_version=s.eval_segment_model_version,
        )
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in error["loc"]) for error in exc.errors()})
        raise SearchConfigError("검색 설정 값이 허용 범위를 벗어났습니다: " + ", ".join(fields)) from None
    for name in ("search_expires_seconds", "sample_label_deadline_seconds", "tmap_max_lookups_per_search"):
        if getattr(s, name) <= 0:
            raise SearchConfigError(f"{_REQUIRED[name]}은 0보다 커야 합니다.")
    params = {
        "search_policy": search_policy.model_dump(),
        "evaluation_policy": policy.model_dump(),
        "search_expires_seconds": s.search_expires_seconds,
        "tmap": {"provider": WALK_PROVIDER, "searchOption": s.tmap_search_option,
                 "max_lookups_per_search": s.tmap_max_lookups_per_search},
        "realtime": {"collection_interval_seconds": s.realtime_interval_seconds,
                     "eta_basis_policy": eta_basis.policy_version(s.tago_eta_basis_policy)},
        "sample_label_deadline_seconds": s.sample_label_deadline_seconds,
        "bias": {"user_seconds": 0.0, "equilibrium_seconds": 0.0},
    }
    version = "cfg-" + hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return SearchConfig(search_policy, policy, s.search_expires_seconds, s.tmap_search_option,
                        s.tmap_max_lookups_per_search, s.sample_label_deadline_seconds,
                        s.realtime_interval_seconds, params, version, s.service_area_geojson,
                        s.tago_eta_basis_policy)


# ---------------------------------------------------------------- 입력·결과 형식

@dataclass(frozen=True)
class PlaceInput:
    """선택한 위치. 장소 검색 제공처 연동 전에는 좌표만 필수이며 이름·주소·제공기관·외부 ID는 받은 그대로 보존한다."""

    lat: float
    lon: float
    name: str | None = None
    address: str | None = None
    provider: str | None = None
    external_id: str | None = None

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "address": self.address, "provider": self.provider,
                "external_id": self.external_id, "lat": self.lat, "lon": self.lon}


@dataclass(frozen=True)
class SearchRequest:
    origin: PlaceInput
    destination: PlaceInput
    start_at: datetime | None = None
    source: str = SOURCE_CLI


@dataclass
class WalkLookup:
    """방향 있는 도보 구간 하나의 조회 결과(후보 legs에 그대로 복사한다)."""

    from_ref: str
    to_ref: str
    status: str
    from_point: dict[str, Any]
    to_point: dict[str, Any]
    query_options: dict[str, str] = field(default_factory=dict)
    distance_m: float | None = None
    duration_seconds: float | None = None
    geometry: dict[str, Any] | None = None
    fetched_at: datetime | None = None
    reason: str | None = None
    provider_code: str | None = None
    provider_message: str | None = None
    provider: str | None = WALK_PROVIDER

    def as_walk_result(self) -> WalkResult | None:
        """평가 입력. 미조회(unknown)는 결과를 주지 않아 walk_result_missing(시간 미확인)이 된다."""
        if self.status == "available":
            return WalkResult(from_ref=self.from_ref, to_ref=self.to_ref, status="available",
                              duration_seconds=self.duration_seconds, distance_meters=self.distance_m)
        if self.status == "no_path":
            return WalkResult(from_ref=self.from_ref, to_ref=self.to_ref, status="no_route")
        if self.status == "error":
            return WalkResult(from_ref=self.from_ref, to_ref=self.to_ref, status="lookup_failed")
        return None


@dataclass
class SearchOutcome:
    search_id: UUID | None
    status: str
    ranking_mode: str
    static_version: str
    candidates: CandidateSearchResult
    evaluation: EvaluationResult | None
    recommendation: RecommendationResult | None
    summary: dict[str, Any]
    saved_candidates: int = 0


# ---------------------------------------------------------------- 정적 스냅샷 어댑터

def routing_snapshot(network: StaticNetwork) -> StaticSnapshot:
    """Phase 2 정적 공개본을 기존 routing 입력 형식으로 옮긴다. 방향은 행별 원값이라 패턴 direction은 None이다."""
    return StaticSnapshot(
        snapshot_id=str(network.data_version),
        stops=tuple(StopRecord(stop_id=s.external_id, name=s.name, lat=s.lat, lon=s.lon) for s in network.stops),
        patterns=tuple(
            RoutePattern(pattern_id=p.pattern_key, route_id=p.route_external_id, direction=None,
                         stops=tuple(PatternStop(stop_id=s.stop_external_id, stop_order=s.seq) for s in p.stops))
            for p in network.patterns
        ),
    )


def required_targets(candidates: Iterable[StaticCandidate]) -> list[RealtimeTarget]:
    """후보 전체의 승하차 정류소 도착·노선 위치 합집합."""
    source = frozenset({"route_search"})
    arrivals, positions = set(), set()
    for candidate in candidates:
        for ride in candidate.rides:
            arrivals.update((ride.board_stop_id, ride.alight_stop_id))
            positions.add(ride.route_id)
    return transit_service.merge_targets([
        [RealtimeTarget("arrivals", stop, source) for stop in sorted(arrivals)],
        [RealtimeTarget("positions", route, source) for route in sorted(positions)],
    ])


def targets_summary(targets: Iterable[RealtimeTarget]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"arrivals": [], "positions": []}
    for target in targets:
        result[target.kind].append(target.external_id)
    return {kind: sorted(ids) for kind, ids in result.items()}


# ---------------------------------------------------------------- 도보

def walk_pairs(candidates: Iterable[StaticCandidate]) -> Counter[tuple[str, str]]:
    """후보가 쓰는 방향 있는 도보 구간과 사용 횟수. 같은 정류소 환승은 넣지 않는다."""
    pairs: Counter[tuple[str, str]] = Counter()
    for candidate in candidates:
        pairs[(ORIGIN_REF, candidate.access_stop_id)] += 1
        for transfer in candidate.transfers:
            if transfer.from_stop_id != transfer.to_stop_id:
                pairs[(transfer.from_stop_id, transfer.to_stop_id)] += 1
        pairs[(candidate.egress_stop_id, DESTINATION_REF)] += 1
    return pairs


def lookup_walks(
    pairs: Counter[tuple[str, str]],
    points: dict[str, dict[str, Any]],
    client: TmapWalkingClient | None,
    *,
    search_option: str,
    max_lookups: int,
) -> tuple[dict[tuple[str, str], WalkLookup], dict[str, int]]:
    """구간을 사용 횟수 순으로 조회한다. 같은 좌표 쌍·옵션은 한 번만 호출하고 한도를 넘은 구간은 미조회로 남긴다."""
    results: dict[tuple[str, str], WalkLookup] = {}
    by_coords: dict[tuple, WalkResponse] = {}
    stats: Counter[str] = Counter()
    for (from_ref, to_ref), _ in sorted(pairs.items(), key=lambda item: (-item[1], item[0])):
        src, dst = points.get(from_ref, {"ref": from_ref}), points.get(to_ref, {"ref": to_ref})
        lookup = WalkLookup(from_ref, to_ref, "unknown", src, dst)
        if src.get("lat") is None or dst.get("lat") is None:
            lookup.reason = "coordinate_unavailable"
            results[(from_ref, to_ref)] = lookup
            stats["unknown_coordinate"] += 1
            continue
        query = WalkQuery(src["lat"], src["lon"], dst["lat"], dst["lon"], search_option)
        lookup.query_options = query.options()
        coord_key = (round(query.from_lat, 7), round(query.from_lon, 7), round(query.to_lat, 7),
                     round(query.to_lon, 7), search_option)
        response = by_coords.get(coord_key)
        if response is None:
            if client is None or len(by_coords) >= max_lookups:
                lookup.reason = "walk_lookup_not_attempted:" + ("no_client" if client is None else "per_search_limit")
                results[(from_ref, to_ref)] = lookup
                stats["unknown_limit"] += 1
                continue
            response = client.route(query)
            by_coords[coord_key] = response
            stats["tmap_calls"] += 1
        else:
            stats["reused_same_coordinates"] += 1
        lookup.fetched_at = response.received_at or response.requested_at
        if response.status == "available":
            lookup.status = "available"
            lookup.distance_m, lookup.duration_seconds = response.distance_m, response.duration_seconds
            lookup.geometry = response.geometry
        else:
            lookup.status = "error"
            lookup.reason = "walk_lookup_failed"
            lookup.provider_code, lookup.provider_message = response.error_code, response.error_message
        stats[lookup.status] += 1
        results[(from_ref, to_ref)] = lookup
    stats["pairs"] = len(pairs)
    return results, dict(stats)


# ---------------------------------------------------------------- 실시간·이력 입력

@dataclass
class RealtimeInputs:
    eta_reports: list[EtaReport]
    queried_stop_ids: frozenset[str]
    target_states: dict[str, dict[str, Any]]
    batch_refs: list[tuple[str, str | None]]
    rejected: dict[str, int]


IN_PROGRESS_RETRIES = 3
IN_PROGRESS_WAIT_SECONDS = 1.0


def collect_targets(db: Database, client: TagoClient, targets: list[RealtimeTarget],
                    interval_seconds: float, clock: Callable[[], datetime], *,
                    sleep: Callable[[float], None] | None = None) -> dict[str, dict[str, Any]]:
    """Phase 2 수집기로 대상마다 간격 이내 공개값은 재사용하고 부족한 대상만 조회한다(도착 먼저).

    같은 대상을 다른 실행(워커의 주기 수집 등)이 수집 중이면(in_progress) 잠금을 기다리지 않고, 다른 대상을 먼저
    처리한 뒤 짧게 몇 번만 다시 확인한다. 끝까지 진행 중이면 그 시점의 마지막 공개값으로 평가한다(서로 막히지 않음).
    """
    import time

    wait = sleep or time.sleep
    collector = transit_service.RealtimeCollector(db, client, interval_seconds=interval_seconds, clock=clock)
    outcomes: dict[str, dict[str, Any]] = {}
    pending = sorted(targets, key=lambda t: (t.kind != "arrivals", t.external_id))
    for attempt in range(IN_PROGRESS_RETRIES + 1):
        if attempt:
            wait(IN_PROGRESS_WAIT_SECONDS)
        busy = []
        for target in pending:
            outcome = collector.collect(target)
            outcomes[outcome.target_key] = {"action": outcome.action, "http_attempts": outcome.http_attempts,
                                            "reason": outcome.reason}
            if outcome.action == "in_progress":
                busy.append(target)
        if not busy:
            break
        pending = busy
    return outcomes


def realtime_inputs(conn, targets: list[RealtimeTarget], network: StaticNetwork, *, now: datetime,
                    interval_seconds: float, max_data_age_seconds: float,
                    eta_basis_policy: str = eta_basis.UNCONFIRMED) -> RealtimeInputs:
    """대상별 마지막 공개 배치 전체를 읽어 평가 입력으로 바꾼다. 최신·오래됨·빈 결과·미제공·실패를 상태로 남긴다.

    ETA 기준 시각은 eta_basis 정책으로 정한다. 기본 정책은 원천이 주지 않은 기준 시각을 채우지 않는다(eta_basis_unconfirmed).
    """
    pattern_by_route: dict[str, list[str]] = {}
    for pattern in network.patterns:
        pattern_by_route.setdefault(pattern.route_external_id, []).append(pattern.pattern_key)
    reports: list[EtaReport] = []
    queried: set[str] = set()
    states: dict[str, dict[str, Any]] = {}
    refs: list[tuple[str, str | None]] = []
    rejected: Counter[str] = Counter()
    for target in targets:
        snap: LatestSnapshot = transit_service.latest_snapshot(conn, target, interval_seconds=interval_seconds, now=now)
        states[snap.target_key] = {
            "kind": target.kind, "external_id": target.external_id, "state": snap.state, "content": snap.content,
            "collected_at": snap.collected_at, "age_seconds": snap.age_seconds, "batch_key": snap.batch_key,
            "pages": snap.pages, "observations": len(snap.observations),
            "last_attempt_status": snap.last_attempt.result_status if snap.last_attempt else None,
            "last_attempt_failed": snap.last_attempt_failed, "not_provided": list(snap.not_provided),
            "within_evaluation_age": snap.age_seconds is not None and snap.age_seconds <= max_data_age_seconds,
        }
        refs.append((snap.target_key, str(snap.batch_key) if snap.batch_key else None))
        if target.kind != "arrivals" or snap.batch_key is None:
            continue
        if states[snap.target_key]["within_evaluation_age"]:
            queried.add(target.external_id)
        for obs in snap.observations:
            route_ext, stop_ext = obs["route_external_id"], obs["stop_external_id"]
            if route_ext is None or stop_ext is None:
                rejected["arrival_route_or_stop_not_in_static"] += 1
                continue
            pattern = obs["pattern_key"]
            if pattern is None:
                keys = pattern_by_route.get(route_ext, [])
                if len(keys) != 1:
                    rejected["arrival_pattern_unresolved"] += 1
                    continue
                pattern = keys[0]
            basis_at, basis_source = eta_basis.resolve_basis(obs, eta_basis_policy)
            reports.append(EtaReport(
                pattern_id=pattern, stop_id=stop_ext, stop_order=obs["stop_seq"], vehicle_id=obs["vehicle_no"],
                run_key=obs["run_key"], remaining_seconds=obs["remaining_seconds"], basis_at=basis_at,
                basis_source=basis_source,
                source_generated_at=obs["source_generated_at"], collected_at=obs["collected_at"],
                source_ref=f"transit_observation:{obs['id']}|batch:{snap.batch_key}",
            ))
    return RealtimeInputs(reports, frozenset(queried), states, refs, dict(rejected))


def segment_observations(conn, pattern_keys: Iterable[str], condition: str, until: datetime) -> list[SegmentObservation]:
    """같은 회차의 인접 경유 위치 도착 이벤트 쌍. 점 시각(exact)만 확인된 도착으로 쓴다."""
    events = transit_repository.arrival_events(conn, sorted(set(pattern_keys)), until=until)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault((event["pattern_key"], event["run_key"]), []).append(event)
    result = []
    for (pattern, run), items in grouped.items():
        by_seq = {e["stop_seq"]: e for e in items}
        for seq, event in sorted(by_seq.items()):
            following = by_seq.get(seq + 1)
            if following is None:
                continue

            def observed(e: dict[str, Any]) -> ObservedEvent:
                exact = e["quality"] == "exact" and e["event_at"] is not None
                return ObservedEvent(observed_at=e["event_at"] if exact else None, event_type="arrival",
                                     match_status="matched" if exact else "uncertain")

            result.append(SegmentObservation(pattern_id=pattern, from_stop_order=seq, to_stop_order=seq + 1,
                                             run_key=run, from_event=observed(event), to_event=observed(following),
                                             condition_key=condition))
    return result


# ---------------------------------------------------------------- 저장 형식

def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return value


def _point(ref: str, stops: dict[str, Any], request: SearchRequest) -> dict[str, Any]:
    if ref == ORIGIN_REF:
        return {"ref": ref, "kind": "origin", "lat": request.origin.lat, "lon": request.origin.lon}
    if ref == DESTINATION_REF:
        return {"ref": ref, "kind": "destination", "lat": request.destination.lat, "lon": request.destination.lon}
    stop = stops.get(ref)
    if stop is None:
        return {"ref": ref, "kind": "stop", "stop_id": ref, "lat": None, "lon": None}
    return {"ref": ref, "kind": "stop", "stop_id": ref, "stop_internal_id": stop.stop_id, "name": stop.name,
            "lat": stop.lat, "lon": stop.lon}


def _walk_leg(index: int, from_ref: str, to_ref: str, walks: dict[tuple[str, str], WalkLookup],
              points: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if from_ref == to_ref:
        return {"leg_index": index, "kind": "walk", "from": points[from_ref], "to": points[to_ref],
                "status": "same_stop", "provider": None, "distance_m": 0.0, "duration_seconds": 0.0,
                "geometry": None, "fetched_at": None, "query_options": {},
                "reason": "same_stop_transfer_no_lookup (준비시간은 평가에서 별도 반영)"}
    walk = walks.get((from_ref, to_ref))
    if walk is None:
        return {"leg_index": index, "kind": "walk", "from": points.get(from_ref), "to": points.get(to_ref),
                "status": "unknown", "provider": None, "distance_m": None, "duration_seconds": None,
                "geometry": None, "fetched_at": None, "query_options": {}, "reason": "walk_not_planned"}
    return {"leg_index": index, "kind": "walk", "from": walk.from_point, "to": walk.to_point,
            "status": walk.status, "provider": walk.provider if walk.status in ("available", "error") else None,
            "distance_m": walk.distance_m, "duration_seconds": walk.duration_seconds, "geometry": walk.geometry,
            "fetched_at": walk.fetched_at, "query_options": walk.query_options, "reason": walk.reason,
            "provider_code": walk.provider_code, "provider_message": walk.provider_message}


def build_legs(candidate: StaticCandidate, evaluated: EvaluatedCandidate, walks: dict[tuple[str, str], WalkLookup],
               points: dict[str, dict[str, Any]], network_info: dict[str, Any]) -> tuple[list[dict[str, Any]], list[int]]:
    """순서 있는 도보·버스 구간. 계산하지 못한 구간도 정적 정보와 이유를 보존한다. (legs, 버스 구간의 leg_index 목록)."""
    legs: list[dict[str, Any]] = []
    bus_indices: list[int] = []
    rides = candidate.rides
    legs.append(_walk_leg(0, ORIGIN_REF, rides[0].board_stop_id, walks, points))
    for i, ride in enumerate(rides):
        timed = evaluated.legs[i] if i < len(evaluated.legs) else None
        route = network_info["routes"].get(ride.route_id)
        directions = network_info["directions"]
        bus_indices.append(len(legs))
        legs.append({
            "leg_index": len(legs), "kind": "bus", "ride_index": i,
            "route_id": ride.route_id, "route_internal_id": route.route_id if route else None,
            "route_no": route.route_no if route else None, "route_type": route.route_type if route else None,
            "pattern_key": ride.pattern_id,
            "board": {**points.get(ride.board_stop_id, {"stop_id": ride.board_stop_id}), "seq": ride.board_position,
                      "direction_code": directions.get((ride.pattern_id, ride.board_position))},
            "alight": {**points.get(ride.alight_stop_id, {"stop_id": ride.alight_stop_id}),
                       "seq": ride.alight_position,
                       "direction_code": directions.get((ride.pattern_id, ride.alight_position))},
            "timed": timed is not None,
            "vehicle": None if timed is None else {
                "vehicle_id": timed.vehicle_id, "run_key": timed.run_key, "vehicle_rank": timed.vehicle_rank,
                "match_status": "confirmed" if timed.run_key else "unconfirmed"},
            "board_eta": _jsonable(timed.board_eta) if timed else None,
            "alight_eta": _jsonable(timed.alight_eta) if timed and timed.alight_eta else None,
            "ride_seconds": timed.ride_seconds if timed else None,
            "reason_codes": list(timed.reason_codes) if timed else ["ride_not_reached_or_no_vehicle_option"],
            # 승하차 구간 형상: 검증된 제공처의 패턴 형상(routes.shape_info)이 있을 때만 잘라 붙이고, 없으면 미확보와 사유.
            **route_shapes.segment_geometry(
                route_shapes.pattern_entry(network_info.get("shapes", {}).get(ride.route_id), ride.pattern_id),
                ride.board_position, ride.alight_position),
        })
        if i + 1 < len(rides):
            legs.append(_walk_leg(len(legs), ride.alight_stop_id, rides[i + 1].board_stop_id, walks, points))
    legs.append(_walk_leg(len(legs), rides[-1].alight_stop_id, DESTINATION_REF, walks, points))
    return legs, bus_indices


def candidate_row(candidate: StaticCandidate, evaluated: EvaluatedCandidate, recommendation: RecommendationResult,
                  walks, points, network_info, sample_ids: dict[tuple[str, int], str],
                  sample_skips: dict[tuple[str, int], str], scene_id: str | None) -> dict[str, Any]:
    legs, bus_indices = build_legs(candidate, evaluated, walks, points, network_info)
    ranked = {item.timed_candidate_id: item for item in recommendation.ranked}
    excluded = {item.timed_candidate_id: item for item in recommendation.excluded}
    rank = ranked.get(evaluated.timed_candidate_id)
    transfers = []
    for t in range(1, evaluated.transfer_count + 1):
        connection = evaluated.connections[t] if t < len(evaluated.connections) else None
        transfers.append({
            "transfer_index": t, "front_leg_index": bus_indices[t - 1], "next_leg_index": bus_indices[t],
            "slack_seconds": connection.slack_seconds if connection else None,
            "status": connection.status if connection else None,
            "probability": evaluated.transfer_probabilities[t - 1] if t - 1 < len(evaluated.transfer_probabilities)
            else None,
            "sample_id": sample_ids.get((evaluated.timed_candidate_id, t)),
            "sample_reason": sample_skips.get((evaluated.timed_candidate_id, t)),
            "reason": None if connection is None else (list(connection.reason_codes) or None),
        })
    evaluation = {
        "timed_candidate_id": evaluated.timed_candidate_id,
        "connection_status": evaluated.connection_status,
        "limited_at_ride_index": evaluated.limited_at_ride_index,
        "first_boarding": _jsonable(evaluated.connections[0]) if evaluated.connections else None,
        "first_boarding_probability": evaluated.first_boarding_probability,
        "connections": _jsonable(evaluated.connections),
        "transfers": transfers,
        "transfer_probabilities": list(evaluated.transfer_probabilities),
        "journey_probability": evaluated.journey_probability,
        "expected_total_seconds": evaluated.expected_total_seconds,
        "expected_comparison_key": evaluated.expected_comparison_key,
        "scene_ids": list(evaluated.provenance.joint_scene_ids),
        "planned_scene_id": scene_id,
        "times": {"walk_seconds": evaluated.walk_seconds, "buffer_seconds": evaluated.buffer_seconds,
                  "wait_seconds": evaluated.wait_seconds, "ride_seconds": evaluated.ride_seconds,
                  "egress_walk_seconds": evaluated.egress_walk_seconds,
                  "planned_arrival_at": _jsonable(evaluated.planned_arrival_at)},
        "reason_codes": list(evaluated.reason_codes),
        "provenance": _jsonable(evaluated.provenance),
        "ranking": (_jsonable(rank) if rank else
                    {"ranked": False, "reason_codes": list(excluded[evaluated.timed_candidate_id].reason_codes)
                     if evaluated.timed_candidate_id in excluded else []}),
    }
    return {
        "static_route_key": evaluated.static_candidate_id,
        "option_key": evaluated.timed_candidate_id,
        "legs": _jsonable(legs),
        "boarding_count": candidate.boarding_count,
        "walking_seconds": evaluated.walk_seconds,
        "nominal_total_seconds": evaluated.nominal_total_seconds,
        "expected_total_seconds": evaluated.expected_total_seconds,
        "journey_probability": evaluated.journey_probability,
        "time_status": evaluated.time_status,
        "nominally_feasible": evaluated.nominally_feasible,
        "evaluation": _jsonable(evaluation),
        "ranking_mode": rank.ranking_mode if rank else None,
        "rank_no": rank.rank if rank else None,
        "evaluated_at": evaluated.provenance.evaluated_at,
    }


def search_status(candidates: CandidateSearchResult, recommendation: RecommendationResult | None) -> tuple[str, str]:
    """(검색 상태, 이유). 잘린 탐색에서 후보가 없으면 경로 없음으로 단정하지 않는다."""
    if not candidates.candidates:
        if candidates.coverage.truncated:
            return "data_limited", "search_truncated_without_candidates"
        return "no_path", "no_static_candidate"
    if recommendation is not None and recommendation.ranked:
        return "ready", "ranked_candidates_available"
    return "data_limited", "no_rankable_candidate"


# ---------------------------------------------------------------- 실행

RealtimeProvider = Callable[[Any, list[RealtimeTarget], StaticNetwork, datetime], RealtimeInputs]


class SearchWorkflow:
    """좌표 검색 한 건을 끝까지 실행한다. 외부 클라이언트·시계는 주입할 수 있다(격리 시험용)."""

    def __init__(
        self,
        db: Database,
        config: SearchConfig,
        *,
        tago_client: TagoClient | None,
        walking_client: TmapWalkingClient | None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        realtime_provider: RealtimeProvider | None = None,
        collect: bool = True,
    ) -> None:
        self.db = db
        self.config = config
        self.tago_client = tago_client
        self.walking_client = walking_client
        self.clock = clock
        self.realtime_provider = realtime_provider
        self.collect = collect

    def plan(self, request: SearchRequest, network: StaticNetwork | None = None,
             ) -> tuple[StaticNetwork, SearchContext, CandidateSearchResult, Any]:
        network = network or transit_service.read_static_network(self.db)
        if network is None:
            raise SearchError("정적 공개본이 없거나 버전이 섞여 있어 검색할 수 없습니다(scripts.import_transit status 확인).")
        now = self.clock()
        context = SearchContext(
            origin=Coordinate(lat=request.origin.lat, lon=request.origin.lon),
            destination=Coordinate(lat=request.destination.lat, lon=request.destination.lon),
            start_at=request.start_at or now, evaluated_at=now, static_snapshot_id=str(network.data_version),
        )
        index = build_search_index(routing_snapshot(network), self.config.search_policy.transfer_radius_meters)
        result = generate_candidates(index, context, self.config.search_policy)
        return network, context, result, index

    def _plan_summary(self, network: StaticNetwork, result: CandidateSearchResult) -> tuple[list[RealtimeTarget], dict]:
        targets = required_targets(result.candidates)
        return targets, {
            "realtime_targets": targets_summary(targets),
            "static_version": str(network.data_version),
            "search_coverage": _jsonable(result.coverage),
            "search_reason_codes": list(result.reason_codes),
            "index_issue_count": len(result.index_issues),
            "access_stops": [s.stop_id for s in result.access_stops],
            "egress_stops": [s.stop_id for s in result.egress_stops],
            "eta_basis_policy": self.config.eta_basis_policy_version,
        }

    def run(self, request: SearchRequest) -> SearchOutcome:
        network, context, result, index = self.plan(request)
        config = self.config
        targets, summary = self._plan_summary(network, result)
        place_info = place_info_for(request)
        with self.db.transaction() as conn:
            search_id = search_repository.create_search(
                conn, source=request.source, origin=(request.origin.lat, request.origin.lon),
                destination=(request.destination.lat, request.destination.lon), place_info=place_info,
                start_at=context.start_at, static_version=network.data_version, config_version=config.config_version,
                params=config.params, summary=summary,
                expires_at=context.evaluated_at + timedelta(seconds=config.expires_seconds),
            )
        try:
            return self._evaluate_and_save(search_id, request, network, context, result, index, targets, summary)
        except Exception as exc:
            with self.db.transaction() as conn:
                search_repository.mark_search_error(conn, search_id, {"error": f"{type(exc).__name__}: "
                                                                               f"{self.db.redact(str(exc))[:300]}"})
            raise

    def run_queued(self, search_id: UUID) -> SearchOutcome:
        """앱이 접수한(processing) 검색을 워커가 실행한다. 접수 때 저장한 입력·출발 기준 시각을 그대로 쓴다.

        접수 뒤 정적 공개본이 바뀌었으면 현재 공개본 하나로 계산하고 검색 행의 static_version을 그 값으로 고친다
        (교체된 경유 순서는 남아 있지 않으므로 섞지 않고 새 공개본 하나만 쓴다).
        """
        with self.db.transaction() as conn:
            row = search_repository.read_search(conn, search_id)
        if row is None or row["status"] != "processing":
            raise SearchError("실행할 접수 검색이 없거나 이미 종료됐습니다.")
        try:
            request = request_from_row(row)
            network = transit_service.read_static_network(self.db)
            # 후보 생성·외부 호출 전에 지원 범위를 판정한다. 범위 밖·판단 불가면 후보 없이 검색을 확정한다.
            scope = self._assess_scope(request, network)
            if scope.status != service_area.IN_SCOPE:
                return self._finish_without_candidates(search_id, row, network, scope)
            network, context, result, index = self.plan(request, network)
            targets, summary = self._plan_summary(network, result)
            summary["service_area"] = scope.summary()
            summary["job"] = (row["summary"] or {}).get("job")
            if str(row["static_version"]) != str(network.data_version):
                summary["static_version_at_acceptance"] = str(row["static_version"])
            # 필요 실시간 대상을 먼저 등록해 두면 계산 중에도 공유 수집 워커가 같은 대상을 취합한다.
            with self.db.transaction() as conn:
                if not search_repository.update_processing_plan(conn, search_id, network.data_version,
                                                                _jsonable(summary)):
                    raise SearchError("검색이 이미 확정됐거나 실패 처리되었습니다.")
            return self._evaluate_and_save(search_id, request, network, context, result, index, targets, summary)
        except Exception as exc:
            with self.db.transaction() as conn:
                search_repository.mark_search_error(conn, search_id, {"error": f"{type(exc).__name__}: "
                                                                               f"{self.db.redact(str(exc))[:300]}"})
            raise

    def _assess_scope(self, request: SearchRequest, network: StaticNetwork | None) -> service_area.ScopeDecision:
        policy = self.config.search_policy
        points = [(service_area.ORIGIN, request.origin.lat, request.origin.lon, policy.access_radius_meters),
                  (service_area.DESTINATION, request.destination.lat, request.destination.lon,
                   policy.egress_radius_meters)]
        state = service_area.load_boundary(self.config.service_area_path)
        with self.db.transaction(isolation="REPEATABLE READ") as conn:
            return service_area.assess(conn, state, network.data_version if network else None, points)

    def _finish_without_candidates(self, search_id: UUID, row: dict[str, Any], network: StaticNetwork | None,
                                   scope: service_area.ScopeDecision) -> SearchOutcome:
        """범위 밖(out_of_scope) 또는 범위 판단 자료 부족(data_limited)으로 후보 생성 없이 검색을 확정한다.

        기존 확정 경로(processing 행 잠금 → finalize)를 쓰므로 이미 끝난 검색을 다시 확정하거나 덮어쓰지 않는다.
        필요 실시간 대상을 등록하지 않으므로 TAGO·TMAP 호출도 없다.
        """
        status = "out_of_scope" if scope.status == service_area.OUT_OF_SCOPE else "data_limited"
        summary = {**(row["summary"] or {}), "service_area": scope.summary(), "scope_reason_codes": scope.reason_codes,
                   "status_reason": scope.reason_codes[0],
                   "static_version": str(network.data_version) if network else str(row["static_version"]),
                   "counts": {"static_candidates": 0, "timed_candidates": 0, "ranked": 0, "limited": 0}}
        with self.db.transaction() as conn:
            if not search_repository.lock_processing_search(conn, search_id):
                raise SearchError("검색이 이미 확정됐거나 실패 처리되어 다시 저장하지 않습니다.")
            search_repository.finalize_search(conn, search_id, status=status, ranking_mode="nominal",
                                              summary=_jsonable(summary))
        return SearchOutcome(search_id, status, "nominal", summary["static_version"], None, None, None, summary, 0)

    def _evaluate_and_save(self, search_id, request, network, context, result, index, targets, summary) -> SearchOutcome:
        config, policy = self.config, self.config.evaluation_policy
        stops = {s.external_id: s for s in network.stops}
        refs = {ORIGIN_REF, DESTINATION_REF} | {ref for pair in walk_pairs(result.candidates) for ref in pair}
        for candidate in result.candidates:
            for ride in candidate.rides:
                refs.update((ride.board_stop_id, ride.alight_stop_id))
        points = {ref: _point(ref, stops, request) for ref in refs}
        walks, walk_stats = lookup_walks(walk_pairs(result.candidates), points, self.walking_client,
                                         search_option=config.tmap_search_option, max_lookups=config.tmap_max_lookups)
        summary["walk_lookups"] = walk_stats

        if self.collect and self.tago_client is not None and targets:
            summary["realtime_collection"] = collect_targets(self.db, self.tago_client, targets,
                                                             config.realtime_interval_seconds, self.clock)
        cohort = sample_rules.condition_key("tago", policy, config.eta_basis_policy)
        now = self.clock()
        # 계산 시각(evaluated_at)은 입력을 확정한 시점이다. 검색 중 수집한 응답이 계획 시점보다 늦게 도착하므로
        # 계획 시점 그대로 두면 그 응답의 기준 시각이 계산 시각보다 늦어(eta_basis_after_evaluation) 쓰이지 않는다.
        # 출발 기준 시각(start_at, 생략 시 접수 시각)은 바꾸지 않는다.
        if now > context.evaluated_at:
            context = context.model_copy(update={"evaluated_at": now})
        with self.db.transaction(isolation="REPEATABLE READ") as conn:
            if self.realtime_provider is not None:
                realtime = self.realtime_provider(conn, targets, network, now)
            else:
                realtime = realtime_inputs(conn, targets, network, now=now,
                                           interval_seconds=config.realtime_interval_seconds,
                                           max_data_age_seconds=policy.max_data_age_seconds,
                                           eta_basis_policy=config.eta_basis_policy)
            segments = segment_observations(conn, [r.pattern_id for c in result.candidates for r in c.rides],
                                            cohort, until=context.evaluated_at)
            shapes = transit_repository.route_shape_infos(
                conn, {r.route_id for c in result.candidates for r in c.rides})
            error_samples = sample_rules.transfer_error_samples(
                evaluation_repository.labeled_samples(conn, cohort, before=context.evaluated_at))
            scenes = sample_rules.joint_scenes(
                evaluation_repository.complete_scenes(conn, cohort, before=context.evaluated_at))
        summary["data_inputs"] = _jsonable(realtime.target_states)
        summary["eta_reports"] = {"usable_input_rows": len(realtime.eta_reports), "rejected": realtime.rejected}
        summary["history_inputs"] = {"segment_observations": len(segments), "transfer_error_samples":
                                     len(error_samples), "joint_scenes": len(scenes), "cohort_key": cohort}
        data_snapshot_id = hashlib.sha256(json.dumps([str(network.data_version), sorted(realtime.batch_refs, key=str)],
                                                     default=str).encode()).hexdigest()[:32]
        summary["data_snapshot_id"] = data_snapshot_id

        inputs = EvaluationInputs(
            data_snapshot_id=data_snapshot_id, sample_condition_key=cohort,
            walks=tuple(w for w in (lookup.as_walk_result() for lookup in walks.values()) if w is not None),
            eta_reports=tuple(realtime.eta_reports), queried_stop_ids=realtime.queried_stop_ids,
            segment_observations=tuple(segments), transfer_error_samples=tuple(error_samples),
            joint_scenes=tuple(scenes),
        )
        evaluation = evaluate_candidates(result, context, inputs, policy,
                                         search_index=index if policy.recovery_search else None)
        recommendation = recommend_candidates(evaluation)
        status, status_reason = search_status(result, recommendation)
        timed = [*evaluation.candidates, *evaluation.limited]
        statics = {c.static_candidate_id: c for c in result.candidates}

        sample_records, sample_skips = [], {}
        for item in timed:
            records, skips = sample_rules.build_sample_records(
                item, static_version=str(network.data_version), cohort=cohort, policy=policy,
                evaluated_at=context.evaluated_at, label_deadline_seconds=config.label_deadline_seconds,
                config_version=config.config_version)
            sample_records.extend((item.timed_candidate_id, idx, rec) for idx, rec in records)
            sample_skips.update({(s.timed_candidate_id, s.transfer_index): s.reason for s in skips})
        scene_records, scene_skips = {}, Counter()
        for static_id in sorted({item.static_candidate_id for item in timed}):
            record, reason = sample_rules.build_scene_record(
                static_id, timed, static_version=str(network.data_version), cohort=cohort, policy=policy,
                evaluated_at=context.evaluated_at, label_deadline_seconds=config.label_deadline_seconds)
            if record is not None:
                scene_records[static_id] = record
            else:
                scene_skips[reason] += 1

        network_info = {"routes": {r.external_id: r for r in network.routes},
                        "directions": {(p.pattern_key, s.seq): s.direction_code
                                       for p in network.patterns for s in p.stops},
                        "shapes": shapes}
        with self.db.transaction() as conn:
            if not search_repository.lock_processing_search(conn, search_id):
                raise SearchError("검색이 이미 확정됐거나 실패 처리되어 다시 저장하지 않습니다.")
            stop_internal = {s.external_id: s.stop_id for s in network.stops}
            ids = evaluation_repository.upsert_samples(conn, [rec for _, _, rec in sample_records], stop_internal)
            sample_ids = {(tid, idx): str(ids[rec.sample_key]) for tid, idx, rec in sample_records}
            scene_ids = evaluation_repository.upsert_scenes(conn, list(scene_records.values()))
            rows = [
                candidate_row(statics[item.static_candidate_id], item, recommendation, walks, points, network_info,
                              sample_ids, sample_skips,
                              str(scene_ids[scene_records[item.static_candidate_id].scene_key])
                              if item.static_candidate_id in scene_records else None)
                for item in timed
            ]
            summary.update({
                "status_reason": status_reason,
                "evaluation_coverage": _jsonable(evaluation.coverage),
                "recommendation": {
                    "ranking_mode": recommendation.ranking_mode,
                    "mode_reason_codes": list(recommendation.mode_reason_codes),
                    "reason_codes": list(recommendation.reason_codes),
                    "coverage_complete": recommendation.coverage_complete,
                    "recommended_timed_candidate_id": recommendation.recommended_timed_candidate_id,
                },
                "counts": {"static_candidates": len(result.candidates), "timed_candidates": len(timed),
                           "ranked": len(recommendation.ranked), "limited": len(recommendation.excluded)},
                "samples": {"saved_or_reused": len(set(sample_ids.values())),
                            "skipped": dict(Counter(sample_skips.values()))},
                "scenes": {"saved_or_reused": len(scene_ids), "skipped": dict(scene_skips)},
            })
            # 순위 후보의 (search_id, ranking_mode) FK가 검색의 확정 모드를 가리키도록 검색을 먼저 확정한다(같은 트랜잭션).
            search_repository.finalize_search(conn, search_id, status=status,
                                              ranking_mode=recommendation.ranking_mode, summary=_jsonable(summary))
            saved = search_repository.insert_candidates(conn, search_id, rows)
        return SearchOutcome(search_id, status, recommendation.ranking_mode, str(network.data_version), result,
                             evaluation, recommendation, summary, saved)


# ---------------------------------------------------------------- 앱 검색 접수 (Phase 4B)

def place_info_for(request: SearchRequest) -> dict[str, Any]:
    return {"origin": request.origin.info(), "destination": request.destination.info(),
            "input_source": request.source, "place_search_provider": None}


def request_from_row(row: dict[str, Any]) -> SearchRequest:
    """저장한 검색 입력으로 요청을 다시 만든다. 장소 정보는 받은 그대로 보존한 값을 쓴다."""
    info = row.get("place_info") or {}

    def place(kind: str, lat: float, lon: float) -> PlaceInput:
        p = info.get(kind) or {}
        return PlaceInput(lat, lon, name=p.get("name"), address=p.get("address"), provider=p.get("provider"),
                          external_id=p.get("external_id"))

    return SearchRequest(place("origin", row["origin_lat"], row["origin_lon"]),
                         place("destination", row["destination_lat"], row["destination_lon"]),
                         start_at=row["start_at"], source=row["source"])


class StaticDataUnavailable(SearchError):
    """정적 공개본이 없거나 버전이 섞여 검색을 접수할 수 없다."""


def accept_app_search(db: Database, config: SearchConfig, user_id: UUID, request: SearchRequest, *,
                      now: datetime) -> dict[str, Any]:
    """앱 검색을 processing으로 접수한다. 계산은 워커가 같은 행을 가져가 실행한다(접수와 계산 분리).

    인증 사용자를 소유자로, 입력·출발 기준 시각·현재 정적 공개본 버전·적용 설정을 저장한다. 외부 API는 호출하지 않는다.
    """
    with db.transaction() as conn:
        versions = transit_repository.current_static_versions(conn)
        if len(versions) != 1:
            raise StaticDataUnavailable("정적 공개본이 없거나 버전이 섞여 있습니다.")
        start_at = request.start_at or now
        summary = {"job": {"state": "queued", "queued_at": now.isoformat(), "attempts": 0}}
        search_id = search_repository.create_search(
            conn, source=SOURCE_APP, origin=(request.origin.lat, request.origin.lon),
            destination=(request.destination.lat, request.destination.lon), place_info=place_info_for(request),
            start_at=start_at, static_version=versions[0], config_version=config.config_version,
            params=config.params, summary=summary, expires_at=now + timedelta(seconds=config.expires_seconds),
            user_id=user_id,
        )
    return {"search_id": search_id, "status": "processing", "start_at": start_at,
            "expires_at": now + timedelta(seconds=config.expires_seconds)}
